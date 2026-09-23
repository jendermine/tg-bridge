# tg-bridge Technical Journal

Date: 2026-09-23
Repo: https://github.com/jendermine/tg-bridge
Status: Active Investigation and Fixes Applied

---

## 1. Incident: Telegram Reply Misrouting Across Multi-Agent Sessions

### Symptoms
A user running two concurrent Antigravity agent sessions in separate terminal windows:
- Session A: PID 130994 in Ptyxis (`/dev/pts/5`), working on "Collection Table Redirect Planning"
- Session B: PID 99768 in Contour (`/dev/pts/2`), working on "Show Tg-Bridge Executed Code"

The user replied on Telegram to a message originating from Session A. Instead of being delivered to Session A, the prompt ("helloo", "yo") was pasted and submitted into Session B (Contour).

### Root Cause Analysis

Four distinct contributing factors caused this misrouting:

1. **Wayland Focus-Stealing Prevention Under Mutter:**
   In GNOME Shell on Wayland, calling `org.gtk.Application.Activate` on an application does not switch window focus from a currently focused window (Contour) to another window (Ptyxis) due to compositor focus-stealing prevention. Because window focus never switched, synthesized keystrokes (Ctrl+Shift+V and Enter) were received by whatever window was already focused on screen (Contour).

2. **Process Hierarchy PID Mismatch for Window Activation:**
   Clipman provides a GNOME Shell extension with `com.clipman.Daemon.ActivateWindowByPid(pid)`, which invokes Mutter's `metaWin.activate()`. Mutter matches `metaWin.get_pid() === targetPid`, where the PID must be the top-level GUI process owning the Wayland surface.
   In `sender.py`, process traversal walked up from `agy` -> `zsh` -> `ptyxis-agent` (PID 6256) -> `ptyxis` (PID 6245). Because `ptyxis-agent` was checked first in the emulator list, the recorded `emulator_pid` was 6256 (`/usr/libexec/ptyxis-agent`). Since 6256 does not own the `MetaWindow` (PID 6245 owns it), window activation failed.

3. **Missing Mapping for Bot Acknowledgment Messages:**
   In Telegram, users frequently swipe to reply to the bot's latest status or acknowledgment message (`[Pasted and submitted to ...]`). Previous versions invoked `sender.py --no-record` for bot replies, meaning the ACK message ID was never recorded in `message_map.json`. When the user replied to that ACK, `resolve_session_from_reply` looked up the message ID, found no match, and returned `None`.

4. **Unsafe Fallback in Route Resolution:**
   When `resolve_route()` received an explicit `reply_to_message`, but `resolve_session_from_reply()` returned `None`, it fell through to `resolve_target_session()`. `resolve_target_session()` reads `active_session.json`, which points to whichever session most recently sent a message. Session B had recently executed `tg-bridge send`, so any failed lookup defaulted directly to Session B.

### Fixes Implemented

1. **Top-Level GUI Emulator Detection:**
   Updated `sender.py` and `listener.py` to identify the true GUI emulator process (e.g. resolving `ptyxis-agent` to parent `/usr/bin/ptyxis`) and record `ancestry_pids` containing all PIDs between `agy` and the top-level window.

2. **Multi-Candidate Window Activation:**
   `inject_prompt_into_session()` now iterates candidate PIDs (`emulator_pid`, `ancestry_pids`, `agy_pid`) calling `com.clipman.Daemon.ActivateWindowByPid(pid)`. Mutter activates the corresponding window regardless of internal architecture.

3. **Dynamic Ancestry Refresh:**
   `resolve_session_from_reply()` dynamically inspects the live process hierarchy for existing `message_map.json` entries that were saved with older formats, repairing `emulator_pid` and `ancestry_pids` on the fly.

4. **Bot ACK Message Mapping:**
   `send_tg_reply()` in `listener.py` now makes direct Telegram API calls with `reply_to_message_id`, retrieves the returned `message_id`, and immediately saves it to `message_map.json` associated with `target_session`. Swiping to reply to bot ACKs now routes reliably to the correct session.

