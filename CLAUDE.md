# D1 Ultra LightBurn Bridge — Project Context for Claude Code

This file gives Claude Code the context it needs to work on this project effectively.
For the public protocol spec, see PROTOCOL.md. For setup instructions, see README.md.

## What This Is

A reverse-engineered bridge between LightBurn and the Hansmaker D1 Ultra laser engraver.
Two approaches exist:

1. **GRBL bridge** (`NOTTESTED_d1ultra_bridge_v2.py`) — LightBurn speaks GRBL over TCP, the bridge
   translates to the D1 Ultra's binary protocol. Works today for basic engraving.
   Missing galvo-specific features (live framing, split marking, cylinder correction).

2. **RPi Zero JCZ bridge** (`NOTDONE_rpi_zero_bridge/`) — A Raspberry Pi Zero presents itself as a
   BJJCZ galvo controller (VID 0x9588, PID 0x9899) over USB. LightBurn sends native JCZ
   commands, the Pi translates to D1 Ultra binary protocol over TCP. This enables full galvo
   features. Experimental, not yet tested on hardware.

## Architecture

```
Approach 1 — GRBL (working, limited features):
  LightBurn  <--GRBL/TCP-->  NOTTESTED_d1ultra_bridge_v2.py  <--Binary/TCP:6000-->  D1 Ultra

Approach 2 — JCZ via RPi Zero (experimental, full galvo):
  LightBurn  <--JCZ/USB-->  RPi Zero  <--Binary/TCP:6000-->  D1 Ultra
```

The D1 Ultra connects via USB but presents as a virtual Ethernet adapter (RNDIS).
Laser IP: 192.168.12.1, host gets 192.168.12.x via DHCP. Protocol: TCP port 6000.

## GRBL vs JCZ — Why Both Exist

Using GRBL as the base means you don't get galvo-specific features like live framing, split
marking, or cylinder correction. A JCZ galvo controller profile is the better starting point
for full support.

The problem: LightBurn's JCZ driver expects a USB device with BJJCZ VID/PID (0x9588:0x9899).
The D1 Ultra's USB presents as RNDIS (network adapter), not a BJJCZ controller. So LightBurn's
JCZ driver won't bind to it. A Raspberry Pi Zero solves this by sitting in the middle — it
presents as BJJCZ to the PC and talks TCP to the laser.

## Critical Protocol Discovery (April 2026)

### 0x0003 (JOB_CONTROL) — The Execution Trigger

Automated pcapng analysis of all 26 Wireshark captures proved that in every successful M+ job:

1. **HOST sends 0x0003** (empty payload, 18 bytes) to 192.168.12.1:6000
2. **LASER echoes 0x0003** (2-byte ACK payload) back to host

Verified with full IP/port extraction across 4 independent captures from 3 different host IPs.
The host initiates execution. See `temp/verify_0x0003_direction.py` for the proof.

**However:** An earlier test of sending 0x0003 from the bridge caused uncontrolled Z-axis
descent. This was likely because 0x0003 was sent without valid job data uploaded first,
or at the wrong point in the sequence. The v2 bridge now sends 0x0003 at the correct
position (after all SETTINGS+PATH pairs). This is being tested.

### Other v2 Fixes (from pcapng analysis)

| Fix | Detail |
|-----|--------|
| JOB_SETTINGS unknown field | Changed from 0.0 to -1.0 (M+ always sends -1.0) |
| WORKSPACE (0x0009) payload | Changed from 40 to 42 bytes (M+ sends 2-byte pad) |
| PRE_JOB (0x0005) | Added before JOB_UPLOAD (M+ sends this) |
| QUERY_14(0x02) pre-job | Added before job (M+ sends this) |
| PNG preview | Changed from 0/286 bytes to ~6 KB (M+ sends 6-14 KB) |
| Unsolicited ACKs | Now responds to 0x0013/0x0014/0x0015 from laser |
| G1 S0 duplicate filter | Removes trailing zero-power points from LightBurn |
| Packet pacing | 10ms between SETTINGS+PATH pairs (matches M+) |

### SETTINGS:PATH Ratio

