#!/usr/bin/env python3
"""
Hansmaker D1 Ultra <-> LightBurn Bridge  v2.4
==============================================
Translates GRBL/TCP (LightBurn) to the D1 Ultra proprietary binary protocol.

  LightBurn  --GRBL/TCP-->  Bridge (localhost:9023)  --D1 Ultra/TCP-->  Laser (192.168.12.1:6000)

!! WARNING — EXPERIMENTAL — USE AT YOUR OWN RISK !!

Changes vs v2.3
────────────────
  ★ REFACTOR: Protocol layer extracted to d1ultra_protocol.py — clean API
    for anyone building D1 Ultra integrations (including LightBurn's team).
  ★ NEW: Pre-job framing using WORKSPACE command (0x0009). The laser traces
    the bounding box natively — no fake zero-power job needed. This matches
    how M+ does preview (verified from pcapng analysis of preview_200 and
    preview_1000 captures). User confirms at console before engraving.
  ★ NEW: 'frame on/off' console toggle, --no-frame CLI flag.

Usage:
    python NOTTESTED_d1ultra_bridge_v2.4.py --listen-port 9023
    python NOTTESTED_d1ultra_bridge_v2.4.py --no-frame --listen-port 9023
    python NOTTESTED_d1ultra_bridge_v2.4.py --replay path/to/capture.pcapng

LightBurn setup:
    Devices -> Create Manually -> GRBL (1.1f+) -> Ethernet/TCP
    Address: 127.0.0.1   Port: 9023
    Origin: Front Left   Disable auto-home
"""

import socket
import struct
import threading
import time
import argparse
import logging
import math
import sys
from typing import Optional, Tuple, List

from d1ultra_protocol import (
    D1Ultra, PacketBuilder, ResponseParser, Cmd, LaserSource, Peripheral,
    crc16_modbus, make_preview_png, MAGIC, TERMINATOR,
    DEFAULT_IP, DEFAULT_PORT,
)

DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 9023
FRAME_SPEED_MM_MIN  = 200.0
FRAME_MARGIN_MM     = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")


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
        self.laser_source   = LaserSource.DIODE
        self.frequency_khz  = 50.0
        self.passes         = 1
        self.speed_mm_min   = 1000.0
        self.job_power      = 0.0

    def start_new_path_group(self, x: float, y: float):
        self.job_path_groups.append([(x, y)])

    def add_cut_point(self, x: float, y: float, power: float):
        if power <= 0.0:
            return
        if self.job_power < power:
            self.job_power = power
        if not self.job_path_groups:
            self.job_path_groups.append([(x, y)])
        else:
            self.job_path_groups[-1].append((x, y))

    def reset_job(self):
        self.job_path_groups = []
        self.job_power = 0.0

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

    def __init__(self, laser: D1Ultra, state: GRBLState,
                 frame_enabled: bool = True):
        self.laser = laser
        self.state = state
        self.frame_enabled = frame_enabled
        self._awaiting_frame_confirm = False
        self._frame_confirm_event = threading.Event()
        self._frame_confirmed = False

    def handle_line(self, line: str) -> str:
        line = line.strip()
        if not line: return "ok"
        if ';' in line: line = line[:line.index(';')].strip()
        if '(' in line: line = line[:line.index('(')].strip()
        if not line: return "ok"

        upper = line.upper()
        if upper == '?':            return self._status_report()
        if upper == '$$':           return self._settings_report()
        if upper == '$H':           return self._home()
        if upper in ('$X','$X\n'):  return self._unlock()
        if upper.startswith('$J='): return self._jog(line[3:])
        if upper == '\x18':         return self._reset()
        if upper in ('!', '~'):     return "ok"
        if upper in ('$FOCUS', '$FOCUS ON'): return self._focus_on()
        if upper == '$FOCUS OFF':   return self._focus_off()
        if upper in ('$AUTOFOCUS', '$AF'): return self._autofocus()
        if upper == '$I':           return self._build_info()
        if upper == '$#':           return self._gcode_parameters()
        if upper == '$G':           return self._gcode_parser_state()
        if upper.startswith('$'):   return "ok"
        return self._parse_gcode(line)

    def _status_report(self) -> str:
        st = "Run" if self.state.is_running else "Idle"
        return (f"<{st}|MPos:{self.state.x:.3f},{self.state.y:.3f},"
                f"{self.state.z:.3f}|FS:{self.state.feed_rate:.0f},"
                f"{self.state.power:.0f}>")

    def _settings_report(self) -> str:
        lines = [f"${k}={v:.3f}" if isinstance(v, float) else f"${k}={v}"
                 for k in sorted(self.GRBL_SETTINGS)]
        return "\n".join(lines + ["ok"])

    def _build_info(self) -> str:
        fw = self.laser.fw_version or "1.0.0"
        frame = "ON" if self.frame_enabled else "OFF"
        return (f"[VER:1.1h.20190825: D1Ultra Bridge v2.4 "
                f"({fw}) frame={frame}]\n[OPT:V,15,128]\nok")

    def _gcode_parameters(self) -> str:
        lines = [f"[{cs}:0.000,0.000,0.000]"
                 for cs in ['G54','G55','G56','G57','G58','G59']]
        lines += ["[G28:0.000,0.000,0.000]", "[G30:0.000,0.000,0.000]",
                  "[G92:0.000,0.000,0.000]", "[TLO:0.000]",
                  "[PRB:0.000,0.000,0.000:0]", "ok"]
        return "\n".join(lines)

    def _gcode_parser_state(self) -> str:
        mode = "G90" if self.state.absolute_mode else "G91"
        return (f"[GC:G0 {mode} G17 G21 G94 G54 M5 M9 T0 "
                f"F{self.state.feed_rate:.0f} S{self.state.power:.0f}]\nok")

    def _home(self) -> str:
        threading.Thread(target=self.laser.home_motors, daemon=True).start()
        self.state.is_homed = True
        return "ok"

    def _unlock(self) -> str:
        return "[MSG:Caution: Unlocked]\nok"

    def _reset(self) -> str:
        self.state.reset_job()
        self.state.is_running = False
        return ("\r\nGrbl 1.1h ['$' for help]\n"
                "[MSG:'$H'|'$X' to unlock]\n[MSG:Caution: Unlocked]")

    def _focus_on(self) -> str:
        self.laser.set_peripheral(Peripheral.FOCUS_LASER, True)
        return "ok"

    def _focus_off(self) -> str:
        self.laser.set_peripheral(Peripheral.FOCUS_LASER, False)
        return "ok"

    def _autofocus(self) -> str:
        threading.Thread(target=self.laser.run_autofocus, daemon=True).start()
        return "ok"

    def _jog(self, params: str) -> str:
        upper = params.upper()
        if 'Z' in upper:
            try:
                z_idx = upper.index('Z')
                end = z_idx + 1
                while end < len(upper) and (upper[end].isdigit() or upper[end] in '.-+'):
                    end += 1
                self.laser.move_z(float(upper[z_idx+1:end]))
            except Exception:
                pass
        return "ok"

    def _parse_gcode(self, line: str) -> str:
        upper = line.upper()
        parts = upper.split()

        f_val = self._extract(upper, 'F')
        s_val = self._extract(upper, 'S')
        x_val = self._extract(upper, 'X')
        y_val = self._extract(upper, 'Y')
        z_val = self._extract(upper, 'Z')

        if s_val is not None: self.state.power = s_val
        if f_val is not None: self.state.feed_rate = f_val

        if 'M3' in parts or 'M03' in parts: self.state.laser_on = True
        if 'M5' in parts or 'M05' in parts: self.state.laser_on = False
        if 'M30' in parts or 'M2' in parts: return self._finish_job()
        if 'G90' in parts: self.state.absolute_mode = True
        if 'G91' in parts: self.state.absolute_mode = False

        if 'G92' in parts:
            if x_val is not None: self.state.x = x_val
            if y_val is not None: self.state.y = y_val
            if z_val is not None: self.state.z = z_val
            return "ok"

        if any(p in parts for p in ('G0', 'G00')):
            if x_val is not None:
                self.state.x = x_val if self.state.absolute_mode else self.state.x + x_val
            if y_val is not None:
                self.state.y = y_val if self.state.absolute_mode else self.state.y + y_val
            if z_val is not None:
                dz = (z_val - self.state.z) if self.state.absolute_mode else z_val
                if abs(dz) > 0.001:
                    self.laser.move_z(dz)
                self.state.z = self.state.z + dz if not self.state.absolute_mode else z_val
            self.state.start_new_path_group(self.state.x, self.state.y)
            return "ok"

        if any(p in parts for p in ('G1', 'G01', 'G2', 'G02', 'G3', 'G03')):
            nx = (x_val if self.state.absolute_mode else self.state.x + x_val) \
                 if x_val is not None else self.state.x
            ny = (y_val if self.state.absolute_mode else self.state.y + y_val) \
                 if y_val is not None else self.state.y

            if any(p in parts for p in ('G2', 'G02', 'G3', 'G03')):
                segs = self._linearise_arc(
                    self.state.x, self.state.y, nx, ny, line, upper,
                    clockwise=any(p in parts for p in ('G2', 'G02')))
                pwr = s_val if s_val is not None else self.state.power
                for sx, sy in segs:
                    self.state.add_cut_point(sx, sy, pwr)
                    self.state.x, self.state.y = sx, sy
            else:
                pwr = s_val if s_val is not None else self.state.power
                self.state.x, self.state.y = nx, ny
                if z_val is not None:
                    dz = (z_val - self.state.z) if self.state.absolute_mode else z_val
                    if abs(dz) > 0.001:
                        self.laser.move_z(dz)
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
    def _linearise_arc(x0, y0, x1, y1, line, upper, clockwise, segments=32):
        i_val = GRBLTranslator._extract(upper, 'I') or 0.0
        j_val = GRBLTranslator._extract(upper, 'J') or 0.0
        cx, cy = x0 + i_val, y0 + j_val
        r = math.sqrt((x0 - cx)**2 + (y0 - cy)**2)
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        if clockwise and a1 > a0: a1 -= 2 * math.pi
        elif not clockwise and a1 < a0: a1 += 2 * math.pi
        return [(cx + r * math.cos(a0 + (a1 - a0) * k / segments),
                 cy + r * math.sin(a0 + (a1 - a0) * k / segments))
                for k in range(1, segments + 1)]

    # ── Job trigger ───────────────────────────────────────────────────────────

    def _finish_job(self) -> str:
        groups = [g for g in self.state.job_path_groups if len(g) >= 2]
        if not groups:
            log.info("M30: no paths — nothing to engrave")
            self.state.reset_job()
            return "ok"

        all_pts = [pt for g in groups for pt in g]
        x_vals = [p[0] for p in all_pts]
        y_vals = [p[1] for p in all_pts]
        cx = (min(x_vals) + max(x_vals)) / 2
        cy = (min(y_vals) + max(y_vals)) / 2
        centred = [[(x - cx, y - cy) for x, y in g] for g in groups]

        power = self.state.job_power_fraction or self.state.power_fraction or 0.5
        speed = self.state.speed_mm_min if self.state.speed_mm_min > 0 else 500.0

        log.info(f"M30: '{self.state.job_name}' — "
                 f"{len(centred)} paths, {power*100:.0f}% power, {speed:.0f} mm/min")
        self.state.is_running = True

        def _run():
            try:
                # Pre-job framing via WORKSPACE command
                if self.frame_enabled:
                    all_c = [pt for g in centred for pt in g]
                    xv = [p[0] for p in all_c]
                    yv = [p[1] for p in all_c]
                    bb = (min(xv) - FRAME_MARGIN_MM, min(yv) - FRAME_MARGIN_MM,
                          max(xv) + FRAME_MARGIN_MM, max(yv) + FRAME_MARGIN_MM)
                    w, h = bb[2] - bb[0], bb[3] - bb[1]
                    log.info(f"Framing: {w:.1f} x {h:.1f} mm at {FRAME_SPEED_MM_MIN:.0f} mm/min")

                    self.laser.set_peripheral(Peripheral.FOCUS_LASER, True)
                    self.laser.preview(FRAME_SPEED_MM_MIN, *bb)

                    # Wait for user confirmation at the console
                    self._frame_confirmed = False
                    self._frame_confirm_event.clear()
                    self._awaiting_frame_confirm = True

                    print()
                    print("  *** FRAMING — align your workpiece ***")
                    print("  Press ENTER to engrave, or type 'cancel' to abort")
                    print()

                    got = self._frame_confirm_event.wait(timeout=120.0)
                    self._awaiting_frame_confirm = False

                    self.laser.stop_preview()
                    self.laser.set_peripheral(Peripheral.FOCUS_LASER, False)

                    if not got or not self._frame_confirmed:
                        log.info("Job cancelled")
                        self.state.is_running = False
                        self.state.reset_job()
                        return

                ok = self.laser.engrave(
                    centred, speed=speed, power=power,
                    frequency=self.state.frequency_khz,
                    passes=self.state.passes,
                    laser_source=self.state.laser_source,
                    job_name=self.state.job_name)
                log.info("Job complete!" if ok else "Job FAILED")
            finally:
                self.state.is_running = False
                self.state.reset_job()

        threading.Thread(target=_run, daemon=True).start()
        return "ok"

    def frame_confirm(self):
        self._frame_confirmed = True
        self._frame_confirm_event.set()

    def frame_cancel(self):
        self._frame_confirmed = False
        self._frame_confirm_event.set()


