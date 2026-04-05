# Hansmaker D1 Ultra — Protocol Specification

**Reverse-engineered from Wireshark captures of M+ software communication**
**April 2026**

This document describes how the Hansmaker D1 Ultra laser engraver communicates with host software. The entire protocol was reverse-engineered by capturing and analyzing TCP traffic between the official M+ software and the laser. No Hansmaker proprietary code was decompiled or referenced.

---

## How the D1 Ultra Communicates

The D1 Ultra does **not** use GRBL, G-code, or any standard laser/CNC protocol. Instead, it speaks a proprietary binary protocol over TCP.

### Physical connection

The laser connects to the host computer via USB, which creates a **virtual Ethernet adapter** (not a serial port). The laser assigns itself `192.168.12.1` and the host gets an IP in the `192.168.12.x` range via DHCP.

### Network layer

- **Protocol:** TCP
- **Port:** 6000 (laser listens)
- **Discovery:** The laser responds to mDNS queries for `hl_device.local` (unicast DNS to `192.168.12.1:53` and multicast mDNS to `224.0.0.251:5353`)
- **Keepalive:** The laser has a ~10 second idle timeout. The host must send STATUS heartbeat pings every ~2 seconds to keep the connection alive, including during job execution.

### Why this matters

Because the D1 Ultra uses a proprietary protocol instead of GRBL, industry-standard laser software like LightBurn cannot communicate with it directly. Users are limited to Hansmaker's own M+ software. The bridge in this repository translates between GRBL (what LightBurn speaks) and the D1 Ultra's binary protocol.

---

## Packet Structure

Every message follows this framing. All multi-byte values are **little-endian**.

```
Offset  Size  Type    Description
------  ----  ------  -----------------------------------------
0x00    2     magic   Always 0x0A 0x0A
0x02    2     u16     Total packet length (header + payload + CRC + terminator)
0x04    2     u16     Padding (always 0x0000)
0x06    2     u16     Sequence number (incrementing per message)
0x08    2     u16     Padding (always 0x0000)
0x0A    2     u16     Message type (0=job data, 1=request/response, 2=notification)
0x0C    2     u16     Command ID
0x0E    var   bytes   Payload (command-specific)
-4      2     u16     CRC-16 checksum (little-endian)
-2      2     magic   Always 0x0D 0x0D (terminator)
```

Minimum packet size: 18 bytes (header + empty payload + CRC + terminator).

Responses from the laser echo the sequence number and command ID. A 20-byte response with 2-byte payload `0x0000` is a generic ACK.

---

## CRC-16 Algorithm

**Algorithm: CRC-16/MODBUS** (verified against 10 independent packets — 10/10 matches)

- **Polynomial:** 0xA001 (reflected 0x8005)
- **Init value:** 0xFFFF
- **Computed over:** Bytes 2 through end-of-payload (everything after the 2-byte magic `0x0A0A`, excluding the CRC field and terminator `0x0D0D`)
- **Stored as:** u16 little-endian at `[length-4 : length-2]`

```python
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
```

---

## Command Reference

| CMD | Direction | msg_type | Description |
|-----|-----------|----------|-------------|
| `0x0000` | Both | 1 | Status query / heartbeat |
| `0x0000` | PC->Laser | 0 | Job settings (per-path parameters) |
| `0x0001` | PC->Laser | 0 | Path data (coordinate segments) |
| `0x0002` | PC->Laser | 1 | Job upload header (name + PNG preview) |
| `0x0003` | **Laser->PC** | 1 | Job control — laser signals "ready to execute" |
| `0x0004` | PC->Laser | 1 | Finalize job (job name) |
| `0x0005` | PC->Laser | 1 | Pre-job initialization |
| `0x0006` | PC->Laser | 1 | Device identification (returns device name) |
| `0x0009` | PC->Laser | 1 | Workspace / preview configuration |
| `0x000B` | PC->Laser | 1 | Motor reset / calibration (returns 283 bytes) |
| `0x000D` | PC->Laser | 1 | Camera capture (returns ~258KB image) |
| `0x000E` | PC->Laser | 1 | Peripheral control (light, buzzer, laser, gate) |
| `0x000F` | PC->Laser | 1 | Z-axis control / autofocus Z-set |
| `0x0012` | PC->Laser | 1 | Autofocus measurement request |
| `0x0013` | PC->Laser | 1 | Device query (state) |
| `0x0014` | PC->Laser | 1 | Device query / setup |
| `0x0015` | PC->Laser | 1 | Device query (firmware?) |
| `0x0018` | PC->Laser | 1 | Device info (serial/HW version) |
| `0x001E` | PC->Laser | 1 | Firmware version query |

