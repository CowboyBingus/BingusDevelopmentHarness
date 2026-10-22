"""Smoke and soak scenarios against the fake game and a fake agent."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import support  # noqa: F401
from hd2h import builds, deploy, scenarios, session
from hd2h.state import State
from fakes import FakeAgent, FakeGame, fake_install, patch_game
from test_dumps import EXE_BASE, GAME_25480438, minidump

FAST_TIME = types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: None)


def travel_to_ship(game):
    def travel():
        for state in (5, 3):
            time.sleep(0.02)
            game.state = state
    threading.Thread(target=travel, daemon=True).start()


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = fake_install(self.tmp.name)
        self.game = FakeGame()
        self.game.on_continue = travel_to_ship
        self.stack = ExitStack()
        patch_game(self.stack, self.game, mock)
        self.stack.enter_context(mock.patch('hd2h.scenarios.time', FAST_TIME))
        # Patch the module's own name, never time.sleep itself: that would turn every thread's
        # sleep (fake agent, fake game, mailbox) into a busy loop.
        self.stack.enter_context(mock.patch('hd2h.navigate.time', FAST_TIME))
        self.stack.enter_context(mock.patch('hd2h.session.game_dll_base', lambda pid, build: 0x7FF600000000))
        self.stack.enter_context(mock.patch('hd2h.session.live_reads', self.reads))
        # The fake install's binaries are no known build; navigation needs the offsets.
        real_preflight = session.preflight
        self.stack.enter_context(mock.patch('hd2h.session.preflight',
                                            lambda *a, **k: dict(real_preflight(*a, **k), build='25480438')))
        self.data_before = deploy.inventory(self.paths.data)
        self.notes = []

    def reads(self, pid, build):
        return {'state': self.game.state, 'state_name': builds.STATES.get(self.game.state),
                'privacy': 2, 'mission': 0}

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def with_agent(self, modules=None):
        def hello(mailbox, session_id, pid, timeout=300):
            self.game.state = 2
            agent = FakeAgent(self.paths.mailbox, self.game, modules)
            agent.start()
            return {'state': {'loader': {'modules': agent.modules}}}
        return mock.patch('hd2h.session.wait_for_hello', hello)

    def test_smoke_passes_when_everything_behaves(self):
        with self.with_agent():
            verdict = scenarios.smoke(self.paths, settle=0, note=self.notes.append)
        failed = [c for c in verdict['checks'] if not c['passed']]
        self.assertTrue(verdict['passed'], failed)
        self.assertEqual(len(verdict['checks']), 13, 'smoke always checks the ship')
        self.assertTrue(any(c['check'] == 'reached the ship' and c['passed'] for c in verdict['checks']))
        run = Path(verdict['run'])
        self.assertTrue((run / 'verdict.json').exists() and (run / 'snapshot.json').exists())
        self.assertEqual(json.loads((run / 'run.json').read_text())['outcome'], 'clean-quit')
        self.assertEqual(deploy.inventory(self.paths.data), self.data_before)
        self.assertIsNone(State(self.paths.state).load())

    def test_smoke_on_the_ship(self):
        with self.with_agent():
            verdict = scenarios.smoke(self.paths, settle=0, note=self.notes.append, ship=True)
        failed = [c for c in verdict['checks'] if not c['passed']]
        self.assertTrue(verdict['passed'], failed)
        names = [c['check'] for c in verdict['checks']]
        self.assertIn('reached the ship', names)
        self.assertIn('ship scene loaded (busiest world > 500 units)', names)
        self.assertEqual(len(verdict['checks']), 13)
        snapshot = json.loads(Path(verdict['run'], 'snapshot.json').read_text())
        self.assertEqual(snapshot['state']['ship_units'], 2194)

    def mission_fakes(self, deploy_error=None, world=True):
        """mission.deploy/leave and capture.run_shot on the fake game: the steps live in test_mission."""
        def shot(paths, name):
            state = State(paths.state)
            entry = {'name': name, 'world': world and name != 'mission-5s', 'saturation': 90.0, 'detail': 5.0,
                     'file': 'shots/%s.png' % name}
            state.update(shots=list(state.load().get('shots') or []) + [entry])
            return entry

        def deploy(paths, manifest, difficulty=1, planet=None, faction=None, note=print, shoot=None):
            if deploy_error:
                raise deploy_error
            shoot('briefing')
            self.game.state = 4
            State(paths.state).update(in_mission=True, missions=[{'operation': 185, 'planet': 270,
                                                                  'difficulty': difficulty}])
            shoot('landed')
            return {'operation': 185, 'planet': 270, 'difficulty': difficulty, 'mission': 151, 'race': 2}

        def leave(paths, manifest, note=print, shoot=None):
            self.game.state = 3
            State(paths.state).update(in_mission=False)
            shoot('ship-after')
            return {'state': 3, 'to_ship_s': 7.5}

        stack = ExitStack()
        stack.enter_context(mock.patch('hd2h.scenarios.mission.deploy', deploy))
        stack.enter_context(mock.patch('hd2h.scenarios.mission.leave', leave))
        stack.enter_context(mock.patch('hd2h.scenarios.capture.run_shot', shot))
        return stack

    def test_smoke_with_a_mission_round_trip(self):
        with self.with_agent(), self.mission_fakes():
            verdict = scenarios.smoke(self.paths, settle=0, note=self.notes.append, mission_run=True, difficulty=4)
        failed = [c for c in verdict['checks'] if not c['passed']]
        self.assertTrue(verdict['passed'], failed)
        self.assertTrue(verdict['mission'])
        names = [c['check'] for c in verdict['checks']]
        self.assertEqual(len(names), 19)
        for name in ('deployed into a mission and got control', 'lobby is Invite Only in the mission',
                     'a mission capture shows the 3D world', 'back on the ship after the mission',
                     'ship capture after the mission shows the 3D world'):
            self.assertIn(name, names)
        run = json.loads(Path(verdict['run'], 'run.json').read_text())
        self.assertTrue(run['clean'], run['not_clean_because'])
        self.assertEqual(run['missions'][0]['difficulty'], 4)
        self.assertEqual([s['name'] for s in run['shots']], ['briefing', 'landed', 'mission-5s', 'ship-after'])
        self.assertTrue(Path(verdict['run']).name.endswith('-smoke-mission'))

    def test_smoke_mission_failures_are_checks_and_the_session_still_ends(self):
        from hd2h import mission
        with self.with_agent(), self.mission_fakes(deploy_error=mission.MissionError('the launch did not start')):
            verdict = scenarios.smoke(self.paths, settle=0, note=self.notes.append, mission_run=True)
        failed = {c['check']: c['detail'] for c in verdict['checks'] if not c['passed']}
        self.assertEqual(failed, {'deployed into a mission and got control': 'the launch did not start'})
        self.assertIsNone(State(self.paths.state).load())
        with self.with_agent(), self.mission_fakes(world=False):
            verdict = scenarios.smoke(self.paths, settle=0, note=self.notes.append, mission_run=True)
        failed = [c['check'] for c in verdict['checks'] if not c['passed']]
        self.assertEqual(failed, ['a mission capture shows the 3D world',
                                  'ship capture after the mission shows the 3D world'])

    def test_a_session_that_ends_inside_a_mission_is_not_clean(self):
        with self.with_agent():
            manifest = session.start(self.paths, note=self.notes.append)
        State(self.paths.state).update(in_mission=True, reached_ship='x')
        self.game.exit()
        summary = session.finish(self.paths, note=self.notes.append)
        self.assertIn('ended inside a mission', summary['not_clean_because'])

    def test_smoke_reports_a_failed_module(self):
        with self.with_agent({'mods/cowboybingus/bingus_test_agent': 'loaded',
                              'mods/cowboybingus/consistent_vaulting': 'load failed: boom'}):
            verdict = scenarios.smoke(self.paths, settle=0, note=self.notes.append)
        self.assertFalse(verdict['passed'])
        failed = {c['check']: c['detail'] for c in verdict['checks'] if not c['passed']}
        self.assertEqual(list(failed), ['no loader module failed'])
        self.assertEqual(failed['no loader module failed'], {'mods/cowboybingus/consistent_vaulting': 'load failed: boom'})

    def test_mod_problems_reads_status_lines(self):
        state = {'mods': {'A': {'status': 'waiting_for_mission'}, 'B': {'status': 'stopped_after_update_error'},
                          'C': {'status': 'unsupported: build'}, 'D': True}}
        self.assertEqual(scenarios.mod_problems(state), {'B': 'stopped_after_update_error',
                                                         'C': 'unsupported: build'})

    def test_cycle_order(self):
        self.assertEqual(scenarios.cycle_order(['a', 'b'], 6), ['a', 'b', 'b', 'a', 'a', 'b'])
        self.assertEqual(scenarios.cycle_order(['a'], 3), ['a', 'a', 'a'])
        self.assertEqual(scenarios.cycle_order(['a', 'b', 'c'], 4), ['a', 'b', 'c', 'a'])

    def test_vanilla_soak_counts_a_classified_crash(self):
        def crash_in_second_cycle(game):
            if game.launches == 2:
                (self.paths.dumps / 'dump-cycle2.dmp').write_bytes(
                    minidump(builds.ACCESS_VIOLATION, EXE_BASE + 0x6C07A6, GAME_25480438))
                game.exit()

        self.game.on_wait = crash_in_second_cycle
        summary = scenarios.soak(self.paths, conditions='vanilla', minutes=0.002, cycles=2, note=self.notes.append)
        self.assertEqual(summary['cycles'], 2)
        vanilla = summary['per_condition']['vanilla']
        self.assertEqual((vanilla['crashes'], vanilla['signatures']), (1, ['gpu-watchdog']))
        lines = [json.loads(line) for line in Path(summary['folder'], 'soak.jsonl').read_text().splitlines()]
        self.assertEqual([(r['monitor'], r['outcome']) for r in lines],
                         [('completed', 'ended-by-harness-kill'), ('exited', 'crash')])
        self.assertEqual(deploy.inventory(self.paths.data), self.data_before)
        self.assertIsNone(State(self.paths.state).load())

    def test_two_condition_soak_on_the_ship(self):
        seen = []
        real_start = session.start

        def start(paths, **kwargs):
            manifest = real_start(paths, **kwargs)
            seen.append((kwargs['condition'], sorted(deploy.inventory(self.paths.data))))
            return manifest

        with self.with_agent(), mock.patch('hd2h.scenarios.session.start', start):
            summary = scenarios.soak(self.paths, conditions=('mods', 'loader-only'), minutes=0.002, cycles=4,
                                     where='ship', note=self.notes.append)
        self.assertEqual([condition for condition, _ in seen], ['mods', 'loader-only', 'loader-only', 'mods'])
        loader_only = seen[1][1]
        self.assertIn('9ba626afa44a3aa3.patch_0', loader_only, 'the loader stays')
        self.assertEqual(len([n for n in loader_only if not n.endswith(('.gpu_resources', '.stream'))]), 2,
                         'loader + agent only')
        for condition in ('mods', 'loader-only'):
            self.assertEqual(summary['per_condition'][condition]['cycles'], 2)
            self.assertEqual(summary['per_condition'][condition]['on_ship'], 2)
            self.assertEqual(summary['per_condition'][condition]['crashes'], 0)
        self.assertEqual(deploy.inventory(self.paths.data), self.data_before)
        self.assertIsNone(State(self.paths.state).load())

    def test_soak_stops_when_the_lobby_is_public(self):
        with mock.patch('hd2h.session.live_reads', lambda pid, build: {'privacy': 0}):
            summary = scenarios.soak(self.paths, conditions='vanilla', minutes=1, cycles=3, note=self.notes.append)
        self.assertEqual(summary['cycles'], 1)
        line = json.loads(Path(summary['folder'], 'soak.jsonl').read_text())
        self.assertEqual(line['monitor'], 'public-lobby')
        self.assertEqual(deploy.inventory(self.paths.data), self.data_before)


if __name__ == '__main__':
    unittest.main()
