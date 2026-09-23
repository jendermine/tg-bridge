#!/usr/bin/env bash
# Setup uinput permissions for ydotool on Fedora/Ubuntu/Arch
set -euo pipefail

echo "Configuring uinput device permissions for ydotool..."
sudo tee /etc/udev/rules.d/99-uinput.rules >/dev/null << 'EOF'
KERNEL=="uinput", GROUP="wheel", MODE="0666"
EOF

sudo mkdir -p /etc/systemd/system/ydotool.service.d
sudo tee /etc/systemd/system/ydotool.service.d/override.conf >/dev/null << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/ydotoold --socket-perm=0666
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ydotool
echo "ydotool service configured successfully!"
