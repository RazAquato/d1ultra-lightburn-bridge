#!/usr/bin/env python3
"""
Hansmaker D1 Ultra <-> LightBurn GRBL Bridge
=============================================

This bridge sits between LightBurn (speaking GRBL over TCP) and the
D1 Ultra laser (speaking its proprietary binary protocol on TCP port 6000).

LightBurn connects to this bridge as a "GRBL" device over TCP.
The bridge translates GRBL commands into D1 Ultra binary packets.

Usage:
    python d1ultra_bridge.py [--laser-ip 192.168.12.1] [--listen-port 23]

Then in LightBurn:
    Devices -> Add Manually -> GRBL
    Connection: TCP/IP, Address: localhost, Port: 23

Protocol reverse-engineered from Wireshark captures, April 2026.
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_LASER_IP = "192.168.12.1"
DEFAULT_LASER_PORT = 6000
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 23

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# D1 Ultra Protocol Constants
# ---------------------------------------------------------------------------

MAGIC = b'\x0a\x0a'
TERMINATOR = b'\x0d\x0d'


def make_preview_png(width: int = 100, height: int = 100) -> bytes:
    """Generate a minimal valid PNG image (white, no dependencies).

    M+ always includes a PNG preview in JOB_DATA.  The laser may require
    it to register the job.  This produces a valid PNG without PIL/Pillow.
    """
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)

    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    # Image data: rows of (filter_byte=0 + white pixels)
    raw = b''
    for _ in range(height):
        raw += b'\x00' + b'\xff' * (width * 3)
    idat = zlib.compress(raw)
    return sig + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat) + _chunk(b'IEND', b'')


class Cmd(IntEnum):
    """D1 Ultra command IDs."""
    STATUS      = 0x0000
    PATH_DATA   = 0x0001
    JOB_UPLOAD  = 0x0002
    JOB_START   = 0x0003
    JOB_FINISH  = 0x0004
    PRE_JOB     = 0x0005
    DEVICE_ID   = 0x0006
    WORKSPACE   = 0x0009
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

class Peripheral(IntEnum):
    """Peripheral module IDs for cmd 0x000E."""
    FILL_LIGHT  = 0x00
    BUZZER      = 0x01
    FOCUS_LASER = 0x02
    SAFETY_GATE = 0x03

class LaserSource(IntEnum):
    """Laser source byte in job settings."""
    IR    = 0
    DIODE = 1

# ---------------------------------------------------------------------------
# CRC-16/MODBUS
# ---------------------------------------------------------------------------

def crc16_modbus(data: bytes) -> int:
    """Calculate CRC-16/MODBUS checksum."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc

# ---------------------------------------------------------------------------
# D1 Ultra Packet Builder
# ---------------------------------------------------------------------------

class PacketBuilder:
    """Constructs valid D1 Ultra binary packets."""

    def __init__(self):
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def build(self, cmd: int, payload: bytes = b'', msg_type: int = 1) -> bytes:
        """Build a complete packet with header, payload, CRC, and terminator."""
        seq = self.next_seq()
        total_len = 14 + len(payload) + 4  # header(14) + payload + CRC(2) + term(2)
        header = struct.pack('<HH HH HH H',
            0x0A0A,     # magic
            total_len,  # total length
            0,          # padding
            seq,        # sequence number
            0,          # padding
            msg_type,   # message type
            cmd,        # command ID
        )
        # CRC over everything after the 2-byte magic
        crc_data = header[2:] + payload
        crc = crc16_modbus(crc_data)
        return header + payload + struct.pack('<H', crc) + TERMINATOR

    def build_status(self) -> bytes:
        """Build a status/heartbeat query (empty cmd 0x0000)."""
        return self.build(Cmd.STATUS)

    def build_motor_reset(self) -> bytes:
        """Build motor reset/calibration query (cmd 0x000B, empty payload).

        Response contains 283 bytes: u32 header + 34 IEEE 754 doubles
        with motor calibration/workspace boundary data.
        """
        return self.build(Cmd.MOTOR_RESET)

    def build_job_settings(self, passes: int, speed_mm_min: float,
                           frequency_khz: float, power_frac: float,
                           laser_source: int = LaserSource.DIODE,
                           unknown: float = 0.0) -> bytes:
        """Build the 55-byte job settings packet (cmd 0x0000 with 37-byte payload).

        IMPORTANT: msg_type=0 per M+ capture (not the default msg_type=1).
        """
        payload = struct.pack('<I', passes)
        payload += struct.pack('<d', speed_mm_min)
        payload += struct.pack('<d', frequency_khz)
        payload += struct.pack('<d', power_frac)
        payload += struct.pack('<B', laser_source)
        payload += struct.pack('<d', unknown)
        return self.build(Cmd.STATUS, payload, msg_type=0)

    def build_path_data(self, segments: List[Tuple[float, float]]) -> bytes:
        """Build a path data packet with coordinate segments.

        Each segment is (x_mm, y_mm) relative to workspace center.
        IMPORTANT: msg_type=0 per M+ capture (not the default msg_type=1).
        """
        count = len(segments)
        payload = struct.pack('<I', count)
        for x, y in segments:
            payload += struct.pack('<d', x)   # X coordinate
            payload += struct.pack('<d', y)   # Y coordinate
            payload += b'\x00' * 16           # reserved
        return self.build(Cmd.PATH_DATA, payload, msg_type=0)

    def build_job_upload(self, job_name: str, png_data: bytes = b'') -> bytes:
        """Build the job upload header (cmd 0x0002).

        Payload format (from M+ capture):
          bytes 0-255:   Job name, null-terminated, zero-padded (256 bytes)
          bytes 256-257: Padding (0x0000)
          bytes 258-261: PNG size as u32 LE
          bytes 262+:    PNG image data (preview thumbnail)
        """
        # Job name: 256 bytes, null-terminated, zero-padded
        name_bytes = job_name.encode('utf-8')[:255]
        name_field = name_bytes + b'\x00' * (256 - len(name_bytes))
        # PNG size is u32 LE (NOT u16!) per M+ capture
        padding = b'\x00\x00'
        png_size = struct.pack('<I', len(png_data))
        payload = name_field + padding + png_size + png_data
        return self.build(Cmd.JOB_UPLOAD, payload)

    def build_job_start(self) -> bytes:
        """Build the job start command (cmd 0x0003)."""
        return self.build(Cmd.JOB_START)

    def build_job_finish(self, job_name: str) -> bytes:
        """Build the job finalize command (cmd 0x0004)."""
        name_bytes = job_name.encode('utf-8')[:255]
        name_field = name_bytes + b'\x00' * (256 - len(name_bytes))
        return self.build(Cmd.JOB_FINISH, name_field)

    def build_pre_job(self) -> bytes:
        """Build the pre-job init command (cmd 0x0005)."""
        return self.build(Cmd.PRE_JOB)

    def build_device_id(self) -> bytes:
        """Build the device identification query (cmd 0x0006)."""
        return self.build(Cmd.DEVICE_ID)

    def build_workspace(self, speed: float, x_min: float, y_min: float,
                        x_max: float, y_max: float) -> bytes:
        """Build the workspace/preview config (cmd 0x0009)."""
        payload = struct.pack('<ddddd', speed, x_min, y_min, x_max, y_max)
        return self.build(Cmd.WORKSPACE, payload)

    def build_peripheral(self, module: int, state: bool) -> bytes:
        """Build a peripheral control command (cmd 0x000E)."""
        payload = struct.pack('<BB', module, 1 if state else 0)
        return self.build(Cmd.PERIPHERAL, payload)

    def build_z_move(self, distance_mm: float) -> bytes:
        """Build a Z-axis move command (cmd 0x000F, mode=0)."""
        payload = struct.pack('<B', 0)          # mode 0: manual move
        payload += struct.pack('<d', distance_mm)  # distance (+ = up, - = down)
        payload += struct.pack('<I', 4)          # parameter
        payload += b'\x00' * 4                   # padding
        return self.build(Cmd.Z_AXIS, payload)

    def build_motor_home(self) -> bytes:
        """Build a motor reset/homing command (cmd 0x000F, mode=2).

        This is the actual motor reset — sends the motors to home position.
        The laser takes several seconds to complete; poll STATUS until done.
        """
        payload = struct.pack('<B', 2)          # mode 2: motor reset/home
        payload += struct.pack('<d', 0.0)       # distance (0.0 for reset)
        payload += struct.pack('<I', 4)         # parameter
        payload += b'\x00' * 4                  # padding
        return self.build(Cmd.Z_AXIS, payload)

    def build_query_14(self, sub: int = 0x02) -> bytes:
        """Build a device query/setup (cmd 0x0014)."""
        return self.build(Cmd.QUERY_14, struct.pack('<B', sub))

    def build_query_13(self) -> bytes:
        """Build a device query (cmd 0x0013)."""
        return self.build(Cmd.QUERY_13)

    def build_query_15(self) -> bytes:
        """Build a device query (cmd 0x0015)."""
        return self.build(Cmd.QUERY_15)

    def build_device_info(self, device_id: int = 0x8B1B, ir_select: int = 0) -> bytes:
        """Build a device info query (cmd 0x0018).

        ir_select: 0=diode laser, 1=IR laser (sets byte 4 of payload).
        """
        payload = struct.pack('<HH', 0x0006, device_id)
        payload += struct.pack('<B', ir_select)
        payload += b'\x00' * (32 - 5)  # pad to 32 bytes
        return self.build(Cmd.DEVICE_INFO, payload)

    def build_z_autofocus(self, z_mm: float) -> bytes:
        """Build a Z-axis autofocus positioning command (cmd 0x000F, mode=1).

        Used during autofocus to move the Z-axis to the measured height.
        """
        payload = struct.pack('<B', 1)          # mode 1: autofocus position
        payload += struct.pack('<d', z_mm)
        payload += struct.pack('<I', 4)
        payload += b'\x00' * 4
        return self.build(Cmd.Z_AXIS, payload)

    def build_autofocus_probe(self) -> bytes:
        """Build an autofocus measurement request (cmd 0x0012).

        20-byte payload: byte[0]=1, rest zeros. Response is 30 bytes with
        3 doubles: measurement1, measurement2, z_height (at offset 22).
        """
        payload = struct.pack('<B', 1) + b'\x00' * 19
        return self.build(Cmd.AUTOFOCUS, payload)

    def build_fw_version(self) -> bytes:
        """Build a firmware version query (cmd 0x001E)."""
        return self.build(Cmd.FW_VERSION)


