#!/usr/bin/env python3
"""
JCZ-to-D1 Ultra Bridge
========================

Main bridge application. Makes a Hansmaker D1 Ultra laser appear as a
BJJCZ galvo controller to LightBurn via USB/IP.

Architecture:
    Thread 1 (USB reader)    — reads 3072-byte JCZ command batches from FunctionFS
    Thread 2 (status writer) — sends status bytes to LightBurn every ~10ms
    Thread 3 (laser monitor) — watches for RNDIS interface (laser on/off detection)
    Thread 4 (heartbeat)     — built into D1Ultra class, sends keepalive every 2s

Data flow:
    LightBurn -> USB/IP -> FunctionFS ep1 (Bulk OUT) -> JCZ parser
        -> coordinate translation -> D1 Ultra TCP -> laser

Run as root:
    sudo python3 jcz_bridge.py
    sudo python3 jcz_bridge.py --setup-only   # just bind UDC, don't run bridge
    sudo python3 jcz_bridge.py --test-laser    # test laser TCP connection only

License: MIT
"""

import os
import sys
import struct
import threading
import time
import logging
import argparse
import signal
from typing import List, Tuple, Optional

from config import (
    LASER_IP, LASER_PORT, LASER_SUBNET, LASER_IFACE_CHECK_SEC,
    LASER_DHCP_SETTLE_SEC,
    FFS_MOUNT, FFS_EP0, FFS_EP_OUT, FFS_EP_IN,
    GADGET_DIR, UDC_NAME,
    FIELD_SIZE_MM, FRAME_SPEED_MM_MIN,
    STATUS_POLL_MS, JOB_NAME_PREFIX, LOG_LEVEL,
)
from d1ultra_protocol import D1Ultra, LaserSource, Peripheral, make_preview_png
from jcz_protocol import (
    JCZOp, JCZCommand, parse_chunk, galvo_to_mm,
    CHUNK_SIZE, COMMANDS_PER_CHUNK,
)
from laser_monitor import LaserMonitor

# ═══════════════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════════════

LOG_FILE = "/var/log/d1ultra-bridge.log"

_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
_log_fmt   = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")

# Console handler
_console = logging.StreamHandler()
_console.setLevel(_log_level)
_console.setFormatter(_log_fmt)

# File handler (append, so history is preserved across restarts)
try:
    _fileh = logging.FileHandler(LOG_FILE)
    _fileh.setLevel(_log_level)
    _fileh.setFormatter(_log_fmt)
except PermissionError:
    _fileh = None

logging.basicConfig(level=_log_level, handlers=[h for h in (_console, _fileh) if h])
log = logging.getLogger("jcz-bridge")


# ═══════════════════════════════════════════════════════════════════════════════
# FunctionFS descriptor setup
# ═══════════════════════════════════════════════════════════════════════════════

