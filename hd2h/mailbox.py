"""The harness side of the agent's file mailbox (see agent/bingus_test_agent.lua)."""
import json
import os
from pathlib import Path
import shutil
import time

AGENT_FILES = ('hello', 'heartbeat', 'bye')


class MailboxError(RuntimeError):
    pass


def _write_atomic(path, text):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(text, encoding='utf-8', newline='\n')
    os.replace(temporary, path)


def parse_marked(data, marker):
    """The JSON object before a final `END <marker>` line, or None while the file is incomplete."""
    text = data.decode('utf-8', errors='replace').rstrip('\r\n')
    body, separator, last = text.rpartition('\n')
    if not separator or last.strip() != 'END %s' % marker:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


class Mailbox:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, name):
        return self.directory / name

    def begin(self, session_id, poll=0.1, heartbeat=2.0):
        """Starts a fresh session: clears old files and writes session.txt."""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clear(keep_session=False)
        _write_atomic(self.path('next-seq.txt'), '1\n')
        _write_atomic(self.path('session.txt'), 'id=%s\npoll=%s\nheartbeat=%s\n' % (session_id, poll, heartbeat))

    def clear(self, keep_session=True):
        if not self.directory.exists():
            return
        for entry in self.directory.iterdir():
            if entry.is_file() and (not keep_session or entry.name != 'session.txt'):
                entry.unlink()

    def end(self):
        """Ends the session: a running agent sees session.txt gone and goes inert."""
        for name in ('session.txt', 'next-seq.txt'):
            path = self.path(name)
            if path.exists():
                path.unlink()
        for entry in list(self.directory.glob('cmd-*')) + list(self.directory.glob('res-*')):
            entry.unlink()

    def session_id(self):
        path = self.path('session.txt')
        if not path.exists():
            return None
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('id='):
                return line[3:]
        return None

    def read(self, name):
        """hello, heartbeat or bye as a dict, or None if absent or incomplete."""
        path = self.path(name + '.json')
        try:
            return parse_marked(path.read_bytes(), name)
        except OSError:
            return None

    def age(self, name):
        """Seconds since the agent last wrote hello/heartbeat/bye, or None."""
        try:
            return time.time() - self.path(name + '.json').stat().st_mtime
        except OSError:
            return None

    def _next_seq(self):
        try:
            seq = int(self.path('next-seq.txt').read_text(encoding='utf-8').strip())
        except (OSError, ValueError):
            raise MailboxError('no active mailbox session (next-seq.txt missing)')
        return self._resync(seq)

    def _resync(self, seq):
        """Catches up with the agent when it is ahead: a client killed after the agent took its command (before it
        wrote next-seq.txt) leaves the agent one number ahead, and every later command would wait for a number the
        agent has passed (live 2026-10-13: 'did not pick up command 133' while the agent expected 134, so a leave
        failed and stop had to kill the game). The heartbeat carries the agent's next number; only a larger number of
        this session is taken, so a heartbeat written before the last command never moves the sequence back."""
        beat = self.read('heartbeat') or {}
        ahead = beat.get('seq')
        if beat.get('session') != self.session_id() or not isinstance(ahead, int) or ahead <= seq:
            return seq
        for stale in range(seq, ahead):  # the killed client's leftovers
            for name in ('cmd-%d.lua' % stale, 'res-%d.json' % stale):
                if self.path(name).exists():
                    self.path(name).unlink()
        _write_atomic(self.path('next-seq.txt'), '%d\n' % ahead)
        return ahead

    def send(self, source, timeout=15.0, interval=0.05):
        """Runs Lua in the game and returns the agent's response dict.

        The chunk gets the agent helpers as the local `agent`. On a timeout an unread
        command is withdrawn, so the sequence stays in step with the agent.
        """
        if self.session_id() is None:
            raise MailboxError('no active mailbox session')
        seq = self._next_seq()
        command, response = self.path('cmd-%d.lua' % seq), self.path('res-%d.json' % seq)
        if response.exists():
            response.unlink()
        _write_atomic(command, source)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = parse_marked(response.read_bytes(), seq)
            except OSError:
                result = None
            if result is not None:
                _write_atomic(self.path('next-seq.txt'), '%d\n' % (seq + 1))
                for path in (response, command):
                    if path.exists():
                        path.unlink()
                return result
            time.sleep(interval)
        if command.exists():
            command.unlink()  # never picked up: withdraw it and keep this number
            raise MailboxError('agent did not pick up command %d within %.0f s' % (seq, timeout))
        _write_atomic(self.path('next-seq.txt'), '%d\n' % (seq + 1))
        raise MailboxError('agent took command %d but did not answer within %.0f s' % (seq, timeout))

    def call(self, source, timeout=15.0):
        """send() that raises on a Lua error and returns the list of returned values."""
        result = self.send(source, timeout)
        if not result.get('ok'):
            raise MailboxError('%s error: %s' % (result.get('stage'), result.get('error')))
        return result.get('values', [])

    def archive_to(self, target):
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        for name in AGENT_FILES:
            path = self.path(name + '.json')
            if path.exists():
                shutil.copy2(path, target / path.name)
