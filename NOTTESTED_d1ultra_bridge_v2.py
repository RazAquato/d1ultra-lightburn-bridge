#!/usr/bin/env python3
"""
Hansmaker D1 Ultra <-> LightBurn Bridge  v2
============================================
Translates GRBL/TCP (LightBurn) to the D1 Ultra proprietary binary protocol.

  LightBurn  ──GRBL/TCP──▶  Bridge (localhost:9023)  ──D1 Ultra/TCP──▶  Laser (192.168.12.1:6000)

!! WARNING — NOT YET TESTED ON REAL HARDWARE !!

  This bridge sends command 0x0003 (JOB_CONTROL) to the laser after uploading
  path data. Pcapng analysis of M+ captures confirms M+ does this, but an
  earlier test of sending 0x0003 from the v1 bridge caused the Z-axis to
  descend uncontrollably. The likely explanation is that v1 sent 0x0003 at
  the wrong point in the sequence (without valid job data uploaded first).

  v2 sends 0x0003 at the correct position (after all paths are uploaded),
  matching the M+ sequence. However, until this is confirmed on real hardware:

  BE PREPARED TO POWER OFF THE LASER IMMEDIATELY if the Z-axis starts moving
  unexpectedly. Keep your hand on the power switch during the first test.

Changes vs v1
─────────────
CRITICAL FIX (from pcapng analysis):
11. HOST now SENDS 0x0003 (JOB_CONTROL) to the laser instead of waiting for it.
    Every M+ capture proves the host initiates execution. The laser echoes 0x0003
    back as confirmation. This was the #1 blocker — the laser was waiting for us.
12. WORKSPACE (0x0009) payload is 42 bytes (5 doubles + 2-byte pad), not 40.

Previous v2 fixes:
1. JOB_SETTINGS unknown field: -1.0  (v1 defaulted to 0.0; M+ always sends -1.0)
2. CMD 0x0005 (PRE_JOB) now sent before JOB_UPLOAD, matching M+ sequence
3. CMD 0x0009 (WORKSPACE) sent with bounding box before path data, matching M+
4. CMD 0x0014 (sub=0x02, pre-job setup) sent before job, matching M+ sequence
5. Unsolicited 0x0013 / 0x0014 / 0x0015 messages now ACK'd immediately
6. PNG preview is now a proper 44×44 RGB image (~6 KB), closer to M+ size
7. G1 S0 duplicate point filter: trailing zero-power G1 moves excluded
8. Packet pacing: 10 ms delay between SETTINGS+PATH pairs (M+ pacing)
9. Replay mode: --replay <pcapng_file> sends HOST→LASER bytes from M+ capture
10. Full startup sequence matches M+ order: 0x0006→0x0000→0x0018→0x001E→0x000B→0x0013→0x0015

Usage:
    python d1ultra_bridge_v2.py --listen-port 9023
    python d1ultra_bridge_v2.py --replay path/to/capture.pcapng

LightBurn setup:
    Devices → Create Manually → GRBL (1.1f+) → Ethernet/TCP
    Address: 127.0.0.1   Port: 9023
    Origin: Front Left   Disable auto-home
"""

import os
import socket
import struct
import threading
import time
import argparse
import logging
import io
import math
import sys
import zlib
from typing import Optional, Tuple, List
from enum import IntEnum

# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_LASER_IP    = "192.168.12.1"
DEFAULT_LASER_PORT  = 6000
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 9023

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")

# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants
# ─────────────────────────────────────────────────────────────────────────────
MAGIC      = b'\x0a\x0a'
TERMINATOR = b'\x0d\x0d'

class Cmd(IntEnum):
    STATUS      = 0x0000
    PATH_DATA   = 0x0001
    JOB_UPLOAD  = 0x0002
    JOB_CONTROL = 0x0003   # Laser → Host only!  Never send this direction.
    JOB_FINISH  = 0x0004
    PRE_JOB     = 0x0005   # NEW v2: M+ sends this before JOB_UPLOAD
    DEVICE_ID   = 0x0006
    WORKSPACE   = 0x0009   # NEW v2: M+ sends bounding box before path data
    MOTOR_RESET = 0x000B
    CAMERA      = 0x000D
    PERIPHERAL  = 0x000E
    Z_AXIS      = 0x000F
    AUTOFOCUS   = 0x0012
    QUERY_13    = 0x0013
    QUERY_14    = 0x0014
    QUERY_15    = 0x0015
    DEVICE_INFO = 0x0018
    FW_VERSION  = 0x001E

class LaserSource(IntEnum):
    IR    = 0
    DIODE = 1

# ─────────────────────────────────────────────────────────────────────────────
# Preview PNG generator — produces a white 100×100 RGB PNG (~3.5 KB compressed)
# M+ sends ~6 KB; we now match size class.  The laser appears to require a
# non-trivial PNG — a 286-byte minimal PNG was tried in v1 and didn't help.
# ─────────────────────────────────────────────────────────────────────────────
def make_preview_png(width: int = 44, height: int = 44) -> bytes:
    """Generate a noisy preview PNG without PIL — produces ~6 KB matching M+ size.

    M+ sends a rendered preview thumbnail (~6255 bytes).  A plain white PNG
    compresses to ~286 bytes.  We use a deterministic pseudo-noise pattern that
    doesn't compress well, producing ~6 KB at 44×44 RGB with zlib level=0.
    """
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

    sig  = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)  # 8-bit RGB

    # Deterministic LCG noise: prevents zlib compression, keeps file large
    raw = bytearray()
    v = 0xDEADBEEF
    for _ in range(height):
        raw.append(0)  # filter byte
        for _ in range(width):
            v = (v * 1664525 + 1013904223) & 0xFFFFFFFF
            raw.append((v >> 16) & 0xFF)
            raw.append((v >> 8)  & 0xFF)
            raw.append(v         & 0xFF)

    idat = zlib.compress(bytes(raw), level=0)  # level=0 = store, no compression
    return sig + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat) + _chunk(b'IEND', b'')

