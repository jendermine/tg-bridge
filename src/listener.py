#!/usr/bin/env python3
import os
import sys
import time
import json
import urllib.request
import urllib.parse
import subprocess
import re

try:
    import psutil
except ImportError:
    psutil = None

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import inbox

CRED_PATH = os.path.expanduser('~/Documents/tg.txt')
BASE_DIR = os.path.expanduser('~/.local/share/tg-bridge')
IMAGES_DIR = os.path.join(BASE_DIR, 'images')
SESSION_PATH = os.path.join(BASE_DIR, 'active_session.json')
MESSAGE_MAP_PATH = os.path.join(BASE_DIR, 'message_map.json')

def load_credentials():
    if not os.path.exists(CRED_PATH):
        sys.stderr.write(f"Credentials file {CRED_PATH} not found.\n")
        sys.exit(1)
    with open(CRED_PATH, 'r') as f:
        lines = [line.strip() for line in f if line.strip()]
    token = lines[0]
    user_id = None
    for line in lines[1:]:
        if 'id:' in line:
            user_id = int(line.split('id:')[1].strip())
            break
    if not user_id:
        sys.stderr.write("User ID not found in credentials file.\n")
        sys.exit(1)
    return token, user_id

def save_message_mapping(msg_id, session_data):
    """Store Telegram message ID to session mapping for reply tracking."""
    if not msg_id or not session_data:
        return
    try:
        data = {}
        if os.path.exists(MESSAGE_MAP_PATH):
            try:
                with open(MESSAGE_MAP_PATH, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}

        data[str(msg_id)] = session_data

        if len(data) > 300:
            keys = sorted(data.keys(), key=lambda k: data[k].get('timestamp', 0))
            for k in keys[:-300]:
                del data[k]

        os.makedirs(os.path.dirname(MESSAGE_MAP_PATH), exist_ok=True)
        with open(MESSAGE_MAP_PATH, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        sys.stderr.write(f"Warning: could not save message mapping: {e}\n")

def send_tg_reply(token, chat_id, text, reply_to_message_id=None, session_data=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_to_message_id:
        payload['reply_to_message_id'] = reply_to_message_id
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            resp_data = json.loads(resp.read().decode())
            if resp_data.get('ok') and session_data:
                new_msg_id = resp_data.get('result', {}).get('message_id')
                if new_msg_id:
                    save_message_mapping(new_msg_id, session_data)
            return resp_data
    except urllib.error.HTTPError as e:
        try:
            plain_text = re.sub(r'<[^>]+>', '', text)
            payload = {'chat_id': chat_id, 'text': plain_text}
            if reply_to_message_id:
                payload['reply_to_message_id'] = reply_to_message_id
            fallback_data = urllib.parse.urlencode(payload).encode()
            fallback_req = urllib.request.Request(url, data=fallback_data)
            with urllib.request.urlopen(fallback_req, timeout=12) as resp:
                resp_data = json.loads(resp.read().decode())
                if resp_data.get('ok') and session_data:
                    new_msg_id = resp_data.get('result', {}).get('message_id')
                    if new_msg_id:
                        save_message_mapping(new_msg_id, session_data)
                return resp_data
        except Exception:
            pass
        sys.stderr.write(f"send_tg_reply HTTP error: {e}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"send_tg_reply error: {e}\n")
        return None

def resolve_session_from_reply(reply_msg_id):
    """Lookup originating session for the replied Telegram message ID."""
    if not os.path.exists(MESSAGE_MAP_PATH):
        return None
    try:
        with open(MESSAGE_MAP_PATH, 'r') as f:
            data = json.load(f)
        session = data.get(str(reply_msg_id))
        if session:
            pid = session.get('agy_pid')
            if pid and psutil and psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                cmd = " ".join(proc.cmdline()).lower()
                if "agy" in proc.name().lower() or "agy" in cmd:
                    # Dynamically refresh ancestry_pids and emulator_pid if missing or outdated
                    if not session.get('ancestry_pids') or session.get('emulator_name') == 'ptyxis-agent':
                        curr = proc
                        pids = [proc.pid]
                        em_name = None
                        em_pid = None
                        while curr.parent():
                            curr = curr.parent()
                            pids.append(curr.pid)
                            pname = curr.name().lower()
                            if pname in ('contour', 'ptyxis', 'gnome-terminal-server', 'kitty', 'alacritty', 'wezterm-gui', 'foot', 'xterm'):
                                em_name = pname
                                em_pid = curr.pid
                                break
                            elif pname == 'ptyxis-agent':
                                parent = curr.parent()
                                if parent and 'ptyxis' in parent.name().lower():
                                    em_name = 'ptyxis'
                                    em_pid = parent.pid
                                    pids.append(parent.pid)
                                    break
                        session['ancestry_pids'] = pids
                        if em_pid:
                            session['emulator_pid'] = em_pid
                            session['emulator_name'] = em_name
                    return session
    except Exception:
        pass
    return None

def lookup_mapped_session(reply_msg_id):
    """Raw message_map.json entry for a Telegram message ID, or None."""
    if not os.path.exists(MESSAGE_MAP_PATH):
        return None
    try:
        with open(MESSAGE_MAP_PATH, 'r') as f:
            return json.load(f).get(str(reply_msg_id))
    except Exception:
        return None

CLAUDE_PREFIX = '/claude '

def strip_claude_prefix(message):
    """If the text (or caption) starts with "/claude ", return a copy without it, else None."""
    for key in ('text', 'caption'):
        value = message.get(key)
        if isinstance(value, str) and value.startswith(CLAUDE_PREFIX):
            stripped = dict(message)
            stripped[key] = value[len(CLAUDE_PREFIX):]
            return stripped
    return None

def resolve_route(message):
    """Decide where an inbound message goes. agy and Claude Code never share a path.

    Order:
      1. Explicit reply to a message:
         - Live Claude message -> 'claude'
         - Dead Claude message -> 'claude_dead'
         - Live agy message -> 'agy'
         - Dead agy message -> 'agy_dead'
         - Unmapped message -> 'unknown_reply'
         (Never fallback to active_session on explicit reply)
      2. "/claude " prefix:
         - Live Claude session -> 'claude'
         - No Claude session -> 'claude_none'
      3. Unprefixed new message:
         - Active agy session -> 'agy'
    """
    reply_to = message.get('reply_to_message')
    if reply_to:
        reply_msg_id = reply_to.get('message_id')
        mapped = lookup_mapped_session(reply_msg_id)
        if mapped:
            if mapped.get('kind') == 'claude':
                if inbox.is_claude_session_alive(mapped):
                    return 'claude', mapped, message
                return 'claude_dead', mapped, message
            else:
                target_session = resolve_session_from_reply(reply_msg_id)
                if target_session:
                    return 'agy', target_session, message
                return 'agy_dead', mapped, message
        else:
            return 'unknown_reply', None, message

    prefixed = strip_claude_prefix(message)
    if prefixed is not None:
        session = inbox.latest_live_claude_session()
        return ('claude' if session else 'claude_none'), session, prefixed

    return 'agy', resolve_target_session(), message

def resolve_target_session():
    """Identify the active agy session communicating with tg-bridge or active on system."""
    # 1. Check last recorded session from sender.py
    if os.path.exists(SESSION_PATH):
        try:
            with open(SESSION_PATH) as f:
                data = json.load(f)
            pid = data.get('agy_pid')
            if pid and psutil and psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                cmd = " ".join(proc.cmdline()).lower()
                if "agy" in proc.name().lower() or "agy" in cmd:
                    return data
        except Exception:
            pass

    # 2. Discover running interactive agy sessions
    if not psutil:
        return None

    candidates = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
        try:
            cmd_list = p.info['cmdline'] or []
            cmd = " ".join(cmd_list).lower()
            name = (p.info['name'] or '').lower()
            if not ('agy' in name or 'agy' in cmd):
                continue
            if '--print' in cmd or ' -p ' in cmd:
                continue
            parent = p.parent()
            if parent:
                parent_cmd = " ".join(parent.cmdline()).lower()
                if "agy" in parent.name().lower() or "agy" in parent_cmd:
                    continue
            candidates.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not candidates:
        return None

    # Pick the most recently started/active session
    candidates.sort(key=lambda p: p.create_time(), reverse=True)
    target = candidates[0]

    terminal = None
    try:
        terminal = target.terminal()
    except Exception:
        pass

    emulator_name = None
    emulator_pid = None
    curr = target
    ancestry_pids = [target.pid]
    while curr.parent():
        curr = curr.parent()
        ancestry_pids.append(curr.pid)
        pname = curr.name().lower()
        if pname in ('contour', 'ptyxis', 'gnome-terminal-server', 'kitty', 'alacritty', 'wezterm-gui', 'foot', 'xterm'):
            emulator_name = pname
            emulator_pid = curr.pid
            break
        elif pname == 'ptyxis-agent':
            parent = curr.parent()
            if parent and 'ptyxis' in parent.name().lower():
                emulator_name = 'ptyxis'
                emulator_pid = parent.pid
                ancestry_pids.append(parent.pid)
                break
        if not terminal:
            try:
                t = curr.terminal()
                if t:
                    terminal = t
            except Exception:
                pass

    return {
        'kind': 'agy',
        'agy_pid': target.pid,
        'terminal': terminal,
        'emulator_name': emulator_name,
        'emulator_pid': emulator_pid,
        'ancestry_pids': ancestry_pids,
        'cwd': target.cwd() if hasattr(target, 'cwd') else None
    }

def inject_prompt_into_session(session=None, env=None):
    """Activate target terminal window and simulate paste (Ctrl+Shift+V) + Enter."""
    if session is None:
        session = resolve_target_session()

    ydotool_env = os.environ.copy()
    if env:
        ydotool_env.update(env)
    ydotool_env['YDOTOOL_SOCKET'] = '/tmp/.ydotool_socket'

    # 1. Check and wake display if in power saving mode (DPMS)
    try:
        res = subprocess.run([
            'gdbus', 'call', '--session',
            '--dest', 'org.gnome.Mutter.DisplayConfig',
            '--object-path', '/org/gnome/Mutter/DisplayConfig',
            '--method', 'org.freedesktop.DBus.Properties.Get',
            'org.gnome.Mutter.DisplayConfig',
            'PowerSaveMode'
        ], env=env, capture_output=True, text=True, check=False)
        # PowerSaveMode: 0=On, 1=Standby, 2=Suspend, 3=Off
        if '<0>' not in res.stdout and '(<0>,)' not in res.stdout:
            subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'org.gnome.Mutter.DisplayConfig',
                '--object-path', '/org/gnome/Mutter/DisplayConfig',
                '--method', 'org.freedesktop.DBus.Properties.Set',
                'org.gnome.Mutter.DisplayConfig',
                'PowerSaveMode',
                '<0>'
            ], env=env, capture_output=True, check=False)
            subprocess.run(['ydotool', 'key', '-d', '10', '42:1', '42:0'], env=ydotool_env, capture_output=True, check=False)
            time.sleep(0.3)
    except Exception:
        pass

    # 2. Activate/Focus the target emulator window via GNOME Shell / clipman
    pids_to_try = []
    if session:
        if session.get('emulator_pid'):
            pids_to_try.append(session['emulator_pid'])
        if session.get('ancestry_pids'):
            for p in reversed(session['ancestry_pids']):
                if p not in pids_to_try:
                    pids_to_try.append(p)
        if session.get('agy_pid') and session['agy_pid'] not in pids_to_try:
            pids_to_try.append(session['agy_pid'])

    for pid in pids_to_try:
        try:
            subprocess.run([
                'gdbus', 'call', '--session',
                '--dest', 'com.clipman.Daemon',
                '--object-path', '/com/clipman/Daemon',
                '--method', 'com.clipman.Daemon.ActivateWindowByPid',
                str(pid)
            ], env=env, capture_output=True, check=False)
        except Exception:
            pass

    emulator_name = session.get('emulator_name') if session else None
    if emulator_name in ('ptyxis', 'ptyxis-agent'):
        subprocess.run([
            'gdbus', 'call', '--session',
            '--dest', 'org.gnome.Ptyxis',
            '--object-path', '/org/gnome/Ptyxis',
            '--method', 'org.gtk.Application.Activate',
            '{}'
        ], env=env, capture_output=True, check=False)

    # Allow Wayland window focus to settle
    time.sleep(0.25)

    # 3. Check if screen is locked
    screen_locked = False
    try:
        res = subprocess.run([
            'gdbus', 'call', '--session',
            '--dest', 'org.gnome.ScreenSaver',
            '--object-path', '/org/gnome/ScreenSaver',
            '--method', 'org.gnome.ScreenSaver.GetActive'
        ], env=env, capture_output=True, text=True, check=False)
        if '(true,)' in res.stdout:
            screen_locked = True
    except Exception:
        pass

    if screen_locked:
        return session, "locked"

    # 4. Simulate Paste (Ctrl+Shift+V)
    pasted = False
    try:
        res = subprocess.run([
            'gdbus', 'call', '--session',
            '--dest', 'com.clipman.Daemon',
            '--object-path', '/com/clipman/Daemon',
            '--method', 'com.clipman.Daemon.SimulatePaste',
            'ctrl-shift-v'
        ], env=env, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            pasted = True
    except Exception:
        pass

    if not pasted:
        # Fallback paste via ydotool (Ctrl+Shift+V): KEY_LEFTCTRL(29), KEY_LEFTSHIFT(42), KEY_V(47)
        subprocess.run(
            ['ydotool', 'key', '-d', '10', '29:1', '42:1', '47:1', '47:0', '42:0', '29:0'],
            env=ydotool_env, capture_output=True, check=False
        )

    # Wait for the terminal to absorb the paste buffer
    time.sleep(0.18)

    # 5. Simulate Enter (KEY_ENTER 28) to SUBMIT the prompt
    subprocess.run(
        ['ydotool', 'key', '-d', '15', '28:1', '28:0'],
        env=ydotool_env, capture_output=True, check=False
    )

    return session, "submitted"

def download_file(token, file_id, dest_folder):
    os.makedirs(dest_folder, exist_ok=True)
    info_url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
    req = urllib.request.Request(info_url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            file_info = json.loads(resp.read().decode())
        if not file_info.get('ok'):
            return None
        remote_path = file_info['result']['file_path']
        ext = os.path.splitext(remote_path)[1] or '.jpg'
        timestamp = int(time.time())
        local_filename = f"tg_image_{timestamp}_{file_id[:8]}{ext}"
        local_path = os.path.join(dest_folder, local_filename)

        download_url = f"https://api.telegram.org/file/bot{token}/{remote_path}"
        urllib.request.urlretrieve(download_url, local_path)
        return local_path
    except Exception as e:
        sys.stderr.write(f"Error downloading file {file_id}: {e}\n")
        return None

def extract_payload(token, message):
    """Normalise a Telegram message into a payload dict, downloading images.

    Returns None for unsupported messages or failed downloads (ignored, as before).
    """
    if 'photo' in message:
        file_id = message['photo'][-1]['file_id']
        payload_type = 'photo'
    elif 'document' in message and message['document'].get('mime_type', '').startswith('image/'):
        file_id = message['document']['file_id']
        payload_type = 'document'
    elif 'text' in message:
        text = message['text']
        return {'type': 'text', 'text': text, 'caption': None, 'image_path': None, 'staged': text}
    else:
        return None

    caption = message.get('caption', '').strip()
    local_img_path = download_file(token, file_id, IMAGES_DIR)
    if not local_img_path:
        return None
    staged_text = f"{caption} [Image: {local_img_path}]".strip() if caption else local_img_path
    return {'type': payload_type, 'text': caption, 'caption': caption or None,
            'image_path': local_img_path, 'staged': staged_text}

def stage_clipboard(text, env):
    subprocess.run(['wl-copy', text], input=text.encode(), env=env, check=False)
    subprocess.run(['wl-copy', '--primary', text], input=text.encode(), env=env, check=False)

def notify_received(payload, env):
    if payload['type'] == 'photo':
        notify_body = f"Saved: {os.path.basename(payload['image_path'])}"
        if payload['caption']:
            notify_body += f"\nCaption: {payload['caption']}"
        subprocess.run([
            'notify-send',
            '-i', payload['image_path'],
            'Telegram Image Received',
            notify_body,
            '-a', 'tg-bridge'
        ], env=env, check=False)
    elif payload['type'] == 'document':
        subprocess.run([
            'notify-send',
            '-i', payload['image_path'],
            'Telegram Image Document',
            f"Saved: {os.path.basename(payload['image_path'])}",
            '-a', 'tg-bridge'
        ], env=env, check=False)
    else:
        subprocess.run([
            'notify-send',
            'Telegram Prompt Received',
            f"Staged in clipboard: {payload['text'][:60]}",
            '-a', 'tg-bridge'
        ], env=env, check=False)

def ack_prefix(payload):
    if payload['type'] == 'photo':
        return f"Image received and saved to: <code>{payload['image_path']}</code>"
    if payload['type'] == 'document':
        return f"Image document received and saved to: <code>{payload['image_path']}</code>"
    return f'Received: "<i>{payload["text"]}</i>"'

def claude_label(session):
    agent_lbl = session.get('agent') or 'Claude Code'
    title_lbl = session.get('title', '')
    target_desc = f"{agent_lbl} | {title_lbl}" if title_lbl else agent_lbl
    return f"{target_desc} (PID {session.get('claude_pid')})"

def process_update(update, token, env):
    """Route one authorized Telegram update to agy (paste) or Claude Code (inbox)."""
    message = update.get('message', {})
    from_id = message.get('from', {}).get('id')
    message_id = message.get('message_id')

    route, target_session, message = resolve_route(message)

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Update {update.get('update_id')}: route={route}, target_pid={target_session.get('agy_pid') or target_session.get('claude_pid') if target_session else None}")
    sys.stdout.flush()

    if route == 'unknown_reply':
        subprocess.run(['notify-send', 'Telegram reply dropped',
                        'Unknown target session for reply', '-a', 'tg-bridge'], env=env, check=False)
        send_tg_reply(token, from_id,
                      "Could not determine target session for replied message. Please reply to an active session message, or use /claude.",
                      reply_to_message_id=message_id)
        return route

    if route == 'agy_dead':
        pid = target_session.get('agy_pid') if target_session else None
        title = target_session.get('title') if target_session else "Target"
        subprocess.run(['notify-send', 'Telegram reply dropped',
                        f"Session '{title}' has ended", '-a', 'tg-bridge'], env=env, check=False)
        send_tg_reply(token, from_id,
                      f"Antigravity session '{title}' (PID {pid}) is no longer running. Message not delivered.",
                      reply_to_message_id=message_id)
        return route

    if route == 'claude_none':
        subprocess.run(['notify-send', 'Telegram /claude message dropped',
                        'No live Claude Code session', '-a', 'tg-bridge'], env=env, check=False)
        send_tg_reply(token, from_id, "No live Claude Code session. Message not delivered.",
                      reply_to_message_id=message_id)
        return route

    payload = extract_payload(token, message)
    if not payload:
        return None

    # Claude Code paths: never touch the clipboard, window focus, paste or ydotool.
    if route == 'claude':
        entry = {
            'update_id': update.get('update_id'),
            'message_id': message_id,
            'date': message.get('date'),
            'text': payload['text'],
            'image_path': payload['image_path'],
            'caption': payload['caption'],
        }
        inbox.append_inbox(target_session['claude_pid'], entry)
        subprocess.run([
            'notify-send',
            'Telegram message queued',
            f"For {claude_label(target_session)}: {(payload['text'] or payload['image_path'] or '')[:60]}",
            '-a', 'tg-bridge'
        ], env=env, check=False)
        send_tg_reply(token, from_id, f"Queued for {claude_label(target_session)}",
                      reply_to_message_id=message_id, session_data=target_session)
        return route

    if route == 'claude_dead':
        subprocess.run(['notify-send', 'Telegram reply dropped',
                        f"{claude_label(target_session)} has ended", '-a', 'tg-bridge'], env=env, check=False)
        send_tg_reply(token, from_id,
                      f"{claude_label(target_session)} has ended. Message not delivered.",
                      reply_to_message_id=message_id)
        return route

    # agy path: inject into target terminal session
    stage_clipboard(payload['staged'], env)
    notify_received(payload, env)

    session, status = inject_prompt_into_session(session=target_session, env=env)
    session_info = ""
    if session and session.get('agy_pid'):
        agent_lbl = session.get('agent', 'Antigravity')
        title_lbl = session.get('title', '')
        term_lbl = session.get('terminal') or session.get('emulator_name') or 'session'
        target_desc = f"{agent_lbl} | {title_lbl}" if title_lbl else agent_lbl
        if status == "locked":
            session_info = f"\n[Display is locked. Input staged in clipboard; unlock screen to submit to {target_desc} (PID {session['agy_pid']}, {term_lbl})]"
        else:
            session_info = f"\n[Pasted and submitted to {target_desc} (PID {session['agy_pid']}, {term_lbl})]"
    else:
        session_info = "\n[Staged in clipboard. Press Ctrl+Shift+V in Antigravity to review]"

    send_tg_reply(
        token,
        from_id,
        f'{ack_prefix(payload)}{session_info}',
        reply_to_message_id=message_id,
        session_data=session or target_session
    )
    return route

def main():
    token, authorized_id = load_credentials()
    os.makedirs(IMAGES_DIR, exist_ok=True)
    print(f"Starting tg-bridge listener for user {authorized_id}...")

    offset = None
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            init_data = json.loads(resp.read().decode())
            if init_data.get('ok') and init_data.get('result'):
                offset = init_data['result'][-1]['update_id'] + 1
    except Exception as e:
        sys.stderr.write(f"Initial getUpdates error: {e}\n")

    env = os.environ.copy()
    if 'WAYLAND_DISPLAY' not in env:
        env['WAYLAND_DISPLAY'] = 'wayland-0'

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=30"
            if offset is not None:
                url += f"&offset={offset}"

            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read().decode())

            if data.get('ok'):
                for update in data.get('result', []):
                    offset = update['update_id'] + 1
                    from_id = update.get('message', {}).get('from', {}).get('id')
                    if from_id != authorized_id:
                        continue

                    process_update(update, token, env)

        except Exception as e:
            sys.stderr.write(f"Polling error: {e}\n")
            time.sleep(3)

if __name__ == '__main__':
    main()
