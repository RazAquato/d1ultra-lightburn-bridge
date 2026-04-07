#!/usr/bin/env python3
"""
USB/IP Latency Test — Bridge Side (Echo Server)
=================================================

Minimal FunctionFS echo server for measuring USB/IP round-trip latency.
Reads from Bulk OUT, immediately writes the same data back on Bulk IN.

Run on the bridge VM:
    sudo bash setup_gadget.sh
    sudo python3 usb_latency_test.py

Then run the Windows-side test script to measure round-trip times.
"""

import os
import struct
import time
import sys
import select
import statistics

# FunctionFS paths
FFS_EP0  = "/dev/ffs-bjjcz/ep0"
FFS_EP1  = "/dev/ffs-bjjcz/ep1"  # Bulk OUT
FFS_EP2  = "/dev/ffs-bjjcz/ep2"  # Bulk IN
GADGET_UDC = "/sys/kernel/config/usb_gadget/bjjcz/UDC"
UDC_NAME = "dummy_udc.0"


def write_descriptors():
    """Write FunctionFS descriptors and bind UDC. Returns ep0 fd."""
    intf = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xFF, 0xFF, 0xFF, 0)
    ep_out_fs = struct.pack('<BBBBHB', 7, 5, 0x01, 0x02, 64, 0)
    ep_in_fs  = struct.pack('<BBBBHB', 7, 5, 0x82, 0x02, 64, 0)
    ep_out_hs = struct.pack('<BBBBHB', 7, 5, 0x01, 0x02, 512, 0)
    ep_in_hs  = struct.pack('<BBBBHB', 7, 5, 0x82, 0x02, 512, 0)

    fs = intf + ep_out_fs + ep_in_fs
    hs = intf + ep_out_hs + ep_in_hs
    header = struct.pack('<IIIII', 3, 20 + len(fs) + len(hs), 3, 3, 3)
    str_blob = struct.pack('<IIIH', 2, 16, 1, 0x0409) + b'\x00\x00'

    fd = os.open(FFS_EP0, os.O_RDWR)
    os.write(fd, header + fs + hs)
    os.write(fd, str_blob)

    with open(GADGET_UDC, 'w') as f:
        f.write(UDC_NAME)

    print(f"Gadget bound to {UDC_NAME}")
    return fd


def run_echo_server(ep0_fd):
    """Read from OUT, echo on IN, measure timing."""
    print("Opening endpoints...")
    ep_out = os.open(FFS_EP1, os.O_RDONLY | os.O_NONBLOCK)
    ep_in  = os.open(FFS_EP2, os.O_WRONLY)
    print("Endpoints open — waiting for USB host")

    # Switch OUT to blocking for simplicity
    import fcntl
    flags = fcntl.fcntl(ep_out, fcntl.F_GETFL)
    fcntl.fcntl(ep_out, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

    poll = select.poll()
    poll.register(ep_out, select.POLLIN)

    count = 0
    latencies = []
    print("\nEcho server running. Ctrl+C to stop.\n")
    print(f"{'#':>5}  {'Size':>5}  {'Read ms':>8}  {'Write ms':>8}  {'Total ms':>8}  Data (hex)")
    print("-" * 75)

    try:
        while True:
            # Wait for data with timeout
            events = poll.poll(5000)
            if not events:
                continue

            t0 = time.perf_counter()
            data = os.read(ep_out, 4096)
            t1 = time.perf_counter()

            if not data:
                print("  (empty read)")
                continue

            # Echo back immediately
            try:
                os.write(ep_in, data)
                t2 = time.perf_counter()
            except OSError as e:
                t2 = time.perf_counter()
                print(f"  write error: {e}")
                continue

            count += 1
            read_ms = (t1 - t0) * 1000
            write_ms = (t2 - t1) * 1000
            total_ms = (t2 - t0) * 1000
            latencies.append(total_ms)

            data_hex = data[:24].hex()
            if len(data) > 24:
                data_hex += "..."

            print(f"{count:>5}  {len(data):>5}  {read_ms:>8.2f}  {write_ms:>8.2f}  {total_ms:>8.2f}  {data_hex}")

            # Stats every 20 packets
            if count % 20 == 0 and latencies:
                avg = statistics.mean(latencies[-20:])
                p95 = sorted(latencies[-20:])[int(0.95 * min(20, len(latencies[-20:])))]
                print(f"       --- avg: {avg:.2f}ms  p95: {p95:.2f}ms ---")

    except KeyboardInterrupt:
        print(f"\n\nStopped after {count} packets")
        if latencies:
            print(f"  Mean:   {statistics.mean(latencies):.2f} ms")
            print(f"  Median: {statistics.median(latencies):.2f} ms")
            print(f"  Min:    {min(latencies):.2f} ms")
            print(f"  Max:    {max(latencies):.2f} ms")
            if len(latencies) > 1:
                print(f"  Stdev:  {statistics.stdev(latencies):.2f} ms")
    finally:
        os.close(ep_out)
        os.close(ep_in)
        os.close(ep0_fd)


def main():
    if not os.path.exists(FFS_EP0):
        print("ERROR: FunctionFS not mounted. Run: sudo bash setup_gadget.sh")
        sys.exit(1)

    ep0_fd = write_descriptors()
    run_echo_server(ep0_fd)


if __name__ == '__main__':
    main()
