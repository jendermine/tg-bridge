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
import argparse
import sqlite3

try:
    import psutil
except ImportError:
    psutil = None

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import inbox

CRED_PATH = os.path.expanduser('~/Documents/tg.txt')
BASE_DIR = os.path.expanduser('~/.local/share/tg-bridge')
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

def get_agy_conv_info(pid, cwd=None):
    """Extract conversation ID and title from agy open file descriptors and summaries db."""
    conv_id = None
    title = None
    agent = None

    if pid:
        fd_dir = f'/proc/{pid}/fd'
        if os.path.exists(fd_dir):
            for entry in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, entry))
                    if '/conversations/' in target and target.endswith('.db'):
                        conv_id = os.path.basename(target).replace('.db', '')
                        break
                except Exception:
                    continue

    if conv_id:
        db_path = os.path.expanduser('~/.gemini/antigravity-cli/conversation_summaries.db')
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute('SELECT title, agent_name FROM conversation_summaries WHERE conversation_id = ?', (conv_id,))
                row = cur.fetchone()
                if row:
                    t, a = row
                    if t:
                        title = t.strip()
                    if a:
                        agent = a.strip()
                conn.close()
            except Exception:
                pass

    if not title:
        title = os.path.basename(cwd) if cwd else 'Antigravity'
    if not agent:
        agent = 'Antigravity'

    return {'conv_id': conv_id, 'title': title, 'agent': agent}