def write_ffs_descriptors(ep0_path: str) -> int:
    """Write USB endpoint descriptors to FunctionFS ep0.

    Registers two bulk endpoints with the kernel:
        EP1 OUT (0x01) — LightBurn sends JCZ commands here
        EP2 IN  (0x82) — bridge sends status responses here

    Uses FunctionFS v2 descriptor format with both full-speed and high-speed
    descriptors (required for USB 2.0 compliance).

    IMPORTANT: Returns the ep0 file descriptor. The caller MUST keep it open
    for the entire lifetime of the gadget. Closing it deactivates the function.
    """
    FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
    FUNCTIONFS_HAS_FS_DESC = 1
    FUNCTIONFS_HAS_HS_DESC = 2
    FUNCTIONFS_STRINGS_MAGIC = 2

    # USB interface descriptor (9 bytes)
    intf = struct.pack('<BBBBBBBBB',
        9,      # bLength
        4,      # bDescriptorType = INTERFACE
        0,      # bInterfaceNumber
        0,      # bAlternateSetting
        2,      # bNumEndpoints
        0xFF,   # bInterfaceClass = Vendor Specific
        0xFF,   # bInterfaceSubClass
        0xFF,   # bInterfaceProtocol
        0,      # iInterface
    )

    # USB endpoint descriptor: bLen(B) bDescType(B) bEndAddr(B) bmAttr(B) wMaxPkt(H) bInterval(B)
    # Full-speed (64-byte max packet)
    ep_out_fs = struct.pack('<BBBBHB', 7, 5, 0x01, 0x02, 64, 0)   # EP1 OUT Bulk
    ep_in_fs  = struct.pack('<BBBBHB', 7, 5, 0x82, 0x02, 64, 0)   # EP2 IN  Bulk

    # High-speed (512-byte max packet)
    ep_out_hs = struct.pack('<BBBBHB', 7, 5, 0x01, 0x02, 512, 0)  # EP1 OUT Bulk
    ep_in_hs  = struct.pack('<BBBBHB', 7, 5, 0x82, 0x02, 512, 0)  # EP2 IN  Bulk

    fs_descs = intf + ep_out_fs + ep_in_fs   # 9 + 7 + 7 = 23 bytes
    hs_descs = intf + ep_out_hs + ep_in_hs   # same structure, different sizes

    # v2 header: magic(4) + length(4) + flags(4) + fs_count(4) + hs_count(4)
    flags = FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
    total_len = 20 + len(fs_descs) + len(hs_descs)
    header = struct.pack('<IIIII',
        FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
        total_len,
        flags,
        3,  # fs descriptor count (1 interface + 2 endpoints)
        3,  # hs descriptor count
    )

    # FunctionFS v2 strings: magic(4) + length(4) + count(4) + lang(2) + null(2)
    str_blob = struct.pack('<IIIH', FUNCTIONFS_STRINGS_MAGIC, 16, 1, 0x0409)
    str_blob += b'\x00\x00'  # one empty string + padding

    # ep0 must be opened with O_RDWR (not 'wb' — O_TRUNC breaks FunctionFS)
    fd = os.open(ep0_path, os.O_RDWR)
    os.write(fd, header + fs_descs + hs_descs)
    os.write(fd, str_blob)

    log.info("FunctionFS descriptors written to ep0")
    return fd  # CALLER MUST KEEP THIS OPEN


def bind_udc(gadget_dir: str, udc_name: str):
    """Bind the gadget to the UDC, making it visible on the dummy_hcd bus."""
    udc_path = os.path.join(gadget_dir, "UDC")
    with open(udc_path, 'w') as f:
        f.write(udc_name)
    log.info(f"Gadget bound to UDC: {udc_name}")


def unbind_udc(gadget_dir: str):
    """Unbind the gadget from UDC."""
    udc_path = os.path.join(gadget_dir, "UDC")
    try:
        with open(udc_path, 'w') as f:
            f.write("")
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# JCZ command translator
# ═══════════════════════════════════════════════════════════════════════════════

