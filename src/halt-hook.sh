#!/bin/sh
# Claude Code PreToolUse hook: while the user has halted work from Telegram (/stop), block
# every tool call. Exit code 2 blocks the call and shows stderr to Claude.
if [ -e "$HOME/.local/share/tg-bridge/HALT" ]; then
    echo "HALTED by the user via Telegram /stop. Do not retry or work around this; stop and wait. The user will send /resume." >&2
    exit 2
fi
exit 0