---

## Device Discovery and Startup

When M+ connects to the laser, this sequence runs:

```
1. DHCP    Host obtains IP on the USB virtual network adapter
2. mDNS    Host queries 'hl_device.local' -> resolves to 192.168.12.1
3. TCP     SYN to 192.168.12.1:6000
4. 0x0006  Device identification -> laser responds "D1 Ultra"
5. 0x0000  Status heartbeat
6. 0x0018  Device info query (serial/HW version)
7. 0x001E  Firmware version -> "1.2.260303.101331"
8. 0x000B  Motor reset/calibration (returns 283 bytes of boundary data)
9. 0x0013  Query device state
10. 0x0015  Query device state
```

---

## Job Execution Sequence

This is how M+ sends a job to the laser.

### Diode laser job

```
1. HOST->LASER:  cmd 0x0018              Query device info
2. HOST->LASER:  cmd 0x0002              Upload job header (name + PNG preview)
3. For EACH path/shape in the design:
   a. HOST->LASER: cmd 0x0000 (msg_type=0)  Set job parameters for this path
   b. HOST->LASER: cmd 0x0001 (msg_type=0)  Send path coordinate segments
4. LASER->HOST:  cmd 0x0003              Laser signals "paths received, ready"
5. HOST->LASER:  cmd 0x0004              Finalize job (send job name)
6. LASER->HOST:  cmd 0x0000              Status updates (running / complete)
```

### Critical protocol rules

- **JOB_SETTINGS before EACH path:** The host must send cmd 0x0000 (msg_type=0) before EACH cmd 0x0001, not just once. M+ sends one SETTINGS+PATH pair per path/shape.
- **Never send 0x0003 to the laser:** The host must NEVER send cmd 0x0003 to the laser. This causes an uncontrolled Z-axis descent. Only the laser sends 0x0003 to the host.
- **msg_type matters:** JOB_SETTINGS and PATH_DATA use msg_type=0. All other commands use msg_type=1.

### IR laser job (additional steps)

Before the job upload, the IR sequence includes autofocus:

```
1-2.  Same as diode (DEVICE_INFO + JOB_DATA)
Pre:  cmd 0x000E [02, 00]    Select IR/focus laser (standby)
      cmd 0x0012              Request autofocus measurement (hw_id=0x1A8B)
        <- Laser returns Z-height measurement
      cmd 0x000F              Set Z-height from measurement
      (Repeat 3 times for averaging)
      cmd 0x000E [02, 01]    Activate IR module
3-6.  Same as diode (but JOB_SETTINGS byte[28]=0 for IR source)
Post: cmd 0x000E [02, 01]    Re-activate after job
```

Note: The autofocus device_id is `0x1A8B` (LE: `8b 1a`), which is different from the DEVICE_INFO device_id `0x8B1B` (LE: `1b 8b`).

---

## Command Details

### CMD 0x0000 — Job Settings (msg_type=0, 37-byte payload)

Sent before EACH PATH_DATA packet.

```
Offset  Size  Type  Field            Values
------  ----  ----  ---------------  -----------------------
0       4     u32   Passes           1, 2, 3, ...
4       8     f64   Speed            mm/min (e.g., 500.0)
12      8     f64   Frequency        kHz (e.g., 20.0, 50.0)
20      8     f64   Power            0.0-1.0 (0.5 = 50%)
28      1     u8    Laser source     1=diode, 0=IR
29      8     f64   Unknown          Always -1.0
```

Confirmed by differential analysis across 4 captures:

| Capture | Passes | Speed | Freq | Power | Source |
|---------|--------|-------|------|-------|--------|
| Diode 50%/500/1 | 1 | 500.0 | 50.0 | 0.50 | 1 (diode) |
| Diode 100%/1000/2 | 2 | 1000.0 | 50.0 | 1.00 | 1 (diode) |
| IR 100%/1000/2/50kHz | 2 | 1000.0 | 50.0 | 1.00 | 0 (IR) |
| IR 100%/1000/2/20kHz | 2 | 1000.0 | **20.0** | 1.00 | 0 (IR) |

