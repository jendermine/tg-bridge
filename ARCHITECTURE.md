# Architecture: Antigravity Telegram Bridge (`tg-bridge`)

`tg-bridge` is a bidirectional, headless bridge linking an authorized Telegram user directly to their active **Google Antigravity (`agy`)** CLI development session on Linux (Wayland / GNOME).

It allows mobile prompt submission, screenshot analysis, and bidirectional AI pairing with automated input staging, terminal window focusing, and command submission.

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
            ImagesDir["images/ (Downloaded Media)"]
        end

        subgraph DesktopSession["User Desktop Session (Wayland)"]
            WLClipboard["Wayland Clipboard (wl-copy)"]
            ClipmanExt["Clipman GNOME Shell Extension (Clutter VK)"]
            ActiveTerminal["Active Terminal (Contour / Ptyxis)"]
            AgyCLI["Antigravity CLI (agy session)"]

            ActiveTerminal --> AgyCLI
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

    ListenerDaemon -->|1. Download Photos/Docs| ImagesDir
    ListenerDaemon -->|2. Stage Prompt| WLClipboard
    ListenerDaemon -->|3. Focus Window| ClipmanExt
    ListenerDaemon -->|4. Paste (Ctrl+Shift+V)| ClipmanExt
    ListenerDaemon -->|5. Submit (KEY_ENTER)| YDoToolService
    YDoToolService -->|Synthesize Return| ActiveTerminal
    ClipmanExt -->|Synthesize Paste| ActiveTerminal

    %% Outbound Connections
    AgyCLI -->|Runs 'tg-bridge send'| TGCLI
    SenderScript -->|Trace caller process tree| ActiveSession
    ActiveSession -.->|Read target session| ListenerDaemon
    SenderScript -->|POST sendMessage / sendPhoto| BotAPI
```

---

## 2. Inbound Message Pipeline (Telegram to `agy`)

When the user sends a text prompt or screenshot from Telegram, the bridge automatically ingests the payload, resolves the target session, raises the terminal, pastes the prompt, and submits it.

```mermaid
sequenceDiagram
    autonumber
    actor User as Telegram User
    participant Bot as Telegram Bot API
    participant Listener as listener.py (Daemon)
    participant Clipboard as wl-copy (Wayland)
    participant Dbus as GNOME D-Bus
    participant Ydo as ydotool
    participant Terminal as Terminal Window (agy)

    User->>Bot: Send prompt or photo attachment
    Listener->>Bot: Long-poll (getUpdates?timeout=30)
    Bot-->>Listener: Return incoming Update JSON
    Listener->>Listener: Verify sender ID == authorized_id

    alt Payload is Image / Document
        Listener->>Bot: Download file via getFile
        Bot-->>Listener: File bytes
        Listener->>Listener: Save to ~/.local/share/tg-bridge/images/
        Listener->>Clipboard: wl-copy "[Image: /path/to/img]" + caption
    else Payload is Plain Text
        Listener->>Clipboard: wl-copy prompt text
    end

    Listener->>Listener: Resolve target session from active_session.json / process scan
    Listener->>Dbus: ActivateWindowByPid(emulator_pid) / Ptyxis.Activate
    Note over Terminal: Target terminal raised & focused on desktop

    Listener->>Dbus: SimulatePaste("ctrl-shift-v")
    Note over Terminal: Clutter Virtual Keyboard pastes clipboard into agy
    Listener->>Listener: Wait 150ms for bracketed paste buffering

    Listener->>Ydo: ydotool key -d 15 28:1 28:0 (KEY_ENTER)
    Note over Terminal: agy receives Enter keystroke and submits prompt!

    Listener->>Bot: sendMessage reply: "✓ Pasted & submitted to Antigravity (PID ...)"
    Bot-->>User: Notification confirming automated submission
```

---

## 3. Outbound Response Pipeline (`agy` to Telegram)

When `agy` generates a response or needs to communicate with the user on Telegram, it invokes `tg-bridge send "<message>"`.

```mermaid
sequenceDiagram
    autonumber
    participant Agy as Antigravity (agy CLI)
    participant CLI as tg-bridge CLI
    participant Sender as sender.py
    participant SessionFile as active_session.json
    participant Bot as Telegram Bot API
    actor User as Telegram User

    Agy->>CLI: tg-bridge send "Task completed with results..."
    CLI->>Sender: python3 sender.py "Task completed with results..."

    Sender->>Sender: Trace os.getpid() parent ancestry via psutil
    Note over Sender: Identifies: agy_pid, PTS terminal (/dev/pts/X),<br/>emulator_pid, emulator_name (contour/ptyxis), cwd
    Sender->>SessionFile: Save active session metadata (JSON)

    Sender->>Sender: markdown_to_tg_html (format headers, bold, code blocks)
    Sender->>Sender: Extract embedded markdown images (![alt](path))
    
    loop Each embedded local image
        Sender->>Bot: POST sendPhoto (multipart/form-data)
    end

    alt Message <= 3800 chars
        Sender->>Bot: POST sendMessage (parse_mode=HTML)
    else Message > 3800 chars
        loop Each chunk <= 3800 chars
            Sender->>Bot: POST sendMessage (chunk, parse_mode=HTML)
        end
    end

    Bot-->>User: Delivered to Telegram chat
```

---

## 4. Session Resolution Strategy

When an incoming message arrives, `listener.py` needs to determine *which* `agy` session to paste into:

```mermaid
flowchart TD
    Start([Incoming Telegram Message]) --> CheckSessionFile{Check active_session.json exists?}
    
    CheckSessionFile -->|Yes| ValidatePID{Is agy_pid alive & running 'agy'?}
    CheckSessionFile -->|No| ScanProcesses[Scan system processes with psutil]

    ValidatePID -->|Alive| UseRecordedSession[Use recorded session: agy_pid, terminal, emulator_pid]
    ValidatePID -->|Dead / Stale| ScanProcesses

    ScanProcesses --> FilterAgy[Filter processes matching 'agy' in name or cmdline]
    FilterAgy --> FilterInteractive[Exclude subagents and non-interactive print mode]
    FilterInteractive --> HasCandidates{Found running interactive agy sessions?}

    HasCandidates -->|Yes| SortCandidates[Sort by create_time descending: pick newest/active]
    HasCandidates -->|No| NoSession[No active session found: Fallback to clipboard only]

    SortCandidates --> TraceAncestry[Trace parent tree to detect terminal device & emulator PID]
    TraceAncestry --> TargetResolved([Target Session Identified])
    UseRecordedSession --> TargetResolved
```

---

## 5. Wayland Virtual Input & Window Focusing

GNOME Mutter on Wayland restricts arbitrary synthetic keypresses and window focus stealing for security. `tg-bridge` bypasses this reliably without requiring root privileges during operation:

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
