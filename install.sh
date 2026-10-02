#!/usr/bin/env bash
# install.sh: Setup and deployment script for tg-bridge
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${HOME}/.local/share/tg-bridge"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "==> Installing tg-bridge..."

# 1. Ensure target directories exist
mkdir -p "$BIN_DIR" "$SHARE_DIR" "$SHARE_DIR/images" "$SHARE_DIR/inbox" "$SYSTEMD_DIR"

# 2. Deploy binary controller
echo "==> Linking CLI binary to $BIN_DIR/tg-bridge"
ln -sf "$SCRIPT_DIR/bin/tg-bridge" "$BIN_DIR/tg-bridge"
chmod +x "$BIN_DIR/tg-bridge"

# 3. Deploy daemon scripts (symlinked for live development)
echo "==> Linking daemon scripts to $SHARE_DIR"
ln -sf "$SCRIPT_DIR/src/listener.py" "$SHARE_DIR/listener.py"
ln -sf "$SCRIPT_DIR/src/sender.py" "$SHARE_DIR/sender.py"
ln -sf "$SCRIPT_DIR/src/inbox.py" "$SHARE_DIR/inbox.py"
ln -sf "$SCRIPT_DIR/src/hotline.py" "$SHARE_DIR/hotline.py"
chmod +x "$SHARE_DIR/listener.py" "$SHARE_DIR/sender.py" "$SHARE_DIR/inbox.py"

# 4. Deploy systemd unit
echo "==> Installing systemd user unit to $SYSTEMD_DIR/tg-bridge.service"
cp "$SCRIPT_DIR/systemd/tg-bridge.service" "$SYSTEMD_DIR/tg-bridge.service"

# 5. Reload systemd daemon
systemctl --user daemon-reload

echo "==> Checking dependencies..."
python3 -c "import psutil" 2>/dev/null || {
    echo "Warning: python3 psutil is recommended. Install via: pip install -r requirements.txt or sudo dnf install python3-psutil"
}

which wl-copy >/dev/null 2>&1 || echo "Warning: wl-copy (wl-clipboard) not found in PATH."
which ydotool >/dev/null 2>&1 || echo "Warning: ydotool not found in PATH."

echo ""
echo "Installation complete!"
echo "Commands:"
echo "  tg-bridge start    # Start the background bridge daemon"
echo "  tg-bridge status   # Check daemon status"
echo "  tg-bridge logs     # View live journal logs"
echo "  tg-bridge send     # Send message or image to Telegram"
echo "  tg-bridge inbox    # Read Telegram replies queued for this Claude Code session"
