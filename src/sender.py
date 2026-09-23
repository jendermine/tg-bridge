#!/usr/bin/env python3
import os
import sys
import re
import html
import subprocess
import urllib.request
import urllib.parse
import json
import time

CRED_PATH = os.path.expanduser('~/Documents/tg.txt')
SESSION_PATH = os.path.expanduser('~/.local/share/tg-bridge/active_session.json')

def record_active_session():
    """Identify the agy session running this command and save to active_session.json."""
    try:
        import psutil
        p = psutil.Process(os.getpid())
        agy_proc = None
        terminal = None
        emulator_proc = None

        curr = p
        while curr.parent():
            curr = curr.parent()
            name = curr.name().lower()
            cmd = " ".join(curr.cmdline()).lower()
            if ("agy" in name or "agy" in cmd) and not agy_proc:
                agy_proc = curr
                try:
                    terminal = curr.terminal()
                except Exception:
                    pass
            elif name in ("contour", "ptyxis", "ptyxis-agent", "gnome-terminal-server", "kitty", "alacritty", "wezterm-gui"):
                if not emulator_proc:
                    emulator_proc = curr

        if not terminal:
            curr = p
            while curr.parent():
                curr = curr.parent()
                try:
                    t = curr.terminal()
                    if t:
                        terminal = t
                        break
                except Exception:
                    pass

        session_data = {
            "agy_pid": agy_proc.pid if agy_proc else None,
            "terminal": terminal,
            "emulator_name": emulator_proc.name() if emulator_proc else None,
            "emulator_pid": emulator_proc.pid if emulator_proc else None,
            "cwd": agy_proc.cwd() if (agy_proc and hasattr(agy_proc, 'cwd')) else os.getcwd(),
            "updated_at": time.time()
        }
        os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
        with open(SESSION_PATH, 'w') as f:
            json.dump(session_data, f, indent=2)
    except Exception as e:
        sys.stderr.write(f"Warning: could not record active session: {e}\n")

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

def send_photo(token, chat_id, photo_path, caption=""):
    if not os.path.exists(photo_path):
        return False
    cmd = [
        'curl', '-s', '-X', 'POST',
        f'https://api.telegram.org/bot{token}/sendPhoto',
        '-F', f'chat_id={chat_id}',
        '-F', f'photo=@{photo_path}'
    ]
    if caption:
        cmd.extend(['-F', f'caption={caption[:1024]}'])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        data = json.loads(res.stdout)
        return data.get('ok', False)
    except Exception as e:
        sys.stderr.write(f"Telegram sendPhoto error: {e}\n")
        return False

def markdown_to_tg_html(text: str):
    images = re.findall(r'!\[([^\]]*)\]\(([^)]+)\)', text)
    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'<b>[Image: \1]</b>', text)

    blocks = []
    def save_block(m):
        blocks.append(m.group(1))
        return f"___CB_{len(blocks)-1}___"
    text = re.sub(r'```(?:\w+)?\n?(.*?)```', save_block, text, flags=re.DOTALL)

    inlines = []
    def save_inline(m):
        inlines.append(m.group(1))
        return f"___IN_{len(inlines)-1}___"
    text = re.sub(r'`([^`]+)`', save_inline, text)

    tag_placeholders = []
    def save_tag(m):
        tag_placeholders.append(m.group(0))
        return f"___TAG_{len(tag_placeholders)-1}___"
    text = re.sub(r'</?(?:b|i|code|pre|a|u|s|blockquote)(?:\s+[^>]*?)?>', save_tag, text)

    text = html.escape(text)

    for i, tag in enumerate(tag_placeholders):
        text = text.replace(f"___TAG_{i}___", tag)

    text = re.sub(r'(?m)^#{1,6}\s+(.+)$', r'<b>\1</b>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\w)\*([^*]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_]+?)_(?!\w)', r'<i>\1</i>', text)

    for i, code in enumerate(inlines):
        text = text.replace(f"___IN_{i}___", f"<code>{html.escape(code)}</code>")
    for i, code in enumerate(blocks):
        text = text.replace(f"___CB_{i}___", f"<pre><code>{html.escape(code.strip())}</code></pre>")

    return text, images

def send_text_chunk(token, chat_id, text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode

    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True
    except urllib.error.HTTPError as e:
        if parse_mode == "HTML":
            fallback_payload = {'chat_id': chat_id, 'text': re.sub(r'<[^>]+>', '', text)}
            fallback_data = urllib.parse.urlencode(fallback_payload).encode()
            fallback_req = urllib.request.Request(url, data=fallback_data)
            try:
                with urllib.request.urlopen(fallback_req, timeout=15):
                    return True
            except Exception as fe:
                sys.stderr.write(f"Telegram fallback send error: {fe}\n")
        sys.stderr.write(f"Telegram send error: {e}\n")
        return False
    except Exception as e:
        sys.stderr.write(f"Telegram send error: {e}\n")
        return False

def main():
    record_active_session()
    if len(sys.argv) > 1:
        first_arg = sys.argv[1]
        if os.path.isfile(first_arg) and first_arg.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
            token, chat_id = load_credentials()
            caption = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
            send_photo(token, chat_id, first_arg, caption=caption)
            return
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()

    if not text.strip():
        return

    token, chat_id = load_credentials()
    formatted, images = markdown_to_tg_html(text)

    for alt, img_path in images:
        clean_path = img_path.replace('file://', '')
        if os.path.exists(clean_path):
            send_photo(token, chat_id, clean_path, caption=alt or "Image attachment")

    max_len = 3800
    if len(formatted) <= max_len:
        send_text_chunk(token, chat_id, formatted, parse_mode="HTML")
    else:
        lines = formatted.split("\n")
        current_chunk = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > max_len:
                send_text_chunk(token, chat_id, "\n".join(current_chunk), parse_mode="HTML")
                current_chunk = [line]
                current_len = len(line)
            else:
                current_chunk.append(line)
                current_len += len(line) + 1
        if current_chunk:
            send_text_chunk(token, chat_id, "\n".join(current_chunk), parse_mode="HTML")

if __name__ == '__main__':
    main()