### CMD 0x0001 — Path Data (msg_type=0)

Variable-length coordinate segments.

```
Offset  Size    Type  Description
------  ------  ----  ------------------
0       4       u32   Segment count (N)
4       N*32    -     Segment array
```

Each segment is 32 bytes:

```
Offset  Size  Type  Description
------  ----  ----  ------------------
0       8     f64   X coordinate (mm)
8       8     f64   Y coordinate (mm)
16      16    -     Reserved (zeros)
```

**Coordinate system:** Coordinates are centered around the design's bounding-box midpoint, NOT absolute bed positions. A 20x20mm square at bed position (100,100)-(120,120) has center (110,110), so path coordinates range from (-10,-10) to (+10,+10).

M+ sends one PATH_DATA packet per path group (split at rapid moves). Each packet is ACK'd before the next pair is sent.

### CMD 0x0002 — Job Upload Header (msg_type=1)

```
Offset  Size   Description
------  -----  ------------------
0       256    Job name (null-terminated, zero-padded)
256     2      Padding (0x0000)
258     4      u32 LE: PNG data size
262     var    PNG image data (preview thumbnail)
```

M+ sends a ~6KB rendered preview PNG. The preview may be mandatory.

### CMD 0x0003 — Job Control (LASER->HOST only)

Empty payload. Sent BY the laser after all path data is received and processed. Signals the laser is ready to execute.

**The host must NEVER send this command.** Sending it causes uncontrolled Z-axis descent.

### CMD 0x0004 — Finalize Job

274-byte packet. Contains job name (256 bytes, zero-padded) + padding. Sent after receiving 0x0003 from laser.

### CMD 0x0006 — Device Identification

Empty request. Response:

```
Offset  Size  Type    Description
------  ----  ------  ------------------
0       2     u16     Status (0=OK)
2       30    string  Device name ("D1 Ultra")
```

### CMD 0x0009 — Workspace / Preview Configuration

40-byte payload with 5 doubles:

```
Offset  Size  Type  Description
------  ----  ----  ------------------
0       8     f64   Preview/move speed (mm/min)
8       8     f64   Bounding box X min (mm)
16      8     f64   Bounding box Y min (mm)
24      8     f64   Bounding box X max (mm)
32      8     f64   Bounding box Y max (mm)
```

### CMD 0x000B — Motor Reset / Calibration

Empty request. Response: 283 bytes of motor calibration / workspace boundary data.

```
Offset  Size  Type    Description
------  ----  ------  ------------------
0       4     u32     Status (0x00000000)
4       272   34*f64  Motor calibration doubles
```

The 34 doubles form 4 repeating regions (motor axes or workspace zones) containing boundary values, offsets, and scale factors. Mandatory at startup.

### CMD 0x000D — Camera Capture

Empty request. Response: ~258KB image data (format TBD, likely JPEG).

### CMD 0x000E — Peripheral Control

2-byte payload:

```
Byte 0: Module        Byte 1: State
0x00 = Fill light     0x00 = OFF
0x01 = Buzzer         0x01 = ON
0x02 = Focus laser
0x03 = Safety gate
```

Examples:
- Light on: `[0x00, 0x01]` / Light off: `[0x00, 0x00]`
- Buzzer on: `[0x01, 0x01]` / Buzzer off: `[0x01, 0x00]`
- Focus laser on: `[0x02, 0x01]` / Focus laser off: `[0x02, 0x00]`
- Safety gate enable: `[0x03, 0x00]` / disable: `[0x03, 0x01]`

### CMD 0x000F — Z-Axis Control

17-byte payload, dual mode:

```
Manual move (byte[0] = 0):
  0     1     u8    Mode: 0
  1     8     f64   Distance mm (positive=UP, negative=DOWN)
  9     4     u32   Parameter (4)
  13    4     -     Padding

Autofocus set (byte[0] = 1):
  0     1     u8    Mode: 1
  1     8     f64   Z-height mm (from cmd 0x0012)
  9     4     u32   Parameter (4)
  13    4     -     Padding
```