5. **Strict Reply Routing:**
   `resolve_route()` no longer falls back to `active_session.json` when `reply_to_message` is present.
   - If the replied message is unknown: alerts the user on Telegram that the target session could not be determined.
   - If the target session process has terminated: alerts the user that the session has ended.
   - Prompts are never injected into an unrelated terminal.

---

## 2. Issue: Prompt Delivery When Display Is Off / Screen Locked

### Problem Statement
When the system display turns off (DPMS blanking) or the session is locked, inbound Telegram prompts fail to deliver to interactive Antigravity CLI sessions.

### Technical Analysis

1. **Mutter DPMS Power Saving:**
   Under GNOME Wayland, display power management sets `org.gnome.Mutter.DisplayConfig.PowerSaveMode`:
   - `0`: Normal / On
   - `1`: Standby
   - `2`: Suspend
   - `3`: Off
   When `PowerSaveMode > 0`, Mutter halts compositor rendering and disables Wayland window focus transitions. Synthesized keystrokes (`ydotool` / `uinput`) do not route to unfocused windows.

2. **Screen Lock Isolation:**
   When the screen is locked (`org.gnome.ScreenSaver.GetActive` returns `true`), Wayland security policies strictly isolate input. All keyboard events are captured by the GNOME Shell lock screen authentication dialog (GdmGreeter / ScreenShield).
   Attempting to simulate Ctrl+Shift+V and Enter while locked risks entering clipboard data into the password prompt or having keystrokes discarded.

3. **Contrast with Claude Code Headless Inbox:**
   Claude Code does not use window activation, clipboard, or keystroke simulation. Inbound messages are appended directly to `~/.local/share/tg-bridge/inbox/claude-<pid>.jsonl`. The Claude Code session reads entries using `tg-bridge inbox --wait`. This file-based IPC runs headlessly and succeeds regardless of display state or screen lock.

### Immediate Mitigations Applied

1. **Display Wake on Inbound Message:**
   Before window activation, `listener.py` inspects `PowerSaveMode`. If greater than 0, it calls `org.gnome.Mutter.DisplayConfig.Set PowerSaveMode 0` and dispatches a wake key event via `ydotool` to restore the display.

2. **Screen Lock Guard:**
   Before pasting, `listener.py` queries `org.gnome.ScreenSaver.GetActive`. If locked:
   - Keystroke injection is aborted to prevent typing into the lockscreen.
   - Prompt is preserved in the Wayland clipboard.
   - Telegram responds with: `[Display is locked. Input staged in clipboard; unlock screen to submit to <Agent> (<PID>, <terminal>)]`.

---

## 3. Parallel Collaboration: Headless Input Architecture for Antigravity

Claude Code is running in parallel and can collaborate on designing and implementing a headless ingestion mechanism for Antigravity.

### Objectives for Claude Code

1. **Decouple Antigravity Delivery from GUI / Wayland:**
   Evaluate how Antigravity CLI sessions can ingest prompts without requiring window focus, clipboard paste, or `ydotool`.

2. **Architectural Options to Explore:**

   - **Option A: Antigravity File Inbox / Sidecar Consumer:**
     Mirror the Claude Code inbox pattern (`~/.local/share/tg-bridge/inbox/agy-<pid>.jsonl`). An Antigravity sidecar, hook, or background task polls or watches this file and injects input directly into the session loop.

   - **Option B: Unix Domain Socket / FIFO per Session:**
     `sender.py` or `agy` registers a dedicated Unix socket (`/tmp/agy-<conv_id>.sock`). `listener.py` writes inbound prompts directly to the socket.

   - **Option C: PTY Master Injection / Multiplexer:**
     Explore terminal proxying (e.g. lightweight pty wrapper or tmux/zellij session control) where input can be written directly to the PTY master file descriptor regardless of display or lock state.

3. **Deliverables:**
   - Review this journal.
   - Propose an implementation plan for headless Antigravity input.
   - Prototype changes in `src/` to support headless agy message delivery.