# ---------------------------------------------------------------------------
# D1 Ultra Protocol Response Parser
# ---------------------------------------------------------------------------

class ResponseParser:
    """Parses responses from the D1 Ultra."""

    @staticmethod
    def parse_packet(data: bytes) -> Optional[dict]:
        """Parse a single packet. Returns dict with cmd, seq, payload, or None."""
        if len(data) < 18:
            return None
        if data[0:2] != MAGIC:
            return None
        pkt_len = struct.unpack('<H', data[2:4])[0]
        if pkt_len > len(data):
            return None
        if data[pkt_len-2:pkt_len] != TERMINATOR:
            return None
        seq = struct.unpack('<H', data[6:8])[0]
        msg_type = struct.unpack('<H', data[10:12])[0]
        cmd = struct.unpack('<H', data[12:14])[0]
        payload = data[14:pkt_len-4]
        # Verify CRC
        crc_expected = struct.unpack('<H', data[pkt_len-4:pkt_len-2])[0]
        crc_computed = crc16_modbus(data[2:pkt_len-4])
        if crc_computed != crc_expected:
            log.warning(f"CRC mismatch: expected 0x{crc_expected:04x}, got 0x{crc_computed:04x}")
        return {
            'cmd': cmd,
            'seq': seq,
            'msg_type': msg_type,
            'payload': payload,
            'length': pkt_len,
        }

    @staticmethod
    def is_ack(parsed: dict) -> bool:
        """Check if a parsed response is a simple ACK."""
        return len(parsed['payload']) <= 2

    @staticmethod
    def parse_device_name(parsed: dict) -> str:
        """Parse device name from cmd 0x0006 response."""
        if parsed['cmd'] != Cmd.DEVICE_ID:
            return ""
        payload = parsed['payload']
        if len(payload) < 4:
            return ""
        name = payload[2:].split(b'\x00')[0].decode('ascii', errors='replace')
        return name

    @staticmethod
    def parse_fw_version(parsed: dict) -> str:
        """Parse firmware version from cmd 0x001E response."""
        if parsed['cmd'] != Cmd.FW_VERSION:
            return ""
        payload = parsed['payload']
        if len(payload) < 6:
            return ""
        strlen = struct.unpack('<I', payload[2:6])[0]
        return payload[6:6+strlen].decode('ascii', errors='replace')


# ---------------------------------------------------------------------------
# D1 Ultra Connection
# ---------------------------------------------------------------------------