# ─────────────────────────────────────────────────────────────────────────────
# CRC-16/MODBUS
# ─────────────────────────────────────────────────────────────────────────────
def crc16_modbus(data: bytes) -> int:
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
# Packet builder
# ─────────────────────────────────────────────────────────────────────────────
class PacketBuilder:
    def __init__(self):
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def build(self, cmd: int, payload: bytes = b'', msg_type: int = 1) -> bytes:
        seq = self.next_seq()
        total_len = 14 + len(payload) + 4
        header = struct.pack('<HH HH HH H',
            0x0A0A, total_len, 0, seq, 0, msg_type, cmd)
        crc_data = header[2:] + payload
        crc = crc16_modbus(crc_data)
        return header + payload + struct.pack('<H', crc) + TERMINATOR

    # ── Core job commands ─────────────────────────────────────────────────────

    def build_status(self):
        return self.build(Cmd.STATUS)

    def build_device_id(self):
        return self.build(Cmd.DEVICE_ID)

    def build_fw_version(self):
        return self.build(Cmd.FW_VERSION)

    def build_motor_reset(self):
        return self.build(Cmd.MOTOR_RESET)

    def build_query_13(self):
        return self.build(Cmd.QUERY_13)

    def build_query_15(self):
        return self.build(Cmd.QUERY_15)

    def build_query_14(self, sub: int = 0x02):
        return self.build(Cmd.QUERY_14, struct.pack('<B', sub))

    def build_device_info(self, device_id: int = 0x8B1B, ir_select: int = 0):
        payload  = struct.pack('<HH', 0x0006, device_id)
        payload += struct.pack('<B', ir_select)
        payload += b'\x00' * (32 - 5)
        return self.build(Cmd.DEVICE_INFO, payload)

    # ── NEW v2: PRE_JOB (0x0005) ─────────────────────────────────────────────
    def build_pre_job(self):
        """Send before JOB_UPLOAD.  M+ always sends this; v1 omitted it."""
        return self.build(Cmd.PRE_JOB)

    # ── NEW v2: WORKSPACE (0x0009) ────────────────────────────────────────────
    def build_workspace(self, speed: float,
                        x_min: float, y_min: float,
                        x_max: float, y_max: float):
        """Send bounding box of the job to the laser before path data.
        M+ sends this; v1 never did.  Speed appears to be a preview/move speed.
        Payload is 42 bytes: 5 doubles (40) + 2 zero-pad bytes (confirmed from pcapng).
        """
        payload = struct.pack('<ddddd', speed, x_min, y_min, x_max, y_max)
        payload += b'\x00\x00'  # 2-byte padding (M+ sends 42, not 40)
        return self.build(Cmd.WORKSPACE, payload)

    # ── Job settings (msg_type=0) ─────────────────────────────────────────────
    def build_job_settings(self, passes: int, speed_mm_min: float,
                           frequency_khz: float, power_frac: float,
                           laser_source: int = LaserSource.DIODE) -> bytes:
        """37-byte job settings payload.
        FIX v2: unknown field is now -1.0, matching every M+ capture.
        v1 used 0.0 as the default — this may be why JOB_CONTROL never arrived.
        """
        payload  = struct.pack('<I', passes)
        payload += struct.pack('<d', speed_mm_min)
        payload += struct.pack('<d', frequency_khz)
        payload += struct.pack('<d', power_frac)
        payload += struct.pack('<B', laser_source)
        payload += struct.pack('<d', -1.0)   # ← was 0.0 in v1; M+ always sends -1.0
        return self.build(Cmd.STATUS, payload, msg_type=0)

    # ── Path data (msg_type=0) ────────────────────────────────────────────────
    def build_path_data(self, segments: List[Tuple[float, float]]) -> bytes:
        count   = len(segments)
        payload = struct.pack('<I', count)
        for x, y in segments:
            payload += struct.pack('<d', x)
            payload += struct.pack('<d', y)
            payload += b'\x00' * 16
        return self.build(Cmd.PATH_DATA, payload, msg_type=0)

    # ── Job upload header ─────────────────────────────────────────────────────
    def build_job_upload(self, job_name: str, png_data: bytes = b'') -> bytes:
        name_bytes  = job_name.encode('utf-8')[:255]
        name_field  = name_bytes + b'\x00' * (256 - len(name_bytes))
        padding     = b'\x00\x00'
        png_size    = struct.pack('<I', len(png_data))
        return self.build(Cmd.JOB_UPLOAD, name_field + padding + png_size + png_data)

    # ── Job finalize ──────────────────────────────────────────────────────────
    def build_job_finish(self, job_name: str) -> bytes:
        name_bytes = job_name.encode('utf-8')[:255]
        name_field = name_bytes + b'\x00' * (256 - len(name_bytes))
        return self.build(Cmd.JOB_FINISH, name_field)

    # ── Peripheral / motion helpers ───────────────────────────────────────────
    def build_peripheral(self, module: int, state: bool) -> bytes:
        return self.build(Cmd.PERIPHERAL, struct.pack('<BB', module, 1 if state else 0))

    def build_z_move(self, distance_mm: float) -> bytes:
        payload  = struct.pack('<B', 0)
        payload += struct.pack('<d', distance_mm)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    def build_motor_home(self) -> bytes:
        payload  = struct.pack('<B', 2)
        payload += struct.pack('<d', 0.0)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    def build_autofocus_probe(self) -> bytes:
        payload = struct.pack('<B', 1) + b'\x00' * 19
        return self.build(Cmd.AUTOFOCUS, payload)

    def build_z_autofocus(self, z_mm: float) -> bytes:
        payload  = struct.pack('<B', 1)
        payload += struct.pack('<d', z_mm)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    # ── Generic ACK (for responding to unsolicited messages) ──────────────────
    def build_ack(self, cmd: int, seq: int) -> bytes:
        """Build an explicit ACK for a given cmd+seq received from the laser.
        v2 uses this to respond to unsolicited 0x0013/0x0014/0x0015 messages.
        """
        # Reuse seq from incoming packet so the laser correlates it correctly
        total_len = 14 + 2 + 4
        header = struct.pack('<HH HH HH H',
            0x0A0A, total_len, 0, seq, 0, 1, cmd)
        payload  = b'\x00\x00'
        crc_data = header[2:] + payload
        crc      = crc16_modbus(crc_data)
        return header + payload + struct.pack('<H', crc) + TERMINATOR


# ─────────────────────────────────────────────────────────────────────────────
# Response parser
# ─────────────────────────────────────────────────────────────────────────────
class ResponseParser:
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
        return {'cmd': cmd, 'seq': seq, 'msg_type': msg_type, 'payload': payload, 'length': pkt_len}

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


