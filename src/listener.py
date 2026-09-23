#!/usr/bin/env python3
import os
import sys
import time
import json
import urllib.request
import urllib.parse
import subprocess

CRED_PATH = os.path.expanduser('~/Documents/tg.txt')
BASE_DIR = os.path.expanduser('~/.local/share/tg-bridge')
IMAGES_DIR = os.path.join(BASE_DIR, 'images')
SESSION_PATH = os.path.join(BASE_DIR, 'active_session.json')

def resolve_target_session():
    """Identify the active agy session communicating with tg-bridge or active on system."""
    try:
        import psutil
    except ImportError:
        psutil = None

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
    while curr.parent():
        curr = curr.parent()
        pname = curr.name().lower()
        if pname in ('contour', 'ptyxis', 'ptyxis-agent', 'gnome-terminal-server', 'kitty', 'alacritty', 'wezterm-gui'):
            emulator_name = pname
            emulator_pid = curr.pid
            break
        if not terminal:
            try:
                t = curr.terminal()
                if t:
                    terminal = t
            except Exception:
                pass

    return {
        'agy_pid': target.pid,
        'terminal': terminal,
        'emulator_name': emulator_name,
        'emulator_pid': emulator_pid,
        'cwd': target.cwd() if hasattr(target, 'cwd') else None
    }

def inject_prompt_into_session(session=None, env=None):
    """Activate target terminal window and simulate paste (Ctrl+Shift+V) + Enter."""
    if session is None:
        session = resolve_target_session()

    emulator_pid = session.get('emulator_pid') if session else None
    emulator_name = session.get('emulator_name') if session else None

    # 1. Activate/Focus the target emulator window
    if emulator_name in ('ptyxis', 'ptyxis-agent'):
        subprocess.run([
            'gdbus', 'call', '--session',
            '--dest', 'org.gnome.Ptyxis',
            '--object-path', '/org/gnome/Ptyxis',
            '--method', 'org.gtk.Application.Activate',
            '{}'
        ], env=env, capture_output=True, check=False)

    # Allow Wayland window focus to settle
    time.sleep(0.12)

    # 2. Simulate Paste (Ctrl+Shift+V)
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

    ydotool_env = os.environ.copy()
    if env:
        ydotool_env.update(env)
    ydotool_env['YDOTOOL_SOCKET'] = '/tmp/.ydotool_socket'

    if not pasted:
        # Fallback paste via ydotool (Ctrl+Shift+V): KEY_LEFTCTRL(29), KEY_LEFTSHIFT(42), KEY_V(47)
        subprocess.run(
            ['ydotool', 'key', '-d', '10', '29:1', '42:1', '47:1', '47:0', '42:0', '29:0'],
            env=ydotool_env, capture_output=True, check=False
        )

    # Wait for the terminal to absorb the paste buffer
    time.sleep(0.15)

    # 3. Simulate Enter (KEY_ENTER 28) to SUBMIT the prompt!
    subprocess.run(
        ['ydotool', 'key', '-d', '15', '28:1', '28:0'],
        env=ydotool_env, capture_output=True, check=False
    )

    return session

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

