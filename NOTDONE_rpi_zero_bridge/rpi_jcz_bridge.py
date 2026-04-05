#!/usr/bin/env python3
"""
Raspberry Pi Zero — JCZ-to-D1 Ultra Bridge
===========================================

Makes the D1 Ultra appear as a BJJCZ galvo controller to LightBurn.

  LightBurn (PC)  ──JCZ/USB──>  Pi Zero  ──TCP/6000──>  D1 Ultra

The Pi Zero uses Linux's FunctionFS (USB gadget) to present as BJJCZ VID:PID
0x9588:0x9899. LightBurn sends 12-byte JCZ commands in 3072-byte chunks via
USB bulk transfers. This bridge parses the commands, extracts movement/laser
operations, and translates them into D1 Ultra binary protocol packets over TCP.

Prerequisites:
  1. Run setup_gadget.sh to configure the USB gadget
  2. Connect the D1 Ultra to the Pi's USB host port
  3. Run this script as root

Based on protocol research from:
  - balor (Bryce Schroeder): https://gitlab.com/bryce15/balor
  - D1 Ultra PROTOCOL.md (this project)

EXPERIMENTAL — NOT YET TESTED ON REAL HARDWARE
"""

import os
import sys
import struct
import socket
import threading
import time
import logging
from typing import List, Tuple, Optional

from jcz_commands import (
    JCZOp, JCZCommand, parse_chunk,
    galvo_to_mm, DEFAULT_FIELD_SIZE_MM,
)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

LASER_IP   = "192.168.12.1"
LASER_PORT = 6000

# FunctionFS mount point (set by setup_gadget.sh)
FFS_DIR = "/dev/jcz_usb"

# USB gadget UDC (will be auto-detected)
GADGET_DIR = "/sys/kernel/config/usb_gadget/bjjcz_bridge"

# Galvo field calibration
FIELD_SIZE_MM = DEFAULT_FIELD_SIZE_MM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jcz-bridge")


# ─────────────────────────────────────────────────────────────────────────────
# D1 Ultra connection (reused from the GRBL bridge)
# ─────────────────────────────────────────────────────────────────────────────

MAGIC      = b'\x0a\x0a'
TERMINATOR = b'\x0d\x0d'

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


class D1UltraPacketBuilder:
    """Minimal packet builder for the D1 Ultra protocol."""

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

    def status(self):
        return self.build(0x0000)

    def device_id(self):
        return self.build(0x0006)

    def device_info(self):
        payload = struct.pack('<HH', 0x0006, 0x8B1B) + b'\x00' * 28
        return self.build(0x0018, payload)

    def fw_version(self):
        return self.build(0x001E)

    def motor_reset(self):
        return self.build(0x000B)

    def job_settings(self, passes: int, speed: float, freq: float,
                     power: float, source: int = 1) -> bytes:
        payload  = struct.pack('<I', passes)
        payload += struct.pack('<d', speed)
        payload += struct.pack('<d', freq)
        payload += struct.pack('<d', power)
        payload += struct.pack('<B', source)
        payload += struct.pack('<d', -1.0)
        return self.build(0x0000, payload, msg_type=0)

    def path_data(self, segments: List[Tuple[float, float]]) -> bytes:
        payload = struct.pack('<I', len(segments))
        for x, y in segments:
            payload += struct.pack('<d', x) + struct.pack('<d', y) + b'\x00' * 16
        return self.build(0x0001, payload, msg_type=0)

    def job_upload(self, name: str, png: bytes = b'') -> bytes:
        name_b = name.encode()[:255] + b'\x00' * max(0, 256 - len(name.encode()[:255]))
        payload = name_b + b'\x00\x00' + struct.pack('<I', len(png)) + png
        return self.build(0x0002, payload)

    def job_control(self) -> bytes:
        return self.build(0x0003)

    def job_finish(self, name: str) -> bytes:
        name_b = name.encode()[:255] + b'\x00' * max(0, 256 - len(name.encode()[:255]))
        return self.build(0x0004, name_b)

    def peripheral(self, module: int, state: bool) -> bytes:
        return self.build(0x000E, struct.pack('<BB', module, 1 if state else 0))


