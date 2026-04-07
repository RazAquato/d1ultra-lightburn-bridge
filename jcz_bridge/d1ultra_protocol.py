#!/usr/bin/env python3
"""
Hansmaker D1 Ultra — Protocol Library
======================================

Pure-Python implementation of the D1 Ultra laser engraver's binary protocol.
No external dependencies — stdlib only.

This module is **standalone**. It has no knowledge of JCZ, GRBL, or any bridge
logic. Import it from any application that needs to talk to a D1 Ultra.

Protocol summary (see PROTOCOL.md for full spec):
    - Transport: TCP to 192.168.12.1:6000 (over USB RNDIS virtual ethernet)
    - Framing:   0x0A0A | u16 len | u16 pad | u16 seq | u16 pad |
                 u16 msg_type | u16 cmd | payload | u16 CRC-16/MODBUS | 0x0D0D
    - All multi-byte values are little-endian.

Usage:
    from d1ultra_protocol import D1Ultra, LaserSource, Peripheral

    laser = D1Ultra("192.168.12.1", 6000)
    laser.connect()
    laser.identify()

    # Preview (red dot traces bounding box)
    laser.preview(speed=200.0, x_min=-10, y_min=-10, x_max=10, y_max=10)
    laser.stop_preview()

    # Engrave
    paths = [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]
    laser.engrave(paths, speed=500.0, power=0.5)

    laser.disconnect()

License: MIT
"""

import socket
import struct
import threading
import time
import logging
import zlib
from enum import IntEnum
from typing import Optional, List, Tuple

__all__ = [
    "D1Ultra", "PacketBuilder", "ResponseParser",
    "Cmd", "LaserSource", "Peripheral",
    "crc16_modbus", "make_preview_png",
]

log = logging.getLogger("d1ultra")

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

MAGIC      = b'\x0a\x0a'
TERMINATOR = b'\x0d\x0d'
MIN_PACKET = 18  # smallest valid packet (header + empty payload + CRC + term)


class Cmd(IntEnum):
    """D1 Ultra command IDs."""
    STATUS      = 0x0000   # heartbeat (msg_type=1) / job settings (msg_type=0)
    PATH_DATA   = 0x0001   # coordinate segments (msg_type=0)
    JOB_UPLOAD  = 0x0002   # job header + PNG preview
    JOB_CONTROL = 0x0003   # host sends to trigger execution; laser echoes
    JOB_FINISH  = 0x0004   # finalize job
    PRE_JOB     = 0x0005   # pre-job init / stop preview
    DEVICE_ID   = 0x0006   # device identification ("D1 Ultra")
    WORKSPACE   = 0x0009   # bounding box / preview trigger
    MOTOR_RESET = 0x000B   # motor calibration (returns 283 bytes)
    CAMERA      = 0x000D   # capture camera image
    PERIPHERAL  = 0x000E   # fill light, buzzer, focus laser, gate
    Z_AXIS      = 0x000F   # Z movement / autofocus set
    AUTOFOCUS   = 0x0012   # autofocus measurement
    QUERY_13    = 0x0013   # device state query
    QUERY_14    = 0x0014   # device query / pre-job setup
    QUERY_15    = 0x0015   # device query
    DEVICE_INFO = 0x0018   # serial / HW version
    FW_VERSION  = 0x001E   # firmware version string


class LaserSource(IntEnum):
    IR    = 0
    DIODE = 1


class Peripheral(IntEnum):
    FILL_LIGHT  = 0
    BUZZER      = 1
    FOCUS_LASER = 2
    SAFETY_GATE = 3


# ═══════════════════════════════════════════════════════════════════════════════
# CRC-16/MODBUS
# ═══════════════════════════════════════════════════════════════════════════════

def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS. Polynomial 0xA001 (reflected 0x8005), init 0xFFFF.

    Computed over bytes 2..end-of-payload (everything after the 0x0A0A magic,
    excluding the CRC field itself and the 0x0D0D terminator).
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# ═══════════════════════════════════════════════════════════════════════════════
# Preview PNG generator (no PIL needed)
# ═══════════════════════════════════════════════════════════════════════════════

