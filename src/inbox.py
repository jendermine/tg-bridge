#!/usr/bin/env python3
"""Claude Code session support for tg-bridge.

Claude Code runs as a `claude` process spawned by the Claude desktop app, with
no terminal or window that can be targeted. Instead of pasting Telegram input,
the listener queues it into a per-process inbox file which the session reads
with `tg-bridge inbox`.
"""
import os
import sys
import json
import time
import fcntl
import argparse

try:
    import psutil
except ImportError:
    psutil = None

BASE_DIR = os.path.expanduser('~/.local/share/tg-bridge')
INBOX_DIR = os.path.join(BASE_DIR, 'inbox')
# Claude Code sessions are recorded here, never in the agy-only active_session.json.
CLAUDE_SESSION_PATH = os.path.join(BASE_DIR, 'active_session_claude.json')
MAX_CLAUDE_SESSIONS = 20

POLL_INTERVAL = 2.0
DEFAULT_WAIT = 1800.0


def is_claude_process(proc):
    """True if proc is a Claude Code CLI process (not the desktop app)."""
    try:
        if proc.name().lower() == 'claude':
            return True
        cmd = proc.cmdline()
        return bool(cmd) and '/claude-code/' in cmd[0]
    except Exception:
        return False


def is_agy_process(proc):
    """Strict agy check (by process name) used only to decide precedence."""
    try:
        return 'agy' in proc.name().lower()
    except Exception:
        return False


def find_claude_ancestor(start_pid=None):
    """Return the nearest Claude Code ancestor, unless an agy process is nearer."""
    if not psutil:
        return None
    try:
        curr = psutil.Process(start_pid or os.getpid())
        while curr.parent():
            curr = curr.parent()
            if is_agy_process(curr):
                return None
            if is_claude_process(curr):
                return curr
    except Exception:
        pass
    return None


def claude_session_data(proc, agent_override=None, title_override=None):
    try:
        cwd = proc.cwd()
    except Exception:
        cwd = os.getcwd()
    try:
        started = proc.create_time()
    except Exception:
        started = None
    return {
        "kind": "claude",
        "claude_pid": proc.pid,
        "claude_create_time": started,
        "cwd": cwd,
        "agent": agent_override or "Claude Code",
        "title": title_override or os.path.basename(cwd.rstrip('/')) or cwd,
        "timestamp": time.time(),
    }


def is_claude_session_alive(session):
    """True if the recorded claude_pid still belongs to the same Claude Code process."""
    pid = session.get('claude_pid') if session else None
    if not pid or not psutil:
        return False
    try:
        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        if not is_claude_process(proc):
            return False
        started = session.get('claude_create_time')
        if started and abs(proc.create_time() - started) > 1.0:
            return False
        return True
    except Exception:
        return False


def _load_claude_sessions():
    try:
        with open(CLAUDE_SESSION_PATH) as f:
            data = json.load(f)
        sessions = data.get('sessions', {})
        return sessions if isinstance(sessions, dict) else {}
    except Exception:
        return {}


def record_claude_session(session):
    """Upsert a Claude Code session (keyed by claude_pid) into active_session_claude.json."""
    os.makedirs(os.path.dirname(CLAUDE_SESSION_PATH), exist_ok=True)
    with open(CLAUDE_SESSION_PATH + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sessions = _load_claude_sessions()
        sessions[str(session['claude_pid'])] = session
        if len(sessions) > MAX_CLAUDE_SESSIONS:
            keep = sorted(sessions, key=lambda k: sessions[k].get('timestamp', 0))[-MAX_CLAUDE_SESSIONS:]
            sessions = {k: sessions[k] for k in keep}
        tmp = CLAUDE_SESSION_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'sessions': sessions}, f, indent=2)
        os.replace(tmp, CLAUDE_SESSION_PATH)


def latest_live_claude_session():
    """Most recently active Claude Code session whose process is still alive, or None."""
    sessions = sorted(_load_claude_sessions().values(), key=lambda s: s.get('timestamp', 0), reverse=True)
    for session in sessions:
        if is_claude_session_alive(session):
            return session
    return None


def inbox_paths(pid):
    stem = os.path.join(INBOX_DIR, f'claude-{pid}')
    return stem + '.jsonl', stem + '.done.jsonl', stem + '.lock'


class _Locked:
    def __init__(self, pid):
        os.makedirs(INBOX_DIR, exist_ok=True)
        self.lock_path = inbox_paths(pid)[2]

    def __enter__(self):
        self.fd = open(self.lock_path, 'a')
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def append_inbox(pid, entry):
    """Append one message entry to the inbox of a Claude Code process."""
    path = inbox_paths(pid)[0]
    with _Locked(pid):
        with open(path, 'a') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
    return path


def read_inbox(pid, consume=True):
    """Return unread entries; when consume, move them to the .done.jsonl file."""
    path, done_path, _ = inbox_paths(pid)
    if not os.path.exists(path):
        return []
    with _Locked(pid):
        with open(path) as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        entries = []
        for ln in lines:
            try:
                entries.append(json.loads(ln))
            except ValueError:
                continue
        if consume and lines:
            with open(done_path, 'a') as f:
                f.write('\n'.join(lines) + '\n')
                f.flush()
                os.fsync(f.fileno())
            with open(path, 'w'):
                pass
    return entries


def format_entry(entry):
    text = entry.get('text') or ''
    out = f"[tg {entry.get('message_id')}] {text}".rstrip()
    if entry.get('image_path'):
        out += f" [Image: {entry['image_path']}]"
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tg-bridge inbox',
                                     description="Read Telegram messages queued for this Claude Code session")
    parser.add_argument('--wait', nargs='?', type=float, const=DEFAULT_WAIT, default=None, metavar='SECONDS',
                        help=f"Block until a message arrives (default timeout {int(DEFAULT_WAIT)}s, exit 3 on timeout)")
    parser.add_argument('--peek', action='store_true', help="Print without marking messages as read")
    args = parser.parse_args(argv)

    proc = find_claude_ancestor()
    if not proc:
        sys.stderr.write("tg-bridge inbox: not running inside a Claude Code session.\n")
        return 2

    consume = not args.peek
    entries = read_inbox(proc.pid, consume=consume)
    if args.wait is not None:
        deadline = time.time() + args.wait
        while not entries and time.time() < deadline:
            time.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.time())))
            entries = read_inbox(proc.pid, consume=consume)
        if not entries:
            return 3

    for entry in entries:
        print(format_entry(entry))
    return 0


if __name__ == '__main__':
    sys.exit(main())
