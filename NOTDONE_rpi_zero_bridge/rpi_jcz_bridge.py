#!/usr/bin/env python3
"""
Raspberry Pi Zero — JCZ-to-D1 Ultra Bridge
===========================================

Makes the D1 Ultra appear as a BJJCZ galvo controller to LightBurn.

  LightBurn (PC)  --JCZ/USB-->  Pi Zero  --TCP/6000-->  D1 Ultra

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
import threading
import time
import logging
from typing import List, Tuple, Optional

# Add parent directory so we can import the shared protocol library
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from d1ultra_protocol import D1Ultra, LaserSource, Peripheral, make_preview_png
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
# FunctionFS endpoint setup
# ─────────────────────────────────────────────────────────────────────────────

def write_ffs_descriptors(ffs_dir: str):
    """Write USB endpoint descriptors to FunctionFS ep0.

    This tells the kernel what USB endpoints to create:
      - EP OUT 0x02: Bulk, 512 bytes (receives JCZ commands from LightBurn)
      - EP IN  0x88: Bulk, 512 bytes (sends status back to LightBurn)
    """
    ep0_path = os.path.join(ffs_dir, "ep0")

    FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
    FUNCTIONFS_HAS_FS_DESC = 1
    FUNCTIONFS_HAS_HS_DESC = 2

    # Interface descriptor (9 bytes)
    intf_desc = struct.pack('<BBBBBBBBB',
        9, 4, 0, 0, 2, 0xFF, 0xFF, 0xFF, 0)

    # EP OUT 0x02 (Bulk, 64 bytes FS)
    ep_out_fs = struct.pack('<BBBBBH', 7, 5, 0x02, 0x02, 64)
    # EP IN 0x88 (Bulk, 64 bytes FS)
    ep_in_fs = struct.pack('<BBBBBH', 7, 5, 0x88, 0x02, 64)
    # EP OUT 0x02 (Bulk, 512 bytes HS)
    ep_out_hs = struct.pack('<BBBBBH', 7, 5, 0x02, 0x02, 512)
    # EP IN 0x88 (Bulk, 512 bytes HS)
    ep_in_hs = struct.pack('<BBBBBH', 7, 5, 0x88, 0x02, 512)

    fs_descs = intf_desc + ep_out_fs + ep_in_fs
    hs_descs = intf_desc + ep_out_hs + ep_in_hs

    header = struct.pack('<III',
        FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
        len(fs_descs) + len(hs_descs) + 12,
        FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC)
    header += struct.pack('<II', 3, 3)

    # FunctionFS strings (required even if empty)
    FUNCTIONFS_STRINGS_MAGIC = 2
    str_header = struct.pack('<III', FUNCTIONFS_STRINGS_MAGIC, 12, 0)

    with open(ep0_path, 'wb') as f:
        f.write(header + fs_descs + hs_descs)
        f.write(str_header)

    log.info("FunctionFS descriptors written")


def bind_udc(gadget_dir: str):
    """Bind the gadget to the UDC (makes it visible to the host PC)."""
    udc_list = os.listdir("/sys/class/udc/")
    if not udc_list:
        log.error("No UDC found. Is dwc2 enabled in /boot/config.txt?")
        return False
    udc = udc_list[0]
    with open(os.path.join(gadget_dir, "UDC"), 'w') as f:
        f.write(udc)
    log.info(f"Bound to UDC: {udc}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# JCZ-to-D1 Ultra translator
# ─────────────────────────────────────────────────────────────────────────────

class JCZTranslator:
    """Translates JCZ command streams into D1 Ultra job sequences."""

    def __init__(self, laser: D1Ultra, field_mm: float = FIELD_SIZE_MM):
        self.laser = laser
        self.field_mm = field_mm
        self._current_path: List[Tuple[float, float]] = []
        self._all_paths: List[List[Tuple[float, float]]] = []
        self._laser_on = False
        self._power = 0.5
        self._speed = 1000.0
        self._freq = 50.0
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
            self._flush_path()
            xy = cmd.xy
            if xy:
                mm_x, mm_y = galvo_to_mm(xy[0], xy[1], self.field_mm)
                self._current_path = [(mm_x, mm_y)]

        elif op == JCZOp.MARK:
            xy = cmd.xy
            if xy:
                mm_x, mm_y = galvo_to_mm(xy[0], xy[1], self.field_mm)
                self._current_path.append((mm_x, mm_y))

        elif op == JCZOp.LASER_ON:
            self._laser_on = True

        elif op == JCZOp.LASER_OFF or (op == 0x8021 and cmd.p1 == 0):
            self._laser_on = False

        elif op == JCZOp.SET_MARK_SPEED:
            self._speed = max(1.0, cmd.p1 * 60.0 / 256.0)

        elif op == JCZOp.SET_POWER:
            self._power = min(1.0, cmd.p1 / 4095.0)

        elif op == JCZOp.SET_Q_PERIOD:
            if cmd.p1 > 0:
                self._freq = 1000.0 / max(1, cmd.p1)

        elif op == JCZOp.JOB_BEGIN:
            log.info("JCZ: JOB_BEGIN")
            self._job_active = True
            self._all_paths = []
            self._current_path = []

        elif op == JCZOp.JOB_END:
            log.info("JCZ: JOB_END")
            self._flush_path()
            if self._all_paths:
                self._execute_d1_job()
            self._job_active = False

    def _flush_path(self):
        if len(self._current_path) >= 2:
            self._all_paths.append(self._current_path[:])
        self._current_path = []

    def _execute_d1_job(self):
        """Translate collected JCZ paths to a D1 Ultra job and execute."""
        paths = self._all_paths
        log.info(f"Executing: {len(paths)} paths, "
                 f"{self._power*100:.0f}% power, {self._speed:.0f} mm/min")

        ok = self.laser.engrave(
            paths,
            speed=self._speed,
            power=self._power,
            frequency=self._freq,
            job_name="jcz_bridge_job")

        if ok:
            log.info("Job sent to D1 Ultra")
        else:
            log.error("Job failed")

    def send_status_ok(self) -> bytes:
        """Build a JCZ status response (ready for more commands)."""
        return b'\x00' * 12


# ─────────────────────────────────────────────────────────────────────────────
# Main bridge loop
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("JCZ-to-D1 Ultra Bridge (Raspberry Pi Zero)")
    log.info("=" * 60)

    # Step 1: Connect to D1 Ultra
    laser = D1Ultra(LASER_IP, LASER_PORT)
    if not laser.connect():
        log.error("Cannot connect to D1 Ultra. Is it plugged into the Pi's host port?")
        sys.exit(1)

    laser.identify()
    log.info("D1 Ultra ready")

    # Step 2: Set up FunctionFS endpoints
    log.info("Setting up USB gadget endpoints...")
    try:
        write_ffs_descriptors(FFS_DIR)
    except Exception as e:
        log.error(f"FunctionFS setup failed: {e}")
        log.error("Did you run: sudo bash setup_gadget.sh ?")
        sys.exit(1)

    if not bind_udc(GADGET_DIR):
        sys.exit(1)

    log.info("USB gadget active — waiting for LightBurn to connect")

    # Step 3: Open endpoint files
    ep_out_path = os.path.join(FFS_DIR, "ep1")
    ep_in_path  = os.path.join(FFS_DIR, "ep2")

    translator = JCZTranslator(laser)

    try:
        ep_out = open(ep_out_path, 'rb', buffering=0)
        ep_in  = open(ep_in_path, 'wb', buffering=0)
        log.info("Endpoints opened — bridge running")

        while True:
            data = b''
            while len(data) < 3072:
                chunk = ep_out.read(3072 - len(data))
                if not chunk:
                    break
                data += chunk

            if len(data) < 3072:
                log.info("LightBurn disconnected")
                break

            translator.process_chunk(data)

            try:
                ep_in.write(translator.send_status_ok())
            except Exception:
                pass

    except FileNotFoundError:
        log.error(f"Endpoint files not found at {FFS_DIR}/ep1 or ep2")
        log.error("Did you run setup_gadget.sh and write descriptors?")
    except KeyboardInterrupt:
        log.info("\nShutting down...")
    except Exception as e:
        log.error(f"Bridge error: {e}")
    finally:
        laser.disconnect()
        try: ep_out.close()
        except: pass
        try: ep_in.close()
        except: pass


if __name__ == '__main__':
    main()
