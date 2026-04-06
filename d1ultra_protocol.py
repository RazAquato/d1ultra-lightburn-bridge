#!/usr/bin/env python3
"""
Hansmaker D1 Ultra — Protocol Library
======================================

Pure protocol implementation for the D1 Ultra laser engraver.
No GRBL, no console, no CLI — just the binary protocol over TCP.

This module can be used standalone by any application that needs to
communicate with the D1 Ultra, or imported as a library.

See PROTOCOL.md for the full specification.

Connection:
    The D1 Ultra connects via USB but presents as a virtual Ethernet adapter
    (RNDIS). The laser is at 192.168.12.1, TCP port 6000.

Usage:
    from d1ultra_protocol import D1Ultra

    laser = D1Ultra()
    laser.connect()
    laser.identify()

    # Preview/frame — traces bounding box with focus laser
    laser.preview(speed=200.0, x_min=-10, y_min=-10, x_max=10, y_max=10)
    laser.stop_preview()

    # Engrave
    paths = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]]
    laser.engrave(paths, speed=500.0, power=0.5, frequency=50.0)

    # Peripheral control
    laser.set_peripheral(Peripheral.FILL_LIGHT, True)
    laser.move_z(5.0)

    laser.disconnect()
"""

import socket
import struct
import threading
import time
import logging
import zlib
from typing import Optional, Tuple, List
from enum import IntEnum

log = logging.getLogger("d1ultra")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_IP   = "192.168.12.1"
DEFAULT_PORT = 6000
MAGIC        = b'\x0a\x0a'
TERMINATOR   = b'\x0d\x0d'


class Cmd(IntEnum):
    """D1 Ultra command IDs. See PROTOCOL.md for full details."""
    STATUS      = 0x0000  # Heartbeat / job settings (msg_type=0)
    PATH_DATA   = 0x0001  # Coordinate segments (msg_type=0)
    JOB_UPLOAD  = 0x0002  # Job header + PNG preview
    JOB_CONTROL = 0x0003  # Host sends to trigger execution; laser echoes
    JOB_FINISH  = 0x0004  # Finalize job
    PRE_JOB     = 0x0005  # Pre-job init / stop preview
    DEVICE_ID   = 0x0006  # Query device name
    WORKSPACE   = 0x0009  # Bounding box / preview trigger
    MOTOR_RESET = 0x000B  # Motor calibration
    CAMERA      = 0x000D  # Capture camera image
    PERIPHERAL  = 0x000E  # Light, buzzer, focus laser, gate
    Z_AXIS      = 0x000F  # Z movement / autofocus set
    AUTOFOCUS   = 0x0012  # Autofocus measurement
    QUERY_13    = 0x0013  # Device state query
    QUERY_14    = 0x0014  # Device query / pre-job setup
    QUERY_15    = 0x0015  # Device query
    DEVICE_INFO = 0x0018  # Serial / HW version
    FW_VERSION  = 0x001E  # Firmware version


class LaserSource(IntEnum):
    IR    = 0
    DIODE = 1


class Peripheral(IntEnum):
    FILL_LIGHT  = 0
    BUZZER      = 1
    FOCUS_LASER = 2
    SAFETY_GATE = 3


# ─────────────────────────────────────────────────────────────────────────────
# CRC-16/MODBUS
# ─────────────────────────────────────────────────────────────────────────────

def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS checksum. Polynomial 0xA001, init 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# ─────────────────────────────────────────────────────────────────────────────
# Preview PNG generator
# ─────────────────────────────────────────────────────────────────────────────

def make_preview_png(width: int = 44, height: int = 44) -> bytes:
    """Generate a ~6 KB noisy PNG matching M+ preview size (no PIL needed)."""
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

    sig  = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    raw = bytearray()
    v = 0xDEADBEEF
    for _ in range(height):
        raw.append(0)
        for _ in range(width):
            v = (v * 1664525 + 1013904223) & 0xFFFFFFFF
            raw.append((v >> 16) & 0xFF)
            raw.append((v >> 8)  & 0xFF)
            raw.append(v         & 0xFF)
    idat = zlib.compress(bytes(raw), level=0)
    return sig + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat) + _chunk(b'IEND', b'')


# ─────────────────────────────────────────────────────────────────────────────
# Packet builder
# ─────────────────────────────────────────────────────────────────────────────

