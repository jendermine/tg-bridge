# Architecture: Antigravity Telegram Bridge (tg-bridge)

tg-bridge is a bidirectional, headless bridge linking an authorized Telegram user directly to their active Google Antigravity (agy) CLI development sessions on Linux (Wayland / GNOME).

It allows mobile prompt submission, screenshot analysis, and multi-agent coordination with automated input staging, terminal window focusing, and command submission.

---

## 1. High-Level System Architecture

```mermaid
flowchart TD
    subgraph TelegramCloud["Telegram Cloud"]
        UserMobile["Authorized User (Mobile/Desktop App)"]
        BotAPI["Telegram Bot API (api.telegram.org)"]
        UserMobile <--> BotAPI
    end

    subgraph HostSystem["Linux Host (Wayland / GNOME 50)"]
        subgraph Services["Background Services"]
            TGService["tg-bridge.service (Systemd User)"]
            ListenerDaemon["listener.py (Daemon)"]
            TGService --> ListenerDaemon
            YDoToolService["ydotool.service (System Daemon)"]
        end

        subgraph LocalState["State & Storage (~/.local/share/tg-bridge/)"]
            Creds["~/Documents/tg.txt (Token & User ID)"]
            ActiveSession["active_session.json (PID, PTS, Emulator)"]
            MessageMap["message_map.json (Message ID to Session Map, kind agy/claude)"]
            ClaudeSessions["active_session_claude.json (Claude Code sessions)"]
            InboxDir["inbox/claude-PID.jsonl (queued replies)"]
            ImagesDir["images/ (Downloaded Media)"]
        end

        subgraph DesktopSession["User Desktop Session (Wayland)"]
            WLClipboard["Wayland Clipboard (wl-copy)"]
            ClipmanExt["Clipman GNOME Shell Extension (Clutter VK)"]
            TermA["Terminal A (Agent 1: Builder)"]
            TermB["Terminal B (Agent 2: Researcher)"]
        end

        subgraph OutboundCLI["Outbound CLI Controller"]
            TGCLI["tg-bridge CLI (~/.local/bin/tg-bridge)"]
            SenderScript["sender.py"]
            TGCLI --> SenderScript
        end
    end

    %% Inbound Connections
    BotAPI -->|Long-polling getUpdates| ListenerDaemon
    Creds -.->|Read Auth| ListenerDaemon
    Creds -.->|Read Auth| SenderScript

    ListenerDaemon -->|1. Download Media| ImagesDir
    ListenerDaemon -->|2. Stage Prompt| WLClipboard
    ListenerDaemon -->|3. Query Reply Mapping| MessageMap
    ListenerDaemon -->|4. Focus Target Terminal| ClipmanExt
    ListenerDaemon -->|5. Paste Ctrl+Shift+V| ClipmanExt
    ListenerDaemon -->|6. Submit KEY_ENTER| YDoToolService
    YDoToolService -->|Synthesize Return| TermA
    YDoToolService -->|Synthesize Return| TermB
    ClipmanExt -->|Synthesize Paste| TermA
    ClipmanExt -->|Synthesize Paste| TermB

    %% Outbound Connections
    TermA -->|tg-bridge send| TGCLI
    TermB -->|tg-bridge send| TGCLI
    SenderScript -->|Trace caller process tree| ActiveSession
    SenderScript -->|Save Message ID Mapping| MessageMap
    SenderScript -->|POST sendMessage / sendPhoto| BotAPI
```

---

## 2. Inbound Message Pipeline & Multi-Agent Routing

When the user sends a prompt from Telegram, the bridge determines whether it is a reply to an existing agent turn, focuses the respective window, and injects the turn.