def record_active_session(agent_override=None, title_override=None):
    """Identify the agent session running this command and record it.

    agy sessions go to active_session.json (agy-only). Claude Code sessions go
    to active_session_claude.json and never touch the agy state.
    """
    if not psutil:
        return {}

    claude_proc = inbox.find_claude_ancestor()
    if claude_proc:
        try:
            session_data = inbox.claude_session_data(claude_proc, agent_override, title_override)
            inbox.record_claude_session(session_data)
            return session_data
        except Exception as e:
            sys.stderr.write(f"Warning: could not record Claude Code session: {e}\n")
            return {}

    try:
        p = psutil.Process(os.getpid())
        agy_proc = None
        terminal = None
        emulator_proc = None
        ancestry_pids = []

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

            if agy_proc:
                ancestry_pids.append(curr.pid)

            if name in ("contour", "ptyxis", "gnome-terminal-server", "kitty", "alacritty", "wezterm-gui", "foot", "xterm"):
                if not emulator_proc:
                    emulator_proc = curr
            elif name == "ptyxis-agent":
                parent = curr.parent()
                if parent and "ptyxis" in parent.name().lower():
                    if not emulator_proc:
                        emulator_proc = parent
                        ancestry_pids.append(parent.pid)

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

        cwd = agy_proc.cwd() if (agy_proc and hasattr(agy_proc, 'cwd')) else os.getcwd()
        conv_info = get_agy_conv_info(agy_proc.pid if agy_proc else None, cwd)

        if agent_override:
            conv_info['agent'] = agent_override
        if title_override:
            conv_info['title'] = title_override

        session_data = {
            "kind": "agy",
            "agy_pid": agy_proc.pid if agy_proc else None,
            "terminal": terminal,
            "emulator_name": emulator_proc.name() if emulator_proc else None,
            "emulator_pid": emulator_proc.pid if emulator_proc else None,
            "ancestry_pids": ancestry_pids,
            "cwd": cwd,
            "conv_id": conv_info.get('conv_id'),
            "title": conv_info.get('title'),
            "agent": conv_info.get('agent'),
            "timestamp": time.time()
        }
        os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
        with open(SESSION_PATH, 'w') as f:
            json.dump(session_data, f, indent=2)

        return session_data
    except Exception as e:
        sys.stderr.write(f"Warning: could not record active session: {e}\n")
        return {}

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

        # Keep map size bounded to most recent 300 entries
        if len(data) > 300:
            keys = sorted(data.keys(), key=lambda k: data[k].get('timestamp', 0))
            for k in keys[:-300]:
                del data[k]

        os.makedirs(os.path.dirname(MESSAGE_MAP_PATH), exist_ok=True)
        with open(MESSAGE_MAP_PATH, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        sys.stderr.write(f"Warning: could not save message mapping: {e}\n")

def send_photo(token, chat_id, photo_path, caption="", session_data=None):
    if not os.path.exists(photo_path):
        return None
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
        if data.get('ok'):
            msg_id = data.get('result', {}).get('message_id')
            if msg_id and session_data:
                save_message_mapping(msg_id, session_data)
            return msg_id
        return None
    except Exception as e:
        sys.stderr.write(f"Telegram sendPhoto error: {e}\n")
        return None

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

def send_text_chunk(token, chat_id, text, parse_mode="HTML", session_data=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text}
    if parse_mode:
        payload['parse_mode'] = parse_mode

    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_data = json.loads(resp.read().decode())
            if resp_data.get('ok'):
                msg_id = resp_data.get('result', {}).get('message_id')
                if msg_id and session_data:
                    save_message_mapping(msg_id, session_data)
                return msg_id
            return None
    except urllib.error.HTTPError as e:
        if parse_mode == "HTML":
            fallback_payload = {'chat_id': chat_id, 'text': re.sub(r'<[^>]+>', '', text)}
            fallback_data = urllib.parse.urlencode(fallback_payload).encode()
            fallback_req = urllib.request.Request(url, data=fallback_data)
            try:
                with urllib.request.urlopen(fallback_req, timeout=15) as fresp:
                    fresp_data = json.loads(fresp.read().decode())
                    if fresp_data.get('ok'):
                        msg_id = fresp_data.get('result', {}).get('message_id')
                        if msg_id and session_data:
                            save_message_mapping(msg_id, session_data)
                        return msg_id
            except Exception as fe:
                sys.stderr.write(f"Telegram fallback send error: {fe}\n")
        sys.stderr.write(f"Telegram send error: {e}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"Telegram send error: {e}\n")
        return None

def main():
    parser = argparse.ArgumentParser(description="Outbound sender for tg-bridge")
    parser.add_argument('--agent', default=None, help="Agent name override")
    parser.add_argument('--title', default=None, help="Conversation title override")
    parser.add_argument('--no-header', action='store_true', help="Do not include agent/conversation header")
    parser.add_argument('--no-record', action='store_true',
                        help="Do not write active session or message map (used for listener acks)")
    parser.add_argument('content', nargs='*', help="Message text or image file path")

    args = parser.parse_args()

    if args.no_record:
        session_data = {}
    else:
        session_data = record_active_session(agent_override=args.agent, title_override=args.title)

    agent_name = session_data.get('agent', 'Antigravity')
    conv_title = session_data.get('title', 'Session')

    token, chat_id = load_credentials()

    if args.content:
        first_arg = args.content[0]
        if os.path.isfile(first_arg) and first_arg.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
            caption_text = " ".join(args.content[1:]) if len(args.content) > 1 else ""
            if not args.no_header:
                prefix = f"[{agent_name} | {conv_title}] "
                caption_text = prefix + caption_text if caption_text else prefix.strip()
            send_photo(token, chat_id, first_arg, caption=caption_text, session_data=session_data)
            return
        raw_text = " ".join(args.content)
    else:
        raw_text = sys.stdin.read()

    if not raw_text.strip():
        return

    formatted, images = markdown_to_tg_html(raw_text)

    # Attach conversation header
    if not args.no_header:
        header = f"<b>[Agent: {html.escape(agent_name)} | {html.escape(conv_title)}]</b>\n\n"
        formatted = header + formatted

    # Send embedded images first
    for alt, img_path in images:
        clean_path = img_path.replace('file://', '')
        if os.path.exists(clean_path):
            img_caption = f"[{agent_name} | {conv_title}] {alt or 'Image attachment'}"
            send_photo(token, chat_id, clean_path, caption=img_caption, session_data=session_data)

    max_len = 3800
    if len(formatted) <= max_len:
        send_text_chunk(token, chat_id, formatted, parse_mode="HTML", session_data=session_data)
    else:
        lines = formatted.split("\n")
        current_chunk = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > max_len:
                send_text_chunk(token, chat_id, "\n".join(current_chunk), parse_mode="HTML", session_data=session_data)
                current_chunk = [line]
                current_len = len(line)
            else:
                current_chunk.append(line)
                current_len += len(line) + 1
        if current_chunk:
            send_text_chunk(token, chat_id, "\n".join(current_chunk), parse_mode="HTML", session_data=session_data)

if __name__ == '__main__':
    main()