# ─────────────────────────────────────────────────────────────────────────────
# GRBL server
# ─────────────────────────────────────────────────────────────────────────────

class GRBLServer:
    def __init__(self, laser: D1Ultra, translator_factory,
                 host: str = DEFAULT_LISTEN_HOST,
                 port: int = DEFAULT_LISTEN_PORT):
        self.laser = laser
        self.translator_factory = translator_factory
        self.host = host
        self.port = port
        self._active_translator: Optional[GRBLTranslator] = None

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(1)
        log.info(f"GRBL server listening on {self.host}:{self.port}")
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()

    def _accept_loop(self, srv):
        while True:
            try:
                conn, addr = srv.accept()
                log.info(f"LightBurn connected from {addr}")
                threading.Thread(target=self._handle_client,
                                 args=(conn,), daemon=True).start()
            except Exception as e:
                log.error(f"Accept error: {e}")
                break

    def _handle_client(self, conn: socket.socket):
        state = GRBLState()
        translator = self.translator_factory(self.laser, state)
        self._active_translator = translator
        buf = b''
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
                    idx = buf.index(b'\n')
                    line = buf[:idx].decode('ascii', errors='replace')
                    buf = buf[idx+1:]
                    resp = translator.handle_line(line)
                    if resp:
                        try:
                            conn.sendall((resp + "\r\n").encode('ascii'))
                        except Exception:
                            break
        except Exception as e:
            log.warning(f"Client error: {e}")
        finally:
            self._active_translator = None
            try: conn.close()
            except: pass


