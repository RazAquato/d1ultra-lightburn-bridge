#!/usr/bin/env python3
"""
USB/IP Latency Test — Windows Side (Client)
=============================================

Sends 12-byte packets to the BJJCZ device over USB and measures round-trip
latency. The bridge side should be running usb_latency_test.py (echo server).

Install on Windows:
    pip install pyusb libusb-package

    # If pyusb can't find the backend, also try:
    pip install libusb

Run:
    python win_usb_latency_test.py

Requirements:
    - BJJCZ device attached via usbip (VID 0x9588, PID 0x9899)
    - WinUSB driver installed (via Zadig)
    - Bridge running usb_latency_test.py
"""

import time
import sys
import statistics

try:
    import usb.core
    import usb.util
except ImportError:
    print("ERROR: pyusb not installed")
    print("  pip install pyusb libusb-package")
    sys.exit(1)


VID = 0x9588
PID = 0x9899
EP_OUT = 0x02   # Bulk OUT endpoint
EP_IN  = 0x81   # Bulk IN endpoint


def find_device():
    """Find the BJJCZ USB device."""
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print(f"ERROR: Device {VID:04x}:{PID:04x} not found")
        print("  Is the bridge running?")
        print("  Is the device attached via usbip?")
        print("  Is WinUSB driver installed (Zadig)?")
        sys.exit(1)
    return dev


def run_test(dev, num_packets=100, packet_size=12):
    """Send packets and measure round-trip latency."""
    # Detach kernel driver if needed
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (usb.core.USBError, NotImplementedError):
        pass

    # Set configuration
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass  # may already be configured

    print(f"Device: {dev.manufacturer} {dev.product}")
    print(f"Endpoints: OUT 0x{EP_OUT:02x}, IN 0x{EP_IN:02x}")
    print(f"Sending {num_packets} packets of {packet_size} bytes")
    print()

    # Test 1: OUT only (measure write completion time)
    print("=== Test 1: Bulk OUT only (write speed) ===")
    test_data = b'\x09' + b'\x00' * (packet_size - 1)  # 0x0009 command
    latencies_out = []

    for i in range(num_packets):
        t0 = time.perf_counter()
        try:
            dev.write(EP_OUT, test_data, timeout=1000)
            t1 = time.perf_counter()
            ms = (t1 - t0) * 1000
            latencies_out.append(ms)
            if i < 5 or i % 20 == 0:
                print(f"  #{i+1:>3}: {ms:.2f} ms")
        except usb.core.USBError as e:
            print(f"  #{i+1:>3}: ERROR: {e}")
            time.sleep(0.1)

    if latencies_out:
        print(f"\n  OUT stats ({len(latencies_out)} successful):")
        print(f"    Mean:   {statistics.mean(latencies_out):.2f} ms")
        print(f"    Median: {statistics.median(latencies_out):.2f} ms")
        print(f"    Min:    {min(latencies_out):.2f} ms")
        print(f"    Max:    {max(latencies_out):.2f} ms")

    # Test 2: OUT+IN round trip (echo test)
    print(f"\n=== Test 2: OUT+IN round trip (echo) ===")
    latencies_rt = []

    for i in range(num_packets):
        t0 = time.perf_counter()
        try:
            dev.write(EP_OUT, test_data, timeout=1000)
            response = dev.read(EP_IN, packet_size, timeout=1000)
            t1 = time.perf_counter()
            ms = (t1 - t0) * 1000
            latencies_rt.append(ms)
            match = (bytes(response) == test_data)
            if i < 5 or i % 20 == 0:
                print(f"  #{i+1:>3}: {ms:.2f} ms  match={match}  "
                      f"resp={bytes(response[:6]).hex()}")
        except usb.core.USBTimeoutError:
            print(f"  #{i+1:>3}: TIMEOUT (>1000ms)")
        except usb.core.USBError as e:
            print(f"  #{i+1:>3}: ERROR: {e}")
            time.sleep(0.1)

    if latencies_rt:
        print(f"\n  Round-trip stats ({len(latencies_rt)} successful):")
        print(f"    Mean:   {statistics.mean(latencies_rt):.2f} ms")
        print(f"    Median: {statistics.median(latencies_rt):.2f} ms")
        print(f"    Min:    {min(latencies_rt):.2f} ms")
        print(f"    Max:    {max(latencies_rt):.2f} ms")
        if len(latencies_rt) > 1:
            print(f"    Stdev:  {statistics.stdev(latencies_rt):.2f} ms")

    # Verdict
    print("\n=== Verdict ===")
    if latencies_rt:
        avg = statistics.mean(latencies_rt)
        if avg < 5:
            print(f"  Average RT: {avg:.1f}ms — EXCELLENT (well under LightBurn timeout)")
        elif avg < 50:
            print(f"  Average RT: {avg:.1f}ms — OK (may work, but tight)")
        elif avg < 200:
            print(f"  Average RT: {avg:.1f}ms — SLOW (likely causing LightBurn timeouts)")
        else:
            print(f"  Average RT: {avg:.1f}ms — TOO SLOW (USB/IP over network not viable)")
    else:
        print("  No successful round-trips — connection issue")


def main():
    dev = find_device()
    run_test(dev, num_packets=50)


if __name__ == '__main__':
    main()