```mermaid
sequenceDiagram
    autonumber
    actor User as Telegram User
    participant Bot as Telegram Bot API
    participant Listener as listener.py (Daemon)
    participant MsgMap as message_map.json
    participant Clipboard as wl-copy (Wayland)
    participant Dbus as GNOME D-Bus
    participant Ydo as ydotool
    participant Terminal as Target Terminal (agy)

    User->>Bot: Send prompt or photo (with optional reply_to)
    Listener->>Bot: Long-poll (getUpdates?timeout=30)
    Bot-->>Listener: Return incoming Update JSON
    Listener->>Listener: Verify sender ID == authorized_id

    alt Has reply_to_message
        Listener->>MsgMap: Query reply_to_message_id
        MsgMap-->>Listener: Originating session (agy_pid, terminal, emulator_pid)
    else No reply (fresh message)
        Listener->>Listener: Fallback to active_session.json / process scan
    end

    alt Payload is Image or Document
        Listener->>Bot: Download file via getFile
        Bot-->>Listener: File bytes
        Listener->>Listener: Save to ~/.local/share/tg-bridge/images/
        Listener->>Clipboard: wl-copy "[Image: /path/to/img]" + caption
    else Payload is Plain Text
        Listener->>Clipboard: wl-copy prompt text
    end

    Listener->>Dbus: ActivateWindowByPid(emulator_pid) / Ptyxis.Activate
    Note over Terminal: Target terminal raised and focused on desktop

    Listener->>Dbus: SimulatePaste("ctrl-shift-v")
    Note over Terminal: Virtual Keyboard pastes clipboard into agy
    Listener->>Listener: Wait 150ms for bracketed paste buffering

    Listener->>Ydo: ydotool key -d 15 28:1 28:0 (KEY_ENTER)
    Note over Terminal: agy receives Enter keystroke and submits prompt!

    Listener->>Bot: sendMessage reply: "[Pasted and submitted to Agent (PID ...)]"
    Bot-->>User: Notification confirming automated submission
```

---

## 3. Outbound Response Pipeline (agy to Telegram)

When agy generates a response or needs to communicate with the user on Telegram, it invokes `tg-bridge send "<message>"`.

```mermaid
sequenceDiagram
    autonumber
    participant Agy as Antigravity (agy CLI)
    participant CLI as tg-bridge CLI
    participant Sender as sender.py
    participant DB as conversation_summaries.db
    participant MsgMap as message_map.json
    participant Bot as Telegram Bot API
    actor User as Telegram User

    Agy->>CLI: tg-bridge send "Task completed with results..."
    CLI->>Sender: python3 sender.py "Task completed with results..."

    Sender->>Sender: Trace os.getpid() parent ancestry via psutil
    Note over Sender: Identifies: agy_pid, PTS terminal (/dev/pts/X),<br/>emulator_pid, emulator_name (contour/ptyxis), cwd

    Sender->>DB: Query open conversation ID for title and agent name
    DB-->>Sender: title="Refactor Auth", agent_name="Builder"

    Sender->>Sender: Format header: [Agent: Builder | Refactor Auth]
    Sender->>Sender: markdown_to_tg_html (format headers, bold, code blocks)

    alt Message <= 3800 chars
        Sender->>Bot: POST sendMessage (parse_mode=HTML)
        Bot-->>Sender: Return {ok: true, result: {message_id: 1042}}
        Sender->>MsgMap: Store 1042 -> session metadata
    else Message > 3800 chars
        loop Each chunk <= 3800 chars
            Sender->>Bot: POST sendMessage (chunk, parse_mode=HTML)
            Bot-->>Sender: Return {ok: true, result: {message_id: chunk_id}}
            Sender->>MsgMap: Store chunk_id -> session metadata
        end
    end

    Bot-->>User: Delivered to Telegram chat with agent header
```

---

## 4. Multi-Agent Session Resolution Decision Tree

```mermaid
flowchart TD
    Start([Incoming Telegram Message]) --> HasReply{Contains reply_to_message?}
    
    HasReply -->|Yes| QueryMap[Lookup reply_to message_id in message_map.json]
    QueryMap --> FoundInMap{Found in message_map?}
    FoundInMap -->|Yes| CheckTargetAlive{Is target agy_pid alive?}
    CheckTargetAlive -->|Yes| UseReplySession[Use mapped session: agy_pid, terminal, emulator_pid]
    CheckTargetAlive -->|No| CheckActiveFile
    FoundInMap -->|No| CheckActiveFile

    HasReply -->|No| CheckActiveFile{Check active_session.json exists?}
    
    CheckActiveFile -->|Yes| ValidateActive{Is active agy_pid alive?}
    CheckActiveFile -->|No| ScanProcesses[Scan system processes with psutil]

    ValidateActive -->|Alive| UseActiveSession[Use last active session]
    ValidateActive -->|Dead / Stale| ScanProcesses

    ScanProcesses --> FilterAgy[Filter processes matching agy in cmdline]
    FilterAgy --> FilterInteractive[Exclude subagents and non-interactive print mode]
    FilterInteractive --> HasCandidates{Found running interactive agy sessions?}

    HasCandidates -->|Yes| SortCandidates[Sort by create_time descending: pick newest]
    HasCandidates -->|No| NoSession[No active session found: Fallback to clipboard only]

    SortCandidates --> TraceAncestry[Trace parent tree to detect terminal device and emulator PID]
    TraceAncestry --> TargetResolved([Target Session Identified])
    UseReplySession --> TargetResolved
    UseActiveSession --> TargetResolved
```