class PacketBuilder:
    """Builds D1 Ultra binary protocol packets with auto-incrementing sequence."""

    def __init__(self):
        self._seq = 0

    def reset_seq(self):
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def build(self, cmd: int, payload: bytes = b'', msg_type: int = 1) -> bytes:
        """Build a complete protocol packet with CRC and framing."""
        seq = self.next_seq()
        total_len = 14 + len(payload) + 4
        header = struct.pack('<HH HH HH H',
            0x0A0A, total_len, 0, seq, 0, msg_type, cmd)
        crc = crc16_modbus(header[2:] + payload)
        return header + payload + struct.pack('<H', crc) + TERMINATOR

    def build_ack(self, cmd: int, seq: int) -> bytes:
        """Build an ACK reusing the incoming sequence number."""
        total_len = 14 + 2 + 4
        header = struct.pack('<HH HH HH H',
            0x0A0A, total_len, 0, seq, 0, 1, cmd)
        payload = b'\x00\x00'
        crc = crc16_modbus(header[2:] + payload)
        return header + payload + struct.pack('<H', crc) + TERMINATOR

    # ── Device queries ────────────────────────────────────────────────────────

    def status(self) -> bytes:
        return self.build(Cmd.STATUS)

    def device_id(self) -> bytes:
        return self.build(Cmd.DEVICE_ID)

    def device_info(self, device_id: int = 0x8B1B, ir_select: int = 0) -> bytes:
        payload = struct.pack('<HH', 0x0006, device_id)
        payload += struct.pack('<B', ir_select)
        payload += b'\x00' * (32 - 5)
        return self.build(Cmd.DEVICE_INFO, payload)

    def fw_version(self) -> bytes:
        return self.build(Cmd.FW_VERSION)

    def motor_reset(self) -> bytes:
        return self.build(Cmd.MOTOR_RESET)

    def query_13(self) -> bytes:
        return self.build(Cmd.QUERY_13)

    def query_14(self, sub: int = 0x02) -> bytes:
        return self.build(Cmd.QUERY_14, struct.pack('<B', sub))

    def query_15(self) -> bytes:
        return self.build(Cmd.QUERY_15)

    # ── Job commands ──────────────────────────────────────────────────────────

    def pre_job(self) -> bytes:
        return self.build(Cmd.PRE_JOB)

    def workspace(self, speed: float,
                  x_min: float, y_min: float,
                  x_max: float, y_max: float) -> bytes:
        """42-byte payload: 5 doubles + 2-byte pad. Also triggers preview."""
        payload = struct.pack('<ddddd', speed, x_min, y_min, x_max, y_max)
        payload += b'\x00\x00'
        return self.build(Cmd.WORKSPACE, payload)

    def job_settings(self, passes: int, speed_mm_min: float,
                     frequency_khz: float, power_frac: float,
                     laser_source: int = LaserSource.DIODE) -> bytes:
        """37-byte job settings. Uses msg_type=0, cmd=STATUS(0x0000)."""
        payload  = struct.pack('<I', passes)
        payload += struct.pack('<d', speed_mm_min)
        payload += struct.pack('<d', frequency_khz)
        payload += struct.pack('<d', power_frac)
        payload += struct.pack('<B', laser_source)
        payload += struct.pack('<d', -1.0)
        return self.build(Cmd.STATUS, payload, msg_type=0)

    def path_data(self, segments: List[Tuple[float, float]]) -> bytes:
        """Path coordinates (msg_type=0). Each segment: f64 X, f64 Y, 16 zero bytes."""
        payload = struct.pack('<I', len(segments))
        for x, y in segments:
            payload += struct.pack('<d', x)
            payload += struct.pack('<d', y)
            payload += b'\x00' * 16
        return self.build(Cmd.PATH_DATA, payload, msg_type=0)

    def job_upload(self, job_name: str, png_data: bytes = b'') -> bytes:
        """Job header: 256-byte name + 2 pad + u32 PNG size + PNG data."""
        name_bytes = job_name.encode('utf-8')[:255]
        name_field = name_bytes + b'\x00' * (256 - len(name_bytes))
        return self.build(Cmd.JOB_UPLOAD,
                          name_field + b'\x00\x00' +
                          struct.pack('<I', len(png_data)) + png_data)

    def job_control(self) -> bytes:
        """Host sends to trigger execution. Laser echoes to confirm."""
        return self.build(Cmd.JOB_CONTROL)

    def job_finish(self, job_name: str) -> bytes:
        name_bytes = job_name.encode('utf-8')[:255]
        return self.build(Cmd.JOB_FINISH,
                          name_bytes + b'\x00' * (256 - len(name_bytes)))

    # ── Peripheral / motion ───────────────────────────────────────────────────

    def peripheral(self, module: int, state: bool) -> bytes:
        return self.build(Cmd.PERIPHERAL, struct.pack('<BB', module, 1 if state else 0))

    def z_move(self, distance_mm: float) -> bytes:
        """Positive = up, negative = down."""
        payload  = struct.pack('<B', 0)
        payload += struct.pack('<d', distance_mm)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    def motor_home(self) -> bytes:
        payload  = struct.pack('<B', 2)
        payload += struct.pack('<d', 0.0)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    def autofocus_probe(self) -> bytes:
        return self.build(Cmd.AUTOFOCUS, struct.pack('<B', 1) + b'\x00' * 19)

    def z_autofocus(self, z_mm: float) -> bytes:
        payload  = struct.pack('<B', 1)
        payload += struct.pack('<d', z_mm)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)