def send_tg_reply(token, chat_id, text):
    sender_script = os.path.join(BASE_DIR, 'sender.py')
    if os.path.exists(sender_script):
        subprocess.run(['python3', sender_script, text], check=False)
    else:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass

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
                    message = update.get('message', {})
                    from_id = message.get('from', {}).get('id')

                    if from_id != authorized_id:
                        continue

                    # 1. Photo
                    if 'photo' in message:
                        photo_list = message['photo']
                        largest_photo = photo_list[-1]
                        file_id = largest_photo['file_id']
                        caption = message.get('caption', '').strip()

                        local_img_path = download_file(token, file_id, IMAGES_DIR)
                        if local_img_path:
                            staged_text = f"{caption} [Image: {local_img_path}]".strip() if caption else local_img_path
                            subprocess.run(['wl-copy', staged_text], input=staged_text.encode(), env=env, check=False)
                            subprocess.run(['wl-copy', '--primary', staged_text], input=staged_text.encode(), env=env, check=False)

                            notify_body = f"Saved: {os.path.basename(local_img_path)}"
                            if caption:
                                notify_body += f"\nCaption: {caption}"
                            subprocess.run([
                                'notify-send',
                                '-i', local_img_path,
                                'Telegram Image Received',
                                notify_body,
                                '-a', 'tg-bridge'
                            ], env=env, check=False)

                            session = inject_prompt_into_session(env=env)
                            session_info = ""
                            if session and session.get('agy_pid'):
                                term_info = session.get('terminal') or session.get('emulator_name') or 'session'
                                session_info = f"\n✓ Pasted & submitted to Antigravity (PID <code>{session['agy_pid']}</code>, <code>{term_info}</code>)."
                            else:
                                session_info = "\nStaged in clipboard. Press <b>Ctrl+Shift+V</b> in Antigravity to review."

                            send_tg_reply(
                                token,
                                from_id,
                                f'Image received and saved to: <code>{local_img_path}</code>{session_info}'
                            )

                    # 2. Document (Image)
                    elif 'document' in message and message['document'].get('mime_type', '').startswith('image/'):
                        doc = message['document']
                        file_id = doc['file_id']
                        caption = message.get('caption', '').strip()

                        local_img_path = download_file(token, file_id, IMAGES_DIR)
                        if local_img_path:
                            staged_text = f"{caption} [Image: {local_img_path}]".strip() if caption else local_img_path
                            subprocess.run(['wl-copy', staged_text], input=staged_text.encode(), env=env, check=False)
                            subprocess.run(['wl-copy', '--primary', staged_text], input=staged_text.encode(), env=env, check=False)

                            subprocess.run([
                                'notify-send',
                                '-i', local_img_path,
                                'Telegram Image Document',
                                f"Saved: {os.path.basename(local_img_path)}",
                                '-a', 'tg-bridge'
                            ], env=env, check=False)

                            session = inject_prompt_into_session(env=env)
                            session_info = ""
                            if session and session.get('agy_pid'):
                                term_info = session.get('terminal') or session.get('emulator_name') or 'session'
                                session_info = f"\n✓ Pasted & submitted to Antigravity (PID <code>{session['agy_pid']}</code>, <code>{term_info}</code>)."
                            else:
                                session_info = "\nStaged in clipboard. Press <b>Ctrl+Shift+V</b> in Antigravity to review."

                            send_tg_reply(
                                token,
                                from_id,
                                f'Image document received and saved to: <code>{local_img_path}</code>{session_info}'
                            )

                    # 3. Plain Text
                    elif 'text' in message:
                        text = message['text']
                        subprocess.run(['wl-copy', text], input=text.encode(), env=env, check=False)
                        subprocess.run(['wl-copy', '--primary', text], input=text.encode(), env=env, check=False)

                        subprocess.run([
                            'notify-send',
                            'Telegram Prompt Received',
                            f'Staged in clipboard: {text[:60]}',
                            '-a', 'tg-bridge'
                        ], env=env, check=False)

                        session = inject_prompt_into_session(env=env)
                        session_info = ""
                        if session and session.get('agy_pid'):
                            term_info = session.get('terminal') or session.get('emulator_name') or 'session'
                            session_info = f"\n✓ Pasted & submitted to Antigravity (PID <code>{session['agy_pid']}</code>, <code>{term_info}</code>)."
                        else:
                            session_info = "\nStaged in clipboard. Press <b>Ctrl+Shift+V</b> in Antigravity to review."

                        send_tg_reply(
                            token,
                            from_id,
                            f'Received: "<i>{text}</i>"{session_info}'
                        )

        except Exception as e:
            sys.stderr.write(f"Polling error: {e}\n")
            time.sleep(3)

if __name__ == '__main__':
    main()
