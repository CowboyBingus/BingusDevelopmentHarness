"""Title screen -> ship navigation against the fake game and a fake agent."""
from contextlib import ExitStack
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import support  # noqa: F401
from hd2h import builds, navigate, session
from fakes import FakeAgent, FakeGame, fake_install, patch_game

BUILD = builds.KNOWN['25480438']


class Navigation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = fake_install(self.tmp.name)
        self.game = FakeGame()
        self.game.state = 2
        self.stack = ExitStack()
        patch_game(self.stack, self.game, mock)
        self.stack.enter_context(mock.patch('hd2h.session.live_reads',
                                            lambda pid, build: {'state': self.game.state,
                                                                'state_name': builds.STATES.get(self.game.state)}))
        self.stack.enter_context(mock.patch('hd2h.session.game_dll_base', lambda pid, build: 0x7FF600000000))
        self.stack.enter_context(mock.patch('hd2h.navigate.time', types.SimpleNamespace(
            monotonic=time.monotonic, sleep=lambda seconds: None)))
        self.notes = []

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def start(self):
        def hello(mailbox, session_id, pid, timeout=300):
            FakeAgent(self.paths.mailbox, self.game).start()
            return {}

        with mock.patch('hd2h.session.wait_for_hello', hello):
            return session.start(self.paths, note=self.notes.append)

    def test_presses_continue_and_waits_for_the_ship(self):
        manifest = self.start()
        presses = []
        original = FakeAgent.run

        def on_continue(game):
            presses.append(1)
            def travel():
                for state in (5, 3):
                    time.sleep(0.05)
                    game.state = state
            threading.Thread(target=travel, daemon=True).start()

        self.game.on_continue = on_continue
        manifest['facts']['build'] = '25480438'  # the fake install's binaries are no known build
        reads = navigate.to_ship(self.paths, manifest, note=self.notes.append, settle=0)
        self.assertEqual(reads['state'], 3)
        self.assertEqual(presses, [1])
        self.assertIn('title screen: continue pressed', self.notes)
        self.game.exit()
        session.finish(self.paths)
        self.assertIs(FakeAgent.run, original)

    def test_continue_source_carries_the_build_offsets(self):
        source = navigate.continue_source(BUILD, 0x7FFCAAD20000)
        self.assertIn('%d, %d, %d, %d, %d' % (0x7FFCAAD20000, 0x3326340, 0xAC21C, 0x11E8, 0xAD6A90), source)
        self.assertIn("if state ~= 2 then", source)
        self.assertIn('title + 324', source)

    def test_refuses_without_the_agent_or_offsets(self):
        with self.assertRaises(navigate.NavigationError):
            navigate.to_ship(self.paths, {'facts': {'build': '25480438'}, 'agent': None, 'pid': 1})
        with self.assertRaises(navigate.NavigationError):
            navigate.to_ship(self.paths, {'facts': {'build': None}, 'agent': {'files': {}}, 'pid': 1})


if __name__ == '__main__':
    unittest.main()
