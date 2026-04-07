#!/usr/bin/env python3
"""
Minimal BJJCZ USB emulator — stabilize the connection first.

No threads, no laser, no translation. Just:
  1. Set up FunctionFS with correct endpoints
  2. Read 12-byte commands from LightBurn
  3. Send 8-byte responses immediately
  4. Log everything

Usage:
    sudo bash setup_gadget.sh
    sudo python3 usb_test_clean.py
    # Then from Windows: usbip attach, open LightBurn
"""

import os
import struct
import sys
import time

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FFS_EP0 = "/dev/ffs-bjjcz/ep0"
GADGET_UDC = "/sys/kernel/config/usb_gadget/bjjcz/UDC"
UDC_NAME = "dummy_udc.0"

# BJJCZ status flags
READY = 0x0020
BUSY  = 0x0004

# Known single-command opcodes (from galvoplotter)
OPCODES = {
    0x0004: "EnableLaser",
    0x0007: "GetVersion",
    0x0009: "GetSerialNo",
    0x000A: "GetListStatus",
    0x000C: "GetPositionXY",
    0x000D: "GotoXY",
    0x0015: "WriteCorTable",
    0x0016: "SetControlMode",
    0x0017: "SetDelayMode",
    0x001B: "SetLaserMode",
    0x001C: "SetTiming",
    0x001D: "SetStandby",
    0x0025: "ReadPort",
    0x0040: "Reset",
}


# ─────────────────────────────────────────────────────────────────────────────
# FunctionFS setup
# ─────────────────────────────────────────────────────────────────────────────

def make_ep_desc(addr, max_pkt):
    """7-byte USB endpoint descriptor."""
    return struct.pack('<BBBBHB', 7, 5, addr, 0x02, max_pkt, 0)


def setup_ffs():
    """Write FunctionFS descriptors, bind UDC. Returns (ep0_fd, ep_count).

    Tries to create endpoints matching the real BJJCZ board:
      EP OUT 0x02, EP IN 0x88

    FunctionFS assigns addresses sequentially per direction:
      OUT: 0x01, 0x02, 0x03, ...
      IN:  0x81, 0x82, 0x83, ..., 0x88

    To get IN at 0x88 we need 8 IN endpoints. We create 2 OUT + 8 IN = 10 total.
    Only ep1 (second OUT = 0x02) and ep9 (eighth IN = 0x88) are used.
    The rest are dummy padding.
    """
    MAGIC_V2 = 3
    HAS_FS = 1
    HAS_HS = 2
    STRINGS_MAGIC = 2

    # We need: OUT 0x01(dummy), OUT 0x02(commands), IN 0x81-0x87(dummy), IN 0x88(status)
    num_out = 2
    num_in = 8
    num_eps = num_out + num_in

    intf = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, num_eps, 0xFF, 0xFF, 0xFF, 0)

    # Full-speed descriptors
    fs = intf
    for i in range(1, num_out + 1):       # OUT 0x01, 0x02
        fs += make_ep_desc(i, 64)
    for i in range(1, num_in + 1):        # IN 0x81 .. 0x88
        fs += make_ep_desc(0x80 | i, 64)

    # High-speed descriptors (same layout, 512-byte packets)
    hs = intf
    for i in range(1, num_out + 1):
        hs += make_ep_desc(i, 512)
    for i in range(1, num_in + 1):
        hs += make_ep_desc(0x80 | i, 512)

    desc_count = 1 + num_eps  # 1 interface + N endpoints
    total_len = 20 + len(fs) + len(hs)
    header = struct.pack('<IIIII', MAGIC_V2, total_len, HAS_FS | HAS_HS,
                         desc_count, desc_count)

    str_blob = struct.pack('<IIIH', STRINGS_MAGIC, 16, 1, 0x0409) + b'\x00\x00'

    print(f"Descriptors: {num_out} OUT + {num_in} IN = {num_eps} endpoints")
    print(f"  OUT addresses: {', '.join(f'0x{i:02x}' for i in range(1, num_out+1))}")
    print(f"  IN  addresses: {', '.join(f'0x{0x80|i:02x}' for i in range(1, num_in+1))}")

    fd = os.open(FFS_EP0, os.O_RDWR)
    try:
        os.write(fd, header + fs + hs)
        os.write(fd, str_blob)
    except OSError as e:
        os.close(fd)
        print(f"FAILED to write descriptors: {e}")
        sys.exit(1)

    print("Descriptors written OK")

    # Bind UDC
    with open(GADGET_UDC, 'w') as f:
        f.write(UDC_NAME)
    print(f"UDC bound: {UDC_NAME}")

    # List created endpoints
    ffs_dir = os.path.dirname(FFS_EP0)
    eps = sorted(f for f in os.listdir(ffs_dir) if f.startswith('ep'))
    print(f"Endpoints created: {', '.join(eps)}")

    return fd, num_eps