def make_preview_png(width: int = 44, height: int = 44) -> bytes:
    """Generate a small noisy PNG matching the size M+ sends (~6 KB).

    The D1 Ultra expects a PNG thumbnail with the job upload.
    We generate random-looking pixels so it's not blank.
    """
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

    sig  = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)

    raw = bytearray()
    v = 0xDEADBEEF
    for _ in range(height):
        raw.append(0)  # filter byte
        for _ in range(width):
            v = (v * 1664525 + 1013904223) & 0xFFFFFFFF
            raw.append((v >> 16) & 0xFF)
            raw.append((v >> 8)  & 0xFF)
            raw.append(v         & 0xFF)

    idat = zlib.compress(bytes(raw), level=0)
    return sig + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat) + _chunk(b'IEND', b'')


# ═══════════════════════════════════════════════════════════════════════════════
# Packet builder
# ═══════════════════════════════════════════════════════════════════════════════

class PacketBuilder:
    """Constructs D1 Ultra binary protocol packets.

    Each packet has an auto-incrementing sequence number.  The laser echoes the
    sequence number in its response, which is how we match requests to replies.
    """

    def __init__(self):
        self._seq = 0

    def reset_seq(self):
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- Core packet assembly -------------------------------------------------

    def build(self, cmd: int, payload: bytes = b'', msg_type: int = 1) -> bytes:
        """Build a complete framed packet: magic + header + payload + CRC + terminator."""
        seq = self._next_seq()
        total_len = 14 + len(payload) + 4  # header(14) + payload + CRC(2) + term(2)
        header = struct.pack('<HH HH HH H',
                             0x0A0A, total_len, 0, seq, 0, msg_type, cmd)
        crc_data = header[2:] + payload
        crc = crc16_modbus(crc_data)
        return header + payload + struct.pack('<H', crc) + TERMINATOR

    def build_ack(self, cmd: int, seq: int) -> bytes:
        """Build an ACK that reuses the incoming sequence number."""
        total_len = 14 + 2 + 4  # header + 2-byte payload + CRC + term
        header = struct.pack('<HH HH HH H',
                             0x0A0A, total_len, 0, seq, 0, 1, cmd)
        payload = b'\x00\x00'
        crc = crc16_modbus(header[2:] + payload)
        return header + payload + struct.pack('<H', crc) + TERMINATOR

    # -- Device queries -------------------------------------------------------

    def status(self) -> bytes:
        """Heartbeat / status poll (cmd 0x0000, msg_type=1)."""
        return self.build(Cmd.STATUS)

    def device_id(self) -> bytes:
        """Query device name (returns 'D1 Ultra')."""
        return self.build(Cmd.DEVICE_ID)

    def device_info(self, device_id: int = 0x8B1B, ir_select: int = 0) -> bytes:
        """Query serial number / hardware version."""
        payload = struct.pack('<HH', 0x0006, device_id)
        payload += struct.pack('<B', ir_select)
        payload += b'\x00' * (32 - len(payload))
        return self.build(Cmd.DEVICE_INFO, payload)

    def fw_version(self) -> bytes:
        """Query firmware version string."""
        return self.build(Cmd.FW_VERSION)

    def motor_reset(self) -> bytes:
        """Motor calibration (returns 283 bytes of boundary data)."""
        return self.build(Cmd.MOTOR_RESET)

    def query_13(self) -> bytes:
        return self.build(Cmd.QUERY_13)

    def query_14(self, sub: int = 0x02) -> bytes:
        return self.build(Cmd.QUERY_14, struct.pack('<B', sub))

    def query_15(self) -> bytes:
        return self.build(Cmd.QUERY_15)

    # -- Job commands ---------------------------------------------------------

    def pre_job(self) -> bytes:
        """Pre-job init. Also used to stop a running preview."""
        return self.build(Cmd.PRE_JOB)

    def workspace(self, speed: float,
                  x_min: float, y_min: float,
                  x_max: float, y_max: float) -> bytes:
        """Bounding box / preview trigger. 42-byte payload (5 doubles + 2-byte pad).

        When sent standalone (after QUERY_13 + QUERY_15 + DEVICE_INFO + QUERY_14),
        the laser physically traces the bounding box — this is native framing.
        """
        payload = struct.pack('<ddddd', speed, x_min, y_min, x_max, y_max)
        payload += b'\x00\x00'
        return self.build(Cmd.WORKSPACE, payload)

    def job_settings(self, passes: int, speed_mm_min: float,
                     frequency_khz: float, power_frac: float,
                     laser_source: int = LaserSource.DIODE) -> bytes:
        """Per-path job parameters. 37 bytes, msg_type=0, cmd=0x0000.

        Args:
            passes:        Number of passes (1, 2, 3, ...).
            speed_mm_min:  Engraving speed in mm/min.
            frequency_khz: Laser frequency in kHz (50.0 typical for diode).
            power_frac:    Power as fraction 0.0-1.0 (0.5 = 50%).
            laser_source:  1=diode, 0=IR.
        """
        payload  = struct.pack('<I', passes)
        payload += struct.pack('<d', speed_mm_min)
        payload += struct.pack('<d', frequency_khz)
        payload += struct.pack('<d', power_frac)
        payload += struct.pack('<B', laser_source)
        payload += struct.pack('<d', -1.0)  # unknown field, always -1.0
        return self.build(Cmd.STATUS, payload, msg_type=0)

    def path_data(self, segments: List[Tuple[float, float]]) -> bytes:
        """Coordinate segments (msg_type=0). Each segment: f64 X, f64 Y, 16 zero bytes.

        Coordinates must be centred on the design's bounding-box midpoint.
        """
        payload = struct.pack('<I', len(segments))
        for x, y in segments:
            payload += struct.pack('<dd', x, y)
            payload += b'\x00' * 16
        return self.build(Cmd.PATH_DATA, payload, msg_type=0)

    def job_upload(self, job_name: str, png_data: bytes = b'') -> bytes:
        """Job header: 256-byte name + 2-byte pad + u32 PNG size + PNG data."""
        name_bytes = job_name.encode('utf-8')[:255]
        name_field = name_bytes + b'\x00' * (256 - len(name_bytes))
        payload = name_field + b'\x00\x00' + struct.pack('<I', len(png_data)) + png_data
        return self.build(Cmd.JOB_UPLOAD, payload)

    def job_control(self) -> bytes:
        """Host sends (empty payload) to trigger execution. Laser echoes to confirm."""
        return self.build(Cmd.JOB_CONTROL)

    def job_finish(self, job_name: str) -> bytes:
        """Finalize job (256-byte name field)."""
        name_bytes = job_name.encode('utf-8')[:255]
        name_field = name_bytes + b'\x00' * (256 - len(name_bytes))
        return self.build(Cmd.JOB_FINISH, name_field)

    # -- Peripheral / motion --------------------------------------------------

    def peripheral(self, module: int, state: bool) -> bytes:
        """Control fill light, buzzer, focus laser, or safety gate."""
        return self.build(Cmd.PERIPHERAL, struct.pack('<BB', module, 1 if state else 0))

    def z_move(self, distance_mm: float) -> bytes:
        """Move Z axis. Positive = up, negative = down."""
        payload  = struct.pack('<B', 0)       # mode 0 = manual move
        payload += struct.pack('<d', distance_mm)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    def z_home(self) -> bytes:
        """Home Z axis."""
        payload  = struct.pack('<B', 2)       # mode 2 = home
        payload += struct.pack('<d', 0.0)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    def autofocus_probe(self) -> bytes:
        """Request autofocus measurement."""
        return self.build(Cmd.AUTOFOCUS, struct.pack('<B', 1) + b'\x00' * 19)

    def z_autofocus_set(self, z_mm: float) -> bytes:
        """Set Z height from autofocus measurement."""
        payload  = struct.pack('<B', 1)       # mode 1 = autofocus set
        payload += struct.pack('<d', z_mm)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)


