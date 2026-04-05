# D1 Ultra LightBurn Bridge

> **STATUS: v2 bridge with critical protocol fix — ready for testing.**
> Pcapng analysis revealed the host must send JOB_CONTROL (0x0003) to start execution.
> Previous versions waited for the laser to send it, which never happened.
> The v2 bridge corrects this along with 11 other protocol fixes.

A translation bridge that lets [LightBurn](https://lightburnsoftware.com/) control the **Hansmaker D1 Ultra** laser engraver.

The D1 Ultra uses a proprietary binary protocol over TCP — not GRBL. This project provides two bridge approaches:

```
Approach 1 — GRBL Bridge (works today):
  LightBurn  --GRBL/TCP-->  Bridge (localhost:9023)  --D1 Ultra/TCP-->  Laser

Approach 2 — RPi Zero JCZ Bridge (experimental, full galvo features):
  LightBurn  --JCZ/USB-->  Raspberry Pi Zero  --D1 Ultra/TCP-->  Laser
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

## Why Two Approaches: GRBL vs JCZ

Using GRBL as the base means you don't get the full galvo feature set. A JCZ galvo controller profile would be a better starting point for full support.

| | GRBL Bridge | JCZ Bridge (RPi Zero) |
|--|-------------|----------------------|
| **How it works** | LightBurn speaks GRBL over TCP, bridge translates | Pi Zero presents as BJJCZ USB device, translates JCZ commands |
| **Hardware needed** | Just the D1 Ultra + PC | D1 Ultra + Raspberry Pi Zero 2W + PC |
| **Live framing** | No | Yes |
| **Split marking** | No | Yes |
| **Cylinder correction** | No | Yes |
| **Status** | v2 ready for testing | Experimental skeleton, needs hardware testing |
| **Complexity** | Single Python script | USB gadget + Linux kernel config + translation |

The GRBL bridge is the practical option today — it covers basic line/fill engraving. The JCZ bridge is the path to full galvo support, but requires a Raspberry Pi Zero and more development work.

---

## Project Structure

```
d1ultra_bridge.py          Original v1 bridge (historical reference)
NOTTESTED_d1ultra_bridge_v2.py       Current bridge with all protocol fixes
PROTOCOL.md                Full D1 Ultra binary protocol specification
wireshark_captures/        26 pcapng files from M+ sessions
NOTDONE_rpi_zero_bridge/           Raspberry Pi Zero JCZ bridge (experimental)
  rpi_jcz_bridge.py        Main JCZ-to-D1 Ultra translator
  jcz_commands.py          JCZ/BJJCZ command definitions and parser
  setup_gadget.sh          Linux USB gadget configuration
  README.md                RPi-specific documentation
```

---

## Current Status

### The Root Cause (found via pcapng analysis)

The v1 bridge uploaded jobs successfully — every packet was ACK'd — but the laser never executed them. Automated analysis of all 26 Wireshark captures revealed the root cause:

**The host must SEND JOB_CONTROL (0x0003) to the laser to trigger execution.**

In every successful M+ capture, the host sends 0x0003 (empty payload), and the laser echoes it back as confirmation. The v1 bridge waited for the laser to send 0x0003 first, which never happened because the laser was waiting for the host.

Earlier documentation incorrectly stated "the host must never send 0x0003." This was wrong.

### v2 Bridge Fixes

| Fix | What changed | Why it matters |
|-----|-------------|----------------|
| **HOST sends 0x0003** | Host now initiates job execution | This was the #1 blocker |
| Unknown field = -1.0 | Was 0.0, M+ always sends -1.0 | May affect job validation |
| WORKSPACE 42-byte payload | Was 40 bytes, M+ sends 42 (2-byte pad) | Matches M+ exactly |
| PRE_JOB (0x0005) | Not sent in v1 | M+ sends before JOB_UPLOAD |
| QUERY_14(0x02) pre-job | Not sent in v1 | M+ sends before job |
| PNG preview ~6 KB | Was 0 or 286 bytes | M+ sends 6-14 KB PNGs |
| ACK unsolicited messages | 0x0013/0x0014/0x0015 ignored in v1 | May gate job execution |
| G1 S0 duplicate filter | Extra closing point from LightBurn | Makes paths match M+ |
| 10ms packet pacing | No pacing in v1 | Matches M+ inter-packet timing |
| Replay mode | New diagnostic tool | Sends raw M+ bytes for testing |
| Full startup sequence | Matches M+ order | Ensures correct laser state |
| Coordinate centering | Absolute coords in v1 capture | M+ centers around bbox midpoint |

### What the GRBL bridge cannot do

The GRBL approach doesn't support galvo-specific LightBurn features:
- Live framing (red dot traces the job boundary)
- Split marking (mark multiple areas in one job)
- Cylinder correction (compensate for curved surfaces)

For these features, see the [RPi Zero JCZ bridge](NOTDONE_rpi_zero_bridge/) (experimental).

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
python NOTTESTED_d1ultra_bridge_v2.py --listen-port 9023
```

### 3. Set up LightBurn

1. Open LightBurn
2. Devices -> Create Manually
3. Select **GRBL** (1.1f or higher)
4. Connection: **Ethernet/TCP**
5. Address: **127.0.0.1**, Port: **9023**
6. Origin: **Front Left**
7. Disable "Auto home your laser on startup"

### Replay mode (diagnostic)

To test if M+'s exact bytes trigger the laser:

```
python NOTTESTED_d1ultra_bridge_v2.py --replay wireshark_captures/svg_from_m+.pcapng
```

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

LightBurn console commands: `$FOCUS`, `$FOCUS OFF`, `$AUTOFOCUS`/`$AF`, `$H`

## Command-Line Options

```
python NOTTESTED_d1ultra_bridge_v2.py [options]

  --laser-ip IP       D1 Ultra IP (default: 192.168.12.1)
  --laser-port PORT   D1 Ultra TCP port (default: 6000)
  --listen-host HOST  Bridge listen address (default: 0.0.0.0)
  --listen-port PORT  Bridge listen port (default: 9023)
  --verbose, -v       Debug logging
  --replay PCAPNG     Replay M+ capture directly to laser (diagnostic)
```

---

## Protocol Documentation

The D1 Ultra's binary protocol has been extensively reverse-engineered and documented.
See **[PROTOCOL.md](PROTOCOL.md)** for the full specification.

The `wireshark_captures/` directory contains 26 pcapng files from M+ sessions.
Open in [Wireshark](https://www.wireshark.org/) and filter by `tcp.port == 6000`.

---

## RPi Zero JCZ Bridge (experimental)

A Raspberry Pi Zero can present itself as a BJJCZ galvo controller (VID 0x9588, PID 0x9899)
using Linux's USB gadget framework. LightBurn detects it as a JCZ device and enables full
galvo features. The Pi translates JCZ commands into D1 Ultra TCP packets.

See **[NOTDONE_NOTDONE_rpi_zero_bridge/README.md](NOTDONE_NOTDONE_rpi_zero_bridge/README.md)** for details.

This is experimental and untested. It requires a Pi Zero 2W with both USB ports available.

---

## Contributing

Contributions are welcome, especially:

- Testing the v2 bridge and reporting results
- Improving the RPi Zero JCZ bridge
- Adding fill/raster engrave support to the GRBL bridge
- Improving autofocus reliability
- Testing with different D1 Ultra firmware versions

## How It Was Built

The D1 Ultra protocol was entirely reverse-engineered from Wireshark captures of M+ communicating with the laser over TCP port 6000. No Hansmaker proprietary code was used, decompiled, or referenced.

The critical JOB_CONTROL fix was found by writing an automated pcapng analyzer that parsed all 26 captures and discovered the host-initiates-execution pattern across every successful M+ job.

## Support

If you find this project useful and want to support the reverse-engineering work:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/razaqato)

## License

MIT License — see [LICENSE](LICENSE) for the full text.

Free to use, modify, fork, and distribute. No warranty of any kind.
