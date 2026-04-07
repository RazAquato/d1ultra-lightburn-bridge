#!/usr/bin/env python3
"""
BJJCZ emulator v3 — OUT only, no IN writes.

Discovery: LightBurn NEVER reads from bulk IN.
It only sends on bulk OUT and checks the transfer completion status.

This version just reads OUT, logs everything, does NOT write to IN.
Goal: see if LightBurn progresses past GetSerialNo when we don't
mess with the IN endpoint at all.
"""

import os
import struct
import sys
import time

FFS_EP0 = "/dev/ffs-bjjcz/ep0"
FFS_DIR = "/dev/ffs-bjjcz"
GADGET_UDC = "/sys/kernel/config/usb_gadget/bjjcz/UDC"
UDC_NAME = "dummy_udc.0"

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


def run(ep0_fd):
    ep_out_path = os.path.join(FFS_DIR, "ep1")

    print(f"Reading from: {ep_out_path}")
    print("NO writes to IN endpoint.")
    print("Waiting for connection...\n")

    n = 0
    while True:
        try:
            fd_out = os.open(ep_out_path, os.O_RDONLY)
        except OSError as e:
            print(f"  open: {e}")
            time.sleep(2)
            continue

        print("  CONNECTED")
        try:
            while True:
                data = os.read(fd_out, 4096)
                if not data:
                    print("  empty read")
                    break

                ts = time.strftime("%H:%M:%S")
                off = 0
                while off + 12 <= len(data):
                    cmd = data[off:off + 12]
                    opcode = struct.unpack_from('<H', cmd, 0)[0]
                    off += 12
                    if opcode == 0:
                        continue

                    n += 1
                    name = OPCODES.get(opcode, f"0x{opcode:04x}")
                    params = struct.unpack_from('<HHHHH', cmd, 2)
                    print(f"  [{ts}] #{n:>4} {name:20s} "
                          f"p={params[0]:04x},{params[1]:04x},{params[2]:04x},{params[3]:04x},{params[4]:04x} "
                          f"raw={cmd.hex()}")

        except OSError as e:
            print(f"  error: {e}")

        try: os.close(fd_out)
        except: pass
        print("  DISCONNECTED\n")
        time.sleep(0.5)


if __name__ == '__main__':
    if not os.path.exists(FFS_EP0):
        print("Run: sudo bash setup_gadget.sh")
        sys.exit(1)

    print("=== BJJCZ v3 — OUT only, no IN ===")
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
