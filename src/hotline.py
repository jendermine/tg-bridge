"""Emergency hotline: Telegram commands handled by the listener itself.

These work even when the Claude desktop app (or any agent session) is hung or
closed, because the tg-bridge listener runs as its own systemd user service.

Commands (only from the authorized user):
  /help                  list commands
  /status                memory, load, heaviest processes, builds/pushes running
  /stop                  stop runaway work: Gradle daemons/builds, git push/pack,
                         adb screenrecord (does not touch the desktop app)
  /killapp               force-quit a hung Claude desktop app
  /ask [project] <msg>   ask for help, read-only. Uses Claude Code (continuing a
                         fork of the project's latest conversation) when its CLI
                         is signed in, otherwise Antigravity (agy) in plan mode.
                         project = a folder under ~/projects (default keyboardme)
  /do [project] <msg>    like /ask, but Antigravity may edit files and run
                         commands in that project (accept-edits mode)
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

AGY_BIN = os.path.expanduser('~/.local/bin/agy')
COMMANDS = ('/help', '/status', '/stop', '/killapp', '/ask', '/do')

# Context for Antigravity, which has none of the Claude conversation.
_AGY_PREAMBLE = (
    "You are answering through an emergency Telegram hotline on jen's Linux PC, because the "
    "Claude desktop app may be frozen or closed. Working directory: {cwd}. For context on "
    "recent work, look at `git log --oneline -15` and `git status` there. Be brief: the reply "
    "is read on a phone.\n\nMessage: ")


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


def _run_agent(cmd, cwd):
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=ASK_TIMEOUT_S,
                             env={**os.environ, 'TG_BRIDGE_HOTLINE': '1'})
        return (out.stdout or '').strip() or (out.stderr or '').strip() or '(no output)'
    except subprocess.TimeoutExpired:
        return f'(timed out after {ASK_TIMEOUT_S // 60} min)'
    except Exception as e:
        return f'(failed to run {os.path.basename(cmd[0])}: {e})'


def _ask_worker(token, chat_id, reply_to, project, prompt, send, allow_edits=False):
    cwd = os.path.join(PROJECTS_DIR, project)
    started = time.time()
    text = None
    who = 'Claude'
    if not allow_edits and os.path.exists(CLAUDE_BIN):
        text = _run_agent([CLAUDE_BIN, '-c', '--fork-session', '-p', prompt], cwd)
        if 'Not logged in' in text or text.startswith('(failed'):
            text = None
    if text is None:
        who = 'Antigravity' + (' (edits allowed)' if allow_edits else '')
        if not os.path.exists(AGY_BIN):
            text = 'Neither a signed-in Claude Code CLI nor agy is available.'
        else:
            text = _run_agent([AGY_BIN, '-p', _AGY_PREAMBLE.format(cwd=cwd) + prompt,
                               '--mode', 'accept-edits' if allow_edits else 'plan',
                               '--add-dir', cwd,
                               '--print-timeout', f'{ASK_TIMEOUT_S - 30}s'], cwd)
    took = int(time.time() - started)
    header = f'<b>{who} ({_escape(project)}, {took}s):</b>\n'
    body = _escape(text)
    chunks = [body[i:i + TG_LIMIT] for i in range(0, len(body), TG_LIMIT)] or ['']
    for i, chunk in enumerate(chunks):
        send(token, chat_id, (header if i == 0 else '') + chunk,
             reply_to_message_id=reply_to if i == 0 else None)


def cmd_ask(args, token, chat_id, reply_to, send, allow_edits=False):
    parts = args.split(maxsplit=1)
    project = DEFAULT_PROJECT
    if parts and os.path.isdir(os.path.join(PROJECTS_DIR, parts[0])) and len(parts) > 1:
        project, args = parts[0], parts[1]
    prompt = args.strip()
    if not prompt:
        return 'Usage: /ask [project] your message'
    threading.Thread(target=_ask_worker,
                     args=(token, chat_id, reply_to, project, prompt, send, allow_edits),
                     daemon=True).start()
    if allow_edits:
        return f'Working on it in {_escape(project)} (Antigravity, edits allowed). Reply comes here.'
    return f'Looking into it in {_escape(project)} (read-only). Reply comes here.'



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
    if cmd == '/do':
        return cmd_ask(rest, token, chat_id, reply_to, send, allow_edits=True)
    return None