class D1UltraConnection:
    """Minimal D1 Ultra TCP connection for the RPi bridge."""

    def __init__(self, ip: str = LASER_IP, port: int = LASER_PORT):
        self.ip = ip
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.builder = D1UltraPacketBuilder()
        self.connected = False
        self._lock = threading.Lock()
        self._recv_buf = b''

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.ip, self.port))
            self.sock.settimeout(2.0)
            self.connected = True
            log.info(f"Connected to D1 Ultra at {self.ip}:{self.port}")
            return True
        except Exception as e:
            log.error(f"Cannot connect to D1 Ultra: {e}")
            return False

    def send_recv(self, pkt: bytes, timeout: float = 5.0) -> Optional[bytes]:
        if not self.connected: return None
        with self._lock:
            try:
                self.sock.sendall(pkt)
                self.sock.settimeout(timeout)
                data = self.sock.recv(4096)
                return data
            except Exception as e:
                log.warning(f"Send/recv error: {e}")
                return None

    def send(self, pkt: bytes):
        if not self.connected: return
        with self._lock:
            try:
                self.sock.sendall(pkt)
            except Exception:
                self.connected = False

    def identify(self):
        """Run startup handshake."""
        self.send_recv(self.builder.device_id())
        self.send_recv(self.builder.status())
        self.send_recv(self.builder.device_info())
        self.send_recv(self.builder.fw_version())
        self.send_recv(self.builder.motor_reset(), timeout=8.0)
        log.info("D1 Ultra identified")

    def start_heartbeat(self):
        def _hb():
            while self.connected:
                time.sleep(2.0)
                if self.connected:
                    self.send_recv(self.builder.status(), timeout=2.0)
        t = threading.Thread(target=_hb, daemon=True)
        t.start()


# ─────────────────────────────────────────────────────────────────────────────
# FunctionFS endpoint setup
# ─────────────────────────────────────────────────────────────────────────────

def write_ffs_descriptors(ffs_dir: str):
    """Write USB endpoint descriptors to FunctionFS ep0.

    This tells the kernel what USB endpoints to create:
      - EP OUT 0x02: Bulk, 512 bytes (receives JCZ commands from LightBurn)
      - EP IN  0x88: Bulk, 512 bytes (sends status back to LightBurn)

    Descriptor format follows the FunctionFS specification.
    """
    ep0_path = os.path.join(ffs_dir, "ep0")

    # FunctionFS descriptor header (magic + flags + fs_count + hs_count + ss_count)
    FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
    FUNCTIONFS_HAS_FS_DESC = 1
    FUNCTIONFS_HAS_HS_DESC = 2

    # Interface descriptor (9 bytes)
    intf_desc = struct.pack('<BBBBBBBBB',
        9,      # bLength
        4,      # bDescriptorType (INTERFACE)
        0,      # bInterfaceNumber
        0,      # bAlternateSetting
        2,      # bNumEndpoints
        0xFF,   # bInterfaceClass (vendor-specific)
        0xFF,   # bInterfaceSubClass
        0xFF,   # bInterfaceProtocol
        0,      # iInterface
    )

    # EP OUT 0x02 (Bulk, 64 bytes for FS)
    ep_out_fs = struct.pack('<BBBBBH',
        7,      # bLength
        5,      # bDescriptorType (ENDPOINT)
        0x02,   # bEndpointAddress (OUT 2)
        0x02,   # bmAttributes (Bulk)
        64,     # wMaxPacketSize (FS)
    )

    # EP IN 0x88 (Bulk, 64 bytes for FS) — address 0x88 = IN endpoint 8
    ep_in_fs = struct.pack('<BBBBBH',
        7,      # bLength
        5,      # bDescriptorType (ENDPOINT)
        0x88,   # bEndpointAddress (IN 8)
        0x02,   # bmAttributes (Bulk)
        64,     # wMaxPacketSize (FS)
    )

    # EP OUT 0x02 (Bulk, 512 bytes for HS)
    ep_out_hs = struct.pack('<BBBBBH',
        7, 5, 0x02, 0x02, 512,
    )

    # EP IN 0x88 (Bulk, 512 bytes for HS)
    ep_in_hs = struct.pack('<BBBBBH',
        7, 5, 0x88, 0x02, 512,
    )

    fs_descs = intf_desc + ep_out_fs + ep_in_fs
    hs_descs = intf_desc + ep_out_hs + ep_in_hs

    # v2 header
    header = struct.pack('<III',
        FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
        len(fs_descs) + len(hs_descs) + 12,  # total length
        FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC,
    )
    # FS descriptor count + HS descriptor count
    header += struct.pack('<II', 3, 3)  # 3 descriptors each (intf + 2 endpoints)

    desc_data = header + fs_descs + hs_descs

    # FunctionFS strings (required even if empty)
    FUNCTIONFS_STRINGS_MAGIC = 2
    str_header = struct.pack('<III', FUNCTIONFS_STRINGS_MAGIC, 12, 0)

    with open(ep0_path, 'wb') as f:
        f.write(desc_data)
        f.write(str_header)

    log.info("FunctionFS descriptors written")