class JCZTranslator:
    """Accumulates JCZ commands and translates them to D1 Ultra jobs.

    State machine:
        Idle -> receives JOB_BEGIN -> accumulating paths
        Accumulating -> receives TRAVEL -> starts new path group
        Accumulating -> receives MARK -> appends to current path
        Accumulating -> receives JOB_END -> executes D1 Ultra job
        LIGHT commands -> triggers live framing (separate from jobs)
    """

    def __init__(self, laser: D1Ultra, field_mm: float = FIELD_SIZE_MM):
        self.laser = laser
        self.field_mm = field_mm

        # Accumulated job state
        self._current_path: List[Tuple[float, float]] = []
        self._all_paths: List[List[Tuple[float, float]]] = []
        self._framing_points: List[Tuple[float, float]] = []
        self._in_job = False
        self._job_count = 0

        # Laser parameters (updated by SET_* commands)
        self._power = 0.5        # 0.0-1.0
        self._speed = 1000.0     # mm/min
        self._freq = 50.0        # kHz
        self._laser_on = False

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
            self._handle(cmd)

    def _handle(self, cmd: JCZCommand):
        op = cmd.opcode

        if op == JCZOp.TRAVEL:
            # Travel = start of new path group (laser off move)
            self._flush_path()
            xy = cmd.xy_galvo
            if xy:
                mm = galvo_to_mm(xy[0], xy[1], self.field_mm)
                self._current_path = [mm]

        elif op == JCZOp.MARK:
            # Mark = laser-on move, append to current path
            xy = cmd.xy_galvo
            if xy:
                mm = galvo_to_mm(xy[0], xy[1], self.field_mm)
                self._current_path.append(mm)

        elif op == JCZOp.LASER_SWITCH:
            self._laser_on = (cmd.p1 != 0)

        elif op == JCZOp.SET_MARK_SPEED:
            # Speed value scaling: p1 is in units that map to mm/min
            # balor uses p1 * 60 / 256, but this needs hardware calibration
            if cmd.p1 > 0:
                self._speed = max(1.0, cmd.p1 * 60.0 / 256.0)

        elif op == JCZOp.SET_POWER:
            # Power: 0-4095 range from JCZ -> 0.0-1.0 for D1 Ultra
            self._power = min(1.0, cmd.p1 / 4095.0)

        elif op == JCZOp.SET_Q_PERIOD:
            # Q-switch period in microseconds -> frequency in kHz
            if cmd.p1 > 0:
                self._freq = 1000.0 / max(1, cmd.p1)

        elif op == JCZOp.JOB_BEGIN:
            log.info("JCZ: JOB_BEGIN")
            self._in_job = True
            self._all_paths = []
            self._current_path = []

        elif op == JCZOp.JOB_END:
            log.info("JCZ: JOB_END")
            self._flush_path()
            if self._all_paths:
                self._execute_job()
            self._in_job = False

    def _flush_path(self):
        """Save current path if it has 2+ points, start fresh."""
        if len(self._current_path) >= 2:
            self._all_paths.append(self._current_path[:])
        self._current_path = []

    def _execute_job(self):
        """Translate accumulated JCZ paths to D1 Ultra job and send."""
        paths = self._all_paths
        if not paths:
            return

        # Centre coordinates on bounding box midpoint (D1 Ultra requirement)
        all_pts = [pt for p in paths for pt in p]
        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0

        centred_paths = [[(x - cx, y - cy) for x, y in p] for p in paths]

        self._job_count += 1
        job_name = f"{JOB_NAME_PREFIX}_{self._job_count:04d}"

        log.info(f"Executing job '{job_name}': {len(centred_paths)} paths, "
                 f"{self._power * 100:.0f}% power, {self._speed:.0f} mm/min")

        ok = self.laser.engrave(
            centred_paths,
            speed=self._speed,
            power=self._power,
            frequency=self._freq,
            job_name=job_name,
        )

        if ok:
            log.info(f"Job '{job_name}' sent to laser")
        else:
            log.error(f"Job '{job_name}' FAILED to send")

    def handle_framing(self, points: List[Tuple[float, float]]):
        """Handle live framing request (LIGHT commands from LightBurn).

        Uses the D1 Ultra's native WORKSPACE preview — the laser traces
        the bounding box with the focus laser. Much simpler than sending
        a zero-power job.
        """
        if not points or len(points) < 2:
            return

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        bbox = (min(xs), min(ys), max(xs), max(ys))

        # Centre on midpoint
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        centred_bbox = (
            bbox[0] - cx, bbox[1] - cy,
            bbox[2] - cx, bbox[3] - cy,
        )

        log.info(f"Framing: {bbox[2]-bbox[0]:.1f} x {bbox[3]-bbox[1]:.1f} mm")

        # Turn on focus laser (red dot)
        self.laser.set_peripheral(Peripheral.FOCUS_LASER, True)

        # Start native preview
        self.laser.preview(
            speed=FRAME_SPEED_MM_MIN,
            x_min=centred_bbox[0], y_min=centred_bbox[1],
            x_max=centred_bbox[2], y_max=centred_bbox[3],
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Bridge core
# ═══════════════════════════════════════════════════════════════════════════════

class JCZBridge:
    """Main bridge: ties together FunctionFS, JCZ translation, and laser."""

    def __init__(self):
        self.laser = D1Ultra(LASER_IP, LASER_PORT)
        self.translator = JCZTranslator(self.laser, FIELD_SIZE_MM)
        self.monitor = LaserMonitor(
            subnet=LASER_SUBNET,
            laser_ip=LASER_IP,
            check_interval=LASER_IFACE_CHECK_SEC,
        )

        self._running = False
        self._busy = False        # True while a job is executing
        self._ep0_fd = -1         # FunctionFS ep0 fd (MUST stay open)
        self._ep_out = None       # FunctionFS bulk OUT file
        self._ep_in = None        # FunctionFS bulk IN file
        self._status_thread: Optional[threading.Thread] = None
        self._ep0_thread: Optional[threading.Thread] = None

    def run(self):
        """Main entry point. Blocks until interrupted."""
        log.info("=" * 60)
        log.info("JCZ-to-D1 Ultra Bridge")
        log.info("=" * 60)

        # Step 1: Write FunctionFS descriptors and bind UDC
        self._setup_gadget()

        # Step 2: Start laser monitor
        self.monitor.on_laser_up = self._on_laser_up
        self.monitor.on_laser_down = self._on_laser_down
        self.monitor.start()
        log.info("Laser monitor started — watching for RNDIS interface")

        # Step 3: Main loop — reconnects when LightBurn disconnects
        self._running = True
        try:
            while self._running:
                self._close_endpoints()
                try:
                    self._open_endpoints()
                except OSError as e:
                    if not self._running:
                        break
                    log.warning(f"Cannot open endpoints: {e}")
                    time.sleep(2.0)
                    continue

                try:
                    self._read_loop()
                except OSError as e:
                    if not self._running:
                        break
                    log.warning(f"Endpoint error: {e}")

                self._close_endpoints()
                if self._running:
                    log.info("Waiting for LightBurn to reconnect...")
                    time.sleep(1.0)
        except KeyboardInterrupt:
            log.info("Interrupted")
        except Exception as e:
            log.error(f"Bridge error: {e}", exc_info=True)
        finally:
            self._shutdown()

    def _setup_gadget(self):
        """Write FunctionFS descriptors and bind UDC."""
        if not os.path.exists(FFS_EP0):
            log.error(f"FunctionFS ep0 not found at {FFS_EP0}")
            log.error("Run: sudo bash setup_gadget.sh")
            sys.exit(1)

        self._ep0_fd = write_ffs_descriptors(FFS_EP0)
        bind_udc(GADGET_DIR, UDC_NAME)
        log.info("USB gadget active on dummy_hcd bus")

        # Start ep0 event handler — MUST run to handle USB control requests
        self._ep0_thread = threading.Thread(
            target=self._ep0_loop, daemon=True, name="ep0-handler")
        self._ep0_thread.start()

    def _ep0_loop(self):
        """Handle USB control requests from the host via FunctionFS ep0.

        FunctionFS forwards events (BIND, UNBIND, ENABLE, DISABLE, SETUP,
        SUSPEND, RESUME) through ep0. If nobody reads these, the USB control
        pipe stalls and the host gives up.

        Event struct (12 bytes): u8 type + 3 bytes pad + 8 bytes union data.
        SETUP events contain the 8-byte USB setup packet in the union.
        """
        import select

        log.info("ep0 event handler started")
        poll = select.poll()
        poll.register(self._ep0_fd, select.POLLIN)

        EVENT_SIZE = 12
        event_names = {
            0: "BIND", 1: "UNBIND", 2: "ENABLE", 3: "DISABLE",
            4: "SETUP", 5: "SUSPEND", 6: "RESUME",
        }

        while self._running and self._ep0_fd >= 0:
            try:
                # Wait for events with 1-second timeout (so we can check _running)
                events = poll.poll(1000)
                if not events:
                    continue

                data = os.read(self._ep0_fd, 256)
                if not data:
                    continue

                # Parse events (may contain multiple 12-byte events)
                offset = 0
                while offset + EVENT_SIZE <= len(data):
                    event_type = data[offset]
                    name = event_names.get(event_type, f"UNKNOWN({event_type})")

                    if event_type == 2:  # ENABLE
                        log.info("USB host connected (ENABLE)")
                    elif event_type == 3:  # DISABLE
                        log.info("USB host disconnected (DISABLE)")
                    elif event_type == 4:  # SETUP (control transfer)
                        # Extract the 8-byte USB setup packet
                        setup = data[offset + 4 : offset + 12]
                        log.debug(f"ep0 SETUP: {setup.hex()}")
                        # Stall unknown vendor requests by writing empty response
                        try:
                            os.read(self._ep0_fd, 0)
                        except OSError:
                            pass
                    else:
                        log.debug(f"ep0 event: {name}")

                    offset += EVENT_SIZE

            except OSError as e:
                if self._running:
                    log.debug(f"ep0 error: {e}")
                    time.sleep(0.1)
            except Exception as e:
                log.debug(f"ep0 handler error: {e}")
                break

        log.info("ep0 event handler stopped")

    def _open_endpoints(self):
        """Open FunctionFS endpoint files for reading/writing."""
        log.info("Opening FunctionFS endpoints...")
        # Use os.open to avoid Python's buffering interfering with USB I/O
        self._ep_out_fd = os.open(FFS_EP_OUT, os.O_RDONLY)
        self._ep_in_fd  = os.open(FFS_EP_IN, os.O_WRONLY)
        log.info("Endpoints opened — waiting for LightBurn")

    def _close_endpoints(self):
        """Close FunctionFS endpoint files."""
        for fd_name in ('_ep_out_fd', '_ep_in_fd'):
            fd = getattr(self, fd_name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, -1)

    def _start_status_thread(self):
        """Start the thread that sends status bytes to LightBurn."""
        self._status_thread = threading.Thread(
            target=self._status_loop, daemon=True, name="status-writer")
        self._status_thread.start()

    def _status_loop(self):
        """Send JCZ status byte to LightBurn via Bulk IN.

        LightBurn polls the Bulk IN endpoint for status. The status byte:
            0x00 = idle/ready
            0x01 = busy (job executing)
        Padded to 12 bytes to match JCZ command size.
        """
        interval = STATUS_POLL_MS / 1000.0
        while self._running and self._ep_in_fd >= 0:
            try:
                status = b'\x01' if self._busy else b'\x00'
                os.write(self._ep_in_fd, status + b'\x00' * 11)
            except OSError as e:
                if self._running:
                    log.warning(f"Status write failed: {e}")
                break
            except Exception:
                break
            time.sleep(interval)

    def _read_loop(self):
        """Main loop: read JCZ commands from FunctionFS Bulk OUT.

        BJJCZ protocol has two command types:
          1. Single commands (opcode < 0x8000): 12 bytes OUT, expects 8 bytes IN
             Examples: GetSerialNo(0x0009), GetVersion(0x0007), GotoXY(0x000D)
          2. List/batch commands (opcode >= 0x8000): sent in 3072-byte batches
             Examples: TRAVEL(0x8001), MARK(0x8005), SET_POWER(0x8012)
             No per-command response — status polled via GetVersion.

        The bridge responds to each single command immediately with 8 bytes.
        Batch commands are accumulated and executed when JOB_END is received.
        """
        log.info("Bridge running — reading JCZ commands")
        buf = b''
        CMD_SIZE = 12

        while self._running:
            try:
                data = os.read(self._ep_out_fd, 8192)
                if not data:
                    log.info("Endpoint read returned empty — LightBurn disconnected")
                    break

                buf += data

                while len(buf) >= CMD_SIZE:
                    cmd_bytes = buf[:CMD_SIZE]
                    buf = buf[CMD_SIZE:]
                    try:
                        self._handle_command(cmd_bytes)
                    except Exception as e:
                        log.error(f"Command handler error: {e}", exc_info=True)

            except OSError as e:
                if self._running:
                    log.warning(f"Read error: {e}")
                break

    def _handle_command(self, data: bytes):
        """Parse a 12-byte JCZ command and respond appropriately."""
        vals = struct.unpack_from('<HHHHHH', data, 0)
        cmd = JCZCommand(*vals)
        opcode = cmd.opcode

        if cmd.is_nop:
            return

        # Single commands (opcode < 0x8000): need 8-byte response on IN
        if opcode < 0x8000:
            response = self._handle_single_command(cmd)
            try:
                os.write(self._ep_in_fd, response)
            except OSError as e:
                log.warning(f"IN write failed for 0x{opcode:04x}: {e}")
        else:
            # Batch/list command (opcode >= 0x8000): accumulate, no response
            log.debug(f"JCZ list cmd: {cmd}")
            self.translator._handle(cmd)

    def _handle_single_command(self, cmd: JCZCommand) -> bytes:
        """Handle a single JCZ command and return 8-byte response.

        Response format: 4x u16 LE = (echo, word1, word2, status_flags)
        Status flags: BUSY=0x0004, READY=0x0020, AXIS=0x0040

        From galvoplotter + balor reverse engineering.
        """
        opcode = cmd.opcode
        READY = 0x0020
        BUSY  = 0x0004

        status = READY
        if self._busy:
            status |= BUSY

        if opcode == 0x0009:
            # GetSerialNo — first command LightBurn sends
            log.info(f"GetSerialNo -> responding with serial + READY")
            return struct.pack('<4H', 0x0009, 0x0001, 0x0001, status)

        elif opcode == 0x0007:
            # GetVersion — also used for status polling
            log.info(f"GetVersion -> responding with version + status")
            return struct.pack('<4H', 0x0007, 0x0502, 0x0000, status)

        elif opcode == 0x000A:
            # GetListStatus — check if list execution is done
            # b1: 0=idle, 1=busy
            list_busy = 0x0001 if self._busy else 0x0000
            log.debug(f"GetListStatus -> busy={self._busy}")
            return struct.pack('<4H', 0x000A, list_busy, 0x0000, status)

        elif opcode == 0x000C:
            # GetPositionXY — current galvo position
            log.debug(f"GetPositionXY")
            return struct.pack('<4H', 0x000C, 0x8000, 0x8000, status)

        elif opcode == 0x0004:
            # EnableLaser
            log.info(f"EnableLaser")
            return struct.pack('<4H', 0x0004, 0x0000, 0x0000, status)

        elif opcode == 0x0040:
            # Reset
            log.info(f"Reset")
            return struct.pack('<4H', 0x0040, 0x0000, 0x0000, status)

        else:
            # Unknown single command — echo opcode with READY status
            log.debug(f"Single cmd 0x{opcode:04x} -> generic ACK")
            return struct.pack('<4H', opcode, 0x0000, 0x0000, status)

    def _on_laser_up(self, iface: str, ip: str):
        """Called when the RNDIS interface appears (laser powered on).

        DHCP can take up to 2 minutes to settle. The interface first gets a
        link-local address, then eventually the correct IP from the laser.
        We wait for LASER_DHCP_SETTLE_SEC before attempting TCP connection.
        """
        settle = LASER_DHCP_SETTLE_SEC
        log.info(f"RNDIS interface {iface} appeared ({ip}) — "
                 f"waiting {settle:.0f}s for DHCP to settle")

        # Poll until we can actually reach the laser, up to settle time
        deadline = time.time() + settle
        connected = False
        while time.time() < deadline:
            if self.laser.connect():
                if self.laser.identify():
                    log.info(f"Laser ready: {self.laser.device_name} "
                             f"(fw {self.laser.fw_version})")
                    connected = True
                    break
                else:
                    self.laser.disconnect()
            # Check every 5 seconds
            time.sleep(5.0)

        if not connected:
            log.warning(f"Could not reach laser after {settle:.0f}s — "
                        f"will retry on next interface check")

    def _on_laser_down(self):
        """Called when the RNDIS interface disappears (laser powered off)."""
        log.info("Laser disconnected")
        self.laser.disconnect()

    def _shutdown(self):
        """Clean shutdown."""
        log.info("Shutting down...")
        self._running = False
        self.monitor.stop()
        self.laser.disconnect()

        self._close_endpoints()

        try:
            unbind_udc(GADGET_DIR)
        except OSError:
            pass

        # Close ep0 last (deactivates FunctionFS)
        if self._ep0_fd >= 0:
            try:
                os.close(self._ep0_fd)
            except OSError:
                pass
            self._ep0_fd = -1

        log.info("Bridge stopped")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_setup_only():
    """Set up gadget, bind UDC, verify, then clean up."""
    log.info("Setup-only mode: writing descriptors and binding UDC")
    fd = write_ffs_descriptors(FFS_EP0)
    try:
        bind_udc(GADGET_DIR, UDC_NAME)
        log.info("Done. Gadget is active. Endpoints available:")
        for ep in ("ep0", "ep1", "ep2"):
            path = os.path.join(FFS_MOUNT, ep)
            exists = "OK" if os.path.exists(path) else "MISSING"
            log.info(f"  {path} — {exists}")
    finally:
        os.close(fd)
        unbind_udc(GADGET_DIR)


def cmd_test_laser():
    """Test TCP connection to the laser."""
    log.info("Testing laser connection...")

    monitor = LaserMonitor(LASER_SUBNET, LASER_IP, check_interval=1.0)
    online, iface, ip = monitor.check_once()

    if not online:
        log.warning(f"No RNDIS interface found in subnet {LASER_SUBNET}")
        log.warning("Is the laser powered on and USB passed through?")
        log.info(f"Trying direct TCP connection to {LASER_IP}:{LASER_PORT} anyway...")

    laser = D1Ultra(LASER_IP, LASER_PORT)
    if laser.connect():
        if laser.identify():
            log.info(f"SUCCESS: {laser.device_name} (fw {laser.fw_version})")
        else:
            log.warning("Connected but identify returned no device name")
        laser.disconnect()
    else:
        log.error(f"FAILED: cannot reach {LASER_IP}:{LASER_PORT}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="JCZ-to-D1 Ultra Bridge — BJJCZ emulator for LightBurn")
    parser.add_argument('--setup-only', action='store_true',
                        help="Set up USB gadget and exit (for testing)")
    parser.add_argument('--test-laser', action='store_true',
                        help="Test TCP connection to the D1 Ultra")
    parser.add_argument('--field-size', type=float, default=FIELD_SIZE_MM,
                        help=f"Override field size in mm (default: {FIELD_SIZE_MM})")
    args = parser.parse_args()

    if args.field_size != FIELD_SIZE_MM:
        log.info(f"Field size override: {args.field_size} mm")

    if args.setup_only:
        cmd_setup_only()
    elif args.test_laser:
        cmd_test_laser()
    else:
        # Handle SIGTERM gracefully (systemd sends this on stop)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        bridge = JCZBridge()
        bridge.run()


if __name__ == '__main__':
    main()
