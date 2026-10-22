"""Stand-ins for Steam, helldivers2.exe and the in-game agent, for lifecycle and scenario tests."""
from pathlib import Path
import json
import threading
import time
import types

import support  # noqa: F401
from hd2h import archive
from hd2h.paths import Paths
from test_settings import MAIN, PROFILE

PID = 4242


class FakeGame:
    """Steam + helldivers2.exe. on_wait(game) runs on every wait() while the game is up."""

    def __init__(self):
        self.pids, self.launches, self.on_wait = [], 0, None
        self.state, self.on_continue = 2, None  # game state index; called when continue is pressed
        self.code = None  # exit code of the last exit: 0 for the engine's own quit
        self.handlers = {}  # crash reporters (crs-handler.exe) running: pid -> creation time
        self.handler_pids = set()  # every crash reporter pid ever added (add_handler)

    def add_handler(self, pid, created):
        self.handlers[pid] = created
        self.handler_pids.add(pid)

    def running(self, name=None):
        if name == 'crs-handler.exe':
            return list(self.handlers)
        return list(self.pids)

    def launch(self, timeout=180):
        self.pids, self.launches = [PID], self.launches + 1
        return PID

    def exit(self, code=0):
        self.pids, self.code = [], code

    def process(self, pid, access=None):
        game = self

        class Handle:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def creation_time(self):
                return game.handlers.get(pid, 133000000000000000)

            def wait(self, seconds):
                if pid in game.handler_pids:  # a crash reporter: gone once terminated
                    return pid not in game.handlers
                deadline = time.monotonic() + min(seconds or 0, 3)
                while True:
                    if game.on_wait and pid in game.pids:
                        game.on_wait(game)
                    if pid not in game.pids:
                        return True
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.01)

            def exit_code(self):
                return None if pid in game.pids else game.code

        return Handle()

    def terminate(self, pid, code=1):
        if pid in self.handlers:
            del self.handlers[pid]
            return True
        self.exit(code)
        return True


class FakeAgent(threading.Thread):
    """Answers mailbox commands like the Lua agent: state() and quit()."""

    def __init__(self, mailbox, game, modules=None):
        super().__init__(daemon=True)
        self.mailbox, self.game, self.frames, self.stopping = Path(mailbox), game, 0, False
        self.modules = modules or {'mods/cowboybingus/vanilla_plus_megapack': 'loaded',
                                   'mods/cowboybingus/bingus_test_agent': 'loaded'}

    def write(self, name, marker, payload):
        (self.mailbox / name).write_text(json.dumps(payload) + '\nEND %s\n' % marker)

    def run(self):
        seq = 1
        while not self.stopping:
            command = self.mailbox / ('cmd-%d.lua' % seq)
            if not command.exists():
                time.sleep(0.01)
                continue
            source = command.read_text()
            command.unlink()
            self.frames += 100
            if 'agent.quit()' in source:
                values = [True]
            elif 'continue pressed' in source:
                if self.game.state == 2:
                    if self.game.on_continue:
                        self.game.on_continue(self.game)
                    values = [True, 'continue pressed']
                else:
                    values = [False, 'not on the title screen (state %d)' % self.game.state]
            elif 'num_units' in source:
                values = [2194 if self.game.state == 3 else 1]
            elif 'agent.state()' in source:
                values = [{'frames': self.frames, 'session': 'fake', 'loader': {'modules': self.modules}}]
            else:
                values = [None]
            self.write('res-%d.json' % seq, seq, {'seq': seq, 'ok': True, 'n': len(values), 'values': values})
            seq += 1
            if values == [True]:
                time.sleep(0.05)
                self.write('bye.json', 'bye', {'reason': 'shutdown', 'session': 'fake'})
                self.game.exit()
                return


def fake_install(tmp):
    tmp = Path(tmp)
    paths = Paths(tmp / 'game', tmp / 'appdata', tmp / 'local', tmp / 'harness')
    (paths.game / 'bin').mkdir(parents=True)
    paths.exe.write_bytes(b'exe')
    paths.game_dll.parent.mkdir(parents=True)
    paths.game_dll.write_bytes(b'dll')
    loader = archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0x'})
    for name, data in {'9ba626afa44a3aa3.patch_0': loader, '9ba626afa44a3aa3.patch_1': b'mod'}.items():
        (paths.data / name).write_bytes(data)
        (paths.data / (name + '.gpu_resources')).write_bytes(b'')
        (paths.data / (name + '.stream')).write_bytes(b'')
    paths.saves.mkdir(parents=True)
    paths.settings_file.write_bytes(MAIN.encode())
    (paths.saves / 'abc_user_settings.config').write_bytes(PROFILE.encode())
    paths.dumps.mkdir(parents=True)
    paths.logs.mkdir(parents=True)
    (paths.logs / 'BingusSharedLoader.log').write_text('Bingus Shared Loader loader-v18; API 1\n')
    return paths


def patch_game(stack, game, mock):
    """Routes every process and Steam call in session/scenarios to the fake game."""
    for target, value in (('hd2h.session.running', game.running), ('hd2h.session.launch', game.launch),
                          ('hd2h.session.winapi.Process', game.process),
                          ('hd2h.session.winapi.terminate', game.terminate),
                          ('hd2h.session.winapi.post_close', lambda pid: 0),
                          ('hd2h.scenarios.winapi.Process', game.process),
                          ('hd2h.scenarios.winapi.keep_awake', lambda on: True),
                          # session's own pauses (after closing a game, between polls) cost no test time
                          ('hd2h.session.time', types.SimpleNamespace(monotonic=time.monotonic,
                                                                      sleep=lambda seconds: None)),
                          # nothing writes dumps late in a fake session
                          ('hd2h.session.DUMP_WAIT', 0.0), ('hd2h.session.DUMP_QUIET', 0.0)):
        stack.enter_context(mock.patch(target, value))
