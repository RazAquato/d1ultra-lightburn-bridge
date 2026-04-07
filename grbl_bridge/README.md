# GRBL Bridge — LightBurn GRBL Mode to D1 Ultra

> **STATUS: Working (v2.3) — line engraving confirmed on real hardware.**
> No longer actively developed. See `jcz_bridge/` for the active approach.

This bridge translates LightBurn's GRBL G-code output to the D1 Ultra's proprietary
binary protocol over TCP. It runs on the same machine as LightBurn (Windows, Mac, or Linux)
and requires no additional hardware.

```
LightBurn  --GRBL/TCP-->  d1ultra_bridge.py (localhost:9023)  --D1 Ultra/TCP-->  Laser (192.168.12.1:6000)
```

## Why This Approach Is No Longer Active

GRBL mode in LightBurn is designed for CNC-style gantry lasers. It works for basic
line engraving but lacks features that galvo/JCZ mode provides:

- No live framing (red dot preview)
- No split marking
- No cylinder correction
- Limited speed/power control compared to JCZ native parameters
- No fill/raster support (never implemented)

The `jcz_bridge/` approach emulates a BJJCZ galvo controller, which gives LightBurn
access to all of its JCZ features. That is the active development path.

## When to Use This

Use the GRBL bridge if:
- You want the simplest possible setup (no Linux VM needed)
- You only need basic line/outline engraving
- You don't need live framing or advanced galvo features

## Files

| File | Description |
|------|-------------|
| `d1ultra_bridge.py` | v2.3 — confirmed working, monolithic |
| `NOTTESTED_d1ultra_bridge_v2.4.py` | v2.4 — adds WORKSPACE framing, uses protocol library (untested) |

Both files use `d1ultra_protocol.py` from the repository root.

## Quick Start

1. Connect D1 Ultra via USB (creates virtual network adapter)
2. `python grbl_bridge/d1ultra_bridge.py --listen-port 9023`
3. LightBurn: Devices -> GRBL -> Ethernet/TCP -> 127.0.0.1:9023

See the [main README](../README.md) for full setup instructions.

## What Works

- Line engraving (SVG shapes, text, any vector content)
- Multiple layers (serialized as separate jobs)
- Power and speed control from LightBurn's layer settings
- Auto-reconnect after laser idle timeout
- Z-axis jog commands
- Interactive console (lights, buzzer, focus, gate, Z-axis, autofocus)

## What Doesn't

- Fill/raster engraving (not implemented)
- Live framing (GRBL limitation)
- Real-time job progress feedback
