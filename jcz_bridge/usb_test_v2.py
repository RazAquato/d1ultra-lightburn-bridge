#!/usr/bin/env python3
"""
BJJCZ USB emulator v2 — with IN endpoint pre-feeding.

LightBurn's protocol:
  1. Sends 12 bytes on OUT (command)
  2. Reads 8 bytes on IN (response)

But LightBurn can also read IN without sending OUT first (status polling).
So we need to always have data ready on IN.

Architecture:
  - Thread 1: keeps writing 8-byte GetVersion/READY responses to IN
  - Main loop: reads OUT commands, updates what the IN thread sends
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
BUSY  = 0x0004

OPCODES = {
    0x0004: "EnableLaser",
    0x0007: "GetVersion",
    0x0009: "GetSerialNo",
    0x000A: "GetListStatus",
    0x000C: "GetPositionXY",
    0x000D: "GotoXY",
    0x0016: "SetControlMode",
    0x0017: "SetDelayMode",
    0x001B: "SetLaserMode",
    0x001C: "SetTiming",
    0x001D: "SetStandby",
    0x0040: "Reset",
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
    print(f"Gadget bound. Endpoints: {sorted(os.listdir(FFS_DIR))}")
    return fd


def respond(opcode):
    """8-byte response for known single commands."""
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


class BJJCZEmulator:
    def __init__(self):
        self.running = True
        self.fd_in = -1
        self.last_response = struct.pack('<4H', 0x0007, 0x0502, 0x0000, READY)
        self.response_lock = threading.Lock()
        self.connected = False

    def in_writer_thread(self):
        """Continuously write status to IN so LightBurn always has data."""
        while self.running:
            if self.fd_in < 0 or not self.connected:
                time.sleep(0.1)
                continue
            try:
                with self.response_lock:
                    data = self.last_response
                os.write(self.fd_in, data)
                time.sleep(0.005)  # 5ms between writes
            except OSError:
                time.sleep(0.1)

    def set_response(self, data):
        with self.response_lock:
            self.last_response = data

    def run(self):
        ep_out_path = os.path.join(FFS_DIR, "ep1")
        ep_in_path  = os.path.join(FFS_DIR, "ep2")

        # Start IN writer thread
        t = threading.Thread(target=self.in_writer_thread, daemon=True)
        t.start()

        print(f"OUT={ep_out_path}  IN={ep_in_path}")
        print("Waiting for connection...\n")

        n = 0
        while self.running:
            # Open endpoints
            try:
                fd_out = os.open(ep_out_path, os.O_RDONLY)
                self.fd_in = os.open(ep_in_path, os.O_WRONLY)
            except OSError as e:
                print(f"  open: {e}")
                time.sleep(2)
                continue

            self.connected = True
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
                        if opcode == 0:
                            continue

                        n += 1
                        ts = time.strftime("%H:%M:%S")
                        name = OPCODES.get(opcode, f"0x{opcode:04x}")

                        if opcode < 0x8000:
                            resp = respond(opcode)
                            # Update what the IN thread sends AND write directly
                            self.set_response(resp)
                            try:
                                os.write(self.fd_in, resp)
                            except OSError:
                                pass
                            print(f"  [{ts}] #{n} {name:20s} -> {resp.hex()}")
                        else:
                            p1 = struct.unpack_from('<H', data, off - 10)[0]
                            p2 = struct.unpack_from('<H', data, off - 8)[0]
                            print(f"  [{ts}] #{n} {name:20s} p1=0x{p1:04x} p2=0x{p2:04x}")

            except OSError as e:
                print(f"  error: {e}")

            self.connected = False
            self.fd_in = -1
            try: os.close(fd_out)
            except: pass
            try: os.close(self.fd_in)
            except: pass
            print("  DISCONNECTED\n")
            time.sleep(0.5)


if __name__ == '__main__':
    if not os.path.exists(FFS_EP0):
        print("Run: sudo bash setup_gadget.sh")
        sys.exit(1)

    print("=== BJJCZ Emulator v2 ===")
    fd = setup()
    emu = BJJCZEmulator()
    try:
        emu.run()
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        emu.running = False
        try:
            with open(GADGET_UDC, 'w') as f: f.write("")
        except: pass
        os.close(fd)
