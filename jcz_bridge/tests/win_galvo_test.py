#!/usr/bin/env python3
"""Test our emulated BJJCZ device with galvoplotter.

Tests two things:
  1. Can galvoplotter connect at all? (endpoint 0x88 vs 0x81)
  2. If not, try with corrected endpoint address.
"""
import struct, sys

# Test 1: galvoplotter's built-in connection (uses 0x88)
print("=== Test 1: galvoplotter native connect (endpoint 0x88) ===")
try:
    from galvo.usb_connection import USBConnection
    conn = USBConnection()
    result = conn.open(0)
    if result >= 0:
        print(f"Connected! Sending GetSerialNo...")
        conn.write(0, struct.pack('<6H', 0x0009, 0, 0, 0, 0, 0))
        resp = conn.read(0)
        print(f"Response: {bytes(resp).hex()}")
        conn.close(0)
    else:
        print(f"Connection failed: {result}")
except Exception as e:
    print(f"FAILED: {e}")

print()

# Test 2: direct pyusb with endpoint 0x81 (what our device actually has)
print("=== Test 2: pyusb direct with endpoint 0x81 ===")
try:
    import usb.core
    dev = usb.core.find(idVendor=0x9588, idProduct=0x9899)
    if not dev:
        print("Device not found"); sys.exit(1)
    try:
        if dev.is_kernel_driver_active(0): dev.detach_kernel_driver(0)
    except: pass
    try: dev.set_configuration()
    except: pass

    cmd = struct.pack('<6H', 0x0009, 0, 0, 0, 0, 0)
    dev.write(0x02, cmd, timeout=1000)
    print("OUT write OK")

    # Try reading from 0x81 (our actual endpoint)
    try:
        resp = dev.read(0x81, 8, timeout=1000)
        print(f"IN 0x81 response: {bytes(resp).hex()} - SUCCESS!")
    except Exception as e:
        print(f"IN 0x81 failed: {e}")

    # Also try 0x88 for comparison
    dev.write(0x02, cmd, timeout=1000)
    try:
        resp = dev.read(0x88, 8, timeout=1000)
        print(f"IN 0x88 response: {bytes(resp).hex()}")
    except Exception as e:
        print(f"IN 0x88 failed: {e}")

except Exception as e:
    print(f"FAILED: {e}")
