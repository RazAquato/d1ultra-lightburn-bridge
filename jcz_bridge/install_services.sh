#!/bin/bash
# Install systemd services for auto-start on boot
set -euo pipefail

echo "Installing systemd services..."

cp systemd/gadget-setup.service /etc/systemd/system/
cp systemd/jcz-bridge.service  /etc/systemd/system/
cp systemd/usbip-server.service /etc/systemd/system/

# Create log file with correct permissions
touch /var/log/d1ultra-bridge.log
chmod 644 /var/log/d1ultra-bridge.log

systemctl daemon-reload
systemctl enable gadget-setup.service
systemctl enable jcz-bridge.service
systemctl enable usbip-server.service

echo "Services installed and enabled."
echo ""
echo "  Start now:  sudo systemctl start gadget-setup jcz-bridge usbip-server"
echo "  Check logs: journalctl -u jcz-bridge -f"
echo "              tail -f /var/log/d1ultra-bridge.log"