class D1UltraConnection:
    """Manages the TCP connection to the D1 Ultra laser.

    Uses a background reader thread to receive all packets from the laser,
    routing them to per-sequence-number queues. This prevents responses from
    getting mixed up when the console and LightBurn both send commands.
    """

    def __init__(self, ip: str = DEFAULT_LASER_IP, port: int = DEFAULT_LASER_PORT):
        self.ip = ip
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.builder = PacketBuilder()
        self.parser = ResponseParser()
        self.send_lock = threading.Lock()       # Serializes sends
        self.connected = False
        self.device_name = ""
        self.fw_version = ""
        self._recv_buf = b''
        self._recv_lock = threading.Lock()
        self._pending: dict = {}                # seq -> threading.Event, result
        self._job_ready = threading.Event()     # Set when laser sends JOB_CONTROL (0x0003)
        self._heartbeat_paused = False          # Pause heartbeat during job execution
        self._reader_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

    def connect(self) -> bool:
        """Connect to the laser and start the background reader."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.ip, self.port))
            self.sock.settimeout(None)           # Reader thread blocks forever
            self.connected = True
            log.info(f"Connected to D1 Ultra at {self.ip}:{self.port}")

            # Start background reader
            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()

            # Start heartbeat to keep connection alive
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()

            return True
        except Exception as e:
            log.error(f"Failed to connect to {self.ip}:{self.port}: {e}")
            self.connected = False
            return False

    def disconnect(self):
        """Disconnect from the laser."""
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        log.info("Disconnected from laser")

    def _reader_loop(self):
        """Background thread: reads all packets from laser, dispatches by seq."""
        while self.connected:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    log.warning("Laser connection closed by remote")
                    self.connected = False
                    break

                with self._recv_lock:
                    self._recv_buf += chunk
                    self._process_recv_buf()

            except OSError:
                if self.connected:
                    log.warning("Laser socket error — connection lost")
                    self.connected = False
                break
            except Exception as e:
                if self.connected:
                    log.error(f"Reader error: {e}")
                break

        # Wake up anyone waiting for responses
        for seq, (evt, _) in list(self._pending.items()):
            evt.set()

    def _process_recv_buf(self):
        """Extract complete packets from the receive buffer and dispatch."""
        while len(self._recv_buf) >= 18:
            # Find magic header
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
                # Bad length — skip this magic and look for next
                self._recv_buf = self._recv_buf[2:]
                continue

            if len(self._recv_buf) < pkt_len:
                return  # Need more data

            pkt_data = self._recv_buf[:pkt_len]
            self._recv_buf = self._recv_buf[pkt_len:]

            parsed = self.parser.parse_packet(pkt_data)
            if not parsed:
                continue

            seq = parsed['seq']
            msg_type = parsed['msg_type']

            # Unsolicited notifications (msg_type=2, seq=0) — just log
            if msg_type == 2:
                log.debug(f"Laser notification: cmd=0x{parsed['cmd']:04x}")
                continue

            # Check for JOB_CONTROL (0x0003) from laser — signals paths received
            if parsed['cmd'] == Cmd.JOB_START:
                log.info("Laser sent JOB_CONTROL (0x0003) — paths received, ready to finalize")
                self._job_ready.set()

            # Route to waiting caller by sequence number
            if seq in self._pending:
                evt, _ = self._pending[seq]
                self._pending[seq] = (evt, parsed)
                evt.set()
            else:
                log.info(f"  LASER unsolicited: seq={seq} cmd=0x{parsed['cmd']:04x} "
                         f"msg_type={msg_type} payload={len(parsed.get('payload', b''))}b")

    def _heartbeat_loop(self):
        """Send periodic STATUS queries to keep the laser connection alive.

        The D1 Ultra closes the TCP connection if it doesn't receive any
        commands for roughly 10 seconds. M+ software sends STATUS polls
        every ~2 seconds. We do the same, but pause during job execution
        to avoid interfering with the job protocol.
        """
        while self.connected:
            try:
                time.sleep(2.0)
                if self.connected and not self._heartbeat_paused:
                    self.send_and_recv(self.builder.build_status(), timeout=2.0)
            except Exception:
                pass  # Don't crash the heartbeat on transient errors

    def send_and_recv(self, packet: bytes, timeout: float = 3.0) -> Optional[dict]:
        """Send a packet and wait for matching response by sequence number."""
        if not self.connected or not self.sock:
            return None

        # Extract the sequence number from the outgoing packet
        if len(packet) < 8:
            return None
        seq = struct.unpack('<H', packet[6:8])[0]

        # Register a wait event for this sequence
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

        # Wait for the reader thread to deliver the matching response
        if evt.wait(timeout=timeout):
            _, result = self._pending.pop(seq, (None, None))
            return result
        else:
            self._pending.pop(seq, None)
            log.debug(f"Timeout waiting for response seq={seq}")
            return None

    def send_only(self, packet: bytes):
        """Send a packet without waiting for response."""
        if not self.connected or not self.sock:
            return
        try:
            with self.send_lock:
                self.sock.sendall(packet)
        except Exception as e:
            log.error(f"Send error: {e}")
            self.connected = False

    def ping(self) -> bool:
        """Send a status query to check if the laser is alive."""
        if not self.connected or not self.sock:
            return False
        resp = self.send_and_recv(self.builder.build_status(), timeout=2.0)
        return resp is not None

    def reconnect(self, max_retries: int = 3, delay: float = 2.0) -> bool:
        """Attempt to reconnect to the laser."""
        self.disconnect()
        for attempt in range(1, max_retries + 1):
            log.info(f"Reconnect attempt {attempt}/{max_retries}...")
            if self.connect():
                if self.identify():
                    log.info("Reconnected and identified successfully")
                    return True
                else:
                    log.warning("Connected but identification failed")
                    return True  # Still connected, just no ID response
            if attempt < max_retries:
                time.sleep(delay)
        log.error("Failed to reconnect after all retries")
        return False

    def ensure_connected(self) -> bool:
        """Make sure we have a live connection; reconnect if needed."""
        if self.connected and self.ping():
            return True
        log.warning("Laser connection lost — attempting reconnect...")
        return self.reconnect()

    def identify(self) -> bool:
        """Run the device identification handshake (mirrors M+ startup)."""
        # Query device name (cmd 0x0006)
        resp = self.send_and_recv(self.builder.build_device_id())
        if resp:
            self.device_name = self.parser.parse_device_name(resp)
            log.info(f"Device: {self.device_name}")

        # Status ping (cmd 0x0000)
        self.send_and_recv(self.builder.build_status())

        # Device info (cmd 0x0018)
        self.send_and_recv(self.builder.build(Cmd.DEVICE_INFO))

        # Query firmware version (cmd 0x001E)
        resp = self.send_and_recv(self.builder.build_fw_version())
        if resp:
            self.fw_version = self.parser.parse_fw_version(resp)
            log.info(f"Firmware: {self.fw_version}")

        # Motor calibration query (cmd 0x000B) — reads workspace boundaries
        resp = self.send_and_recv(self.builder.build_motor_reset(), timeout=5.0)
        if resp:
            log.info("Motor calibration data received (283 bytes)")
        else:
            log.warning("Motor calibration query got no response")

        # Query device state (cmd 0x0013, 0x0015)
        self.send_and_recv(self.builder.build(Cmd.QUERY_13))
        self.send_and_recv(self.builder.build(Cmd.QUERY_15))

        return bool(self.device_name)

    def home_motors(self, retract_mm: float = 5.0,
                    timeout: float = 60.0) -> bool:
        """Send motor reset/homing command, then retract off the endstop.

        Sends cmd 0x000F mode=2 to drive the motor to the top endstop.
        Once the ACK arrives, moves down by retract_mm to protect the
        motor from sitting against the endstop.
        """
        log.info("Sending motor home command...")
        pkt = self.builder.build_motor_home()

        # ACK arrives when the motor reaches the top endstop.
        resp = self.send_and_recv(pkt, timeout=timeout)
        if not resp:
            log.warning("Motor homing: no response (timed out)")

        # Retract off the endstop
        log.info(f"Endstop reached — retracting {retract_mm:.1f}mm...")
        retract_pkt = self.builder.build_z_move(-retract_mm)
        retract_timeout = max(5.0, retract_mm * 3.0)
        r = self.send_and_recv(retract_pkt, timeout=retract_timeout)
        if r:
            log.info("Motor homing + retraction complete")
        else:
            log.warning("Retraction: no ACK (may still be moving)")
        return True

    def run_autofocus(self, hw_id: int = 0x1A8B) -> Optional[float]:
        """Run the full autofocus sequence (3 probes, matches M+ behavior).

        Returns the final averaged Z-height in mm, or None on failure.

        Sequence (from Wireshark capture of M+):
        1. STATUS ping
        2. QUERY_15, QUERY_14(0x02)
        3. DEVICE_INFO with ir_select=1 (activate IR laser for probing)
        4. For each of 3 probes:
           a. Send AUTOFOCUS probe request (cmd 0x0012)
           b. Wait for response (30 bytes: status + 3 doubles)
           c. Extract Z-height from third double (offset 22)
           d. Send Z_AXIS mode=1 to move to measured height
           e. Poll STATUS while Z-axis moves
        5. DEVICE_INFO with ir_select=0 (back to diode)
        """
        log.info("Autofocus: starting...")

        # Pre-probe setup
        self.send_and_recv(self.builder.build_status())
        self.send_and_recv(self.builder.build_query_15())
        self.send_and_recv(self.builder.build_query_14(0x02))
        self.send_and_recv(
            self.builder.build_device_info(device_id=hw_id, ir_select=1))

        measurements = []
        for i in range(3):
            log.info(f"  Autofocus probe {i+1}/3...")

            # Send autofocus probe
            pkt = self.builder.build_autofocus_probe()
            resp = self.send_and_recv(pkt, timeout=10.0)

            if not resp or not resp.get('payload'):
                log.warning(f"  Probe {i+1} failed — no response")
                continue

            payload = resp['payload']
            if len(payload) < 30:
                log.warning(f"  Probe {i+1} short response ({len(payload)} bytes)")
                continue

            # Parse response: 6 bytes header + 3 doubles
            meas1 = struct.unpack_from('<d', payload, 6)[0]
            meas2 = struct.unpack_from('<d', payload, 14)[0]
            z_val = struct.unpack_from('<d', payload, 22)[0]
            log.info(f"    Z = {z_val:.3f}mm (raw: {meas1:.6f}, {meas2:.6f})")
            measurements.append(z_val)

            # Move Z-axis to measured position (mode=1 autofocus move)
            # ACK only arrives once the motor reaches position, so allow
            # plenty of time (60 s) for long travels.
            z_pkt = self.builder.build_z_autofocus(z_val)
            log.info(f"    Moving Z to {z_val:.3f}mm (waiting for motor)...")
            z_resp = self.send_and_recv(z_pkt, timeout=60.0)
            if not z_resp:
                log.warning(f"    Z move timed out — motor may still be moving")

            # Let the motor fully settle, then STATUS-poll before next probe
            if i < 2:
                # M+ polls STATUS several times between probes
                for _ in range(5):
                    time.sleep(0.4)
                    self.send_and_recv(self.builder.build_status(), timeout=2.0)

        # Restore to diode laser
        self.send_and_recv(
            self.builder.build_device_info(device_id=hw_id, ir_select=0))

        if measurements:
            avg_z = sum(measurements) / len(measurements)
            log.info(f"  Autofocus complete: Z = {avg_z:.3f}mm "
                     f"(avg of {len(measurements)} probes)")
            return avg_z
        else:
            log.warning("  Autofocus FAILED — no measurements")
            return None

    def send_path_segments(self, segments: List[Tuple[float, float]],
                           batch_size: int = 500) -> bool:
        """Send path data in batches, waiting for ACK between each."""
        for i in range(0, len(segments), batch_size):
            batch = segments[i:i+batch_size]
            pkt = self.builder.build_path_data(batch)
            resp = self.send_and_recv(pkt, timeout=10.0)
            if not resp:
                log.error(f"No ACK for path batch {i//batch_size}")
                return False
            sent = i + len(batch)
            total = len(segments)
            if sent % 1000 < batch_size:
                log.info(f"  Path data: {sent}/{total} segments sent")
        return True


# ---------------------------------------------------------------------------
# GRBL State Machine
# ---------------------------------------------------------------------------

class GRBLState:
    """Tracks the virtual GRBL machine state."""

    def __init__(self):
        # Position (in mm, absolute)
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        # Feed rate
        self.feed_rate = 1000.0  # mm/min
        # Laser
        self.laser_on = False
        self.power = 0.0        # 0-1000 (S value)
        self.max_power = 1000.0 # S-max
        # State
        self.absolute_mode = True  # G90
        self.is_running = False
        self.is_homed = False
        # Job accumulator — path groups split at G0 boundaries.
        # Each group is a list of (x, y) points: the G0 start + G1 cuts.
        self.job_path_groups: List[List[Tuple[float, float]]] = []
        self.job_name = "lightburn_job"
        # Bounding box tracker
        self.bb_x_min = float('inf')
        self.bb_x_max = float('-inf')
        self.bb_y_min = float('inf')
        self.bb_y_max = float('-inf')
        # Settings
        self.laser_source = LaserSource.DIODE
        self.frequency_khz = 50.0
        self.passes = 1
        self.speed_mm_min = 1000.0
        # Job power: captures the max S-value seen while laser is on,
        # because the G-code ends with S0/M5 which resets power to 0.
        self.job_power = 0.0

    def update_bounding_box(self, x: float, y: float):
        self.bb_x_min = min(self.bb_x_min, x)
        self.bb_x_max = max(self.bb_x_max, x)
        self.bb_y_min = min(self.bb_y_min, y)
        self.bb_y_max = max(self.bb_y_max, y)

    def start_new_path_group(self, start_x: float, start_y: float):
        """Begin a new path group (called on G0 rapid moves).

        NOTE: We do NOT update the bounding box here — the BB is computed
        from filtered groups (>= 2 points) in _finish_job.  Otherwise the
        final G0 X0Y0 return-to-home would skew the center point.
        """
        self.job_path_groups.append([(start_x, start_y)])

    def add_cut_point(self, x: float, y: float):
        """Add a cut point to the current path group (called on G1 laser-on)."""
        if not self.job_path_groups:
            # No G0 yet — create an implicit group starting at current pos
            self.job_path_groups.append([(x, y)])
        else:
            self.job_path_groups[-1].append((x, y))

    def reset_job(self):
        self.job_path_groups = []
        self.job_power = 0.0
        self.bb_x_min = float('inf')
        self.bb_x_max = float('-inf')
        self.bb_y_min = float('inf')
        self.bb_y_max = float('-inf')

    @property
    def power_fraction(self) -> float:
        """Power as a 0.0-1.0 fraction."""
        if self.max_power == 0:
            return 0.0
        return min(1.0, self.power / self.max_power)


# ---------------------------------------------------------------------------
# GRBL Command Parser & Translator
# ---------------------------------------------------------------------------

class GRBLTranslator:
    """Translates GRBL G-code commands to D1 Ultra protocol calls."""

    # Standard GRBL settings responses
    GRBL_SETTINGS = {
        0: 10,       # Step pulse time (usec)
        1: 25,       # Step idle delay (msec)
        2: 0,        # Step pulse invert
        3: 0,        # Step direction invert
        4: 0,        # Invert step enable
        5: 0,        # Invert limit pins
        6: 0,        # Invert probe pin
        10: 1,       # Status report options
        11: 0.010,   # Junction deviation (mm)
        12: 0.002,   # Arc tolerance (mm)
        13: 0,       # Report in inches
        20: 0,       # Soft limits enable
        21: 0,       # Hard limits enable
        22: 0,       # Homing cycle enable
        23: 0,       # Homing direction invert
        24: 25.0,    # Homing feed rate (mm/min)
        25: 500.0,   # Homing seek rate (mm/min)
        26: 250,     # Homing debounce (msec)
        27: 1.0,     # Homing pull-off (mm)
        30: 1000,    # Max spindle speed (RPM) — maps to S value
        31: 0,       # Min spindle speed
        32: 1,       # Laser mode enabled
        100: 80.0,   # X steps/mm
        101: 80.0,   # Y steps/mm
        102: 80.0,   # Z steps/mm
        110: 5000.0, # X max rate (mm/min)
        111: 5000.0, # Y max rate (mm/min)
        112: 500.0,  # Z max rate (mm/min)
        120: 200.0,  # X acceleration (mm/s^2)
        121: 200.0,  # Y acceleration (mm/s^2)
        122: 50.0,   # Z acceleration (mm/s^2)
        130: 400.0,  # X max travel (mm)
        131: 400.0,  # Y max travel (mm)
        132: 100.0,  # Z max travel (mm)
    }

    def __init__(self, laser: D1UltraConnection, state: GRBLState):
        self.laser = laser
        self.state = state

    def handle_line(self, line: str) -> str:
        """Process a single GRBL command line and return the response."""
        line = line.strip()
        if not line:
            return "ok"

        # Remove comments
        if ';' in line:
            line = line[:line.index(';')].strip()
        if '(' in line:
            line = line[:line.index('(')].strip()
        if not line:
            return "ok"

        upper = line.upper()

        # --- Special commands ---
        if upper == '?' :
            return self._status_report()
        if upper == '$$':
            return self._settings_report()
        if upper == '$H':
            return self._home()
        if upper == '$X' or upper == '$X\n':
            return self._unlock()
        if upper.startswith('$J='):
            return self._jog(line[3:])
        if upper == '\x18':  # Ctrl-X soft reset
            return self._reset()
        if upper == '!' :  # Feed hold
            return "ok"
        if upper == '~' :  # Cycle resume
            return "ok"
        if upper == '$FOCUS' or upper == '$FOCUS ON':
            return self._focus_on()
        if upper == '$FOCUS OFF':
            return self._focus_off()
        if upper == '$AUTOFOCUS' or upper == '$AF':
            return self._autofocus()
        if upper == '$I':
            return self._build_info()
        if upper == '$#':
            return self._gcode_parameters()
        if upper == '$G':
            return self._gcode_parser_state()
        if upper.startswith('$'):
            # Other $ commands — just acknowledge
            return "ok"

        # --- G-code parsing ---
        return self._parse_gcode(line)

    def _status_report(self) -> str:
        """Generate a GRBL-style status report."""
        if self.state.is_running:
            status = "Run"
        elif self.state.is_homed:
            status = "Idle"
        else:
            status = "Idle"
        x, y, z = self.state.x, self.state.y, self.state.z
        return f"<{status}|MPos:{x:.3f},{y:.3f},{z:.3f}|FS:{self.state.feed_rate:.0f},{self.state.power:.0f}>"

    def _settings_report(self) -> str:
        """Generate the $$ settings dump."""
        lines = []
        for key in sorted(self.GRBL_SETTINGS.keys()):
            val = self.GRBL_SETTINGS[key]
            if isinstance(val, float):
                lines.append(f"${key}={val:.3f}")
            else:
                lines.append(f"${key}={val}")
        lines.append("ok")
        return "\n".join(lines)

    def _build_info(self) -> str:
        """Handle $I — return GRBL build info."""
        fw = self.laser.fw_version or "1.0.0"
        return f"[VER:1.1h.20190825: D1Ultra Bridge ({fw})]\n[OPT:V,15,128]\nok"

    def _gcode_parameters(self) -> str:
        """Handle $# — return GCode coordinate system offsets."""
        lines = []
        # Work coordinate offsets (all zero — we use machine coordinates)
        for cs in ['G54', 'G55', 'G56', 'G57', 'G58', 'G59']:
            lines.append(f"[{cs}:0.000,0.000,0.000]")
        lines.append("[G28:0.000,0.000,0.000]")
        lines.append("[G30:0.000,0.000,0.000]")
        lines.append("[G92:0.000,0.000,0.000]")
        lines.append("[TLO:0.000]")
        lines.append("[PRB:0.000,0.000,0.000:0]")
        lines.append("ok")
        return "\n".join(lines)

    def _gcode_parser_state(self) -> str:
        """Handle $G — return current GCode parser state."""
        mode = "G90" if self.state.absolute_mode else "G91"
        return f"[GC:G0 {mode} G54 M0 M5 M9 T0 F{self.state.feed_rate:.0f} S0]\nok"

    def _home(self) -> str:
        """Handle $H homing command — sends real motor home to laser."""
        log.info("Homing: sending motor home command to laser...")
        self.laser.home_motors()
        self.state.x = 0.0
        self.state.y = 0.0
        self.state.z = 0.0
        self.state.is_homed = True
        log.info("Homing complete: position reset to 0,0,0")
        return "ok"

    def _focus_on(self) -> str:
        """Turn on the focus laser pointer."""
        pkt = self.laser.builder.build_peripheral(Peripheral.FOCUS_LASER, True)
        self.laser.send_and_recv(pkt)
        log.info("Focus laser pointer ON")
        return "[MSG:Focus laser ON]\r\nok"

    def _focus_off(self) -> str:
        """Turn off the focus laser pointer."""
        pkt = self.laser.builder.build_peripheral(Peripheral.FOCUS_LASER, False)
        self.laser.send_and_recv(pkt)
        log.info("Focus laser pointer OFF")
        return "[MSG:Focus laser OFF]\r\nok"

    def _autofocus(self) -> str:
        """Run the IR autofocus sequence (3 probes, matches M+ behavior)."""
        z = self.laser.run_autofocus()
        if z is not None:
            self.state.z = z
            return f"[MSG:Autofocus done Z={z:.3f}mm]\r\nok"
        else:
            return "[MSG:Autofocus FAILED]\r\nok"

    def _unlock(self) -> str:
        """Handle $X unlock command."""
        self.state.is_homed = True
        return "ok"

    def _reset(self) -> str:
        """Handle soft reset."""
        self.state.is_running = False
        self.state.laser_on = False
        self.state.reset_job()
        return "Grbl 1.1h ['$' for help]\r\n"

    def _jog(self, params: str) -> str:
        """Handle $J= jog commands."""
        # Parse jog parameters
        parts = params.upper().split()
        x, y, z, f = None, None, None, None
        for part in parts:
            for p in self._split_gcode_words(part):
                if p.startswith('X'):
                    x = float(p[1:])
                elif p.startswith('Y'):
                    y = float(p[1:])
                elif p.startswith('Z'):
                    z = float(p[1:])
                elif p.startswith('F'):
                    f = float(p[1:])
        if x is not None:
            self.state.x = x
        if y is not None:
            self.state.y = y
        if z is not None:
            # Z-axis jog -> move the elevating platform
            # ACK arrives after motor reaches position; scale timeout with distance
            delta = z - self.state.z
            if abs(delta) > 0.01:
                z_timeout = max(5.0, abs(delta) * 3.0)
                pkt = self.laser.builder.build_z_move(delta)
                self.laser.send_and_recv(pkt, timeout=z_timeout)
                log.info(f"Z-axis move: {delta:+.2f}mm")
            self.state.z = z
        return "ok"

    def _split_gcode_words(self, line: str) -> List[str]:
        """Split a G-code line into individual words (G0, X10, Y20, etc.)."""
        words = []
        current = ""
        for ch in line:
            if ch.isalpha() and current:
                words.append(current)
                current = ch
            else:
                current += ch
        if current:
            words.append(current)
        return words

    def _parse_gcode(self, line: str) -> str:
        """Parse and execute a G-code line."""
        words = self._split_gcode_words(line.upper())

        g_cmd = None
        m_cmd = None
        x_val = None
        y_val = None
        z_val = None
        f_val = None
        s_val = None

        for word in words:
            if not word:
                continue
            letter = word[0]
            try:
                value = float(word[1:]) if len(word) > 1 else 0
            except ValueError:
                continue

            if letter == 'G':
                g_cmd = int(value)
            elif letter == 'M':
                m_cmd = int(value)
            elif letter == 'X':
                x_val = value
            elif letter == 'Y':
                y_val = value
            elif letter == 'Z':
                z_val = value
            elif letter == 'F':
                f_val = value
            elif letter == 'S':
                s_val = value

        # Update feed rate if specified
        if f_val is not None:
            self.state.feed_rate = f_val
            self.state.speed_mm_min = f_val

        # Update power if specified
        if s_val is not None:
            self.state.power = s_val
            # Track max power seen while laser is on (for job settings).
            # The G-code ends with S0 + M5, so we can't read power at
            # job-finish time — capture it during the job instead.
            if s_val > self.state.job_power and self.state.laser_on:
                self.state.job_power = s_val

        # Handle M-codes
        if m_cmd is not None:
            return self._handle_m_code(m_cmd, s_val)

        # Handle G-codes
        if g_cmd is not None:
            return self._handle_g_code(g_cmd, x_val, y_val, z_val, f_val, s_val)

        # If just coordinates with no G command, treat as G1 (linear move)
        if x_val is not None or y_val is not None or z_val is not None:
            return self._handle_g_code(1, x_val, y_val, z_val, f_val, s_val)

        return "ok"

    def _handle_g_code(self, cmd: int, x: Optional[float], y: Optional[float],
                       z: Optional[float], f: Optional[float],
                       s: Optional[float]) -> str:
        """Handle G-code commands."""

        if cmd == 0:  # G0 - Rapid move (laser off)
            self._move(x, y, z, rapid=True)
            return "ok"

        elif cmd == 1:  # G1 - Linear move (laser on if M3/M4 active)
            self._move(x, y, z, rapid=False)
            return "ok"

        elif cmd in (2, 3):  # G2/G3 - Arc (CW/CCW)
            # For now, linearize arcs
            # TODO: proper arc interpolation
            self._move(x, y, z, rapid=False)
            return "ok"

        elif cmd == 4:  # G4 - Dwell
            return "ok"

        elif cmd == 10:  # G10 - Set coordinate offset
            return "ok"

        elif cmd == 20:  # G20 - Inches mode
            log.warning("Inches mode not supported, staying in mm")
            return "ok"

        elif cmd == 21:  # G21 - Millimeters mode
            return "ok"

        elif cmd == 28:  # G28 - Go to predefined position
            self.state.x = 0.0
            self.state.y = 0.0
            return "ok"

        elif cmd == 90:  # G90 - Absolute positioning
            self.state.absolute_mode = True
            return "ok"

        elif cmd == 91:  # G91 - Relative positioning
            self.state.absolute_mode = False
            return "ok"

        elif cmd == 92:  # G92 - Set position
            if x is not None:
                self.state.x = x
            if y is not None:
                self.state.y = y
            if z is not None:
                self.state.z = z
            return "ok"

        return "ok"

    def _move(self, x: Optional[float], y: Optional[float],
              z: Optional[float], rapid: bool):
        """Execute a move, accumulating path segments for the job."""
        # Calculate target position
        if self.state.absolute_mode:
            target_x = x if x is not None else self.state.x
            target_y = y if y is not None else self.state.y
            target_z = z if z is not None else self.state.z
        else:
            target_x = self.state.x + (x or 0.0)
            target_y = self.state.y + (y or 0.0)
            target_z = self.state.z + (z or 0.0)

        # Handle Z-axis movement
        if z is not None and abs(target_z - self.state.z) > 0.01:
            delta = target_z - self.state.z
            z_timeout = max(5.0, abs(delta) * 3.0)
            pkt = self.laser.builder.build_z_move(delta)
            self.laser.send_and_recv(pkt, timeout=z_timeout)
            log.info(f"Z-axis move: {delta:+.2f}mm")

        # Accumulate path data for the job
        if rapid:
            # G0 rapid move — start a new path group with this destination
            self.state.start_new_path_group(target_x, target_y)
        elif self.state.laser_on:
            # G1 with laser on — add cut point to current path group
            self.state.add_cut_point(target_x, target_y)

        # Update position
        self.state.x = target_x
        self.state.y = target_y
        self.state.z = target_z

    def _handle_m_code(self, cmd: int, s_val: Optional[float]) -> str:
        """Handle M-code commands."""

        if cmd == 0 or cmd == 1:  # M0/M1 - Program pause
            return "ok"

        elif cmd == 2 or cmd == 30:  # M2/M30 - Program end
            self._finish_job()
            return "ok"

        elif cmd == 3 or cmd == 4:  # M3/M4 - Laser on (CW/CCW)
            self.state.laser_on = True
            if s_val is not None:
                self.state.power = s_val
                if s_val > self.state.job_power:
                    self.state.job_power = s_val
            log.info(f"Laser ON, power={self.state.power_fraction*100:.0f}%")
            return "ok"

        elif cmd == 5:  # M5 - Laser off
            self.state.laser_on = False
            log.info("Laser OFF")
            return "ok"

        elif cmd == 8:  # M8 - Air assist on (map to peripheral?)
            return "ok"

        elif cmd == 9:  # M9 - Air assist off
            return "ok"

        elif cmd == 114:  # M114 - Report position
            return f"X:{self.state.x:.3f} Y:{self.state.y:.3f} Z:{self.state.z:.3f}\nok"

        return "ok"

    def _finish_job(self):
        """Send accumulated path data to the laser and execute the job.

        Protocol sequence (derived from M+ SVG capture — same SVG used in
        both M+ and LightBurn for 1:1 comparison):
          1. DEVICE_INFO query  (0x0018, msg_type=1)
          2. JOB_DATA upload    (0x0002, msg_type=1) — name + optional PNG
          3. For each path group (split at G0 rapid moves):
             a. JOB_SETTINGS    (0x0000, msg_type=0) — passes/speed/freq/power
             b. PATH_DATA       (0x0001, msg_type=0) — coordinate points
          4. Wait for laser's JOB_CONTROL (0x0003)   — laser signals ready
          5. JOB_NAME finalize  (0x0004, msg_type=1) — 256-byte name field

        IMPORTANT: M+ sends JOB_SETTINGS before EACH PATH_DATA group, not
        just once.  Also, coordinates are centered around the design's
        bounding-box midpoint (origin = design center, not bed origin).
        """
        # Filter out path groups with < 2 points (need start + at least one cut)
        groups = [g for g in self.state.job_path_groups if len(g) >= 2]
        if not groups:
            log.info("No path data to send")
            return

        total_segs = sum(len(g) for g in groups)

        # Use job_power (max S seen during job) rather than current power,
        # because the G-code ends with S0/M5 before M2 triggers this.
        job_power_frac = min(1.0, self.state.job_power / self.state.max_power) \
            if self.state.max_power > 0 else 0.0

        # Compute bounding box from filtered groups only (not from G0 X0Y0
        # return-to-home moves that would skew the center point).
        all_pts = [pt for g in groups for pt in g]
        bb_x_min = min(x for x, y in all_pts)
        bb_x_max = max(x for x, y in all_pts)
        bb_y_min = min(y for x, y in all_pts)
        bb_y_max = max(y for x, y in all_pts)
        cx = (bb_x_min + bb_x_max) / 2.0
        cy = (bb_y_min + bb_y_max) / 2.0

        log.info(f"Sending job: {len(groups)} path groups, "
                 f"{total_segs} total points, "
                 f"power={job_power_frac*100:.0f}%, "
                 f"speed={self.state.speed_mm_min:.0f}mm/min, "
                 f"center=({cx:.1f},{cy:.1f})")

        def _job_log_pkt(label: str, pkt: bytes):
            """Log first 40 bytes of a packet in hex for debugging."""
            hx = pkt[:40].hex(' ')
            log.info(f"  JOB {label} ({len(pkt)}b): {hx}")

        try:
            # 1. Query device info
            pkt = self.laser.builder.build_device_info()
            _job_log_pkt("DEVICE_INFO →", pkt)
            resp = self.laser.send_and_recv(pkt)
            if resp:
                rp = resp.get('payload', b'')
                log.info(f"  JOB DEVICE_INFO ← cmd=0x{resp.get('cmd',0):04x} "
                         f"payload[0:16]={rp[:16].hex(' ') if rp else '(empty)'}")
            else:
                log.warning("  JOB DEVICE_INFO ← no response")

            # 2. Upload job header (256-byte name + PNG preview)
            #    M+ always sends a PNG preview (~6 KB).  The laser may
            #    require it to register the job and send JOB_CONTROL.
            png_preview = make_preview_png(100, 100)
            pkt = self.laser.builder.build_job_upload(self.state.job_name, png_preview)
            _job_log_pkt("JOB_UPLOAD →", pkt)
            resp = self.laser.send_and_recv(pkt, timeout=5.0)
            if resp:
                rp = resp.get('payload', b'')
                log.info(f"  JOB JOB_UPLOAD ← cmd=0x{resp.get('cmd',0):04x} "
                         f"payload={rp.hex(' ') if rp else '(empty)'}")
            else:
                log.warning("No ACK for job upload — continuing anyway")

            # 3. For each path group: send JOB_SETTINGS then PATH_DATA
            #    M+ capture shows this alternating pattern is required.
            self.state.is_running = True
            self.laser._job_ready.clear()

            for gi, group in enumerate(groups):
                # 3a. JOB_SETTINGS before each PATH_DATA
                pkt = self.laser.builder.build_job_settings(
                    passes=self.state.passes,
                    speed_mm_min=self.state.speed_mm_min,
                    frequency_khz=self.state.frequency_khz,
                    power_frac=job_power_frac,
                    laser_source=self.state.laser_source,
                )
                if gi == 0:
                    _job_log_pkt(f"JOB_SETTINGS[{gi}] →", pkt)
                resp = self.laser.send_and_recv(pkt)
                if not resp:
                    log.warning(f"No ACK for JOB_SETTINGS[{gi}]")

                # 3b. PATH_DATA — center coordinates around design midpoint
                centered = [(x - cx, y - cy) for x, y in group]
                pkt = self.laser.builder.build_path_data(centered)
                if gi == 0:
                    _job_log_pkt(f"PATH_DATA[{gi}] →", pkt)
                resp = self.laser.send_and_recv(pkt)
                if not resp:
                    log.warning(f"No ACK for PATH_DATA[{gi}]")

                log.info(f"  Path group {gi+1}/{len(groups)}: "
                         f"{len(group)} points sent")

            # 4. Wait for laser's JOB_CONTROL (0x0003)
            log.info("  Waiting for laser JOB_CONTROL (0x0003)...")
            if not self.laser._job_ready.wait(timeout=15.0):
                log.warning("  Timeout waiting for laser JOB_CONTROL — "
                            "continuing to finalize anyway")
            else:
                log.info("  Laser sent JOB_CONTROL — paths accepted")

            # 5. Finalize job (sends 256-byte name field)
            pkt = self.laser.builder.build_job_finish(self.state.job_name)
            _job_log_pkt("JOB_FINISH →", pkt)
            resp = self.laser.send_and_recv(pkt, timeout=10.0)
            if resp:
                rp = resp.get('payload', b'')
                log.info(f"  JOB JOB_FINISH ← cmd=0x{resp.get('cmd',0):04x} "
                         f"payload={rp.hex(' ') if rp else '(empty)'}")
            else:
                log.warning("No ACK for job finalize")

            self.state.is_running = False
            log.info("Job complete")

        except Exception as e:
            log.error(f"Job execution failed: {e}")
            self.state.is_running = False
        finally:
            self.state.reset_job()