# ─────────────────────────────────────────────────────────────────────────────
# Replay tool
# ─────────────────────────────────────────────────────────────────────────────

def replay_pcapng(path: str, laser_ip: str = DEFAULT_IP,
                  laser_port: int = DEFAULT_PORT):
    """Replay HOST->LASER packets from a pcapng capture."""
    log.info(f"Replay: {path}")
    with open(path, 'rb') as f:
        raw = f.read()
    if len(raw) < 8 or struct.unpack('<I', raw[0:4])[0] != 0x0A0D0D0A:
        log.error("Not a pcapng file"); return

    host_payloads = []
    offset = 0
    laser_ip_bytes = bytes(int(b) for b in laser_ip.split('.'))

    while offset + 8 <= len(raw):
        bt = struct.unpack('<I', raw[offset:offset+4])[0]
        bl = struct.unpack('<I', raw[offset+4:offset+8])[0]
        if bl < 12 or offset + bl > len(raw): break
        bd = raw[offset:offset+bl]; offset += bl
        if bt == 6 and len(bd) >= 28:
            cl = struct.unpack('<I', bd[20:24])[0]
            if 28 + cl <= len(bd):
                ts_h = struct.unpack('<I', bd[12:16])[0]
                ts_l = struct.unpack('<I', bd[16:20])[0]
                pl = _extract_tcp_payload(bd[28:28+cl], laser_ip_bytes, laser_port)
                if pl and len(pl) >= 18 and b'\x0a\x0a' in pl:
                    host_payloads.append((((ts_h << 32) | ts_l) * 1000, pl))

    log.info(f"Found {len(host_payloads)} HOST->LASER packets")
    if not host_payloads: return

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    try: sock.connect((laser_ip, laser_port))
    except Exception as e: log.error(f"Connect failed: {e}"); return
    sock.settimeout(None)

    job_ready = threading.Event()
    stop = threading.Event()

    def reader():
        rb = b''
        while not stop.is_set():
            try:
                chunk = sock.recv(4096)
                if not chunk: break
                rb += chunk
                while len(rb) >= 18:
                    idx = rb.find(b'\x0a\x0a')
                    if idx == -1: rb = b''; break
                    if idx > 0: rb = rb[idx:]
                    if len(rb) < 4: break
                    pl = struct.unpack('<H', rb[2:4])[0]
                    if len(rb) < pl: break
                    pkt = rb[:pl]; rb = rb[pl:]
                    if len(pkt) >= 14:
                        cmd = struct.unpack('<H', pkt[12:14])[0]
                        seq = struct.unpack('<H', pkt[6:8])[0]
                        log.info(f"  LASER: cmd=0x{cmd:04x} seq={seq}")
                        if cmd == 0x0003:
                            log.info("  >> JOB_CONTROL!"); job_ready.set()
            except Exception: break

    threading.Thread(target=reader, daemon=True).start()
    prev_ts = host_payloads[0][0]
    for i, (ts, payload) in enumerate(host_payloads):
        if i > 0:
            delay = min((ts - prev_ts) / 1e9, 0.5)
            if delay > 0.001: time.sleep(delay)
        prev_ts = ts
        cmd = struct.unpack('<H', payload[12:14])[0] if len(payload) >= 14 else 0
        log.info(f"  -> {i+1}/{len(host_payloads)}: cmd=0x{cmd:04x} len={len(payload)}")
        try: sock.sendall(payload)
        except Exception as e: log.error(f"Send: {e}"); break
        time.sleep(0.005)

    log.info("Waiting 10s for JOB_CONTROL...")
    log.info("SUCCESS!" if job_ready.wait(timeout=10.0) else "JOB_CONTROL not received")
    stop.set(); sock.close()


