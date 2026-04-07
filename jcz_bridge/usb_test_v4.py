#!/usr/bin/env python3
"""
BJJCZ emulator v4 — debug IN endpoint.

Writes continuously to IN endpoint in a tight loop.
If Windows can read from IN, the transport works.
If not, there's a fundamental FunctionFS/USB/IP issue with IN.
"""

import os
import struct
import sys
import time
import threading

FFS_EP0 = "/dev/ffs-bjjcz/ep0"
FFS_DIR = "/dev/ffs-bjjcz"
GADGET_UDC = "/sys/kernel/config/usb_gadget/bjjcz/UDC"
UDC_NAME = "dummy_udc.0"

READY = 0x0020


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


def out_reader(ep_out_path):
    """Read and discard OUT data."""
    while True:
        try:
            fd = os.open(ep_out_path, os.O_RDONLY)
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                opcode = struct.unpack_from('<H', data, 0)[0]
                print(f"  OUT: 0x{opcode:04x} ({len(data)} bytes)")
            os.close(fd)
        except OSError as e:
            print(f"  OUT error: {e}")
        time.sleep(0.5)


def in_writer(ep_in_path):
    """Continuously write status to IN."""
    resp = struct.pack('<4H', 0x0007, 0x0502, 0x0000, READY)
    count = 0
    while True:
        try:
            fd = os.open(ep_in_path, os.O_WRONLY)
            print(f"  IN endpoint opened for writing")
            while True:
                n = os.write(fd, resp)
                count += 1
                if count % 100 == 0:
                    print(f"  IN: wrote {count} responses ({n} bytes each)")
                # Don't sleep — let USB/IP pull as fast as it can
            os.close(fd)
        except OSError as e:
            print(f"  IN error: {e}")
        time.sleep(0.5)


if __name__ == '__main__':
    if not os.path.exists(FFS_EP0):
        print("Run: sudo bash setup_gadget.sh"); sys.exit(1)

    print("=== BJJCZ v4 — IN write debug ===")
    fd = setup()

    ep_out_path = os.path.join(FFS_DIR, "ep1")
    ep_in_path  = os.path.join(FFS_DIR, "ep2")

    # Run OUT reader in background
    t_out = threading.Thread(target=out_reader, args=(ep_out_path,), daemon=True)
    t_out.start()

    # Run IN writer in foreground
    try:
        in_writer(ep_in_path)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        try:
            with open(GADGET_UDC, 'w') as f: f.write("")
        except: pass
        os.close(fd)