# ─────────────────────────────────────────────────────────────────────────────
# Command response
# ─────────────────────────────────────────────────────────────────────────────

def make_response(opcode, busy=False):
    """Build 8-byte response for a single JCZ command.

    Format: 4x u16 LE = (echo, word1, word2, status_flags)
    """
    status = READY
    if busy:
        status |= BUSY

    if opcode == 0x0009:  # GetSerialNo
        return struct.pack('<4H', 0x0009, 0x1234, 0x5678, status)
    elif opcode == 0x0007:  # GetVersion
        return struct.pack('<4H', 0x0007, 0x0502, 0x0000, status)
    elif opcode == 0x000A:  # GetListStatus
        return struct.pack('<4H', 0x000A, 0x0000, 0x0000, status)
    elif opcode == 0x000C:  # GetPositionXY
        return struct.pack('<4H', 0x000C, 0x8000, 0x8000, status)
    else:
        return struct.pack('<4H', opcode, 0x0000, 0x0000, status)


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def run(ep0_fd, num_eps):
    """Main loop: read commands, send responses. Reconnects on USB disconnect."""
    ffs_dir = os.path.dirname(FFS_EP0)

    # Figure out which ep files to use
    # ep0 = control, ep1 = first OUT (0x01, dummy), ep2 = second OUT (0x02, commands)
    # ep3-ep9 = IN 0x81-0x87 (dummy), ep10 = IN 0x88 (status)
    # FunctionFS names them ep0, ep1, ep2, ... ep10
    ep_out_path = os.path.join(ffs_dir, "ep2")   # OUT 0x02
    ep_in_path  = os.path.join(ffs_dir, f"ep{num_eps}")  # IN 0x88 (last endpoint)

    print(f"\nUsing: OUT={ep_out_path}  IN={ep_in_path}")
    print("Waiting for LightBurn to connect...\n")

    count = 0
    while True:
        # Open endpoints (blocks until USB host connects for OUT)
        try:
            ep_out = os.open(ep_out_path, os.O_RDONLY)
            ep_in  = os.open(ep_in_path, os.O_WRONLY)
        except OSError as e:
            print(f"  Open failed: {e} — retrying in 2s")
            time.sleep(2)
            continue

        print("  Endpoints open — connection active")

        try:
            while True:
                # Read one or more 12-byte commands
                data = os.read(ep_out, 4096)
                if not data:
                    print("  Empty read — disconnected")
                    break

                # Process each 12-byte command
                offset = 0
                while offset + 12 <= len(data):
                    cmd = data[offset:offset + 12]
                    offset += 12

                    opcode = struct.unpack_from('<H', cmd, 0)[0]

                    if opcode == 0x0000:
                        continue  # NOP

                    count += 1
                    name = OPCODES.get(opcode, f"0x{opcode:04x}")
                    ts = time.strftime("%H:%M:%S")

                    if opcode < 0x8000:
                        # Single command — respond with 8 bytes
                        resp = make_response(opcode)
                        try:
                            os.write(ep_in, resp)
                            print(f"  [{ts}] #{count:>4} {name:20s} -> {resp.hex()}")
                        except OSError as e:
                            print(f"  [{ts}] #{count:>4} {name:20s} -> WRITE FAILED: {e}")
                            break
                    else:
                        # List/batch command — no response needed
                        p1 = struct.unpack_from('<H', cmd, 2)[0]
                        p2 = struct.unpack_from('<H', cmd, 4)[0]
                        print(f"  [{ts}] #{count:>4} {name:20s} p1=0x{p1:04x} p2=0x{p2:04x}")

        except OSError as e:
            print(f"  Endpoint error: {e}")

        # Clean up for reconnect
        try: os.close(ep_out)
        except: pass
        try: os.close(ep_in)
        except: pass
        print("  Disconnected — waiting for reconnect...\n")
        time.sleep(0.5)


def main():
    if not os.path.exists(FFS_EP0):
        print("ERROR: Run 'sudo bash setup_gadget.sh' first")
        sys.exit(1)

    print("=" * 60)
    print("BJJCZ USB Emulator — Clean Test")
    print("=" * 60)

    ep0_fd, num_eps = setup_ffs()

    try:
        run(ep0_fd, num_eps)
    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        # Unbind UDC
        try:
            with open(GADGET_UDC, 'w') as f:
                f.write("")
        except: pass
        os.close(ep0_fd)


if __name__ == '__main__':
    main()