def bind_udc(gadget_dir: str):
    """Bind the gadget to the UDC (makes it visible to the host PC)."""
    udc_list = os.listdir("/sys/class/udc/")
    if not udc_list:
        log.error("No UDC found. Is dwc2 enabled in /boot/config.txt?")
        return False
    udc = udc_list[0]
    udc_path = os.path.join(gadget_dir, "UDC")
    with open(udc_path, 'w') as f:
        f.write(udc)
    log.info(f"Bound to UDC: {udc}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# JCZ-to-D1 Ultra translator
# ─────────────────────────────────────────────────────────────────────────────

class JCZTranslator:
    """Translates JCZ command streams into D1 Ultra job sequences."""

    def __init__(self, laser: D1UltraConnection, field_mm: float = FIELD_SIZE_MM):
        self.laser = laser
        self.field_mm = field_mm
        self._current_path: List[Tuple[float, float]] = []
        self._all_paths: List[List[Tuple[float, float]]] = []
        self._laser_on = False
        self._power = 0.5       # 0.0-1.0
        self._speed = 1000.0    # mm/min
        self._freq = 50.0       # kHz
        self._job_active = False

    def process_chunk(self, data: bytes):
        """Process a 3072-byte JCZ command chunk."""
        try:
            commands = parse_chunk(data)
        except ValueError as e:
            log.warning(f"Bad chunk: {e}")
            return

        for cmd in commands:
            if cmd.is_nop:
                continue
            self._handle_command(cmd)

    def _handle_command(self, cmd: JCZCommand):
        op = cmd.opcode

        if op == JCZOp.TRAVEL:
            # Rapid move — end current path, start new one
            self._flush_path()
            xy = cmd.xy
            if xy:
                mm_x, mm_y = galvo_to_mm(xy[0], xy[1], self.field_mm)
                self._current_path = [(mm_x, mm_y)]

        elif op == JCZOp.MARK:
            # Cut/mark move — add to current path
            xy = cmd.xy
            if xy:
                mm_x, mm_y = galvo_to_mm(xy[0], xy[1], self.field_mm)
                self._current_path.append((mm_x, mm_y))

        elif op == JCZOp.LASER_ON:
            self._laser_on = True

        elif op == JCZOp.LASER_OFF or (op == 0x8021 and cmd.p1 == 0):
            self._laser_on = False

        elif op == JCZOp.SET_MARK_SPEED:
            # JCZ speed is in mm/s (roughly); convert to mm/min
            self._speed = max(1.0, cmd.p1 * 60.0 / 256.0)

        elif op == JCZOp.SET_POWER:
            # JCZ power: 0-4095 → 0.0-1.0
            self._power = min(1.0, cmd.p1 / 4095.0)

        elif op == JCZOp.SET_Q_PERIOD:
            # Q-switch period to kHz (approximate)
            if cmd.p1 > 0:
                self._freq = 1000.0 / max(1, cmd.p1)

        elif op == JCZOp.JOB_BEGIN:
            log.info("JCZ: JOB_BEGIN received")
            self._job_active = True
            self._all_paths = []
            self._current_path = []

        elif op == JCZOp.JOB_END:
            log.info("JCZ: JOB_END received")
            self._flush_path()
            if self._all_paths:
                self._execute_d1_job()
            self._job_active = False

    def _flush_path(self):
        """Save current path if it has any mark points."""
        if len(self._current_path) >= 2:
            self._all_paths.append(self._current_path[:])
        self._current_path = []

    def _execute_d1_job(self):
        """Translate collected JCZ paths to a D1 Ultra job and execute."""
        b = self.laser.builder
        paths = self._all_paths
        log.info(f"Executing D1 Ultra job: {len(paths)} paths, "
                 f"{self._power*100:.0f}% power, {self._speed:.0f} mm/min")

        # Step 1: DEVICE_INFO
        self.laser.send_recv(b.device_info())

        # Step 2: JOB_UPLOAD (minimal PNG)
        # Generate a small valid PNG
        import zlib
        def _make_png():
            def _ch(ct, d):
                c = ct + d
                return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)
            sig = b'\x89PNG\r\n\x1a\n'
            ihdr = struct.pack('>IIBBBBB', 44, 44, 8, 2, 0, 0, 0)
            raw = bytearray()
            v = 0xDEADBEEF
            for _ in range(44):
                raw.append(0)
                for _ in range(44):
                    v = (v * 1664525 + 1013904223) & 0xFFFFFFFF
                    raw.extend([(v>>16)&0xFF, (v>>8)&0xFF, v&0xFF])
            idat = zlib.compress(bytes(raw), level=0)
            return sig + _ch(b'IHDR', ihdr) + _ch(b'IDAT', idat) + _ch(b'IEND', b'')

        png = _make_png()
        self.laser.send_recv(b.job_upload("jcz_bridge_job", png), timeout=10.0)

        # Step 3: JOB_SETTINGS + PATH_DATA for each path
        for i, path in enumerate(paths):
            self.laser.send_recv(
                b.job_settings(1, self._speed, self._freq, self._power))
            self.laser.send_recv(b.path_data(path), timeout=10.0)
            time.sleep(0.010)
            if (i + 1) % 50 == 0:
                log.info(f"  {i+1}/{len(paths)} paths sent")

        # Step 4: HOST sends JOB_CONTROL (0x0003)
        log.info("Sending JOB_CONTROL to laser...")
        r = self.laser.send_recv(b.job_control(), timeout=15.0)
        if r:
            log.info("  Laser confirmed JOB_CONTROL")
        else:
            log.warning("  No JOB_CONTROL response")

        # Step 5: JOB_FINISH
        self.laser.send_recv(b.job_finish("jcz_bridge_job"))
        log.info("Job sent to D1 Ultra")

    def send_status_ok(self) -> bytes:
        """Build a JCZ status response (ready for more commands)."""
        # Real BJJCZ sends a status word; simplest response is all-zeros
        return b'\x00' * 12


