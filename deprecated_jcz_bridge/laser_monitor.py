#!/usr/bin/env python3
"""
Laser Monitor — Detects when the D1 Ultra is connected/disconnected
====================================================================

The D1 Ultra connects via USB and creates an RNDIS virtual network adapter.
When the laser is powered off, the RNDIS interface disappears from the VM.

This module watches for the RNDIS interface by checking whether any network
interface has an IP in the expected subnet (e.g. 192.168.12.x). It avoids
pointless pinging when the laser is off.

Usage:
    from laser_monitor import LaserMonitor

    monitor = LaserMonitor(subnet="192.168.12.", laser_ip="192.168.12.1")
    monitor.start()

    # Called whenever laser appears/disappears
    monitor.on_laser_up   = lambda iface, ip: print(f"Laser UP on {iface}")
    monitor.on_laser_down = lambda: print("Laser DOWN")

    monitor.wait_for_laser()  # blocks until RNDIS interface appears
"""

import subprocess
import threading
import time
import logging
from typing import Optional, Callable, Tuple

log = logging.getLogger("laser-monitor")

__all__ = ["LaserMonitor"]


class LaserMonitor:
    """Watches for the D1 Ultra's RNDIS network interface."""

    def __init__(self, subnet: str = "192.168.12.",
                 laser_ip: str = "192.168.12.1",
                 check_interval: float = 2.0):
        self.subnet = subnet
        self.laser_ip = laser_ip
        self.check_interval = check_interval

        self.laser_online = False
        self.rndis_iface: Optional[str] = None
        self.local_ip: Optional[str] = None

        # Callbacks (set by the bridge)
        self.on_laser_up:   Optional[Callable[[str, str], None]] = None
        self.on_laser_down: Optional[Callable[[], None]] = None

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._laser_ready = threading.Event()

    def start(self):
        """Start background monitoring thread."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="laser-monitor")
        self._thread.start()

    def stop(self):
        """Stop monitoring."""
        self._stop.set()

    def wait_for_laser(self, timeout: Optional[float] = None) -> bool:
        """Block until the laser's RNDIS interface appears. Returns True if found."""
        return self._laser_ready.wait(timeout=timeout)

    def check_once(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """Check right now if the RNDIS interface is up.

        Returns: (is_online, interface_name, local_ip)
        """
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show"],
                capture_output=True, text=True, timeout=5)
            lines = result.stdout.splitlines()

            current_iface = None
            for line in lines:
                line = line.strip()
                # Interface line: "2: enx001122334455: <...>"
                if not line.startswith("inet") and ":" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        current_iface = parts[1].strip().split("@")[0]
                # IP line: "inet 192.168.12.100/24 ..."
                elif line.startswith("inet "):
                    ip = line.split()[1].split("/")[0]
                    if ip.startswith(self.subnet) and ip != self.laser_ip:
                        return (True, current_iface, ip)

            return (False, None, None)
        except Exception as e:
            log.debug(f"Interface check error: {e}")
            return (False, None, None)

    def _monitor_loop(self):
        """Background loop: check for RNDIS interface periodically."""
        while not self._stop.is_set():
            online, iface, ip = self.check_once()

            if online and not self.laser_online:
                # Laser just appeared
                self.laser_online = True
                self.rndis_iface = iface
                self.local_ip = ip
                self._laser_ready.set()
                log.info(f"Laser ONLINE — interface {iface}, local IP {ip}")
                if self.on_laser_up:
                    try:
                        self.on_laser_up(iface, ip)
                    except Exception as e:
                        log.error(f"on_laser_up callback error: {e}")

            elif not online and self.laser_online:
                # Laser just disappeared
                self.laser_online = False
                self.rndis_iface = None
                self.local_ip = None
                self._laser_ready.clear()
                log.info("Laser OFFLINE — RNDIS interface gone")
                if self.on_laser_down:
                    try:
                        self.on_laser_down()
                    except Exception as e:
                        log.error(f"on_laser_down callback error: {e}")

            self._stop.wait(self.check_interval)