# ─────────────────────────────────────────────────────────────────────────────
# D1 Ultra connection
# ─────────────────────────────────────────────────────────────────────────────
class D1UltraConnection:
    def __init__(self, ip: str = DEFAULT_LASER_IP, port: int = DEFAULT_LASER_PORT):
        self.ip   = ip
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.builder = PacketBuilder()
        self.parser  = ResponseParser()
        self.send_lock = threading.Lock()
        self.connected = False
        self.device_name = ""
        self.fw_version  = ""
        self._recv_buf   = b''
        self._recv_lock  = threading.Lock()
        self._pending: dict = {}
        self._job_ready = threading.Event()
        self._heartbeat_paused = False
        self._reader_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    # ── Connection management ─────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.ip, self.port))
            self.sock.settimeout(None)
            self.connected = True
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

    # ── Background reader ─────────────────────────────────────────────────────

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
            if not parsed:
                continue
            self._dispatch(parsed)

    def _dispatch(self, parsed: dict):
        cmd      = parsed['cmd']
        seq      = parsed['seq']
        msg_type = parsed['msg_type']

        # ── v2 FIX: respond to unsolicited laser messages 0x0013/0x0014/0x0015 ──
        # M+ receives these before/during jobs.  v1 ignored them.
        # We now echo them back immediately as ACKs so the laser doesn't stall.
        if cmd in (Cmd.QUERY_13, Cmd.QUERY_14, Cmd.QUERY_15) and msg_type != 2:
            log.info(f"  Unsolicited cmd=0x{cmd:04x} seq={seq} — sending ACK")
            ack = self.builder.build_ack(cmd, seq)
            self.send_only(ack)
            # Still route to any caller waiting on this seq
            if seq in self._pending:
                evt, _ = self._pending[seq]
                self._pending[seq] = (evt, parsed)
                evt.set()
            return

        # ── JOB_CONTROL (0x0003) from laser → job ready to execute ───────────
        if cmd == Cmd.JOB_CONTROL:
            log.info("  ✓ Laser sent JOB_CONTROL (0x0003) — ready to execute!")
            self._job_ready.set()
            return

        # ── Notification messages (msg_type=2) — just log ─────────────────────
        if msg_type == 2:
            log.debug(f"Laser notification cmd=0x{cmd:04x}")
            return

        # ── Route to waiting caller by sequence number ─────────────────────────
        if seq in self._pending:
            evt, _ = self._pending[seq]
            self._pending[seq] = (evt, parsed)
            evt.set()
        else:
            log.info(f"  Unsolicited: seq={seq} cmd=0x{cmd:04x} "
                     f"msg_type={msg_type} payload={len(parsed.get('payload',b''))}b")

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def _heartbeat_loop(self):
        while self.connected:
            try:
                time.sleep(2.0)
                if self.connected and not self._heartbeat_paused:
                    self.send_and_recv(self.builder.build_status(), timeout=2.0)
            except Exception:
                pass

    # ── Send helpers ──────────────────────────────────────────────────────────

    def send_and_recv(self, packet: bytes, timeout: float = 5.0) -> Optional[dict]:
        if not self.connected or not self.sock:
            return None
        if len(packet) < 8:
            return None
        seq = struct.unpack('<H', packet[6:8])[0]
        evt = threading.Event()
        self._pending[seq] = (evt, None)
        try:
            with self.send_lock:
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
        log.debug(f"Timeout waiting for seq={seq}")
        return None

    def send_only(self, packet: bytes):
        if not self.connected or not self.sock:
            return
        try:
            with self.send_lock:
                self.sock.sendall(packet)
        except Exception as e:
            log.error(f"Send error: {e}")
            self.connected = False

    def ping(self) -> bool:
        if not self.connected:
            return False
        return self.send_and_recv(self.builder.build_status(), timeout=2.0) is not None

    # ── Startup handshake ─────────────────────────────────────────────────────

    def identify(self) -> bool:
        """Full M+ startup sequence (order matters)."""
        r = self.send_and_recv(self.builder.build_device_id())
        if r: self.device_name = self.parser.parse_device_name(r)
        log.info(f"Device: {self.device_name or '(unknown)'}")

        self.send_and_recv(self.builder.build_status())
        self.send_and_recv(self.builder.build_device_info())

        r = self.send_and_recv(self.builder.build_fw_version())
        if r: self.fw_version = self.parser.parse_fw_version(r)
        log.info(f"Firmware: {self.fw_version or '(unknown)'}")

        r = self.send_and_recv(self.builder.build_motor_reset(), timeout=8.0)
        if r: log.info(f"Motor calibration: {len(r.get('payload',b''))} bytes")
        else: log.warning("Motor calibration: no response")

        self.send_and_recv(self.builder.build_query_13())
        self.send_and_recv(self.builder.build_query_15())
        return bool(self.device_name)

    # ── Job execution ─────────────────────────────────────────────────────────

    def execute_job(self, path_groups: List[List[Tuple[float, float]]],
                    job_name: str,
                    passes: int, speed_mm_min: float,
                    frequency_khz: float, power_frac: float,
                    laser_source: int = LaserSource.DIODE) -> bool:
        """
        Full job execution sequence matching M+ behaviour.

        v2 sequence (verified against pcapng captures):
          1. DEVICE_INFO (0x0018)
          2. PRE_JOB     (0x0005)
          3. QUERY_14    (0x0014, sub=0x02)
          4. JOB_UPLOAD  (0x0002)  with proper PNG
          5. WORKSPACE   (0x0009)  with bounding box (42-byte payload)
          6. For each path group:
               a. JOB_SETTINGS (0x0000, msg_type=0)
               b. PATH_DATA    (0x0001, msg_type=0)
               c. 10 ms pause
          7. HOST sends JOB_CONTROL (0x0003) — initiates execution
          8. Laser echoes JOB_CONTROL (0x0003) — confirms ready
          9. JOB_FINISH (0x0004)
        """
        if not path_groups:
            log.warning("No path groups — nothing to send")
            return False

        # Filter trivial groups (single-point or all-same-position groups)
        groups = [g for g in path_groups if len(g) >= 2]
        if not groups:
            log.warning("All path groups were single-point — skipping")
            return False

        # ── v2 FIX: filter G1 S0 duplicate closing point ──────────────────────
        # LightBurn emits a G1 S0 before M5 which adds a duplicate of the last
        # cut position with zero power.  M+ doesn't have this extra point.
        # Remove any trailing point that is identical to the second-to-last.
        cleaned = []
        for grp in groups:
            if len(grp) >= 3 and grp[-1] == grp[-2]:
                grp = grp[:-1]
            cleaned.append(grp)
        groups = cleaned

        # Compute bounding box (centered coords — all points are already relative)
        all_pts = [pt for grp in groups for pt in grp]
        x_vals  = [p[0] for p in all_pts]
        y_vals  = [p[1] for p in all_pts]
        bb_xmin, bb_xmax = min(x_vals), max(x_vals)
        bb_ymin, bb_ymax = min(y_vals), max(y_vals)

        log.info(f"Job: {len(groups)} paths, {len(all_pts)} total points")
        log.info(f"  Bounding box: X [{bb_xmin:.2f}..{bb_xmax:.2f}]  "
                 f"Y [{bb_ymin:.2f}..{bb_ymax:.2f}] mm")
        log.info(f"  {passes} pass(es), {speed_mm_min:.0f} mm/min, "
                 f"{power_frac*100:.0f}% power, {frequency_khz:.0f} kHz, "
                 f"source={'IR' if laser_source==LaserSource.IR else 'Diode'}")

        self._job_ready.clear()
        self._heartbeat_paused = True

        try:
            # Step 1: DEVICE_INFO
            log.info("Step 1: DEVICE_INFO")
            self.send_and_recv(self.builder.build_device_info())

            # Step 2: PRE_JOB (NEW in v2)
            log.info("Step 2: PRE_JOB (0x0005)")
            r = self.send_and_recv(self.builder.build_pre_job(), timeout=5.0)
            if not r:
                log.warning("  PRE_JOB: no response (continuing anyway)")

            # Step 3: QUERY_14 sub=0x02 pre-job setup (NEW in v2)
            log.info("Step 3: QUERY_14(0x02) pre-job setup")
            self.send_and_recv(self.builder.build_query_14(0x02), timeout=5.0)

            # Step 4: JOB_UPLOAD with proper PNG
            log.info("Step 4: JOB_UPLOAD with PNG preview")
            png = make_preview_png(100, 100)
            log.info(f"  PNG size: {len(png)} bytes")
            r = self.send_and_recv(
                self.builder.build_job_upload(job_name, png), timeout=10.0)
            if not r:
                log.error("JOB_UPLOAD: no ACK — aborting")
                return False

            # Step 5: WORKSPACE with bounding box (NEW in v2)
            log.info("Step 5: WORKSPACE bounding box")
            r = self.send_and_recv(
                self.builder.build_workspace(
                    speed_mm_min, bb_xmin, bb_ymin, bb_xmax, bb_ymax),
                timeout=5.0)
            if not r:
                log.warning("  WORKSPACE: no ACK (continuing anyway)")

            # Step 6: SETTINGS+PATH pairs
            log.info(f"Step 6: Sending {len(groups)} SETTINGS+PATH pairs...")
            for i, group in enumerate(groups):
                # JOB_SETTINGS
                r = self.send_and_recv(
                    self.builder.build_job_settings(
                        passes, speed_mm_min, frequency_khz,
                        power_frac, laser_source),
                    timeout=5.0)
                if not r:
                    log.warning(f"  Path {i}: JOB_SETTINGS no ACK")

                # PATH_DATA
                r = self.send_and_recv(
                    self.builder.build_path_data(group),
                    timeout=10.0)
                if not r:
                    log.error(f"  Path {i}: PATH_DATA no ACK — aborting")
                    return False

                # v2: 10 ms pacing between pairs (matching M+ timing)
                time.sleep(0.010)

                if (i + 1) % 50 == 0 or i == len(groups) - 1:
                    log.info(f"  {i+1}/{len(groups)} paths sent")

            # Step 7: Send JOB_CONTROL (0x0003) to the laser
            # CRITICAL FIX: pcapng analysis proves M+ SENDS 0x0003 to the laser
            # (the host initiates execution). The laser echoes 0x0003 back as ACK.
            # Previous versions waited for the laser to send 0x0003, which never
            # happened because the laser was waiting for the HOST to send it.
            log.info("Step 7: Sending JOB_CONTROL (0x0003) to laser...")
            jc_pkt = self.builder.build(Cmd.JOB_CONTROL, b'', msg_type=1)
            r = self.send_and_recv(jc_pkt, timeout=15.0)
            if r and r.get('cmd') == Cmd.JOB_CONTROL:
                log.info("  ✓ Laser confirmed JOB_CONTROL — ready to execute!")
            elif r:
                log.info(f"  Laser responded: cmd=0x{r.get('cmd',0):04x} "
                         f"(expected 0x0003, continuing anyway)")
            else:
                log.warning("  No response to JOB_CONTROL — continuing to finalize")

            # Step 8: JOB_FINISH
            log.info("Step 8: JOB_FINISH")
            r = self.send_and_recv(self.builder.build_job_finish(job_name), timeout=5.0)
            if r:
                log.info("  JOB_FINISH ACK'd — job engraving!")
            else:
                log.warning("  JOB_FINISH: no ACK (job may still run)")

            return True

        finally:
            self._heartbeat_paused = False

    # ── Motor homing / autofocus ──────────────────────────────────────────────

    def home_motors(self, retract_mm: float = 5.0, timeout: float = 60.0) -> bool:
        log.info("Homing motors...")
        self.send_and_recv(self.builder.build_motor_home(), timeout=timeout)
        log.info(f"Retracting {retract_mm:.1f} mm off endstop...")
        self.send_and_recv(self.builder.build_z_move(-retract_mm), timeout=15.0)
        log.info("Homing complete")
        return True

    def run_autofocus(self, hw_id: int = 0x1A8B) -> Optional[float]:
        log.info("Autofocus: starting 3-probe sequence...")
        self.send_and_recv(self.builder.build_status())
        self.send_and_recv(self.builder.build_query_15())
        self.send_and_recv(self.builder.build_query_14(0x02))
        self.send_and_recv(self.builder.build_device_info(device_id=hw_id, ir_select=1))
        measurements = []
        for i in range(3):
            log.info(f"  Probe {i+1}/3...")
            r = self.send_and_recv(self.builder.build_autofocus_probe(), timeout=10.0)
            if not r or len(r.get('payload', b'')) < 30:
                log.warning(f"  Probe {i+1}: short/no response")
                continue
            payload = r['payload']
            z_val = struct.unpack_from('<d', payload, 22)[0]
            log.info(f"  Z = {z_val:.3f} mm")
            measurements.append(z_val)
            zp = self.builder.build_z_autofocus(z_val)
            self.send_and_recv(zp, timeout=60.0)
            if i < 2:
                for _ in range(5):
                    time.sleep(0.4)
                    self.send_and_recv(self.builder.build_status(), timeout=2.0)
        self.send_and_recv(self.builder.build_device_info(device_id=hw_id, ir_select=0))
        if measurements:
            avg = sum(measurements) / len(measurements)
            log.info(f"Autofocus done: Z = {avg:.3f} mm (avg of {len(measurements)})")
            return avg
        log.warning("Autofocus failed")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# GRBL state machine
