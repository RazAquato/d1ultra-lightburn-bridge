#!/usr/bin/env python3
"""
Simplest possible BJJCZ emulator. 2 endpoints, no tricks.

Just OUT 0x02 + IN 0x81. Read command, write response, log everything.
If this doesn't work, the endpoint address isn't the problem.
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


def make_ep(addr, max_pkt):
    return struct.pack('<BBBBHB', 7, 5, addr, 0x02, max_pkt, 0)


def setup():
    """Minimal 2-endpoint setup. Returns ep0 fd."""
    intf = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xFF, 0xFF, 0xFF, 0)

    fs = intf + make_ep(0x01, 64) + make_ep(0x82, 64)
    hs = intf + make_ep(0x01, 512) + make_ep(0x82, 512)

    total = 20 + len(fs) + len(hs)
    header = struct.pack('<IIIII', 3, total, 3, 3, 3)
    strings = struct.pack('<IIIH', 2, 16, 1, 0x0409) + b'\x00\x00'

    fd = os.open(FFS_EP0, os.O_RDWR)
    os.write(fd, header + fs + hs)
    os.write(fd, strings)
    print("Descriptors: OUT 0x01, IN 0x82 (kernel will assign 0x02, 0x81)")

    with open(GADGET_UDC, 'w') as f:
        f.write(UDC_NAME)
    print(f"UDC bound. Endpoints: {sorted(os.listdir(FFS_DIR))}")
    return fd


def respond(opcode):
    """8-byte response: echo opcode + status with READY."""
    if opcode == 0x0009:
        return struct.pack('<4H', 0x0009, 0x1234, 0x5678, READY)
    elif opcode == 0x0007:
        return struct.pack('<4H', 0x0007, 0x0502, 0x0000, READY)
    else:
        return struct.pack('<4H', opcode, 0x0000, 0x0000, READY)


def run(ep0_fd):
    ep_out_path = os.path.join(FFS_DIR, "ep1")  # OUT
    ep_in_path  = os.path.join(FFS_DIR, "ep2")  # IN

    print(f"OUT={ep_out_path}  IN={ep_in_path}")
    print("Waiting for connection...\n")

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
            while True:
                data = os.read(fd_out, 4096)
                if not data:
                    print("  empty read")
                    break

                off = 0
                while off + 12 <= len(data):
                    opcode = struct.unpack_from('<H', data, off)[0]
                    off += 12
                    if opcode == 0: continue

                    n += 1
                    ts = time.strftime("%H:%M:%S")

                    if opcode < 0x8000:
                        resp = respond(opcode)
                        os.write(fd_in, resp)
                        print(f"  [{ts}] #{n} opcode=0x{opcode:04x} -> {resp.hex()}")
                    else:
                        print(f"  [{ts}] #{n} opcode=0x{opcode:04x} (list cmd)")

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
        print("Run: sudo bash setup_gadget.sh")
        sys.exit(1)

    print("=== Simple BJJCZ Test ===")
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