# ═══════════════════════════════════════════════════════════════════════════════
# Response parser
# ═══════════════════════════════════════════════════════════════════════════════

class ResponseParser:
    """Parses D1 Ultra binary protocol response packets."""

    @staticmethod
    def parse_packet(data: bytes) -> Optional[dict]:
        """Parse a single packet from raw bytes. Returns None on invalid data."""
        if len(data) < MIN_PACKET or data[0:2] != MAGIC:
            return None

        pkt_len = struct.unpack('<H', data[2:4])[0]
        if pkt_len > len(data) or pkt_len < MIN_PACKET:
            return None
        if data[pkt_len - 2 : pkt_len] != TERMINATOR:
            return None

        seq      = struct.unpack('<H', data[6:8])[0]
        msg_type = struct.unpack('<H', data[10:12])[0]
        cmd      = struct.unpack('<H', data[12:14])[0]
        payload  = data[14 : pkt_len - 4]

        crc_expected = struct.unpack('<H', data[pkt_len - 4 : pkt_len - 2])[0]
        crc_actual   = crc16_modbus(data[2 : pkt_len - 4])

        if crc_actual != crc_expected:
            log.warning(f"CRC mismatch seq={seq} cmd=0x{cmd:04x}: "
                        f"expected=0x{crc_expected:04x} got=0x{crc_actual:04x}")

        return {
            "cmd":      cmd,
            "seq":      seq,
            "msg_type": msg_type,
            "payload":  payload,
            "length":   pkt_len,
        }

    @staticmethod
    def parse_device_name(pkt: dict) -> str:
        """Extract device name from DEVICE_ID response."""
        payload = pkt.get("payload", b"")
        if len(payload) < 4:
            return ""
        return payload[2:].split(b'\x00')[0].decode('ascii', errors='replace')

    @staticmethod
    def parse_fw_version(pkt: dict) -> str:
        """Extract firmware version string from FW_VERSION response."""
        payload = pkt.get("payload", b"")
        if len(payload) < 6:
            return ""
        n = struct.unpack('<I', payload[2:6])[0]
        return payload[6:6 + n].decode('ascii', errors='replace')

    @staticmethod
    def parse_status_state(pkt: dict) -> int:
        """Parse status response: 0=idle, 1=busy/running."""
        payload = pkt.get("payload", b"")
        if len(payload) >= 6:
            return struct.unpack('<H', payload[4:6])[0]
        return 0

    @staticmethod
    def parse_autofocus_z(pkt: dict) -> Optional[float]:
        """Extract Z-height measurement from autofocus response."""
        payload = pkt.get("payload", b"")
        if len(payload) >= 30:
            return struct.unpack('<d', payload[22:30])[0]
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# D1 Ultra connection manager
# ═══════════════════════════════════════════════════════════════════════════════