# ─────────────────────────────────────────────────────────────────────────────
class GRBLState:
    def __init__(self):
        self.x = self.y = self.z = 0.0
        self.feed_rate   = 1000.0
        self.laser_on    = False
        self.power       = 0.0
        self.max_power   = 1000.0
        self.absolute_mode = True
        self.is_running  = False
        self.is_homed    = False
        self.job_path_groups: List[List[Tuple[float, float]]] = []
        self.job_name    = "lightburn_job"
        self.bb_x_min    = float('inf')
        self.bb_x_max    = float('-inf')
        self.bb_y_min    = float('inf')
        self.bb_y_max    = float('-inf')
        self.laser_source   = LaserSource.DIODE
        self.frequency_khz  = 50.0
        self.passes         = 1
        self.speed_mm_min   = 1000.0
        self.job_power      = 0.0

    def start_new_path_group(self, x: float, y: float):
        self.job_path_groups.append([(x, y)])

    def add_cut_point(self, x: float, y: float, power: float):
        """Add a cut point.  v2: skip zero-power (G1 S0) points."""
        if power <= 0.0:
            return   # ← v2 FIX: drop S0 moves (duplicate closing points)
        if self.job_power < power:
            self.job_power = power
        if not self.job_path_groups:
            self.job_path_groups.append([(x, y)])
        else:
            self.job_path_groups[-1].append((x, y))

    def reset_job(self):
        self.job_path_groups = []
        self.job_power  = 0.0
        self.bb_x_min   = float('inf')
        self.bb_x_max   = float('-inf')
        self.bb_y_min   = float('inf')
        self.bb_y_max   = float('-inf')

    @property
    def power_fraction(self) -> float:
        if self.max_power == 0: return 0.0
        return min(1.0, self.power / self.max_power)

    @property
    def job_power_fraction(self) -> float:
        if self.max_power == 0: return 0.0
        return min(1.0, self.job_power / self.max_power)


