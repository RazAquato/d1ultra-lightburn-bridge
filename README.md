# D1 Ultra LightBurn Bridge

> **STATUS: Working — line engraving confirmed on real hardware (v2.3).**
> The bridge successfully sends jobs to the D1 Ultra and triggers execution.
> Tested with SVG shapes and text at various power/speed settings.

A translation bridge that lets [LightBurn](https://lightburnsoftware.com/) control the **Hansmaker D1 Ultra** laser engraver.

The D1 Ultra uses a proprietary binary protocol over TCP — not GRBL. This bridge translates between the two:

```
LightBurn  --GRBL/TCP-->  d1ultra-bridge.py (localhost:9023)  --D1 Ultra/TCP-->  Laser (192.168.12.1:6000)
```

---

## Important Notices

### Do NOT Contact Hansmaker About This Bridge

**Hansmaker did not create this software and has no involvement with it.** Please do not contact Hansmaker support, forums, or social media with questions or bug reports related to this bridge. They are a small team and have enough to deal with supporting their own products.

- Issues with this bridge -> [open a GitHub issue here](https://github.com/RazAquato/d1ultra-lightburn-bridge/issues)
- Issues with M+ or D1 Ultra hardware -> contact Hansmaker through their official channels

**This project is not affiliated with, endorsed by, or supported by Hansmaker in any way.**

### Disclaimer

**This software is provided as-is, with absolutely no warranty.** It was reverse-engineered from Wireshark captures of the official M+ software and may be incomplete or incorrect. Use at your own risk. The authors are not responsible for any damage to your laser, materials, property, or anything else. Always wear appropriate laser safety equipment and never leave a running laser unattended.

### Community Project

This project is provided to the community as a starting point. Pull requests are welcome. You are welcome to fork it and build on it.

The protocol documentation here is freely available for anyone to use — including LightBurn's team if they ever add Hansmaker support.

---

## Requirements

- Python 3.8+
- No external packages (stdlib only)
- Hansmaker D1 Ultra connected via USB
- LightBurn (any version with GRBL support)

## Quick Start

### 1. Connect the laser

Plug in the D1 Ultra via USB. It creates a virtual network adapter.

```
ping 192.168.12.1
```

If the adapter doesn't get an IP, unplug and replug the USB cable.

### 2. Start the bridge

```
python d1ultra-bridge.py --listen-port 9023
```

### 3. Set up LightBurn

1. Open LightBurn
2. Devices -> Create Manually
3. Select **GRBL** (1.1f or higher)
4. Connection: **Ethernet/TCP**
5. Address: **127.0.0.1**, Port: **9023**
6. Origin: **Front Left**
7. Disable "Auto home your laser on startup"

### 4. Engrave

Design your job in LightBurn, set power/speed in the Cuts/Layers panel, and hit Start. The bridge translates the G-code into D1 Ultra protocol commands automatically.

---

## What Works

- Line engraving (SVG shapes, text, any vector content)
- Multiple layers (each layer is serialized as a separate job)
- Power and speed control from LightBurn's layer settings
- Auto-reconnect after laser idle timeout
- Z-axis jog commands
- Interactive console for manual laser control (lights, buzzer, focus, gate, Z-axis)
- Replay mode for diagnostics (send raw M+ pcapng bytes to laser)

## Known Limitations

- **Fill/raster engraving**: Not yet implemented — only line/outline mode works
- **Live framing**: Not available (GRBL limitation — no red dot boundary trace)
- **Autofocus**: Partially decoded, may not work reliably
- **Job monitoring**: No real-time progress feedback from the laser during engraving

---

## Project Structure

```
d1ultra-bridge.py          Current bridge (v2.3 — confirmed working)
PROTOCOL.md                Full D1 Ultra binary protocol specification
CLAUDE.md                  Development log and remaining investigation notes
captures/                  26 stripped pcapng files from M+ sessions
```

---

## How It Works

The D1 Ultra expects this job sequence over TCP port 6000:

1. **DEVICE_INFO** (0x0018) — query device
2. **PRE_JOB** (0x0005) — signal upcoming job
3. **QUERY_14** (0x0014, sub=0x02) — pre-job setup
4. **JOB_UPLOAD** (0x0002) — job name + PNG preview thumbnail
5. **WORKSPACE** (0x0009) — bounding box of the design
6. For each path/shape:
   - **JOB_SETTINGS** (0x0000, msg_type=0) — power, speed, passes, frequency
   - **PATH_DATA** (0x0001, msg_type=0) — coordinates centered on design midpoint
7. **JOB_CONTROL** (0x0003) — host tells laser to execute (laser echoes back as confirmation)
8. **JOB_FINISH** (0x0004) — finalize

The bridge parses LightBurn's GRBL G-code, collects path data, centers coordinates around the bounding-box midpoint, and sends the above sequence.

### Key Discovery

The critical blocker in v1 was that the **host must send JOB_CONTROL (0x0003)** to initiate execution. Earlier documentation incorrectly stated the laser sends this. Pcapng analysis of every successful M+ capture proved the host always initiates. The v1 bridge waited for the laser to send it, which never happened.

---

## Interactive Console

The bridge provides an interactive `d1ultra>` prompt alongside the LightBurn connection.

| Command | Description |
|---------|-------------|
| `light on/off` | Fill light |
| `buzzer on/off` | Buzzer |
| `focus on/off` | Focus laser pointer |
| `gate on/off` | Safety gate |
| `home` | Home/reset all motors |
| `up/down <mm>` | Move Z-axis (default 5mm) |
| `autofocus` | IR autofocus (3-probe average) |
| `ping` | Check laser connection |
| `status` / `info` | Show bridge/device state |
| `help` / `quit` | Help or shut down |

LightBurn console commands also work: `$FOCUS`, `$FOCUS OFF`, `$AUTOFOCUS`/`$AF`, `$H`

## Command-Line Options

```
python d1ultra-bridge.py [options]

  --laser-ip IP       D1 Ultra IP (default: 192.168.12.1)
  --laser-port PORT   D1 Ultra TCP port (default: 6000)
  --listen-host HOST  Bridge listen address (default: 0.0.0.0)
  --listen-port PORT  Bridge listen port (default: 9023)
  --verbose, -v       Debug logging
  --replay PCAPNG     Replay M+ capture directly to laser (diagnostic)
```

### Replay mode (diagnostic)

To verify that M+'s exact bytes trigger the laser:

```
python d1ultra-bridge.py --replay captures/svg_from_m+.pcapng
```

---

## Protocol Documentation

The D1 Ultra's binary protocol has been extensively reverse-engineered and documented.
See **[PROTOCOL.md](PROTOCOL.md)** for the full specification including packet format, CRC algorithm, command reference, and job sequence.

The `captures/` directory contains 26 pcapng files from M+ sessions, stripped to only laser protocol traffic (TCP port 6000). Open in [Wireshark](https://www.wireshark.org/) and filter by `tcp.port == 6000`.

---

## Version History

### v2.3 (current)
- Auto-reconnect after laser idle timeout
- Job serialization lock (multi-layer jobs queue properly instead of stomping on each other)

### v2.1
- Fixed ACK feedback loop (unsolicited 0x0013/0x0015 responses no longer flood)
- PNG preview size matched to ~6KB

### v2.0
- **Host sends JOB_CONTROL (0x0003)** — the #1 blocker fix
- Added PRE_JOB (0x0005), WORKSPACE (0x0009), QUERY_14(0x02) pre-job commands
- JOB_SETTINGS unknown field corrected to -1.0
- WORKSPACE 42-byte payload (was 40)
- PNG preview ~6KB (was 286 bytes)
- ACK unsolicited laser messages
- G1 S0 duplicate point filter
- 10ms packet pacing
- Replay mode for diagnostics
- Full M+ startup sequence
- Coordinate centering around bounding-box midpoint

### v1
- Initial bridge — connected, uploaded jobs, received ACKs, but jobs never executed

---

## Contributing

Contributions are welcome, especially:

- Adding fill/raster engrave support
- Improving autofocus reliability
- Testing with different D1 Ultra firmware versions
- Adding real-time job progress monitoring
- Improving the RPi Zero JCZ bridge concept (see CLAUDE.md)

## How It Was Built

The D1 Ultra protocol was entirely reverse-engineered from Wireshark captures of M+ communicating with the laser over TCP port 6000. No Hansmaker proprietary code was used, decompiled, or referenced.

## Support

If you find this project useful and want to support the reverse-engineering work:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/razaqato)

## License

MIT License — see [LICENSE](LICENSE) for the full text.

Free to use, modify, fork, and distribute. No warranty of any kind.
