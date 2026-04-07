#!/usr/bin/env python3
"""
BJJCZ USB Emulator using raw_gadget.
EP OUT 0x02, EP IN 0x88 — matching real BJJCZ hardware.
"""

import os, sys, struct, fcntl, time, threading

def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def _IO(t, nr):      return _IOC(0, t, nr, 0)
def _IOW(t, nr, sz): return _IOC(1, t, nr, sz)
def _IOR(t, nr, sz): return _IOC(2, t, nr, sz)
def _IOWR(t, nr, sz): return _IOC(3, t, nr, sz)

T = ord('U')
INIT        = _IOW(T, 0, 257)
RUN         = _IO(T, 1)
EVENT_FETCH = _IOR(T, 2, 65544)
EP0_WRITE   = _IOW(T, 3, 65544)
EP_ENABLE   = _IOW(T, 5, 9)
EP_WRITE    = _IOW(T, 7, 65544)
EP_READ     = _IOWR(T, 8, 65544)
CONFIGURE   = _IO(T, 9)
VBUS_DRAW   = _IOW(T, 10, 4)
EP0_STALL   = _IO(T, 12)

VID, PID = 0x9588, 0x9899
READY = 0x0020

OPCODES = {
    0x0004: "EnableLaser", 0x0007: "GetVersion", 0x0009: "GetSerialNo",
    0x000A: "GetListStatus", 0x000C: "GetPositionXY", 0x000D: "GotoXY",
    0x0016: "SetControlMode", 0x0017: "SetDelayMode", 0x001B: "SetLaserMode",
    0x001C: "SetTiming", 0x001D: "SetStandby", 0x0040: "Reset",
}

# USB descriptors
DEVICE_DESC = struct.pack('<BBHBBBBHHHBBBB',
    18, 1, 0x0200, 0xFF, 0xFF, 0xFF, 64, VID, PID, 0x0005, 1, 2, 3, 1)
INTF_DESC = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xFF, 0xFF, 0xFF, 0)
EP_OUT_DESC = struct.pack('<BBBBHB', 7, 5, 0x02, 0x02, 512, 0)
EP_IN_DESC  = struct.pack('<BBBBHB', 7, 5, 0x88, 0x02, 512, 0)
CONFIG_DESC = struct.pack('<BBHBBBBB', 9, 2, 32, 1, 1, 0, 0x80, 250) + INTF_DESC + EP_OUT_DESC + EP_IN_DESC
DEVQUAL = struct.pack('<BBHBBBBBB', 10, 6, 0x0200, 0xFF, 0xFF, 0xFF, 64, 1, 0)
LANGID = struct.pack('<BBH', 4, 3, 0x0409)
def _ms(s):
    e = s.encode('utf-16-le')
    return struct.pack('BB', 2+len(e), 3) + e
STRINGS = {0: LANGID, 1: _ms("Beijing JCZ Technology"), 2: _ms("BJJCZ Fiber Laser"), 3: _ms("D1ULTRA-BRIDGE-001")}

def jcz_respond(opcode):
    if opcode == 0x0009: return struct.pack('<4H', 0x0009, 0x1234, 0x5678, READY)
    elif opcode == 0x0007: return struct.pack('<4H', 0x0007, 0x0502, 0x0000, READY)
    elif opcode == 0x000A: return struct.pack('<4H', 0x000A, 0x0000, 0x0000, READY)
    elif opcode == 0x000C: return struct.pack('<4H', 0x000C, 0x8000, 0x8000, READY)
    else: return struct.pack('<4H', opcode, 0x0000, 0x0000, READY)


