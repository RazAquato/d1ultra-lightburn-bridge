# D1 Ultra LightBurn Bridge — Project Context for Claude Code

This file gives Claude Code the context it needs to work on this project effectively.
For the public protocol spec, see PROTOCOL.md. For setup instructions, see README.md.

## What This Is

A reverse-engineered bridge between LightBurn and the Hansmaker D1 Ultra laser engraver.
LightBurn speaks GRBL over TCP, the bridge translates to the D1 Ultra's binary protocol.
Confirmed working on real hardware as of v2.3 — line engraving tested with SVG and text.

```
LightBurn  --GRBL/TCP-->  d1ultra_bridge.py (localhost:9023)  --D1 Ultra/TCP-->  Laser (192.168.12.1:6000)
```

The D1 Ultra connects via USB but presents as a virtual Ethernet adapter (RNDIS).
Laser IP: 192.168.12.1, host gets 192.168.12.x via DHCP. Protocol: TCP port 6000.

### RPi Zero JCZ Bridge (experimental, not yet functional)

A second approach exists in `NOTDONE_rpi_zero_bridge/` — a Raspberry Pi Zero presents itself
as a BJJCZ galvo controller (VID 0x9588, PID 0x9899) over USB. This would enable full galvo
features (live framing, split marking, cylinder correction) that the GRBL approach can't do.
Experimental skeleton only, not tested on hardware. Has an unsolved USB wiring problem
(Pi Zero only has one data port, needs both device and host mode simultaneously).

## Current Status

### What works (v2.3, confirmed on hardware)

- Line engraving (SVG shapes, text, any vector content)
- Multiple layers (serialized — each layer queues as a separate job)
- Power and speed control from LightBurn's layer settings
- Auto-reconnect after laser idle timeout
- Z-axis jog commands
- Interactive console (lights, buzzer, focus, gate, Z-axis, autofocus)
- Replay mode (send raw M+ pcapng bytes to laser for diagnostics)

### Known limitations

- **Fill/raster engraving**: Not implemented — only line/outline mode
- **Live framing**: Not available (GRBL limitation — needs JCZ approach)
- **Autofocus**: Partially decoded, may not work reliably
- **Job monitoring**: No real-time progress feedback during engraving

## Protocol Summary

Full spec in PROTOCOL.md. Quick reference:

- **Packet:** `0x0A0A + u16 len + u16 pad + u16 seq + u16 pad + u16 msg_type + u16 cmd + payload + u16 CRC-16/MODBUS + 0x0D0D`
- **CRC:** over bytes 2 through end of payload
- **Heartbeat:** STATUS (0x0000) every ~2s. Laser disconnects after ~10s idle.

### Job execution sequence (8 steps)

```
1. DEVICE_INFO  (0x0018)
2. PRE_JOB     (0x0005)
3. QUERY_14    (0x0014, sub=0x02) — pre-job setup
4. JOB_UPLOAD  (0x0002) — job name + ~6KB PNG preview
5. WORKSPACE   (0x0009) — bounding box (42-byte payload: 5 doubles + 2-byte pad)
6. [JOB_SETTINGS (0x0000, msg_type=0) + PATH_DATA (0x0001, msg_type=0)] x N — 10ms pacing
7. JOB_CONTROL (0x0003) — HOST sends, LASER echoes to confirm execution
8. JOB_FINISH  (0x0004)
```

### Key protocol facts

- **JOB_SETTINGS:** 37 bytes, msg_type=0. passes(u32) + speed(f64) + freq(f64) + power(f64) + source(u8) + unknown(f64, always -1.0)
- **PATH_DATA:** msg_type=0. count(u32) + segments[](f64 X + f64 Y + 16 zero bytes). Coordinates centered on design bbox midpoint.
- **SETTINGS:PATH ratio:** Not always 1:1. M+ sends SETTINGS once per unique parameter set (1:1 for multi-object SVG, 1:many for uniform text). Sending before every path is safe.
- **Unsolicited messages:** Laser sends 0x0013/0x0014/0x0015 periodically. Must ACK each unique (cmd, seq) pair exactly once — ACKing duplicates causes feedback loops (v2.0 bug, fixed in v2.1).

## Version History

| Version | Key Changes |
|---------|------------|
| v2.3 | Auto-reconnect after idle timeout; job serialization lock for multi-layer jobs |
| v2.1 | Fixed ACK feedback loop (unsolicited message handling); PNG preview size fix (44x44) |
| v2.0 | HOST sends 0x0003 (critical blocker fix); PRE_JOB, WORKSPACE, QUERY_14 added; -1.0 unknown field; 42-byte WORKSPACE; G1 S0 filter; 10ms pacing; replay mode |
| v1 | Connected, uploaded jobs, received ACKs, but jobs never executed (waited for laser to send 0x0003) |