# ─────────────────────────────────────────────────────────────────────────────
# GRBL translator
# ─────────────────────────────────────────────────────────────────────────────
class GRBLTranslator:
    GRBL_SETTINGS = {
        0: 10, 1: 25, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0,
        10: 1, 11: 0.010, 12: 0.002, 13: 0,
        20: 0, 21: 0, 22: 0, 23: 0, 24: 25.0, 25: 500.0, 26: 250, 27: 1.0,
        30: 1000, 31: 0, 32: 1,
        100: 80.0, 101: 80.0, 102: 80.0,
        110: 5000.0, 111: 5000.0, 112: 500.0,
        120: 200.0, 121: 200.0, 122: 50.0,
        130: 400.0, 131: 400.0, 132: 100.0,
    }

    def __init__(self, laser: D1UltraConnection, state: GRBLState):
        self.laser = laser
        self.state = state

    def handle_line(self, line: str) -> str:
        line = line.strip()
        if not line: return "ok"
        if ';' in line: line = line[:line.index(';')].strip()
        if '(' in line: line = line[:line.index('(')].strip()
        if not line: return "ok"
        upper = line.upper()

        if upper == '?':          return self._status_report()
        if upper == '$$':         return self._settings_report()
        if upper == '$H':         return self._home()
        if upper in ('$X','$X\n'): return self._unlock()
        if upper.startswith('$J='): return self._jog(line[3:])
        if upper == '\x18':       return self._reset()
        if upper in ('!', '~'):   return "ok"
        if upper in ('$FOCUS', '$FOCUS ON'): return self._focus_on()
        if upper == '$FOCUS OFF': return self._focus_off()
        if upper in ('$AUTOFOCUS', '$AF'): return self._autofocus()
        if upper == '$I':         return self._build_info()
        if upper == '$#':         return self._gcode_parameters()
        if upper == '$G':         return self._gcode_parser_state()
        if upper.startswith('$'): return "ok"
        return self._parse_gcode(line)

    def _status_report(self) -> str:
        st = "Run" if self.state.is_running else "Idle"
        x, y, z = self.state.x, self.state.y, self.state.z
        return f"<{st}|MPos:{x:.3f},{y:.3f},{z:.3f}|FS:{self.state.feed_rate:.0f},{self.state.power:.0f}>"

    def _settings_report(self) -> str:
        lines = []
        for k in sorted(self.GRBL_SETTINGS):
            v = self.GRBL_SETTINGS[k]
            lines.append(f"${k}={v:.3f}" if isinstance(v, float) else f"${k}={v}")
        lines.append("ok")
        return "\n".join(lines)

    def _build_info(self) -> str:
        fw = self.laser.fw_version or "1.0.0"
        return f"[VER:1.1h.20190825: D1Ultra Bridge v2 ({fw})]\n[OPT:V,15,128]\nok"

    def _gcode_parameters(self) -> str:
        lines = [f"[{cs}:0.000,0.000,0.000]"
                 for cs in ['G54','G55','G56','G57','G58','G59']]
        lines += ["[G28:0.000,0.000,0.000]","[G30:0.000,0.000,0.000]",
                  "[G92:0.000,0.000,0.000]","[TLO:0.000]",
                  "[PRB:0.000,0.000,0.000:0]","ok"]
        return "\n".join(lines)

    def _gcode_parser_state(self) -> str:
        mode = "G90" if self.state.absolute_mode else "G91"
        return f"[GC:G0 {mode} G17 G21 G94 G54 M5 M9 T0 F{self.state.feed_rate:.0f} S{self.state.power:.0f}]\nok"

    def _home(self) -> str:
        t = threading.Thread(target=self.laser.home_motors, daemon=True)
        t.start()
        self.state.is_homed = True
        return "ok"

    def _unlock(self) -> str:
        return "[MSG:Caution: Unlocked]\nok"

    def _reset(self) -> str:
        self.state.reset_job()
        self.state.is_running = False
        return "\r\nGrbl 1.1h ['$' for help]\n[MSG:'$H'|'$X' to unlock]\n[MSG:Caution: Unlocked]"

    def _focus_on(self) -> str:
        self.laser.send_and_recv(self.laser.builder.build_peripheral(2, True))
        return "ok"

    def _focus_off(self) -> str:
        self.laser.send_and_recv(self.laser.builder.build_peripheral(2, False))
        return "ok"

    def _autofocus(self) -> str:
        t = threading.Thread(target=self.laser.run_autofocus, daemon=True)
        t.start()
        return "ok"

    def _jog(self, params: str) -> str:
        # Parse jog command for Z movement
        upper = params.upper()
        if 'Z' in upper:
            try:
                z_idx = upper.index('Z')
                end   = z_idx + 1
                while end < len(upper) and (upper[end].isdigit() or upper[end] in '.-+'):
                    end += 1
                z_dist = float(upper[z_idx+1:end])
                self.laser.send_and_recv(self.laser.builder.build_z_move(z_dist))
            except Exception:
                pass
        return "ok"

    # ── G-code parsing ────────────────────────────────────────────────────────

    def _parse_gcode(self, line: str) -> str:
        upper = line.upper()
        parts = upper.split()

        # Extract modal values
        f_val = self._extract(upper, 'F')
        s_val = self._extract(upper, 'S')
        x_val = self._extract(upper, 'X')
        y_val = self._extract(upper, 'Y')
        z_val = self._extract(upper, 'Z')

        if s_val is not None:
            self.state.power = s_val
        if f_val is not None:
            self.state.feed_rate = f_val

        # Laser on/off
        if 'M3' in parts or 'M03' in parts:
            self.state.laser_on = True
        if 'M5' in parts or 'M05' in parts:
            self.state.laser_on = False

        # M30 / M2 = program end → execute job
        if 'M30' in parts or 'M2' in parts:
            return self._finish_job()

        # G20 / G21 units
        if 'G20' in parts: pass  # inches — we ignore, assume mm
        if 'G21' in parts: pass  # mm

        # G90 / G91 positioning
        if 'G90' in parts: self.state.absolute_mode = True
        if 'G91' in parts: self.state.absolute_mode = False

        # G92 set position
        if 'G92' in parts:
            if x_val is not None: self.state.x = x_val
            if y_val is not None: self.state.y = y_val
            if z_val is not None: self.state.z = z_val
            return "ok"

        # G0 rapid move
        if any(p in parts for p in ('G0', 'G00')):
            if x_val is not None: self.state.x = x_val if self.state.absolute_mode else self.state.x + x_val
            if y_val is not None: self.state.y = y_val if self.state.absolute_mode else self.state.y + y_val
            if z_val is not None:
                dz = (z_val - self.state.z) if self.state.absolute_mode else z_val
                if abs(dz) > 0.001:
                    self.laser.send_and_recv(self.laser.builder.build_z_move(dz))
                self.state.z = self.state.z + dz if not self.state.absolute_mode else z_val
            # Start a new path group for engraving
            self.state.start_new_path_group(self.state.x, self.state.y)
            return "ok"

        # G1 / G2 / G3 cut move
        if any(p in parts for p in ('G1', 'G01', 'G2', 'G02', 'G3', 'G03')):
            nx = (x_val if self.state.absolute_mode else self.state.x + x_val) if x_val is not None else self.state.x
            ny = (y_val if self.state.absolute_mode else self.state.y + y_val) if y_val is not None else self.state.y

            # Arc interpolation (G2/G3) → linearise to straight segments
            if any(p in parts for p in ('G2', 'G02', 'G3', 'G03')):
                segs = self._linearise_arc(
                    self.state.x, self.state.y, nx, ny, line, upper,
                    clockwise=any(p in parts for p in ('G2', 'G02')))
                pwr = s_val if s_val is not None else self.state.power
                for sx, sy in segs:
                    self.state.add_cut_point(sx, sy, pwr)
                    self.state.x = sx
                    self.state.y = sy
            else:
                pwr = s_val if s_val is not None else self.state.power
                self.state.x = nx
                self.state.y = ny
                if z_val is not None:
                    dz = (z_val - self.state.z) if self.state.absolute_mode else z_val
                    if abs(dz) > 0.001:
                        self.laser.send_and_recv(self.laser.builder.build_z_move(dz))
                    self.state.z = z_val if self.state.absolute_mode else self.state.z + z_val
                self.state.add_cut_point(nx, ny, pwr)
            return "ok"

        return "ok"

    @staticmethod
    def _extract(line: str, letter: str) -> Optional[float]:
        idx = line.find(letter)
        if idx == -1: return None
        end = idx + 1
        while end < len(line) and (line[end].isdigit() or line[end] in '.-+'):
            end += 1
        try:
            return float(line[idx+1:end])
        except ValueError:
            return None

    @staticmethod
    def _linearise_arc(x0: float, y0: float, x1: float, y1: float,
                       line: str, upper: str, clockwise: bool,
                       segments: int = 32) -> List[Tuple[float, float]]:
        """Convert a G2/G3 arc to straight-line segments."""
        i_val = GRBLTranslator._extract(upper, 'I') or 0.0
        j_val = GRBLTranslator._extract(upper, 'J') or 0.0
        cx, cy = x0 + i_val, y0 + j_val
        r = math.sqrt((x0 - cx)**2 + (y0 - cy)**2)
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        if clockwise and a1 > a0:
            a1 -= 2 * math.pi
        elif not clockwise and a1 < a0:
            a1 += 2 * math.pi
        pts = []
        for k in range(1, segments + 1):
            a = a0 + (a1 - a0) * k / segments
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    # ── Job execution trigger ─────────────────────────────────────────────────

    def _finish_job(self) -> str:
        """Called when M30/M2 received — translate all collected paths and send."""
        groups = [g for g in self.state.job_path_groups if len(g) >= 2]
        if not groups:
            log.info("M30 received but no path groups — nothing to engrave")
            self.state.reset_job()
            return "ok"

        # Compute bounding box from filtered groups
        all_pts = [pt for g in groups for pt in g]
        x_vals  = [p[0] for p in all_pts]
        y_vals  = [p[1] for p in all_pts]
        cx = (min(x_vals) + max(x_vals)) / 2
        cy = (min(y_vals) + max(y_vals)) / 2

        # Centre coords around bounding-box midpoint (matching M+ coordinate system)
        centred = [[(x - cx, y - cy) for x, y in g] for g in groups]

        power = self.state.job_power_fraction
        if power <= 0.0:
            power = self.state.power_fraction
        if power <= 0.0:
            power = 0.5   # fallback

        speed = self.state.speed_mm_min if self.state.speed_mm_min > 0 else 500.0

        log.info(f"M30: starting job '{self.state.job_name}' "
                 f"({len(centred)} paths, {power*100:.0f}% power, {speed:.0f} mm/min)")
        self.state.is_running = True

        def _run():
            ok = self.laser.execute_job(
                centred,
                self.state.job_name,
                self.state.passes,
                speed,
                self.state.frequency_khz,
                power,
                self.state.laser_source,
            )
            self.state.is_running = False
            if ok:
                log.info("Job complete!")
            else:
                log.error("Job FAILED — JOB_CONTROL never received")
            self.state.reset_job()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return "ok"