### CMD 0x0012 — Autofocus Measurement

20-byte request (u32=1 + 16 bytes zeros). Response:

```
Offset  Size  Type  Description
------  ----  ----  ------------------
0       2     u16   Status (0=OK)
2       4     u32   Count (1)
6       8     f64   Calibration value 1
14      8     f64   Calibration value 2
22      8     f64   Z-height measurement (mm)
```

### CMD 0x0014 — Device Query/Setup

Single-byte payload: `0x00` = query laser head type, `0x01` = query state, `0x02` = pre-job setup.

### CMD 0x0018 — Device Info Query

36-byte payload:

```
Offset  Size  Description
------  ----  ------------------
0       2     u16: Function code (0x0006)
2       2     u16: Device ID (0x1B8B)
4       32    Reserved zeros
```

### CMD 0x001E — Firmware Version Query

Empty request. Response:

```
Offset  Size  Type    Description
------  ----  ------  ------------------
0       2     u16     Status (0=OK)
2       4     u32     String length
6       var   string  Version (e.g., "1.2.260303.101331")
```

---

## Fill Engrave vs Line Engrave

Fill and line engrave use identical commands and settings. The only difference is the path data:

- **Line engrave:** Outline coordinates only (6 PATH_DATA packets for "hello")
- **Fill engrave:** Outline + parallel scan lines to fill the shape (~740 PATH_DATA packets for "hello")

The mode is determined entirely by the host software generating different paths.

---

## Open Questions

- [ ] The `Unknown = -1.0` field (byte 29-37) in job settings — possibly "Thickness" (M+ shows 2.00mm)?
- [ ] What does the frequency field control for diode mode? (PWM frequency? Always 50?)
- [ ] Reserved 16 bytes per path segment — power per point? Curve control points?
- [ ] Cutting vs engraving — different commands or just different parameters?
- [ ] WiFi configuration commands (not yet captured)
- [ ] Camera image format (JPEG? PNG? Raw?)

---

## Wireshark Captures

The `wireshark_captures/` directory contains the raw pcapng files used to reverse-engineer this protocol. These can be opened in [Wireshark](https://www.wireshark.org/) and filtered by `tcp.port == 6000`.

| Capture | Description |
|---------|-------------|
| `connect_and_motor_reset.pcapng` | Startup: device discovery, identification, motor calibration |
| `full_capture_connect_reset_motor.pcapng` | Extended startup sequence |
| `scan_connect_laser.pcapng` | mDNS discovery + TCP connect |
| `hello_engrave_test.pcapng` | M+ "hello" text engrave (line mode) |
| `hello_mark_line_engrave_*.pcapng` | M+ "hello" line engrave, 70% power, 400 speed |
| `hello_mark_fill_engrave_*.pcapng` | M+ "hello" fill engrave, 70% power, 400 speed |
| `svg_from_m+.pcapng` | M+ engraving a test SVG (reference for bridge comparison) |
| `svg_from_lightburn.pcapng` | Bridge attempt at same SVG (for comparison) |
| `ir_20.pcapng` | IR laser job at 20kHz |
| `ir_capture_world.pcapng` | IR laser "world" engrave |
| `world_engrave_test_*.pcapng` | Diode "world" engrave, 100% power, 1000 speed, 2 passes |
| `autofocus.pcapng` | IR autofocus sequence |
| `up-20mm.pcapng` / `down-20mm.pcapng` | Z-axis movement |
| `ligts_on_off.pcapng` | Fill light toggle |
| `buzzer_on_off.pcapng` | Buzzer toggle |
| `focus_laser_on_off.pcapng` | Focus laser pointer toggle |
| `enable_safety_gate_on_off.pcapng` | Safety gate toggle |
| `capture_image_from_camera.pcapng` | Camera image capture |
| `see_screenshot.pcapng` | Screenshot/preview capture |
| `preview_200.pcapng` / `preview_1000.pcapng` | Preview at different speeds |
| `mplus_asks_to_reset_motor.pcapng` | Motor reset request |
| `mplus_sends_reset_command.pcapng` | Motor reset command |
| `mplus_recieved_reset_ok_from_laser.pcapng` | Motor reset acknowledgment |