## File Structure

```
d1ultra_protocol.py            Protocol library — clean API for D1 Ultra communication
d1ultra_bridge.py              GRBL bridge v2.3 (confirmed working, monolithic)
NOTTESTED_d1ultra_bridge_v2.4.py  v2.4 bridge — uses protocol library, adds WORKSPACE framing
PROTOCOL.md                    Full binary protocol specification
CLAUDE.md                      This file (project context for Claude Code)
README.md                      Public documentation
LICENSE                         MIT
requirements.txt               No external deps (stdlib only)
.gitignore                     Excludes temp/, __pycache__, debug.txt
wireshark_captures/            26 pcapng files from M+ sessions
NOTDONE_rpi_zero_bridge/       RPi Zero JCZ bridge (experimental skeleton)
  rpi_jcz_bridge.py            Main bridge: FunctionFS USB gadget + JCZ translation
  jcz_commands.py              BJJCZ command definitions + parser
  setup_gadget.sh              Linux USB gadget setup (creates BJJCZ VID/PID device)
  README.md                    RPi-specific docs + wiring diagram
```

## Critical Discovery Log

### 0x0003 (JOB_CONTROL) — The Execution Trigger

The v1 bridge uploaded jobs successfully — every packet was ACK'd — but the laser never
executed them. Automated pcapng analysis of all 26 captures proved that in every successful
M+ job, the **HOST sends 0x0003** (empty payload) to the laser, and the laser echoes it back.

The v1 bridge waited for the laser to send it, which never happened. This was the #1 blocker.

### ACK Feedback Loop (v2.0 bug, fixed v2.1)

When the bridge sent queries (0x0013/0x0015), the laser's responses were being treated as
"unsolicited" and ACK'd. The laser then responded to the ACK, creating infinite retransmission.
Fix: check `self._pending` FIRST — if a caller is waiting for that seq, route as normal response.
Only ACK truly unsolicited messages, and track `_acked_unsolicited` to never ACK the same
(cmd, seq) pair twice.

### WORKSPACE (0x0009) — Native Preview/Framing (v2.4 discovery)

Analysis of preview_200.pcapng and preview_1000.pcapng revealed that M+ does NOT send a full
job for preview. Instead, WORKSPACE alone triggers the laser to physically trace the bounding
box. The sequence is:

  QUERY_13 -> QUERY_15 -> DEVICE_INFO -> QUERY_14(0x02) -> WORKSPACE(speed, bbox)
  ...laser traces bounding box, STATUS shows busy (state=0x0001)...
  PRE_JOB (0x0005) -> stops the preview

No JOB_UPLOAD, no PATH_DATA, no JOB_CONTROL needed. The WORKSPACE ACK is asynchronous —
it arrives after PRE_JOB stops the preview. The speed field in WORKSPACE is the trace speed
(M+ uses 200 or 1000 mm/min). This is vastly simpler than the zero-power-job approach.

## RPi Zero JCZ Bridge — Architecture

### How it works

1. Pi Zero's USB device port presents as BJJCZ controller (configfs gadget)
2. LightBurn detects it as JCZ and sends 12-byte bulk commands in 3072-byte chunks
3. Bridge parses JCZ movement/laser commands, translates to D1 Ultra TCP packets
4. Pi Zero's USB host port connects to D1 Ultra (RNDIS ethernet to 192.168.12.1)

### JCZ Protocol (from Bryce Schroeder's balor project)

- USB endpoints: Bulk OUT 0x02 (commands), Bulk IN 0x88 (status)
- Command format: 12 bytes — u16 opcode + 5x u16 params (little-endian)
- Chunking: 256 commands per transfer (3072 bytes), padded with NOP (0x8002)
- Coordinates: 16-bit unsigned (0x0000-0xFFFF), center=0x8000
- Key opcodes: TRAVEL(0x8001), MARK(0x8005), SET_POWER(0x8012), JOB_BEGIN(0x8051)

### What needs work

- FunctionFS endpoint descriptors need real-hardware testing
- Unsolved USB wiring: Pi Zero has one data port, needs both device and host mode
- Galvo-to-mm coordinate calibration (field size mapping)
- JCZ speed/power/frequency value scaling
- Status response format (currently returns zeros)

## External Context

- Project posted to the official Hansmaker Facebook group
- Protocol documentation is freely available for anyone to use
- balor project (GitLab: bryce15/balor) has the BJJCZ reverse engineering
- galvoplotter (GitHub: meerk40t/galvoplotter) is a higher-level BJJCZ Python library