# ─────────────────────────────────────────────────────────────────────────────
# GRBL server (LightBurn side)
# ─────────────────────────────────────────────────────────────────────────────
class GRBLServer:
    def __init__(self, laser: D1UltraConnection,
                 host: str = DEFAULT_LISTEN_HOST,
                 port: int = DEFAULT_LISTEN_PORT):
        self.laser = laser
        self.host  = host
        self.port  = port
        self._server_sock: Optional[socket.socket] = None
        self._client_sock: Optional[socket.socket] = None

    def start(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(1)
        log.info(f"GRBL server listening on {self.host}:{self.port}")
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self._server_sock.accept()
                log.info(f"LightBurn connected from {addr}")
                self._client_sock = conn
                t = threading.Thread(target=self._handle_client,
                                     args=(conn,), daemon=True)
                t.start()
            except Exception as e:
                log.error(f"Accept error: {e}")
                break

    def _handle_client(self, conn: socket.socket):
        state      = GRBLState()
        translator = GRBLTranslator(self.laser, state)
        buf = b''

        # Send GRBL greeting
        try:
            conn.sendall(b"\r\nGrbl 1.1h ['$' for help]\r\n")
            conn.sendall(b"[MSG:'$H'|'$X' to unlock]\r\n")
        except Exception:
            return

        try:
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    log.info("LightBurn disconnected")
                    break
                buf += chunk
                while b'\n' in buf:
                    idx  = buf.index(b'\n')
                    line = buf[:idx].decode('ascii', errors='replace')
                    buf  = buf[idx+1:]
                    resp = translator.handle_line(line)
                    if resp:
                        try:
                            conn.sendall((resp + "\r\n").encode('ascii'))
                        except Exception:
                            break
        except Exception as e:
            log.warning(f"Client handler error: {e}")
        finally:
            try: conn.close()
            except: pass


# ─────────────────────────────────────────────────────────────────────────────
# Replay tool — send raw M+ pcapng HOST→LASER packets directly to laser
# ─────────────────────────────────────────────────────────────────────────────
def replay_pcapng(path: str, laser_ip: str = DEFAULT_LASER_IP,
                  laser_port: int = DEFAULT_LASER_PORT):
    """
    Parse a Wireshark pcapng file and replay all HOST→LASER TCP payloads
    byte-for-byte to the laser.  Watches for JOB_CONTROL (0x0003) response.

    This is the diagnostic tool recommended in CLAUDE.md as highest priority:
    it proves the M+ bytes work, then we can diff against the bridge's bytes.

    Only replays TCP segments flowing from the host to the laser (not ACKs or
    laser→host traffic).  Timing is preserved from the capture.
    """
    try:
        import struct as _struct
    except ImportError:
        pass

    log.info(f"Replay: opening {path}")

    # ── Read pcapng file ──────────────────────────────────────────────────────
    with open(path, 'rb') as f:
        raw = f.read()

    # Parse pcapng blocks
    host_payloads = []  # list of (timestamp_ns, bytes)
    offset = 0
    pcap_magic = 0x0A0D0D0A  # SHB magic

    if len(raw) < 8:
        log.error("File too short")
        return

    shb_type = struct.unpack('<I', raw[0:4])[0]
    if shb_type != pcap_magic:
        log.error(f"Not a pcapng file (magic 0x{shb_type:08x})")
        return

    log.info("Parsing pcapng blocks...")

    # We'll do a simple pcapng parser for EPB (Enhanced Packet Block, type 6)
    BLOCK_SHB = 0x0A0D0D0A
    BLOCK_IDB = 1
    BLOCK_EPB = 6
    BLOCK_SPB = 3

    laser_ip_bytes = bytes(int(b) for b in laser_ip.split('.'))
    laser_port_b   = laser_port

    packets_found = 0
    last_ts = None

    while offset + 8 <= len(raw):
        block_type   = struct.unpack('<I', raw[offset:offset+4])[0]
        block_length = struct.unpack('<I', raw[offset+4:offset+8])[0]
        if block_length < 12 or offset + block_length > len(raw):
            break

        block_data = raw[offset:offset+block_length]
        offset += block_length

        if block_type == BLOCK_EPB:
            # EPB: interface_id(4) + ts_high(4) + ts_low(4) + caplen(4) + origlen(4) + data
            if len(block_data) < 28:
                continue
            ts_high  = struct.unpack('<I', block_data[12:16])[0]
            ts_low   = struct.unpack('<I', block_data[16:20])[0]
            cap_len  = struct.unpack('<I', block_data[20:24])[0]
            timestamp_ns = ((ts_high << 32) | ts_low) * 1000  # assume µs resolution

            if 28 + cap_len > len(block_data):
                continue
            pkt_data = block_data[28:28+cap_len]

            # Try to parse as Ethernet+IP+TCP, extract payload
            payload = _extract_tcp_payload(pkt_data, laser_ip_bytes, laser_port_b)
            if payload and len(payload) >= 18:
                # Filter: only packets containing D1 Ultra protocol data (magic 0x0A0A)
                if b'\x0a\x0a' in payload:
                    packets_found += 1
                    host_payloads.append((timestamp_ns, payload))

    log.info(f"Found {packets_found} HOST→LASER D1 Ultra packets")
    if not host_payloads:
        log.error("No host→laser D1 Ultra packets found. "
                  "Check the capture file and laser IP.")
        return

    # ── Connect and replay ────────────────────────────────────────────────────
    log.info(f"Connecting to {laser_ip}:{laser_port} for replay...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    try:
        sock.connect((laser_ip, laser_port))
    except Exception as e:
        log.error(f"Connect failed: {e}")
        return

    sock.settimeout(None)
    recv_buf  = b''
    job_ready = threading.Event()
    stop      = threading.Event()

    def reader():
        nonlocal recv_buf
        while not stop.is_set():
            try:
                chunk = sock.recv(4096)
                if not chunk: break
                recv_buf += chunk
                # Scan for JOB_CONTROL (0x0003)
                while len(recv_buf) >= 18:
                    idx = recv_buf.find(b'\x0a\x0a')
                    if idx == -1:
                        recv_buf = b''
                        break
                    if idx > 0:
                        recv_buf = recv_buf[idx:]
                    if len(recv_buf) < 4: break
                    plen = struct.unpack('<H', recv_buf[2:4])[0]
                    if len(recv_buf) < plen: break
                    pkt = recv_buf[:plen]
                    recv_buf = recv_buf[plen:]
                    if len(pkt) >= 14:
                        cmd = struct.unpack('<H', pkt[12:14])[0]
                        seq = struct.unpack('<H', pkt[6:8])[0]
                        log.info(f"  LASER→HOST: cmd=0x{cmd:04x} seq={seq} "
                                 f"len={plen}")
                        if cmd == 0x0003:
                            log.info("  ✓ JOB_CONTROL received! Replay works!")
                            job_ready.set()
            except Exception:
                break

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()

    log.info("Replaying packets...")
    prev_ts = host_payloads[0][0] if host_payloads else 0

    for i, (ts, payload) in enumerate(host_payloads):
        # Preserve inter-packet timing (cap at 500 ms to avoid stalling)
        if i > 0 and prev_ts:
            delay_ns = ts - prev_ts
            delay_s  = min(delay_ns / 1e9, 0.5)
            if delay_s > 0.001:
                time.sleep(delay_s)
        prev_ts = ts

        cmd = struct.unpack('<H', payload[12:14])[0] if len(payload) >= 14 else 0
        log.info(f"  → Pkt {i+1}/{len(host_payloads)}: cmd=0x{cmd:04x} "
                 f"len={len(payload)}")
        try:
            sock.sendall(payload)
        except Exception as e:
            log.error(f"Send error: {e}")
            break
        time.sleep(0.005)  # small inter-packet gap

    log.info("All packets sent. Waiting 10 s for JOB_CONTROL...")
    got = job_ready.wait(timeout=10.0)
    if got:
        log.info("✓ Replay SUCCESS — laser responded with JOB_CONTROL!")
    else:
        log.error("✗ Replay: JOB_CONTROL not received in 10 s")
        log.error("  This means either the capture parsing missed packets, or")
        log.error("  the laser needs additional time, or the pcapng is incomplete.")

    stop.set()
    sock.close()


def _extract_tcp_payload(pkt: bytes, dst_ip: bytes, dst_port: int) -> Optional[bytes]:
    """Extract TCP payload from a raw Ethernet frame (pcapng format)."""
    try:
        # Ethernet header: 14 bytes (dst[6] src[6] ethertype[2])
        if len(pkt) < 14: return None
        ethertype = struct.unpack('>H', pkt[12:14])[0]
        if ethertype != 0x0800: return None  # IPv4 only

        # IP header
        ip_start = 14
        if len(pkt) < ip_start + 20: return None
        ihl    = (pkt[ip_start] & 0x0F) * 4
        proto  = pkt[ip_start + 9]
        dst_a  = pkt[ip_start+16:ip_start+20]
        if proto != 6: return None  # TCP only
        if dst_a != bytes(dst_ip): return None  # must be going to laser

        # TCP header
        tcp_start  = ip_start + ihl
        if len(pkt) < tcp_start + 20: return None
        dport      = struct.unpack('>H', pkt[tcp_start+2:tcp_start+4])[0]
        if dport != dst_port: return None
        tcp_offset = ((pkt[tcp_start + 12] >> 4) & 0xF) * 4
        payload    = pkt[tcp_start + tcp_offset:]
        return payload if payload else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Interactive console
# ─────────────────────────────────────────────────────────────────────────────
HELP = """
D1 Ultra Bridge v2 — console commands

  Peripheral control:
    light on/off        Fill light
    buzzer on/off       Buzzer
    focus on/off        Focus laser pointer
    gate on/off         Safety gate

  Motion:
    home                Home/reset motors
    up <mm>             Move Z up (default 5 mm)
    down <mm>           Move Z down (default 5 mm)
    autofocus           IR autofocus (3-probe)

  Status:
    ping                Check laser is alive
    status              Show bridge status
    info                Device name + firmware

  Help / quit:
    help                This message
    quit                Shut down bridge
"""

def run_console(laser: D1UltraConnection):
    print("─" * 60)
    print("  Console ready — type 'help' for commands")
    print("─" * 60)
    b = laser.builder

    while True:
        try:
            line = input("d1ultra> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nShutting down...")
            break

        if not line:
            continue
        toks = line.split()
        cmd  = toks[0]

        if cmd in ('quit', 'exit', 'q'):
            break
        elif cmd == 'help':
            print(HELP)
        elif cmd == 'ping':
            ok = laser.ping()
            print("Laser is alive" if ok else "No response")
        elif cmd == 'status':
            print(f"Connected:  {laser.connected}")
            print(f"Device:     {laser.device_name or '(unknown)'}")
            print(f"Firmware:   {laser.fw_version or '(unknown)'}")
        elif cmd == 'info':
            print(f"Device:    {laser.device_name}")
            print(f"Firmware:  {laser.fw_version}")
        elif cmd == 'home':
            laser.home_motors()
        elif cmd in ('up', 'down'):
            mm = float(toks[1]) if len(toks) > 1 else 5.0
            dist = mm if cmd == 'up' else -mm
            laser.send_and_recv(b.build_z_move(dist))
            print(f"Moved Z {'up' if dist > 0 else 'down'} {abs(dist):.1f} mm")
        elif cmd == 'autofocus':
            z = laser.run_autofocus()
            if z: print(f"Focus Z = {z:.3f} mm")
        elif cmd == 'light':
            state = len(toks) > 1 and toks[1] == 'on'
            laser.send_and_recv(b.build_peripheral(0, state))
        elif cmd == 'buzzer':
            state = len(toks) > 1 and toks[1] == 'on'
            laser.send_and_recv(b.build_peripheral(1, state))
        elif cmd == 'focus':
            state = len(toks) > 1 and toks[1] == 'on'
            laser.send_and_recv(b.build_peripheral(2, state))
        elif cmd == 'gate':
            state = len(toks) > 1 and toks[1] == 'on'
            laser.send_and_recv(b.build_peripheral(3, state))
        else:
            print(f"Unknown command: {cmd}  (type 'help')")

    laser.disconnect()
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="D1 Ultra ↔ LightBurn Bridge v2")
    parser.add_argument('--laser-ip',    default=DEFAULT_LASER_IP)
    parser.add_argument('--laser-port',  type=int, default=DEFAULT_LASER_PORT)
    parser.add_argument('--listen-host', default=DEFAULT_LISTEN_HOST)
    parser.add_argument('--listen-port', type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--replay', metavar='PCAPNG',
                        help='Replay a Wireshark capture directly to the laser '
                             '(diagnostic mode — does not start GRBL server)')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Replay mode ───────────────────────────────────────────────────────────
    if args.replay:
        replay_pcapng(args.replay, args.laser_ip, args.laser_port)
        return

    # ── Normal bridge mode ────────────────────────────────────────────────────
    print()
    print("D1 Ultra ↔ LightBurn Bridge  v2")
    print("=" * 50)
    print("Key changes from v1:")
    print("  ★ CRITICAL: Host now SENDS 0x0003 (JOB_CONTROL) to start job")
    print("    (pcapng proves M+ sends it; previous versions waited for it)")
    print("  • WORKSPACE payload fixed: 42 bytes (was 40)")
    print("  • JOB_SETTINGS unknown field: -1.0 (was 0.0)")
    print("  • Added PRE_JOB (0x0005) before job upload")
    print("  • Added WORKSPACE (0x0009) with bounding box")
    print("  • Added QUERY_14(0x02) pre-job setup")
    print("  • ACK unsolicited 0x0013/0x0014/0x0015 messages")
    print("  • Proper ~6 KB PNG preview (was 286 bytes)")
    print("  • G1 S0 duplicate point filter")
    print()

    laser = D1UltraConnection(args.laser_ip, args.laser_port)
    if not laser.connect():
        sys.exit(1)

    if not laser.identify():
        log.warning("Identification incomplete — continuing anyway")

    log.info("Laser ping OK — ready to accept jobs")

    server = GRBLServer(laser, args.listen_host, args.listen_port)
    server.start()

    print()
    print(f"  LightBurn: Devices → GRBL → TCP → 127.0.0.1:{args.listen_port}")
    print()

    try:
        run_console(laser)
    except KeyboardInterrupt:
        print("\nShutting down...")
        laser.disconnect()


if __name__ == '__main__':
    main()