# ─────────────────────────────────────────────────────────────────────────────
# Main bridge loop
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("JCZ-to-D1 Ultra Bridge (Raspberry Pi Zero)")
    log.info("=" * 60)

    # Step 1: Connect to D1 Ultra
    laser = D1UltraConnection(LASER_IP, LASER_PORT)
    if not laser.connect():
        log.error("Cannot connect to D1 Ultra. Is it plugged into the Pi's host port?")
        sys.exit(1)

    laser.identify()
    laser.start_heartbeat()
    log.info("D1 Ultra ready")

    # Step 2: Set up FunctionFS endpoints
    log.info("Setting up USB gadget endpoints...")
    try:
        write_ffs_descriptors(FFS_DIR)
    except Exception as e:
        log.error(f"FunctionFS setup failed: {e}")
        log.error(f"Did you run: sudo bash setup_gadget.sh ?")
        sys.exit(1)

    # Bind to UDC (makes us visible to the host PC)
    if not bind_udc(GADGET_DIR):
        sys.exit(1)

    log.info("USB gadget active — waiting for LightBurn to connect")

    # Step 3: Open endpoint files
    ep_out_path = os.path.join(FFS_DIR, "ep1")  # Bulk OUT (receives from host)
    ep_in_path  = os.path.join(FFS_DIR, "ep2")  # Bulk IN (sends to host)

    translator = JCZTranslator(laser)

    try:
        ep_out = open(ep_out_path, 'rb', buffering=0)
        ep_in  = open(ep_in_path, 'wb', buffering=0)
        log.info("Endpoints opened — bridge running")

        while True:
            # Read a 3072-byte command chunk from LightBurn
            data = b''
            while len(data) < 3072:
                chunk = ep_out.read(3072 - len(data))
                if not chunk:
                    log.warning("EP OUT closed")
                    break
                data += chunk

            if len(data) < 3072:
                log.info("LightBurn disconnected")
                break

            # Process the chunk
            translator.process_chunk(data)

            # Send status response
            try:
                ep_in.write(translator.send_status_ok())
            except Exception:
                pass  # Status write failures are non-fatal

    except FileNotFoundError:
        log.error(f"Endpoint files not found at {FFS_DIR}/ep1 or ep2")
        log.error("Did you run setup_gadget.sh and write descriptors?")
    except KeyboardInterrupt:
        log.info("\nShutting down...")
    except Exception as e:
        log.error(f"Bridge error: {e}")
    finally:
        laser.connected = False
        try: ep_out.close()
        except: pass
        try: ep_in.close()
        except: pass


if __name__ == '__main__':
    main()
