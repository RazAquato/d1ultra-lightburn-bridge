#!/usr/bin/env python3
"""
BJJCZ emulator v5 — pre-queued IN responses.

The trick: write the response to IN BEFORE the host reads it.
Sequence:
  1. Pre-queue a default response on IN
  2. Read command from OUT
  3. Immediately queue the NEXT response on IN
  4. Go to step 2

This way there's always data waiting on IN when the host reads.
"""

import os
import struct
import sys
import time

FFS_EP0 = "/dev/ffs-bjjcz/ep0"
FFS_DIR = "/dev/ffs-bjjcz"
GADGET_UDC = "/sys/kernel/config/usb_gadget/bjjcz/UDC"
UDC_NAME = "dummy_udc.0"

READY = 0x0020

OPCODES = {
    0x0004: "EnableLaser", 0x0007: "GetVersion", 0x0009: "GetSerialNo",
    0x000A: "GetListStatus", 0x000C: "GetPositionXY", 0x000D: "GotoXY",
    0x0015: "WriteCorTable", 0x0016: "SetControlMode", 0x0017: "SetDelayMode",
    0x001B: "SetLaserMode", 0x001C: "SetTiming", 0x001D: "SetStandby",
    0x0025: "ReadPort", 0x0040: "Reset",
}


def make_ep(addr, max_pkt):
    return struct.pack('<BBBBHB', 7, 5, addr, 0x02, max_pkt, 0)


def setup():
    intf = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xFF, 0xFF, 0xFF, 0)
    fs = intf + make_ep(0x01, 64) + make_ep(0x82, 64)
    hs = intf + make_ep(0x01, 512) + make_ep(0x82, 512)
    total = 20 + len(fs) + len(hs)
    header = struct.pack('<IIIII', 3, total, 3, 3, 3)
    strings = struct.pack('<IIIH', 2, 16, 1, 0x0409) + b'\x00\x00'
    fd = os.open(FFS_EP0, os.O_RDWR)
    os.write(fd, header + fs + hs)
    os.write(fd, strings)
    with open(GADGET_UDC, 'w') as f:
        f.write(UDC_NAME)
    print(f"Gadget ready. Endpoints: {sorted(os.listdir(FFS_DIR))}")
    return fd


def respond(opcode):
    if opcode == 0x0009:
        return struct.pack('<4H', 0x0009, 0x1234, 0x5678, READY)
    elif opcode == 0x0007:
        return struct.pack('<4H', 0x0007, 0x0502, 0x0000, READY)
    elif opcode == 0x000A:
        return struct.pack('<4H', 0x000A, 0x0000, 0x0000, READY)
    elif opcode == 0x000C:
        return struct.pack('<4H', 0x000C, 0x8000, 0x8000, READY)
    else:
        return struct.pack('<4H', opcode, 0x0000, 0x0000, READY)


# Default response (GetSerialNo — first thing LightBurn asks for)
DEFAULT_RESP = struct.pack('<4H', 0x0009, 0x1234, 0x5678, READY)


def run(ep0_fd):
    ep_out_path = os.path.join(FFS_DIR, "ep1")
    ep_in_path  = os.path.join(FFS_DIR, "ep2")

    print(f"OUT={ep_out_path}  IN={ep_in_path}")
    print("Pre-queued IN mode. Waiting for connection...\n")

    n = 0
    while True:
        try:
            fd_out = os.open(ep_out_path, os.O_RDONLY)
            fd_in  = os.open(ep_in_path, os.O_WRONLY)
        except OSError as e:
            print(f"  open: {e}")
            time.sleep(2)
            continue

        print("  CONNECTED")

        try:
            # Pre-queue the first response BEFORE any host read
            os.write(fd_in, DEFAULT_RESP)
            print(f"  Pre-queued default response: {DEFAULT_RESP.hex()}")

            last_opcode = 0x0009

            while True:
                data = os.read(fd_out, 4096)
                if not data:
                    print("  empty read")
                    break

                off = 0
                while off + 12 <= len(data):
                    opcode = struct.unpack_from('<H', data, off)[0]
                    off += 12
                    if opcode == 0:
                        continue

                    n += 1
                    name = OPCODES.get(opcode, f"0x{opcode:04x}")

                    if opcode < 0x8000:
                        # Queue response for THIS command (host will read it next)
                        resp = respond(opcode)
                        try:
                            os.write(fd_in, resp)
                        except OSError:
                            pass
                        if opcode != last_opcode or n <= 5:
                            ts = time.strftime("%H:%M:%S")
                            print(f"  [{ts}] #{n:>4} {name:20s} -> {resp.hex()}")
                            last_opcode = opcode
                    else:
                        p1 = struct.unpack_from('<H', data, off - 10)[0]
                        p2 = struct.unpack_from('<H', data, off - 8)[0]
                        ts = time.strftime("%H:%M:%S")
                        print(f"  [{ts}] #{n:>4} {name:20s} p1=0x{p1:04x} p2=0x{p2:04x}")

        except OSError as e:
            print(f"  error: {e}")

        try: os.close(fd_out)
        except: pass
        try: os.close(fd_in)
        except: pass
        print("  DISCONNECTED\n")
        time.sleep(0.5)


if __name__ == '__main__':
    if not os.path.exists(FFS_EP0):
        print("Run: sudo bash setup_gadget.sh"); sys.exit(1)

    print("=== BJJCZ v5 — pre-queued IN ===")
    fd = setup()
    try:
        run(fd)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        try:
            with open(GADGET_UDC, 'w') as f: f.write("")
        except: pass
        os.close(fd)
