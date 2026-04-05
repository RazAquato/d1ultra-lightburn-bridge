# D1 Ultra LightBurn Bridge

A translation bridge that lets [LightBurn](https://lightburnsoftware.com/) control the **Hansmaker D1 Ultra** laser engraver.

The D1 Ultra uses a proprietary binary protocol over TCP — not GRBL. This bridge sits between LightBurn and the laser, translating GRBL commands into the D1 Ultra's native protocol in real time.

```
LightBurn  ──GRBL/TCP──▶  Bridge (localhost:9023)  ──D1 Ultra/TCP──▶  Laser (192.168.12.1:6000)
```

## Disclaimer

**This software is provided as-is, with absolutely no warranty.** It was reverse-engineered from Wireshark captures of the official M+ software and may be incomplete or incorrect. Use at your own risk. The authors are not responsible for any damage to your laser, materials, property, or anything else. Always wear appropriate laser safety equipment and never leave a running laser unattended.

This is a community project and is **not maintained**. You are welcome to fork it and build on it.

## Requirements

- Python 3.8+
- No external packages (stdlib only)
- Hansmaker D1 Ultra
- LightBurn (any version with GRBL support)

## Quick start

### 1. Connect the laser

Plug in the D1 Ultra via USB. It creates a virtual network adapter. Verify the connection:

```
ping 192.168.12.1
```

If the adapter doesn't get an IP, unplug and replug the USB cable.

### 2. Start the bridge

```
python d1ultra_bridge.py --listen-port 9023
```

You should see:

```
D1 Ultra <-> LightBurn GRBL Bridge
Connected to D1 Ultra at 192.168.12.1:6000
Device: D1 Ultra
Firmware: 1.2.260303.101331
Motor calibration data received (283 bytes)
Laser ping OK — ready to accept jobs
GRBL server listening on 0.0.0.0:9023

──────────────────────────────────────────────────
  Console ready — type 'help' for commands
  (LightBurn can connect at the same time)
──────────────────────────────────────────────────
d1ultra>
```

### 3. Set up LightBurn

1. Open LightBurn
2. Devices > Create Manually
3. Select **GRBL** (top option, 1.1f or higher)
4. Connection: **Ethernet/TCP**
5. Address: **127.0.0.1**, Port: **9023**
6. Origin: **Front Left**
7. Disable "Auto home your laser on startup"

## Interactive console

The bridge provides an interactive `d1ultra>` prompt that runs alongside the LightBurn connection. You can type commands at any time (except while a job is actively engraving).

### Peripheral controls

| Command | Description |
|---|---|
| `light on` | Turn on the fill light |
| `light off` | Turn off the fill light |
| `buzzer on` | Turn on the buzzer |
| `buzzer off` | Turn off the buzzer |
| `focus on` | Turn on the focus laser pointer |
| `focus off` | Turn off the focus laser pointer |
| `gate on` | Enable the safety gate |
| `gate off` | Disable the safety gate |

### Motion controls

| Command | Description |
|---|---|
| `home` | Home/reset all motors (takes a few seconds) |
| `up <mm>` | Move Z-axis up (default 5mm) |
| `down <mm>` | Move Z-axis down (default 5mm) |
| `autofocus` | Run IR autofocus sequence (3 probes averaged) |

### Status commands

| Command | Description |
|---|---|
| `ping` | Check if laser is responding |
| `status` | Show laser status and bridge state |
| `info` | Show device name and firmware version |
| `help` | List all commands |
| `quit` | Shut down the bridge |

## LightBurn console commands

You can also send commands from LightBurn's Console tab (bottom panel):

| Command | Description |
|---|---|
| `$FOCUS` | Turn on the focus laser pointer |
| `$FOCUS OFF` | Turn off the focus laser pointer |
| `$AUTOFOCUS` or `$AF` | Run IR autofocus (3-probe average) |
| `$H` | Home/reset motors |

## Command-line options

```
python d1ultra_bridge.py [options]

  --laser-ip IP       D1 Ultra IP (default: 192.168.12.1)
  --laser-port PORT   D1 Ultra TCP port (default: 6000)
  --listen-host HOST  Bridge listen address (default: 0.0.0.0)
  --listen-port PORT  Bridge listen port (default: 23)
  --verbose, -v       Debug logging
```

Note: ports below 1024 (like the default 23) may require administrator privileges. Use `--listen-port 9023` or similar to avoid this.

## Supported features

- Line engraving
- Fill engraving
- Diode laser
- IR laser (1064nm) with autofocus
- Adjustable power, speed, passes, and frequency
- Z-axis movement
- Motor homing
- Arc interpolation (G2/G3 arcs linearized to line segments)
- Peripheral control (light, buzzer, focus laser, safety gate)
- Auto-reconnect if laser connection drops

## Known limitations

- Camera feed is not available through LightBurn (the D1 Ultra camera uses a proprietary protocol, not USB video)
- Rotary table accessory not yet supported
- WiFi connection not supported (USB only)
- The autofocus sequence was reverse-engineered from IR laser captures and may need adjustment
- No real-time job progress feedback (LightBurn shows progress based on G-code lines sent, not actual laser position)

## How it was built

The D1 Ultra protocol was entirely reverse-engineered from Wireshark captures of the Hansmaker M+ software communicating with the laser over TCP port 6000. See [PROTOCOL.md](PROTOCOL.md) for the full protocol specification including packet structure, CRC algorithm, command IDs, and payload formats.

## Protocol overview

Every packet follows this structure:

```
0A 0A                    Magic header
[u16 LE total length]    Includes header + payload + CRC + terminator
[u16 LE padding]         Always 0x0000
[u16 LE sequence]        Incrementing per message
[u16 LE padding]         Always 0x0000
[u16 LE message type]    0x0001 = request/response, 0x0002 = notification
[u16 LE command ID]      See PROTOCOL.md for full list
[payload bytes]          Command-specific, variable length
[u16 LE CRC-16]          CRC-16/MODBUS over bytes after magic
0D 0D                    Terminator
```

All values are little-endian. Coordinates, speed, and power use IEEE 754 double-precision floats.

## Support

If you find this project useful and want to support the work that went into reverse-engineering this protocol:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/razaqato)

## License

MIT License — see [LICENSE](LICENSE) for the full text.

Free to use, modify, fork, and distribute. No warranty of any kind.