class D1Ultra:
    """High-level interface to the Hansmaker D1 Ultra laser engraver.

    Manages TCP connection, heartbeat keepalive, response routing, and
    provides methods for device queries, job execution, preview/framing,
    and peripheral control.
    """

    def __init__(self, ip: str = "192.168.12.1", port: int = 6000):
        self.ip   = ip
        self.port = port

        self.sock: Optional[socket.socket] = None
        self.builder = PacketBuilder()
        self.parser  = ResponseParser()
        self.connected = False

        # Device info (populated by identify())
        self.device_name = ""
        self.fw_version  = ""

        # Internal state
        self._send_lock = threading.Lock()
        self._recv_buf  = b''
        self._recv_lock = threading.Lock()
        self._pending: dict = {}               # seq -> (Event, parsed_result)
        self._acked_unsolicited: set = set()   # (cmd, seq) pairs already ACK'd
        self._heartbeat_paused = False
        self._job_lock = threading.Lock()
        self._reader_thread:    Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    # -- Connection -----------------------------------------------------------

    def connect(self) -> bool:
        """Open TCP connection to the laser. Starts reader + heartbeat threads."""
        try:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass

            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.ip, self.port))
            self.sock.settimeout(None)
            self.connected = True

            self._recv_buf = b''
            self._pending.clear()
            self._acked_unsolicited.clear()
            self.builder.reset_seq()

            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True, name="d1ultra-reader")
            self._reader_thread.start()

            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="d1ultra-heartbeat")
            self._heartbeat_thread.start()

            log.info(f"Connected to {self.ip}:{self.port}")
            return True

        except Exception as e:
            log.error(f"Connection failed: {e}")
            self.connected = False
            return False

    def disconnect(self):
        """Close connection and stop background threads."""
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def ensure_connected(self) -> bool:
        """Reconnect if needed. Returns True if connected."""
        if self.connected:
            return True
        log.info("Reconnecting...")
        for attempt in range(3):
            if self.connect() and self.identify():
                log.info("Reconnected successfully")
                return True
            time.sleep(1.0)
        log.error("Could not reconnect after 3 attempts")
        return False

    # -- Startup handshake ----------------------------------------------------

    def identify(self) -> bool:
        """Run the full M+ startup sequence. Returns True if device responds."""
        r = self._send_recv(self.builder.device_id())
        if r:
            self.device_name = self.parser.parse_device_name(r)
        log.info(f"Device: {self.device_name or '(no response)'}")

        self._send_recv(self.builder.status())
        self._send_recv(self.builder.device_info())

        r = self._send_recv(self.builder.fw_version())
        if r:
            self.fw_version = self.parser.parse_fw_version(r)
        log.info(f"Firmware: {self.fw_version or '(no response)'}")

        r = self._send_recv(self.builder.motor_reset(), timeout=8.0)
        if r:
            log.info(f"Motor calibration: {len(r.get('payload', b''))} bytes")

        self._send_recv(self.builder.query_13())
        self._send_recv(self.builder.query_15())
        return bool(self.device_name)

    # -- Preview / framing ----------------------------------------------------

    def preview(self, speed: float,
                x_min: float, y_min: float,
                x_max: float, y_max: float) -> bool:
        """Start native preview — laser traces bounding box with focus laser.

        M+ sequence: QUERY_13 -> QUERY_15 -> DEVICE_INFO -> QUERY_14(0x02) -> WORKSPACE.
        The laser enters busy state and physically traces the rectangle.
        Call stop_preview() to stop.
        """
        self._send_recv(self.builder.query_13())
        self._send_recv(self.builder.query_15())
        self._send_recv(self.builder.device_info())
        self._send_recv(self.builder.query_14(0x02))
        r = self._send_recv(
            self.builder.workspace(speed, x_min, y_min, x_max, y_max),
            timeout=10.0)
        if r:
            log.info(f"Preview started: {x_max - x_min:.1f}x{y_max - y_min:.1f}mm "
                     f"at {speed:.0f} mm/min")
            return True
        log.warning("Preview: no WORKSPACE ACK")
        return False

    def stop_preview(self):
        """Stop a running preview. Sends PRE_JOB (0x0005)."""
        self._send_recv(self.builder.pre_job(), timeout=5.0)
        log.info("Preview stopped")

    # -- Job execution --------------------------------------------------------

    def engrave(self, path_groups: List[List[Tuple[float, float]]],
                speed: float = 500.0, power: float = 0.5,
                frequency: float = 50.0, passes: int = 1,
                laser_source: int = LaserSource.DIODE,
                job_name: str = "d1ultra_job") -> bool:
        """Execute an engraving job.

        Coordinates must be centred on the design's bounding-box midpoint
        (not absolute bed positions).

        Args:
            path_groups: List of paths. Each path is [(x, y), ...] in mm.
            speed:       Engraving speed in mm/min.
            power:       Laser power 0.0-1.0.
            frequency:   Frequency in kHz (50.0 typical for diode).
            passes:      Number of passes per path.
            laser_source: LaserSource.DIODE or LaserSource.IR.
            job_name:    Name shown on laser display.

        Returns:
            True if job was submitted successfully.
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
            log.error("Cannot engrave: laser not reachable")
            return False

        # Filter empty paths, remove duplicate closing points
        groups = [g for g in path_groups if len(g) >= 2]
        groups = [g[:-1] if len(g) >= 3 and g[-1] == g[-2] else g for g in groups]
        if not groups:
            log.warning("No valid paths to engrave")
            return False

        # Compute bounding box
        all_pts = [pt for g in groups for pt in g]
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        bbox = (min(xs), min(ys), max(xs), max(ys))

        log.info(f"Engrave: {len(groups)} paths, {len(all_pts)} pts, "
                 f"{power * 100:.0f}% @ {speed:.0f} mm/min")

        self._heartbeat_paused = True
        try:
            # Step 1: DEVICE_INFO
            self._send_recv(self.builder.device_info())

            # Step 2: PRE_JOB
            self._send_recv(self.builder.pre_job(), timeout=5.0)

            # Step 3: QUERY_14(0x02)
            self._send_recv(self.builder.query_14(0x02), timeout=5.0)

            # Step 4: JOB_UPLOAD with PNG preview
            png = make_preview_png(44, 44)
            r = self._send_recv(self.builder.job_upload(job_name, png), timeout=10.0)
            if not r:
                log.error("JOB_UPLOAD: no ACK — aborting")
                return False

            # Step 5: WORKSPACE (bounding box)
            self._send_recv(self.builder.workspace(speed, *bbox), timeout=5.0)

            # Step 6: SETTINGS + PATH pairs (10ms pacing)
            for i, group in enumerate(groups):
                self._send_recv(
                    self.builder.job_settings(passes, speed, frequency, power, laser_source),
                    timeout=5.0)
                r = self._send_recv(self.builder.path_data(group), timeout=10.0)
                if not r:
                    log.error(f"Path {i}: no ACK — aborting")
                    return False
                time.sleep(0.010)  # 10ms pacing between pairs
                if (i + 1) % 50 == 0 or i == len(groups) - 1:
                    log.info(f"  {i + 1}/{len(groups)} paths sent")

            # Step 7: JOB_CONTROL — triggers execution
            r = self._send_recv(self.builder.job_control(), timeout=15.0)
            if r:
                log.info("JOB_CONTROL confirmed — laser executing")
            else:
                log.warning("JOB_CONTROL: no response (job may still run)")

            # Step 8: JOB_FINISH
            self._send_recv(self.builder.job_finish(job_name), timeout=5.0)
            log.info("Job submitted successfully")
            return True

        finally:
            self._heartbeat_paused = False

    # -- Peripheral control ---------------------------------------------------

    def set_peripheral(self, module: int, state: bool):
        """Control fill light (0), buzzer (1), focus laser (2), safety gate (3)."""
        self._send_recv(self.builder.peripheral(module, state))

    def move_z(self, distance_mm: float):
        """Move Z axis. Positive = up, negative = down."""
        self._send_recv(self.builder.z_move(distance_mm))

    def home_z(self):
        """Home Z axis motors."""
        log.info("Homing Z axis...")
        self._send_recv(self.builder.z_home(), timeout=60.0)
        log.info("Z homing complete")

    def run_autofocus(self) -> Optional[float]:
        """3-probe IR autofocus. Returns average Z height in mm, or None."""
        log.info("Autofocus: 3-probe sequence")
        self._send_recv(self.builder.status())
        self._send_recv(self.builder.query_15())
        self._send_recv(self.builder.query_14(0x02))
        self._send_recv(self.builder.device_info(device_id=0x1A8B, ir_select=1))

        measurements = []
        for i in range(3):
            log.info(f"  Probe {i + 1}/3...")
            r = self._send_recv(self.builder.autofocus_probe(), timeout=10.0)
            z = self.parser.parse_autofocus_z(r) if r else None
            if z is not None:
                log.info(f"  Z = {z:.3f} mm")
                measurements.append(z)
                self._send_recv(self.builder.z_autofocus_set(z), timeout=60.0)
                if i < 2:
                    for _ in range(5):
                        time.sleep(0.4)
                        self._send_recv(self.builder.status(), timeout=2.0)

        self._send_recv(self.builder.device_info(device_id=0x1A8B, ir_select=0))

        if measurements:
            avg = sum(measurements) / len(measurements)
            log.info(f"Autofocus result: Z = {avg:.3f} mm")
            return avg
        log.warning("Autofocus failed — no valid measurements")
        return None

    # -- Internal: send/recv with sequence tracking ---------------------------

    def _send_recv(self, packet: bytes, timeout: float = 5.0) -> Optional[dict]:
        """Send packet and wait for matching response by sequence number."""
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
        """Send without waiting for a response."""
        if not self.connected or not self.sock:
            return
        try:
            with self._send_lock:
                self.sock.sendall(packet)
        except Exception as e:
            log.error(f"Send error: {e}")
            self.connected = False

    # -- Internal: background reader ------------------------------------------

    def _reader_loop(self):
        """Background thread: reads TCP data and dispatches parsed packets."""
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

        # Wake up anyone still waiting
        for seq, (evt, _) in list(self._pending.items()):
            evt.set()

    def _process_recv_buf(self):
        """Extract and dispatch complete packets from the receive buffer."""
        while len(self._recv_buf) >= MIN_PACKET:
            idx = self._recv_buf.find(MAGIC)
            if idx == -1:
                self._recv_buf = b''
                return
            if idx > 0:
                self._recv_buf = self._recv_buf[idx:]

            if len(self._recv_buf) < 4:
                return
            pkt_len = struct.unpack('<H', self._recv_buf[2:4])[0]
            if pkt_len < MIN_PACKET or pkt_len > 65535:
                self._recv_buf = self._recv_buf[2:]
                continue
            if len(self._recv_buf) < pkt_len:
                return  # need more data

            pkt_data = self._recv_buf[:pkt_len]
            self._recv_buf = self._recv_buf[pkt_len:]
            parsed = self.parser.parse_packet(pkt_data)
            if parsed:
                self._dispatch(parsed)

    def _dispatch(self, parsed: dict):
        """Route a parsed packet to the correct handler."""
        seq = parsed["seq"]
        cmd = parsed["cmd"]

        # Route to waiting caller first — prevents ACK feedback loops (v2.1 fix)
        if seq in self._pending:
            evt, _ = self._pending[seq]
            self._pending[seq] = (evt, parsed)
            evt.set()
            return

        # JOB_CONTROL echo from laser (confirms job execution started)
        if cmd == Cmd.JOB_CONTROL:
            log.info("Laser echoed JOB_CONTROL (0x0003) — execution confirmed")
            return

        # ACK unsolicited laser queries (once per (cmd, seq) to avoid loops)
        if cmd in (Cmd.QUERY_13, Cmd.QUERY_14, Cmd.QUERY_15):
            ack_key = (cmd, seq)
            if ack_key not in self._acked_unsolicited:
                self._send_only(self.builder.build_ack(cmd, seq))
                self._acked_unsolicited.add(ack_key)
            return

        # Notifications (msg_type=2) — ignore
        if parsed.get("msg_type") == 2:
            return

        log.debug(f"Unhandled packet: seq={seq} cmd=0x{cmd:04x}")

    # -- Internal: heartbeat --------------------------------------------------

    def _heartbeat_loop(self):
        """Background thread: sends STATUS every ~2s to keep connection alive."""
        while self.connected:
            try:
                time.sleep(2.0)
                if self.connected and not self._heartbeat_paused:
                    self._send_recv(self.builder.status(), timeout=2.0)
            except Exception:
                pass
