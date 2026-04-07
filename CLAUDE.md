# D1 Ultra LightBurn Bridge — Project Context for Claude Code

## What This Is

A reverse-engineered bridge between LightBurn and the Hansmaker D1 Ultra laser engraver.
Two approaches exist; the JCZ bridge is the active development path.

## Active: JCZ Bridge (`jcz_bridge/`)

Emulates a BJJCZ galvo controller (VID 0x9588, PID 0x9899) using Linux USB gadget
framework. Exported over USB/IP to Windows. LightBurn connects in JCZFiber mode.

```
Windows (LightBurn) --USB/IP--> Linux VM (configfs gadget + jcz_bridge.py) --TCP--> D1 Ultra
```

### Key technical details
- **Endpoint 0x88**: Achieved by modifying `dummy_hcd.ko` — commented out ep1in/ep6in/ep11in/ep2in-bulk
  so `usb_ep_autoconfig()` picks ep8in-bulk (address 0x88). Source in `jcz_bridge/kernel/`.
- **configfs + FunctionFS**: Kernel handles EP0 (USB/IP compatible). raw_gadget was tried
  and FAILED — EP0 handled in userspace causes stalls when USB/IP forwards control transfers.
- **Protocol**: JCZ commands are 12 bytes (u16 opcode + 5x u16 params), sent in 3072-byte
  chunks (256 commands). Single commands (opcode < 0x8000) need 8-byte response.
  List commands (opcode >= 0x8000) are batched, no per-command response.

### Status (2026-04-07)
- LightBurn connects, detects device, shows "Ready"
- Framing (TRAVEL commands) and engrave jobs (SET_POWER, MARK) captured successfully
- 250K+ protocol exchanges, zero errors
- NOT YET: actual laser driving (JCZ->D1 Ultra translation). Waiting for USB cable.

### Startup
```bash
sudo bash jcz_bridge/start_configfs.sh
# Windows: usbip.exe attach --remote <vm-ip> --busid 2-1
```

## Stable: GRBL Bridge (`grbl_bridge/`)

Translates LightBurn GRBL G-code to D1 Ultra protocol. Works (v2.3), but limited —
no live framing, no fill/raster, no galvo features. No longer actively developed.

```
LightBurn --GRBL/TCP--> d1ultra_bridge.py (localhost:9023) --TCP--> D1 Ultra
```

## Shared: Protocol Library (root)

- `d1ultra_protocol.py` — standalone D1 Ultra API (connect, identify, engrave, preview)
- `PROTOCOL.md` — full binary protocol specification

Both bridges use this library. It has no dependencies on bridge-specific code.

## D1 Ultra Protocol Quick Reference

- **Connection**: USB RNDIS virtual ethernet -> 192.168.12.1:6000 TCP
- **Packet**: `0x0A0A + u16 len + u16 pad + u16 seq + u16 pad + u16 msg_type + u16 cmd + payload + u16 CRC-16/MODBUS + 0x0D0D`
- **Heartbeat**: STATUS (0x0000) every ~2s, laser disconnects after ~10s idle
- **Job sequence**: DEVICE_INFO -> PRE_JOB -> QUERY_14 -> JOB_UPLOAD -> WORKSPACE -> [SETTINGS + PATH] x N -> JOB_CONTROL -> JOB_FINISH
- **Critical**: HOST must send JOB_CONTROL (0x0003) to trigger execution

## File Structure

```
d1ultra_protocol.py          Shared protocol library
PROTOCOL.md                  Protocol specification
jcz_bridge/                  Active: BJJCZ emulator over USB/IP
  kernel/dummy_hcd.c         Modified kernel module (ep8in-bulk)
  jcz_bridge.py              Main bridge
  jcz_protocol.py            JCZ command parser
  start_configfs.sh          One-command startup
grbl_bridge/                 Stable: GRBL translation bridge
  d1ultra_bridge.py          v2.3 (working)
wireshark_captures/          26 pcapng files from M+ sessions
```

## Critical Discovery Log

### Endpoint 0x88 (2026-04-07)
LightBurn's JCZ driver hardcodes endpoint 0x88 for IN. Stock dummy_hcd gives 0x81.
Fix: modify dummy_hcd to remove earlier IN bulk endpoints, forcing ep8in-bulk allocation.
raw_gadget gives 0x88 but breaks USB/IP (EP0 stalls). configfs + modified dummy_hcd works.

### JOB_CONTROL 0x0003 (v2.0)
The HOST must send this to trigger job execution. v1 waited for the laser to send it.

### ACK Feedback Loop (v2.1)
Unsolicited 0x0013/0x0015 responses must be ACK'd exactly once per (cmd, seq) pair.