Not always 1:1. Pcapng shows:
- SVG with separate objects: 3 SETTINGS, 3 PATHS (1:1 per object)
- "hello" text: 1 SETTINGS, 4-1799 PATHS (1:many, same settings for all)

M+ sends SETTINGS once per unique parameter set. Sending before every path is safe but wasteful.

## Protocol Summary

Full spec in PROTOCOL.md. Quick reference:

- **Packet:** `0x0A0A + u16 len + u16 pad + u16 seq + u16 pad + u16 msg_type + u16 cmd + payload + u16 CRC-16/MODBUS + 0x0D0D`
- **CRC:** over bytes 2 through end of payload
- **Job sequence:** DEVICE_INFO → JOB_UPLOAD (with PNG) → [SETTINGS + PATH] × N → HOST sends 0x0003 → LASER echoes 0x0003 → JOB_FINISH
- **JOB_SETTINGS:** 37 bytes, msg_type=0. passes(u32) + speed(f64) + freq(f64) + power(f64) + source(u8) + unknown(f64, always -1.0)
- **PATH_DATA:** msg_type=0. count(u32) + segments[](f64 X + f64 Y + 16 zero bytes each). Coordinates centered on design bbox midpoint.
- **Heartbeat:** STATUS (0x0000) every ~2s. Laser disconnects after ~10s idle.

## File Structure

```
d1ultra_bridge.py            v1 bridge (historical, has the original blocker)
NOTTESTED_d1ultra_bridge_v2.py         v2 bridge with all protocol fixes (current)
PROTOCOL.md                  Full binary protocol specification
CLAUDE.md                    This file (project context for Claude Code)
README.md                    Public documentation
LICENSE                      MIT
requirements.txt             No external deps (stdlib only)
.gitignore                   Excludes temp/, __pycache__, debug.txt
wireshark_captures/          26 pcapng files from M+ sessions
NOTDONE_rpi_zero_bridge/             RPi Zero JCZ bridge (experimental)
  rpi_jcz_bridge.py          Main bridge: FunctionFS USB gadget + JCZ translation
  jcz_commands.py            BJJCZ command definitions + parser
  setup_gadget.sh            Linux USB gadget setup (creates BJJCZ VID/PID device)
  README.md                  RPi-specific docs
temp/                        Gitignored test workspace
  analyze_captures.py        Pcapng analyzer (protocol verification)
  deep_analysis.py           TCP-reassembled deep analysis
  verify_0x0003_direction.py Full IP/port proof for 0x0003 direction
```

## RPi Zero JCZ Bridge — Architecture

### How it works

1. Pi Zero's USB device port presents as BJJCZ controller (configfs gadget)
2. LightBurn detects it as JCZ and sends 12-byte bulk commands in 3072-byte chunks
3. Bridge parses JCZ movement/laser commands, translates to D1 Ultra TCP packets
4. Pi Zero's USB host port connects to D1 Ultra (RNDIS ethernet to 192.168.12.1)

### JCZ Protocol (from Bryce Schroeder's balor project)

- USB endpoints: Bulk OUT 0x02 (commands), Bulk IN 0x88 (status)
- Command format: 12 bytes — u16 opcode + 5× u16 params (little-endian)
- Chunking: 256 commands per transfer (3072 bytes), padded with NOP (0x8002)
- Coordinates: 16-bit unsigned (0x0000-0xFFFF), center=0x8000
- Key opcodes: TRAVEL(0x8001), MARK(0x8005), SET_POWER(0x8012), JOB_BEGIN(0x8051)

### What needs work

- FunctionFS endpoint descriptors need real-hardware testing
- Galvo-to-mm coordinate calibration (field size mapping)
- JCZ speed/power/frequency value scaling
- Status response format (currently returns zeros)
- Real-time performance on Pi Zero 2W under load

## External Context

- Project posted to the official Hansmaker Facebook group
- Protocol documentation is freely available for anyone to use
- balor project (GitLab: bryce15/balor) has the BJJCZ reverse engineering
- galvoplotter (GitHub: meerk40t/galvoplotter) is a higher-level BJJCZ Python library
