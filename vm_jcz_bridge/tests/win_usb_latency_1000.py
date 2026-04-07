#!/usr/bin/env python3
"""USB/IP latency test — 1000 packets, output to file."""
import time, sys, statistics
try:
    import usb.core, usb.util
except ImportError:
    print("pip install pyusb"); sys.exit(1)

VID, PID = 0x9588, 0x9899
EP_OUT, EP_IN = 0x02, 0x81
N = 1000

dev = usb.core.find(idVendor=VID, idProduct=PID)
if not dev:
    print(f"Device {VID:04x}:{PID:04x} not found"); sys.exit(1)
try:
    if dev.is_kernel_driver_active(0): dev.detach_kernel_driver(0)
except: pass
try: dev.set_configuration()
except: pass

print(f"Device: {dev.manufacturer} {dev.product}")
out = open("output.txt", "w")

# Test 1: OUT only
out.write("=== Test 1: OUT only ===\n")
lat = []
data = b'\x09' + b'\x00' * 11
for i in range(N):
    t0 = time.perf_counter()
    try:
        dev.write(EP_OUT, data, timeout=2000)
        ms = (time.perf_counter() - t0) * 1000
        lat.append(ms)
        out.write(f"{i+1},{ms:.2f}\n")
    except Exception as e:
        out.write(f"{i+1},ERROR,{e}\n")

if lat:
    out.write(f"\nOUT stats: n={len(lat)} mean={statistics.mean(lat):.2f} "
              f"median={statistics.median(lat):.2f} min={min(lat):.2f} "
              f"max={max(lat):.2f} stdev={statistics.stdev(lat):.2f}\n\n")
    print(f"OUT: n={len(lat)} median={statistics.median(lat):.2f}ms "
          f"max={max(lat):.2f}ms")

# Test 2: OUT+IN round trip
out.write("=== Test 2: OUT+IN round trip ===\n")
lat2 = []
fails = 0
for i in range(N):
    t0 = time.perf_counter()
    try:
        dev.write(EP_OUT, data, timeout=2000)
        resp = dev.read(EP_IN, 64, timeout=2000)
        ms = (time.perf_counter() - t0) * 1000
        lat2.append(ms)
        out.write(f"{i+1},{ms:.2f},{len(resp)},{bytes(resp).hex()}\n")
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        fails += 1
        out.write(f"{i+1},{ms:.2f},FAIL,{e}\n")

if lat2:
    out.write(f"\nRT stats: n={len(lat2)} fails={fails} mean={statistics.mean(lat2):.2f} "
              f"median={statistics.median(lat2):.2f} min={min(lat2):.2f} "
              f"max={max(lat2):.2f} stdev={statistics.stdev(lat2):.2f}\n")
    print(f"RT:  n={len(lat2)} fails={fails} median={statistics.median(lat2):.2f}ms "
          f"max={max(lat2):.2f}ms")

    # Histogram
    out.write("\nHistogram:\n")
    buckets = [0,1,2,5,10,50,100,500,1000,9999]
    for j in range(len(buckets)-1):
        c = sum(1 for x in lat2 if buckets[j] <= x < buckets[j+1])
        if c: out.write(f"  {buckets[j]:>4}-{buckets[j+1]:>4}ms: {c}\n")

out.close()
print("Results written to output.txt")
