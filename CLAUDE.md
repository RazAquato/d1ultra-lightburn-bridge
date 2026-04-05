# D1 Ultra LightBurn Bridge — Project Status

## What This Is

A Python bridge that translates GRBL commands from LightBurn into the Hansmaker D1 Ultra's proprietary binary protocol over TCP port 6000. The goal is to let LightBurn control the D1 Ultra directly, since Hansmaker only provides their own M+ software.

## Architecture

```
LightBurn  <--GRBL/TCP-->  d1ultra_bridge.py  <--Binary/TCP:6000-->  D1 Ultra
```

- LightBurn connects to the bridge as a GRBL 1.1h device (TCP, configurable port)
- Bridge translates G-code into Hansmaker's binary packet protocol
- Laser communicates over USB virtual network adapter (laser=192.168.12.1, host=192.168.12.x)

## Protocol Knowledge (Reverse-Engineered)

### Packet Format
- 14-byte header: `magic(0x0A0A) + u16 total_len + u16 pad + u16 seq + u16 pad + u16 msg_type + u16 cmd`
- Variable payload
- 4-byte tail: `u16 CRC-16/MODBUS + 0x0D0D terminator`
- CRC is computed over bytes 2 through end of payload (everything after magic, before CRC)

### Known Commands
| Cmd    | Name          | msg_type | Description |
|--------|---------------|----------|-------------|
| 0x0000 | STATUS        | 1        | Heartbeat / status query |
| 0x0000 | JOB_SETTINGS  | 0        | Per-path job parameters (passes, speed, freq, power, laser_source) |
| 0x0001 | PATH_DATA     | 0        | Coordinate segments for one path group |
| 0x0002 | JOB_DATA      | 1        | Job upload header (name + PNG preview) |
| 0x0003 | JOB_CONTROL   | 1        | Laser signals "paths received, ready to execute" (laser→host only) |
| 0x0004 | JOB_NAME      | 1        | Job finalize (256-byte name field) |
| 0x0012 | AUTOFOCUS     | 1        | Autofocus probe request |
| 0x0013 | QUERY_13      | 1        | Unsolicited laser status |
| 0x0014 | QUERY_14      | 1        | Unsolicited laser info |
| 0x0015 | QUERY_15      | 1        | Unsolicited laser info |
| 0x0018 | DEVICE_INFO   | 1        | Device info query (32-byte payload with device_id) |

### M+ Job Execution Sequence (from pcapng captures)
```
1. HOST → LASER: DEVICE_INFO (0x0018, msg_type=1)
2. HOST → LASER: JOB_DATA    (0x0002, msg_type=1) — name + PNG preview
3. For EACH path/shape in the design:
   a. HOST → LASER: JOB_SETTINGS (0x0000, msg_type=0)
   b. HOST → LASER: PATH_DATA    (0x0001, msg_type=0)
4. LASER → HOST: JOB_CONTROL  (0x0003, msg_type=1) — "ready to execute"
5. HOST → LASER: JOB_NAME     (0x0004, msg_type=1) — finalize
6. LASER → HOST: STATUS        (0x0000) — job running / complete
```

**Critical protocol detail:** The host must send JOB_SETTINGS before EACH PATH_DATA, not just once. M+ sends one SETTINGS+PATH pair per path/shape in the design.

**Critical protocol detail:** The host must NEVER send 0x0003 to the laser. M+ never does this. Sending it triggers a Z-axis descent sequence.

### PATH_DATA Format
- Coordinates are centered around the design's bounding-box midpoint (not absolute bed position)
- Each segment: `f64 X + f64 Y + 16 zero bytes` = 32 bytes
- Payload: `u32 segment_count + segments[]`

### JOB_SETTINGS Payload (37 bytes)
```
u32  passes
f64  speed_mm_min
f64  frequency_khz
f64  power_frac (0.0–1.0)
u8   laser_source (0=IR, 1=Diode)
f64  unknown (always 0.0 in captures)
```

### JOB_DATA Payload
```
bytes 0-255:   Job name, null-padded
bytes 256-257: Padding (0x0000)
bytes 258-261: PNG size as u32 LE
bytes 262+:    PNG image data (preview thumbnail)
```

### Other Details
- device_id for DEVICE_INFO: 0x8B1B (packs as `1b 8b` LE)
- device_id for autofocus: 0x1A8B (packs as `8b 1a` LE) — different!
- Laser has ~10 second idle timeout; heartbeat STATUS pings every 2s keep it alive
- Heartbeat must continue running during job execution

## What Works