# ---------------------------------------------------------------------------
# GRBL TCP Server (LightBurn connects here)
# ---------------------------------------------------------------------------

class GRBLServer:
    """TCP server that speaks GRBL to LightBurn."""

    def __init__(self, laser: D1UltraConnection, host: str, port: int):
        self.laser = laser
        self.host = host
        self.port = port
        self.state = GRBLState()
        self.translator = GRBLTranslator(laser, self.state)

    def start(self):
        """Start the GRBL TCP server."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(1)
        log.info(f"GRBL server listening on {self.host}:{self.port}")
        log.info(f"In LightBurn: Add device -> GRBL -> TCP/IP -> localhost:{self.port}")

        while True:
            try:
                client, addr = server.accept()
                log.info(f"LightBurn connected from {addr}")
                self._handle_client(client)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Server error: {e}")

        server.close()

    def _handle_client(self, client: socket.socket):
        """Handle a LightBurn client connection."""
        client.settimeout(None)

        # Open debug log for raw byte-level tracing
        debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "debug.txt")
        dbg = open(debug_path, 'w')
        dbg.write(f"=== LightBurn debug log — {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        dbg.flush()

        def dbg_log(msg):
            ts = time.strftime('%H:%M:%S')
            dbg.write(f"[{ts}] {msg}\n")
            dbg.flush()

        # Ensure laser is connected before accepting commands
        if not self.laser.ensure_connected():
            log.error("Laser not reachable — sending error to LightBurn")
            dbg_log("ERROR: Laser not reachable")
            client.sendall(b"ALARM:9\r\n")  # GRBL alarm: homing fail
            client.close()
            dbg.close()
            return

        log.info(f"Laser OK: {self.laser.device_name} (FW {self.laser.fw_version})")

        # Send GRBL welcome banner
        welcome = "Grbl 1.1h ['$' for help]\r\n"
        client.sendall(welcome.encode('ascii'))
        dbg_log(f"SENT banner: {repr(welcome)}")

        REALTIME_CHARS = set(b'?!~\x18')
        buffer = ""
        try:
            while True:
                data = client.recv(4096)
                if not data:
                    dbg_log("RECV: connection closed (empty read)")
                    break

                # Log raw bytes
                dbg_log(f"RECV raw {len(data)}b: {data.hex()} | ascii: {repr(data)}")

                # GRBL realtime characters (? ! ~ Ctrl-X) must be handled
                # immediately — they arrive without a newline delimiter.
                # Extract them before adding the rest to the line buffer.
                text = ""
                for byte in data:
                    if byte in REALTIME_CHARS:
                        ch = chr(byte)
                        dbg_log(f"  REALTIME char: {repr(ch)}")
                        response = self.translator.handle_line(ch)
                        if response:
                            dbg_log(f"  REALTIME resp: {repr(response)}")
                            client.sendall((response + "\r\n").encode('ascii'))
                    else:
                        text += chr(byte) if byte < 128 else '?'

                buffer += text

                # Process complete lines
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip('\r')

                    if not line:
                        continue

                    dbg_log(f"  LINE: {repr(line)}")
                    log.debug(f"GRBL <- {line}")
                    response = self.translator.handle_line(line)

                    if response:
                        dbg_log(f"  RESP: {repr(response)}")
                        log.debug(f"GRBL -> {response[:80]}")
                        client.sendall((response + "\r\n").encode('ascii'))

        except ConnectionResetError:
            log.info("LightBurn disconnected")
            dbg_log("LightBurn disconnected (reset)")
        except Exception as e:
            log.error(f"Client handler error: {e}")
            dbg_log(f"ERROR: {e}")
        finally:
            client.close()
            log.info("Client connection closed")
            dbg_log("=== Connection closed ===")
            dbg.close()
            log.info(f"Debug log written to: {debug_path}")


# ---------------------------------------------------------------------------
# Interactive Console (runs alongside GRBL server)
# ---------------------------------------------------------------------------

class InteractiveConsole:
    """Interactive command console that runs in a thread alongside the bridge.

    Provides direct laser control commands while LightBurn is connected.
    WARNING: Do not send commands while a job is running!
    """

    def __init__(self, laser: D1UltraConnection, state: 'GRBLState'):
        self.laser = laser
        self.state = state
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the console in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _print_help(self):
        print()
        print("  Available commands:")
        print("  ─────────────────────────────────────────────")
        print("  ping            Ping the laser")
        print("  home            Home/reset motors")
        print("  light on|off    Toggle fill light")
        print("  buzzer on|off   Toggle buzzer")
        print("  focus on|off    Toggle focus laser pointer")
        print("  gate on|off     Toggle safety gate")
        print("  autofocus       Run IR autofocus (3 probes)")
        print("  up <mm>         Move Z-axis up (default 5mm)")
        print("  down <mm>       Move Z-axis down (default 5mm)")
        print("  status          Query device status")
        print("  info            Query device info")
        print("  help            Show this help")
        print("  quit            Shut down the bridge")
        print()

    def _run(self):
        """Console input loop."""
        print()
        print("─" * 50)
        print("  Console ready — type 'help' for commands")
        print("  (LightBurn can connect at the same time)")
        print("─" * 50)
        print()

        while True:
            try:
                cmd = input("d1ultra> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nShutting down...")
                import os
                os._exit(0)

            if not cmd:
                continue

            # Safety check
            if self.state.is_running:
                if cmd not in ('status', 'info', 'ping', 'help', 'quit', 'exit', 'q'):
                    print("  WARNING: Job is running! Only status/ping/info allowed.")
                    print("  Wait for the job to finish or use 'status' to check.")
                    continue

            parts = cmd.split()
            action = parts[0]

            try:
                if action in ('quit', 'exit', 'q'):
                    print("Shutting down bridge...")
                    import os
                    os._exit(0)

                elif action == 'help':
                    self._print_help()

                elif action == 'ping':
                    if self.laser.ping():
                        print("  OK — laser is alive")
                    else:
                        print("  FAILED — no response")

                elif action == 'home':
                    print("  Sending motor home command...")
                    print("  Motor travels to top endstop, then retracts — can take 30+ sec...")
                    self.laser.home_motors()
                    print("  Motor homing complete (incl. retraction)")

                elif action == 'light':
                    on = len(parts) > 1 and parts[1] == 'on'
                    pkt = self.laser.builder.build_peripheral(Peripheral.FILL_LIGHT, on)
                    resp = self.laser.send_and_recv(pkt)
                    print(f"  Fill light {'ON' if on else 'OFF'}" + (" — OK" if resp else " — no ACK"))

                elif action == 'buzzer':
                    on = len(parts) > 1 and parts[1] == 'on'
                    pkt = self.laser.builder.build_peripheral(Peripheral.BUZZER, on)
                    resp = self.laser.send_and_recv(pkt)
                    print(f"  Buzzer {'ON' if on else 'OFF'}" + (" — OK" if resp else " — no ACK"))

                elif action == 'focus':
                    on = len(parts) > 1 and parts[1] == 'on'
                    pkt = self.laser.builder.build_peripheral(Peripheral.FOCUS_LASER, on)
                    resp = self.laser.send_and_recv(pkt)
                    print(f"  Focus laser {'ON' if on else 'OFF'}" + (" — OK" if resp else " — no ACK"))

                elif action == 'gate':
                    on = len(parts) > 1 and parts[1] == 'on'
                    pkt = self.laser.builder.build_peripheral(Peripheral.SAFETY_GATE, on)
                    resp = self.laser.send_and_recv(pkt)
                    print(f"  Safety gate {'ON' if on else 'OFF'}" + (" — OK" if resp else " — no ACK"))

                elif action == 'autofocus':
                    self._do_autofocus()

                elif action in ('up', 'down'):
                    mm = float(parts[1]) if len(parts) > 1 else 5.0
                    if action == 'down':
                        mm = -mm
                    pkt = self.laser.builder.build_z_move(mm)
                    # Motor ACK arrives only after move completes;
                    # allow ~3 s per mm of travel, minimum 5 s
                    z_timeout = max(5.0, abs(mm) * 3.0)
                    print(f"  Z-axis move {mm:+.1f}mm (timeout {z_timeout:.0f}s)...")
                    resp = self.laser.send_and_recv(pkt, timeout=z_timeout)
                    print(f"  Z-axis move {mm:+.1f}mm" + (" — OK" if resp else " — no ACK"))

                elif action == 'status':
                    resp = self.laser.send_and_recv(self.laser.builder.build_status())
                    if resp:
                        print(f"  Laser status: {resp}")
                    else:
                        print("  No response")
                    print(f"  Bridge state: running={self.state.is_running}, "
                          f"pos=({self.state.x:.2f}, {self.state.y:.2f}, {self.state.z:.2f}), "
                          f"laser={'ON' if self.state.laser_on else 'OFF'}")

                elif action == 'info':
                    resp = self.laser.send_and_recv(self.laser.builder.build(Cmd.DEVICE_INFO))
                    if resp:
                        print(f"  Device info: {resp}")
                    else:
                        print("  No response")
                    print(f"  Device: {self.laser.device_name}")
                    print(f"  Firmware: {self.laser.fw_version}")

                else:
                    print(f"  Unknown command: '{cmd}' — type 'help' for available commands")

            except Exception as e:
                print(f"  Error: {e}")

    def _do_autofocus(self):
        """Run the full autofocus sequence (3 probes averaged)."""
        print("  Starting autofocus sequence (IR probe × 3)...")
        z = self.laser.run_autofocus()
        if z is not None:
            self.state.z = z
            print(f"  Autofocus complete: Z = {z:.3f}mm")
        else:
            print("  Autofocus FAILED — no measurements received")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="D1 Ultra <-> LightBurn GRBL Bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python d1ultra_bridge.py
  python d1ultra_bridge.py --laser-ip 192.168.12.1 --listen-port 23
  python d1ultra_bridge.py --verbose

Then in LightBurn:
  Devices -> Add Manually -> GRBL -> TCP/IP
  Address: localhost  Port: 23
        """,
    )
    parser.add_argument('--laser-ip', default=DEFAULT_LASER_IP,
                        help=f"D1 Ultra IP address (default: {DEFAULT_LASER_IP})")
    parser.add_argument('--laser-port', type=int, default=DEFAULT_LASER_PORT,
                        help=f"D1 Ultra TCP port (default: {DEFAULT_LASER_PORT})")
    parser.add_argument('--listen-host', default=DEFAULT_LISTEN_HOST,
                        help=f"Bridge listen address (default: {DEFAULT_LISTEN_HOST})")
    parser.add_argument('--listen-port', type=int, default=DEFAULT_LISTEN_PORT,
                        help=f"Bridge listen port (default: {DEFAULT_LISTEN_PORT})")
    parser.add_argument('--verbose', '-v', action='store_true',
                        help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Connect to laser
    laser = D1UltraConnection(args.laser_ip, args.laser_port)

    log.info("=" * 60)
    log.info("D1 Ultra <-> LightBurn GRBL Bridge")
    log.info("=" * 60)

    if not laser.connect():
        log.warning("Cannot connect to laser yet. Is it powered on and USB connected?")
        log.info(f"Tried: {args.laser_ip}:{args.laser_port}")
        log.info("Starting in offline mode — will auto-connect when LightBurn connects")
    else:
        laser.identify()
        if laser.ping():
            log.info("Laser ping OK — ready to accept jobs")
        else:
            log.warning("Laser connected but not responding to ping")

    # Create GRBL server
    server = GRBLServer(laser, args.listen_host, args.listen_port)

    # Start interactive console (runs in background thread)
    console = InteractiveConsole(laser, server.state)
    console.start()

    # Start GRBL server (blocks main thread)
    try:
        server.start()
    except KeyboardInterrupt:
        log.info("\nShutting down...")
    finally:
        laser.disconnect()


if __name__ == '__main__':
    main()