# ─────────────────────────────────────────────────────────────────────────────
# Response parser
# ─────────────────────────────────────────────────────────────────────────────

class ResponseParser:
    """Parses D1 Ultra response packets."""

    @staticmethod
    def parse_packet(data: bytes) -> Optional[dict]:
        if len(data) < 18 or data[0:2] != MAGIC:
            return None
        pkt_len = struct.unpack('<H', data[2:4])[0]
        if pkt_len > len(data) or data[pkt_len-2:pkt_len] != TERMINATOR:
            return None
        seq      = struct.unpack('<H', data[6:8])[0]
        msg_type = struct.unpack('<H', data[10:12])[0]
        cmd      = struct.unpack('<H', data[12:14])[0]
        payload  = data[14:pkt_len-4]
        crc_exp  = struct.unpack('<H', data[pkt_len-4:pkt_len-2])[0]
        crc_got  = crc16_modbus(data[2:pkt_len-4])
        if crc_got != crc_exp:
            log.warning(f"CRC mismatch seq={seq}: expected 0x{crc_exp:04x} got 0x{crc_got:04x}")
        return {'cmd': cmd, 'seq': seq, 'msg_type': msg_type,
                'payload': payload, 'length': pkt_len}

    @staticmethod
    def parse_device_name(p: dict) -> str:
        payload = p.get('payload', b'')
        if len(payload) < 4: return ""
        return payload[2:].split(b'\x00')[0].decode('ascii', errors='replace')

    @staticmethod
    def parse_fw_version(p: dict) -> str:
        payload = p.get('payload', b'')
        if len(payload) < 6: return ""
        n = struct.unpack('<I', payload[2:6])[0]
        return payload[6:6+n].decode('ascii', errors='replace')

    @staticmethod
    def parse_status_state(p: dict) -> int:
        """Parse status response: 0=idle, 1=busy/running."""
        payload = p.get('payload', b'')
        if len(payload) >= 6:
            return struct.unpack('<H', payload[4:6])[0]
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# D1 Ultra connection
# ─────────────────────────────────────────────────────────────────────────────