- TCP connection to laser over USB virtual NIC
- Heartbeat / keepalive
- Firmware version and motor calibration data retrieval
- GRBL 1.1h emulation (LightBurn connects and sends G-code)
- G-code parsing into path groups (split at G0 rapid moves)
- Coordinate centering (GRBL absolute → design-centered)
- Full job packet sequence: DEVICE_INFO → JOB_DATA → [SETTINGS+PATH]×N → JOB_FINISH
- Laser ACKs every packet we send
- Z-axis commands (manual jog)
- Console interface with interactive commands

## What Does NOT Work — The Blocker

**The laser never sends JOB_CONTROL (0x0003) after receiving our job data.**

Without JOB_CONTROL, the job is uploaded but never executes. The laser ACKs every packet, stays connected, responds to heartbeats, and ACKs JOB_FINISH — but never fires JOB_CONTROL and never engraves.

When M+ sends the same SVG with identical settings, the laser DOES send JOB_CONTROL and the job executes.

### What Has Been Tried
1. Sending JOB_START (0x0003) host→laser — caused Z-axis descent, removed
2. Single JOB_SETTINGS + all PATH_DATA — changed to per-path SETTINGS+PATH pairs
3. Absolute GRBL coordinates — changed to centered coordinates matching M+
4. No PNG in JOB_DATA — added 286-byte PNG preview
5. Various timeout/heartbeat adjustments

### Verified Matching M+ (via pcapng comparison)
- DEVICE_INFO request/response: byte-identical
- JOB_SETTINGS payload: byte-identical (same passes/speed/freq/power/laser_source)
- PATH_DATA format: correct (u32 count + f64 X + f64 Y + 16 zero pad per segment)
- PATH_DATA coordinates: centered, matching M+ values (±9.0mm for the test SVG)
- JOB_FINISH: same structure, just different job name string
- CRC-16/MODBUS: computed correctly

### Known Remaining Differences vs M+
1. **PNG preview size**: Our PNG is 286 bytes; M+'s is ~6255 bytes. Unknown if size matters.
2. **G1 S0 duplicate point**: LightBurn's G-code emits `G1 S0` before M5, which adds a duplicate closing point to the last path group (6 points vs M+'s 5 for the square). Should be filtered.
3. **Unsolicited laser messages (0x0013, 0x0014, 0x0015)**: M+ receives these before/during the job. Our bridge also receives them (via reader thread) but doesn't respond. Unknown if a response is needed.

## Recommended Next Steps

### 1. Protocol Replay Tool (Highest Priority)
Write a tool that sends the **exact raw bytes** from the M+ pcapng capture to the laser (replaying the HOST→LASER packets with correct timing). If the laser responds with JOB_CONTROL, we know the M+ bytes work. Then change one variable at a time (our job name, our PNG, our coordinates, etc.) to find exactly what breaks it.

This eliminates all guesswork — we'd know within minutes which specific byte(s) the laser cares about.

### 2. Capture Current Bridge Traffic
Run Wireshark during a bridge job and compare the ACTUAL wire bytes against the M+ capture. The bridge logs only show the first 40 bytes of each packet — there could be a bug in the remaining bytes that we can't see in logs.

### 3. Investigate Unsolicited Messages
The M+ capture shows the laser sends 0x0013 and 0x0015 BEFORE DEVICE_INFO, and 0x0014 AFTER. These might be "handshake" messages that the host needs to acknowledge or that put the laser into a state where it accepts jobs. Check if the bridge receives these and whether responding changes anything.

### 4. Filter Duplicate Points
Remove the extra point caused by `G1 S0` (duplicate of the last position). While probably not the root cause, it makes the path data diverge from M+.

## Files

- `d1ultra_bridge.py` — The main bridge (single file, ~1600 lines)
- `test_square.svg` — Simple 20×20mm test SVG (square with X diagonals)
- `parse_engrave_capture.py` — pcapng parser for protocol analysis
- `parse_engrave_capture_detailed.py` — Enhanced parser with full hex dumps
- `CAPTURE_ANALYSIS_SUMMARY.txt` — Analysis of the "hello" engrave capture
- `capture_comparison.txt` — Side-by-side M+ vs bridge comparison (SVG test)
- `HEX_PAYLOAD_COMPARISON.txt` — Byte-level hex comparison

### Pcapng Captures (in uploads/)
- `hello_mark_line_engrave_70power_400speed_processing1.pcapng` — M+ "hello" text engrave
- `svg_from_m+.pcapng` — M+ test_square.svg engrave (reference)
- `svg_from_lightburn.pcapng` — Bridge test_square.svg attempt (pre-fix)
- `autofocus.pcapng` — M+ autofocus sequence
- `full_capture_connect_reset_motor.pcapng` — M+ connect + motor reset

## Project Goals
- MIT license, GitHub-ready
- Marked as unmaintained / community project
- No warranty
- Clear notice that Hansmaker should NOT be contacted about this bridge