---

## 5. Claude Code Routing (inbox, no input injection)

Claude Code runs as a `claude` process (`~/.config/Claude/claude-code/<ver>/claude`) spawned by the Claude desktop app. It has no terminal or targetable window, so tg-bridge never pastes into it. agy and Claude Code are kept fully separate.

Outbound (`sender.py`):
- Walks the caller's ancestors. If a process named `claude` (or whose argv[0] contains `/claude-code/`) is found before any process named `agy`, the session is `{kind: "claude", claude_pid, claude_create_time, cwd, agent: "Claude Code", title, timestamp}`.
- Claude sessions are upserted into `active_session_claude.json` (keyed by pid, newest 20). `active_session.json` stays agy-only and agy sessions are recorded with `kind: "agy"`.
- Every sent message ID is mapped to its session in `message_map.json`. `--no-record` skips both writes; the listener uses it for all acks.

Inbound (`listener.py`, `resolve_route`), in order:
1. Reply to a `kind: "claude"` message: if the pid is alive and still the same `claude` process (create time matches), append `{update_id, message_id, date, text, image_path, caption}` to `inbox/claude-<pid>.jsonl` under a lock and ack `Queued for Claude Code | <title> (PID n)`. If it has ended, ack that the message was not delivered. Never falls back to agy.
2. Reply to an agy message (or a legacy entry without `kind`) whose agy pid is alive: agy paste path, unchanged.
3. Text or caption starting with `/claude `: prefix stripped, queued to the most recent live session in `active_session_claude.json`; if none is alive, ack and drop.
4. Anything else: unchanged agy path (active_session.json, else newest agy process, else clipboard only).

Claude routes use only `notify-send`: no `wl-copy`, no window focus, no paste, no `ydotool`.

Reading (`inbox.py`, `tg-bridge inbox`): resolves the caller's `claude_pid` the same way, prints `[tg <message_id>] <text> [Image: <path>]` lines, and under the same lock moves consumed lines to `claude-<pid>.done.jsonl`. `--peek` does not consume; `--wait [SECONDS]` polls every 2s (default 1800s) and exits 3 on timeout; exit 2 outside Claude Code.

---

## 6. Wayland Virtual Input & Window Focusing

GNOME Mutter on Wayland restricts arbitrary synthetic keypresses and window focus stealing for security. tg-bridge bypasses this reliably without requiring root privileges during operation:

```mermaid
flowchart LR
    subgraph Trigger
        Listener["listener.py"]
    end

    subgraph WindowFocus["1. Window Activation"]
        DBusFocus["D-Bus Call:<br/>com.clipman.Daemon.ActivateWindowByPid(pid)"]
        ShellWin["Mutter metaWin.activate(now)"]
        DBusFocus --> ShellWin
    end

    subgraph PasteInjection["2. Paste Simulation"]
        DBusPaste["D-Bus Call:<br/>SimulatePaste('ctrl-shift-v')"]
        ClutterVK["GNOME Clutter Virtual Keyboard<br/>vk.notify_keyval(...)"]
        DBusPaste --> ClutterVK
    end

    subgraph SubmitInjection["3. Enter Keystroke"]
        YDoToolCLI["ydotool key 28:1 28:0<br/>Socket: /tmp/.ydotool_socket (0666)"]
        UInput["Linux Kernel /dev/uinput"]
        YDoToolCLI --> UInput
    end

    subgraph Target["Target Process"]
        TerminalPty["Terminal PTY (Contour / Ptyxis)"]
        AgyPrompt["Antigravity Interactive Prompt"]
        TerminalPty --> AgyPrompt
    end

    Listener --> WindowFocus
    WindowFocus --> PasteInjection
    PasteInjection --> SubmitInjection
    ClutterVK --> Target
    UInput --> Target
```