class D1Ultra:
    """High-level interface to the Hansmaker D1 Ultra laser engraver.

    Handles TCP connection, heartbeat, response routing, and provides
    methods for device queries, job execution, preview/framing, and
    peripheral control.
    """

    def __init__(self, ip: str = DEFAULT_IP, port: int = DEFAULT_PORT):
        self.ip   = ip
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.builder = PacketBuilder()
        self.parser  = ResponseParser()
        self.connected = False
        self.device_name = ""
        self.fw_version  = ""

        self._send_lock = threading.Lock()
        self._recv_buf  = b''
        self._recv_lock = threading.Lock()
        self._pending: dict = {}           # seq -> (Event, parsed_result)
        self._acked_unsolicited: set = set()
        self._heartbeat_paused = False
        self._job_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to the laser over TCP."""
        try:
            if self.sock:
                try: self.sock.close()
                except: pass
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.ip, self.port))
            self.sock.settimeout(None)
            self.connected = True
            self._recv_buf = b''
            self._pending.clear()
            self._acked_unsolicited.clear()
            self.builder.reset_seq()
            log.info(f"Connected to D1 Ultra at {self.ip}:{self.port}")

            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
            return True
        except Exception as e:
            log.error(f"Failed to connect: {e}")
            return False

    def disconnect(self):
        self.connected = False
        if self.sock:
            try: self.sock.close()
            except: pass

    def ensure_connected(self) -> bool:
        """Reconnect and re-identify if connection was lost."""
        if self.connected:
            return True
        log.info("Reconnecting...")
        for attempt in range(3):
            if self.connect() and self.identify():
                log.info("Reconnected")
                return True
            time.sleep(1.0)
        log.error("Could not reconnect after 3 attempts")
        return False

    def ping(self) -> bool:
        return self.connected and self._send_recv(self.builder.status(), timeout=2.0) is not None

    # ── Startup ───────────────────────────────────────────────────────────────

    def identify(self) -> bool:
        """Run the full M+ startup handshake. Returns True if device responds."""
        r = self._send_recv(self.builder.device_id())
        if r: self.device_name = self.parser.parse_device_name(r)
        log.info(f"Device: {self.device_name or '(unknown)'}")

        self._send_recv(self.builder.status())
        self._send_recv(self.builder.device_info())

        r = self._send_recv(self.builder.fw_version())
        if r: self.fw_version = self.parser.parse_fw_version(r)
        log.info(f"Firmware: {self.fw_version or '(unknown)'}")

        r = self._send_recv(self.builder.motor_reset(), timeout=8.0)
        if r: log.info(f"Motor calibration: {len(r.get('payload',b''))} bytes")

        self._send_recv(self.builder.query_13())
        self._send_recv(self.builder.query_15())
        return bool(self.device_name)

    # ── Preview / framing ─────────────────────────────────────────────────────

    def preview(self, speed: float,
                x_min: float, y_min: float,
                x_max: float, y_max: float) -> bool:
        """Start a preview trace — laser traces the bounding box with focus laser.

        The laser physically moves the galvo mirrors to trace the rectangle
        defined by the bounding box. Call stop_preview() to stop.

        M+ sequence (from pcapng analysis):
            QUERY_13 -> QUERY_15 -> DEVICE_INFO -> QUERY_14(0x02) -> WORKSPACE
        The laser enters busy state immediately after WORKSPACE is sent.

        Args:
            speed: Trace speed in mm/min (M+ uses 200 or 1000)
            x_min, y_min, x_max, y_max: Bounding box in mm (centered coordinates)
        """
        self._send_recv(self.builder.query_13())
        self._send_recv(self.builder.query_15())
        self._send_recv(self.builder.device_info())
        self._send_recv(self.builder.query_14(0x02))
        r = self._send_recv(self.builder.workspace(speed, x_min, y_min, x_max, y_max),
                            timeout=10.0)
        if r:
            log.info(f"Preview started: {x_max-x_min:.1f} x {y_max-y_min:.1f} mm at {speed:.0f} mm/min")
            return True
        log.warning("Preview: no WORKSPACE ACK")
        return False

    def stop_preview(self):
        """Stop a running preview. M+ sends PRE_JOB (0x0005) to cancel."""
        self._send_recv(self.builder.pre_job(), timeout=5.0)
        log.info("Preview stopped")

    # ── Job execution ─────────────────────────────────────────────────────────

    def engrave(self, path_groups: List[List[Tuple[float, float]]],
                speed: float = 500.0, power: float = 0.5,
                frequency: float = 50.0, passes: int = 1,
                laser_source: int = LaserSource.DIODE,
                job_name: str = "d1ultra_job") -> bool:
        """Execute an engraving job.

        Coordinates must be centered around the design's bounding-box midpoint
        (not absolute bed positions).

        Args:
            path_groups: List of paths. Each path is a list of (x, y) tuples in mm.
            speed: Engraving speed in mm/min.
            power: Laser power 0.0-1.0 (0.5 = 50%).
            frequency: Laser frequency in kHz.
            passes: Number of passes per path.
            laser_source: LaserSource.DIODE or LaserSource.IR.
            job_name: Name shown on laser display.
        """
        if self._job_lock.locked():
            log.info("Waiting for previous job to finish...")
        self._job_lock.acquire()
        try:
            return self._engrave_locked(path_groups, speed, power, frequency,
                                        passes, laser_source, job_name)
        finally:
            self._job_lock.release()

    def _engrave_locked(self, path_groups, speed, power, frequency,
                        passes, laser_source, job_name) -> bool:
        if not self.ensure_connected():
            log.error("Cannot engrave — laser not reachable")
            return False

        groups = [g for g in path_groups if len(g) >= 2]
        if not groups:
            log.warning("No valid paths — skipping")
            return False

        # Remove duplicate closing points (LightBurn G1 S0 artifact)
        groups = [g[:-1] if len(g) >= 3 and g[-1] == g[-2] else g for g in groups]

        all_pts = [pt for g in groups for pt in g]
        x_vals = [p[0] for p in all_pts]
        y_vals = [p[1] for p in all_pts]
        bb = (min(x_vals), min(y_vals), max(x_vals), max(y_vals))

        log.info(f"Engrave: {len(groups)} paths, {len(all_pts)} points, "
                 f"{power*100:.0f}% power, {speed:.0f} mm/min")

        self._heartbeat_paused = True
        try:
            # Step 1: DEVICE_INFO
            self._send_recv(self.builder.device_info())

            # Step 2: PRE_JOB
            self._send_recv(self.builder.pre_job(), timeout=5.0)

            # Step 3: QUERY_14(0x02)
            self._send_recv(self.builder.query_14(0x02), timeout=5.0)

            # Step 4: JOB_UPLOAD with PNG
            png = make_preview_png(44, 44)
            r = self._send_recv(self.builder.job_upload(job_name, png), timeout=10.0)
            if not r:
                log.error("JOB_UPLOAD: no ACK — aborting")
                return False

            # Step 5: WORKSPACE
            self._send_recv(self.builder.workspace(speed, *bb), timeout=5.0)

            # Step 6: SETTINGS + PATH pairs
            for i, group in enumerate(groups):
                self._send_recv(
                    self.builder.job_settings(passes, speed, frequency, power, laser_source),
                    timeout=5.0)
                r = self._send_recv(self.builder.path_data(group), timeout=10.0)
                if not r:
                    log.error(f"Path {i}: no ACK — aborting")
                    return False
                time.sleep(0.010)
                if (i + 1) % 50 == 0 or i == len(groups) - 1:
                    log.info(f"  {i+1}/{len(groups)} paths sent")

            # Step 7: JOB_CONTROL
            r = self._send_recv(self.builder.job_control(), timeout=15.0)
            if r:
                log.info("JOB_CONTROL confirmed")
            else:
                log.warning("JOB_CONTROL: no response")

            # Step 8: JOB_FINISH
            self._send_recv(self.builder.job_finish(job_name), timeout=5.0)
            log.info("Job submitted")
            return True
        finally:
            self._heartbeat_paused = False

    # ── Peripheral control ────────────────────────────────────────────────────

    def set_peripheral(self, module: int, state: bool):
        """Control fill light, buzzer, focus laser, or safety gate."""
        self._send_recv(self.builder.peripheral(module, state))

    def move_z(self, distance_mm: float):
        """Move Z axis. Positive = up, negative = down."""
        self._send_recv(self.builder.z_move(distance_mm))

    def home_motors(self, retract_mm: float = 5.0):
        """Home all motors and retract off endstop."""
        log.info("Homing motors...")
        self._send_recv(self.builder.motor_home(), timeout=60.0)
        self._send_recv(self.builder.z_move(-retract_mm), timeout=15.0)
        log.info("Homing complete")

    def run_autofocus(self, hw_id: int = 0x1A8B) -> Optional[float]:
        """3-probe IR autofocus. Returns average Z height in mm, or None."""
        log.info("Autofocus: 3-probe sequence...")
        self._send_recv(self.builder.status())
        self._send_recv(self.builder.query_15())
        self._send_recv(self.builder.query_14(0x02))
        self._send_recv(self.builder.device_info(device_id=hw_id, ir_select=1))

        measurements = []
        for i in range(3):
            log.info(f"  Probe {i+1}/3...")
            r = self._send_recv(self.builder.autofocus_probe(), timeout=10.0)
            if not r or len(r.get('payload', b'')) < 30:
                continue
            z_val = struct.unpack_from('<d', r['payload'], 22)[0]
            log.info(f"  Z = {z_val:.3f} mm")
            measurements.append(z_val)
            self._send_recv(self.builder.z_autofocus(z_val), timeout=60.0)
            if i < 2:
                for _ in range(5):
                    time.sleep(0.4)
                    self._send_recv(self.builder.status(), timeout=2.0)

        self._send_recv(self.builder.device_info(device_id=hw_id, ir_select=0))

        if measurements:
            avg = sum(measurements) / len(measurements)
            log.info(f"Autofocus: Z = {avg:.3f} mm")
            return avg
        log.warning("Autofocus failed")
        return None

    # ── Internal: send/recv with sequence tracking ────────────────────────────

    def _send_recv(self, packet: bytes, timeout: float = 5.0) -> Optional[dict]:
        if not self.connected or not self.sock or len(packet) < 8:
            return None
        seq = struct.unpack('<H', packet[6:8])[0]
        evt = threading.Event()
        self._pending[seq] = (evt, None)
        try:
            with self._send_lock:
                self.sock.sendall(packet)
        except Exception as e:
            log.error(f"Send error: {e}")
            self._pending.pop(seq, None)
            self.connected = False
            return None
        if evt.wait(timeout=timeout):
            _, result = self._pending.pop(seq, (None, None))
            return result
        self._pending.pop(seq, None)
        return None

    def _send_only(self, packet: bytes):
        if not self.connected or not self.sock:
            return
        try:
            with self._send_lock:
                self.sock.sendall(packet)
        except Exception as e:
            log.error(f"Send error: {e}")
            self.connected = False

    # ── Internal: background reader and dispatcher ────────────────────────────

    def _reader_loop(self):
        while self.connected:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    log.warning("Laser closed connection")
                    self.connected = False
                    break
                with self._recv_lock:
                    self._recv_buf += chunk
                self._process_recv_buf()
            except OSError:
                if self.connected:
                    log.warning("Laser socket error")
                    self.connected = False
                break
        for seq, (evt, _) in list(self._pending.items()):
            evt.set()

    def _process_recv_buf(self):
        while len(self._recv_buf) >= 18:
            idx = self._recv_buf.find(MAGIC)
            if idx == -1:
                self._recv_buf = b''
                return
            if idx > 0:
                self._recv_buf = self._recv_buf[idx:]
            if len(self._recv_buf) < 4:
                return
            pkt_len = struct.unpack('<H', self._recv_buf[2:4])[0]
            if pkt_len < 18 or pkt_len > 65535:
                self._recv_buf = self._recv_buf[2:]
                continue
            if len(self._recv_buf) < pkt_len:
                return
            pkt_data = self._recv_buf[:pkt_len]
            self._recv_buf = self._recv_buf[pkt_len:]
            parsed = self.parser.parse_packet(pkt_data)
            if parsed:
                self._dispatch(parsed)

    def _dispatch(self, parsed: dict):
        cmd = parsed['cmd']
        seq = parsed['seq']
        msg_type = parsed['msg_type']

        # Route to waiting caller first (prevents ACK feedback loops)
        if seq in self._pending:
            evt, _ = self._pending[seq]
            self._pending[seq] = (evt, parsed)
            evt.set()
            return

        if cmd == Cmd.JOB_CONTROL:
            log.info("Laser echoed JOB_CONTROL (0x0003)")
            return

        # ACK unsolicited laser queries (once per seq to avoid loops)
        if cmd in (Cmd.QUERY_13, Cmd.QUERY_14, Cmd.QUERY_15):
            ack_key = (cmd, seq)
            if ack_key not in self._acked_unsolicited:
                self._send_only(self.builder.build_ack(cmd, seq))
                self._acked_unsolicited.add(ack_key)
            return

        if msg_type == 2:
            return  # notification, ignore

        log.debug(f"Unsolicited: seq={seq} cmd=0x{cmd:04x}")

    def _heartbeat_loop(self):
        while self.connected:
            try:
                time.sleep(2.0)
                if self.connected and not self._heartbeat_paused:
                    self._send_recv(self.builder.status(), timeout=2.0)
            except Exception:
                pass
