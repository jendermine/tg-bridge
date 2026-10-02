"""Emergency hotline: Telegram commands handled by the listener itself.

These work even when the Claude desktop app (or any agent session) is hung or
closed, because the tg-bridge listener runs as its own systemd user service.

Commands (only from the authorized user):
  /help                  list commands
  /status                memory, load, heaviest processes, builds/pushes running
  /stop                  stop runaway work: Gradle daemons/builds, git push/pack,
                         adb screenrecord (does not touch the desktop app)
  /killapp               force-quit a hung Claude desktop app
  /ask [project] <msg>   talk to Claude Code headlessly: continues (as a fork) the
                         latest conversation of that project and replies here.
                         project = a folder name under ~/projects (default
                         keyboardme), e.g. "/ask tg-bridge why is X failing"
"""

import os
import shlex
import signal
import subprocess
import threading
import time

PROJECTS_DIR = os.path.expanduser('~/projects')
DEFAULT_PROJECT = 'keyboardme'
CLAUDE_BIN = os.path.expanduser('~/.local/bin/claude')
ASK_TIMEOUT_S = 15 * 60
TG_LIMIT = 3900

COMMANDS = ('/help', '/status', '/stop', '/killapp', '/ask')


def is_hotline(text):
    if not text:
        return False
    word = text.strip().split(maxsplit=1)[0].lower()
    word = word.split('@', 1)[0]  # /status@botname
    return word in COMMANDS


def _run(cmd, timeout=10):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or '') + (out.stderr or '')
    except Exception as e:
        return f'({e})'


def _escape(text):
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _pre(text):
    return f'<pre>{_escape(text.strip())[:TG_LIMIT]}</pre>'


# Process patterns considered "runaway work" for /stop.
_STOP_PATTERNS = [
    ('Gradle daemons', 'org.gradle.launcher.daemon'),
    ('Gradle wrapper', 'org.gradle.wrapper.GradleWrapperMain'),
    ('Kotlin daemons', 'KotlinCompileDaemon'),
    ('git push', 'git.* push'),
    ('git pack-objects', 'git pack-objects'),
    ('git send-pack', 'git send-pack'),
    ('adb screenrecord', 'screenrecord'),
]


def _pids(pattern):
    out = _run(['pgrep', '-f', pattern])
    me = os.getpid()
    return [int(p) for p in out.split() if p.isdigit() and int(p) != me]


def cmd_status():
    mem = _run(['free', '-h']).splitlines()
    load = open('/proc/loadavg').read().split()[:3]
    top = _run(['ps', '-eo', 'pid,rss,etime,comm', '--sort=-rss']).splitlines()[:9]
    lines = ['Load: ' + ' '.join(load)]
    lines += mem[:2]
    lines.append('')
    lines.append('Top memory (RSS KB):')
    lines += top
    busy = []
    for label, pattern in _STOP_PATTERNS:
        n = len(_pids(pattern))
        if n:
            busy.append(f'{label}: {n}')
    lines.append('')
    lines.append('Running work: ' + (', '.join(busy) if busy else 'none'))
    app = len(_pids('claude-desktop'))
    lines.append(f'Claude desktop processes: {app}')
    return _pre('\n'.join(lines))


def cmd_stop():
    stopped = []
    for label, pattern in _STOP_PATTERNS:
        pids = _pids(pattern)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if pids:
            stopped.append(f'{label} ({len(pids)})')
    time.sleep(3)
    killed = []
    for label, pattern in _STOP_PATTERNS:
        for pid in _pids(pattern):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(str(pid))
            except ProcessLookupError:
                pass
    msg = 'Stopped: ' + (', '.join(stopped) if stopped else 'nothing was running')
    if killed:
        msg += f'\nForce-killed (ignored SIGTERM): {", ".join(killed)}'
    return _escape(msg)


def cmd_killapp():
    pids = _pids('claude-desktop')
    if not pids:
        return 'Claude desktop is not running.'
    _run(['pkill', '-TERM', '-f', 'claude-desktop'])
    time.sleep(4)
    left = _pids('claude-desktop')
    if left:
        _run(['pkill', '-KILL', '-f', 'claude-desktop'])
    return (f'Claude desktop closed ({len(pids)} processes'
            + (', force-killed' if left else '') + '). Reopen it when ready.')


def _ask_worker(token, chat_id, reply_to, project, prompt, send):
    cwd = os.path.join(PROJECTS_DIR, project)
    cmd = [CLAUDE_BIN, '-c', '--fork-session', '-p', prompt]
    started = time.time()
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=ASK_TIMEOUT_S,
                             env={**os.environ, 'TG_BRIDGE_HOTLINE': '1'})
        text = (out.stdout or '').strip() or (out.stderr or '').strip() or '(no output)'
    except subprocess.TimeoutExpired:
        text = f'(timed out after {ASK_TIMEOUT_S // 60} min)'
    except Exception as e:
        text = f'(failed to run claude: {e})'
    if 'Not logged in' in text:
        text = ('The Claude Code CLI is not signed in, so /ask cannot reach Claude. '
                'One-time fix on the PC: run `claude` in a terminal, type /login and sign in '
                'with your Claude account. /status, /stop and /killapp work without it.')
    took = int(time.time() - started)
    header = f'<b>Claude ({_escape(project)}, {took}s):</b>\n'
    body = _escape(text)
    chunks = [body[i:i + TG_LIMIT] for i in range(0, len(body), TG_LIMIT)] or ['']
    for i, chunk in enumerate(chunks):
        send(token, chat_id, (header if i == 0 else '') + chunk,
             reply_to_message_id=reply_to if i == 0 else None)


def cmd_ask(args, token, chat_id, reply_to, send):
    parts = args.split(maxsplit=1)
    project = DEFAULT_PROJECT
    if parts and os.path.isdir(os.path.join(PROJECTS_DIR, parts[0])) and len(parts) > 1:
        project, args = parts[0], parts[1]
    prompt = args.strip()
    if not prompt:
        return 'Usage: /ask [project] your message'
    if not os.path.exists(CLAUDE_BIN):
        return f'claude CLI not found at {CLAUDE_BIN}'
    threading.Thread(target=_ask_worker,
                     args=(token, chat_id, reply_to, project, prompt, send),
                     daemon=True).start()
    return (f'Asking Claude in {_escape(project)} (continuing its latest conversation as a '
            f'fork, headless; tools that need approval are not available). '
            f'Reply comes here.')


def handle(text, token, chat_id, reply_to, send):
    """Returns the immediate reply text for a hotline command."""
    head, _, rest = text.strip().partition(' ')
    cmd = head.lower().split('@', 1)[0]
    if cmd == '/help':
        return _pre(__doc__.split('Commands', 1)[1])
    if cmd == '/status':
        return cmd_status()
    if cmd == '/stop':
        return cmd_stop()
    if cmd == '/killapp':
        return cmd_killapp()
    if cmd == '/ask':
        return cmd_ask(rest, token, chat_id, reply_to, send)
    return None