def main():
    if not os.path.exists('/dev/raw-gadget'):
        print("ERROR: load raw_gadget module"); sys.exit(1)

    print("=== BJJCZ Raw Gadget (OUT 0x02, IN 0x88) ===")

    # We run in a loop — each iteration handles one USB lifecycle
    # (connect -> enumerate -> configure -> bulk I/O -> disconnect)
    while True:
        fd = os.open('/dev/raw-gadget', os.O_RDWR)

        # Init + Run
        buf = bytearray(257)
        buf[0:9] = b'dummy_udc'; buf[128:139] = b'dummy_udc.0'; buf[256] = 3
        fcntl.ioctl(fd, INIT, bytes(buf))
        fcntl.ioctl(fd, RUN)
        print("\nGadget started, enumerating...", flush=True)

        ep_out_h = -1
        ep_in_h = -1
        bulk_stop = threading.Event()
        cmd_count = 0

        def ep0_write(data):
            b = bytearray(8 + len(data))
            struct.pack_into('<HHI', b, 0, 0, 0, len(data))
            b[8:8+len(data)] = data
            fcntl.ioctl(fd, EP0_WRITE, bytes(b))

        def bulk_loop():
            nonlocal cmd_count
            print("  Bulk handler running", flush=True)
            while not bulk_stop.is_set():
                try:
                    rb = bytearray(8 + 4096)
                    struct.pack_into('<HHI', rb, 0, ep_out_h, 0, 4096)
                    fcntl.ioctl(fd, EP_READ, rb, True)
                    length = struct.unpack_from('<I', rb, 4)[0]
                    data = bytes(rb[8:8+length])
                    if not data: continue

                    off = 0
                    while off + 12 <= len(data):
                        opcode = struct.unpack_from('<H', data, off)[0]
                        off += 12
                        if opcode == 0: continue
                        cmd_count += 1
                        name = OPCODES.get(opcode, f"0x{opcode:04x}")
                        if opcode < 0x8000:
                            resp = jcz_respond(opcode)
                            wb = bytearray(8 + len(resp))
                            struct.pack_into('<HHI', wb, 0, ep_in_h, 0, len(resp))
                            wb[8:8+len(resp)] = resp
                            fcntl.ioctl(fd, EP_WRITE, bytes(wb))
                            ts = time.strftime("%H:%M:%S")
                            if cmd_count <= 20 or cmd_count % 100 == 0:
                                print(f"  [{ts}] #{cmd_count} {name} -> {resp.hex()}", flush=True)
                        else:
                            ts = time.strftime("%H:%M:%S")
                            print(f"  [{ts}] #{cmd_count} {name} (list)", flush=True)
                except OSError as e:
                    if not bulk_stop.is_set():
                        print(f"  Bulk error: {e}", flush=True)
                    break

        try:
            while True:
                ebuf = bytearray(65544)
                struct.pack_into('<II', ebuf, 0, 0, 8)
                fcntl.ioctl(fd, EVENT_FETCH, ebuf, True)
                etype = struct.unpack_from('<I', ebuf, 0)[0]
                elen = struct.unpack_from('<I', ebuf, 4)[0]

                if etype == 5:  # RESET
                    print("  USB reset", flush=True)
                    bulk_stop.set()
                    ep_out_h = -1; ep_in_h = -1
                    continue
                elif etype == 6:  # DISCONNECT
                    print("  USB disconnect — restarting", flush=True)
                    bulk_stop.set()
                    break
                elif etype != 2:  # not CONTROL
                    continue

                data = bytes(ebuf[8:8+elen])
                if elen < 8: continue
                rt, rq, val, idx, ln = struct.unpack_from('<BBHHH', data, 0)
                dt, di = val >> 8, val & 0xFF

                try:
                    if rt == 0x80 and rq == 0x06:  # GET_DESCRIPTOR
                        if dt == 1: ep0_write(DEVICE_DESC[:ln])
                        elif dt == 2: ep0_write(CONFIG_DESC[:ln])
                        elif dt == 3: ep0_write(STRINGS.get(di, LANGID)[:ln])
                        elif dt == 6: ep0_write(DEVQUAL[:ln])
                        else: fcntl.ioctl(fd, EP0_STALL)
                    elif rt == 0x00 and rq == 0x09:  # SET_CONFIGURATION
                        b1 = bytearray(EP_OUT_DESC + b'\x00\x00')
                        fcntl.ioctl(fd, EP_ENABLE, b1, True)
                        b2 = bytearray(EP_IN_DESC + b'\x00\x00')
                        fcntl.ioctl(fd, EP_ENABLE, b2, True)
                        fcntl.ioctl(fd, VBUS_DRAW, struct.pack('<I', 250))
                        fcntl.ioctl(fd, CONFIGURE)
                        try: ep0_write(b'')
                        except: pass
                        ep_out_h = 1; ep_in_h = 5
                        print(f"  CONFIGURED (OUT=h1 IN=h5)", flush=True)
                        bulk_stop.clear()
                        threading.Thread(target=bulk_loop, daemon=True).start()
                    elif rt == 0x80 and rq == 0x00:  # GET_STATUS
                        ep0_write(struct.pack('<H', 0)[:ln])
                    elif rq in (0x01, 0x03, 0x0B):  # CLEAR/SET_FEATURE, SET_INTERFACE
                        try: ep0_write(b'')
                        except: pass
                    else:
                        fcntl.ioctl(fd, EP0_STALL)
                except OSError as e:
                    print(f"  ep0 error: {e}", flush=True)

        except KeyboardInterrupt:
            print(f"\nStopped. {cmd_count} commands.")
            os.close(fd)
            sys.exit(0)
        except OSError as e:
            print(f"  Fatal: {e}", flush=True)

        bulk_stop.set()
        os.close(fd)
        print("  Restarting in 1s...", flush=True)
        time.sleep(1)


if __name__ == '__main__':
    main()