def _extract_tcp_payload(pkt, dst_ip, dst_port):
    try:
        if len(pkt) < 14: return None
        if struct.unpack('>H', pkt[12:14])[0] != 0x0800: return None
        ip = 14; ihl = (pkt[ip] & 0x0F) * 4
        if pkt[ip+9] != 6: return None
        if pkt[ip+16:ip+20] != bytes(dst_ip): return None
        tcp = ip + ihl
        if struct.unpack('>H', pkt[tcp+2:tcp+4])[0] != dst_port: return None
        off = ((pkt[tcp+12] >> 4) & 0xF) * 4
        pl = pkt[tcp+off:]
        return pl if pl else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Interactive console
# ─────────────────────────────────────────────────────────────────────────────

HELP = """
D1 Ultra Bridge v2.4 — console commands

  Framing:
    frame on/off        Toggle pre-job framing
    frame status        Show framing state

  Peripheral:
    light on/off        Fill light
    buzzer on/off       Buzzer
    focus on/off        Focus laser pointer
    gate on/off         Safety gate

  Motion:
    home                Home/reset motors
    up/down <mm>        Move Z (default 5mm)
    autofocus           IR autofocus (3-probe)

  Status:
    ping / status / info

  help / quit
"""


def run_console(laser: D1Ultra, server: GRBLServer,
                frame_enabled: bool = True):
    print("-" * 56)
    print("  Console ready — type 'help' for commands")
    print(f"  Framing: {'ON' if frame_enabled else 'OFF'}")
    print("-" * 56)

    while True:
        # Check if a translator is waiting for frame confirmation
        t = server._active_translator
        if t and t._awaiting_frame_confirm:
            try:
                user_in = input("  [ENTER to engrave / cancel]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                t.frame_cancel(); break
            if user_in == '':
                t.frame_confirm()
            elif user_in == 'cancel':
                t.frame_cancel()
            else:
                print("  ENTER = engrave, 'cancel' = abort")
            continue

        try:
            line = input("d1ultra> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nShutting down..."); break

        if not line: continue
        toks = line.split()
        cmd = toks[0]

        if cmd in ('quit', 'exit', 'q'):
            break
        elif cmd == 'help':
            print(HELP)
        elif cmd == 'frame':
            sub = toks[1] if len(toks) > 1 else ''
            if sub == 'on':
                frame_enabled = True
                print("Framing ON")
            elif sub == 'off':
                frame_enabled = False
                print("Framing OFF")
            elif sub == 'status':
                print(f"Framing: {'ON' if frame_enabled else 'OFF'}")
            else:
                print("Usage: frame on | off | status")
            # Update any active translator
            if server._active_translator:
                server._active_translator.frame_enabled = frame_enabled
        elif cmd == 'ping':
            print("Alive" if laser.ping() else "No response")
        elif cmd == 'status':
            print(f"Connected: {laser.connected}")
            print(f"Device:    {laser.device_name or '?'}")
            print(f"Firmware:  {laser.fw_version or '?'}")
            print(f"Framing:   {'ON' if frame_enabled else 'OFF'}")
        elif cmd == 'info':
            print(f"Device:   {laser.device_name}")
            print(f"Firmware: {laser.fw_version}")
        elif cmd == 'home':
            laser.home_motors()
        elif cmd in ('up', 'down'):
            mm = float(toks[1]) if len(toks) > 1 else 5.0
            dist = mm if cmd == 'up' else -mm
            laser.move_z(dist)
            print(f"Z {'up' if dist > 0 else 'down'} {abs(dist):.1f} mm")
        elif cmd == 'autofocus':
            z = laser.run_autofocus()
            if z: print(f"Z = {z:.3f} mm")
        elif cmd in ('light', 'buzzer', 'focus', 'gate'):
            module = {'light': 0, 'buzzer': 1, 'focus': 2, 'gate': 3}[cmd]
            laser.set_peripheral(module, len(toks) > 1 and toks[1] == 'on')
        else:
            print(f"Unknown: {cmd}")

    laser.disconnect()
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="D1 Ultra <-> LightBurn Bridge v2.4")
    parser.add_argument('--laser-ip',    default=DEFAULT_IP)
    parser.add_argument('--laser-port',  type=int, default=DEFAULT_PORT)
    parser.add_argument('--listen-host', default=DEFAULT_LISTEN_HOST)
    parser.add_argument('--listen-port', type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument('--no-frame',    action='store_true',
                        help='Disable pre-job framing')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--replay', metavar='PCAPNG')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.replay:
        replay_pcapng(args.replay, args.laser_ip, args.laser_port)
        return

    frame_on = not args.no_frame

    print()
    print("D1 Ultra <-> LightBurn Bridge  v2.4")
    print("=" * 50)
    print("NEW: Protocol library extracted to d1ultra_protocol.py")
    print("NEW: Pre-job framing via WORKSPACE command (matches M+)")
    print(f"     Framing: {'ON' if frame_on else 'OFF (--no-frame)'}")
    print()

    laser = D1Ultra(args.laser_ip, args.laser_port)
    if not laser.connect():
        sys.exit(1)
    if not laser.identify():
        log.warning("Identification incomplete — continuing")

    def make_translator(l, state):
        return GRBLTranslator(l, state, frame_enabled=frame_on)

    server = GRBLServer(laser, make_translator, args.listen_host, args.listen_port)
    server.start()

    print(f"  LightBurn: Devices -> GRBL -> TCP -> 127.0.0.1:{args.listen_port}")
    print()

    try:
        run_console(laser, server, frame_on)
    except KeyboardInterrupt:
        print("\nShutting down...")
        laser.disconnect()


if __name__ == '__main__':
    main()
