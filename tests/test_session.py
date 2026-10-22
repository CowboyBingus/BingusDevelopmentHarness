"""Session lifecycle on a fake install: Steam, the game process and the agent are simulated."""
from contextlib import ExitStack
import json
import time
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
from hd2h import archive, builds, deploy, session, settings
from hd2h.state import State
from fakes import PID, FakeAgent, FakeGame, fake_install, patch_game
from test_dumps import EXE_BASE, GAME_25480438, minidump


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = fake_install(self.tmp.name)
        self.game = FakeGame()
        self.stack = ExitStack()
        patch_game(self.stack, self.game, mock)
        self.stack.enter_context(mock.patch('hd2h.session.wait_for_hello',
                                            lambda *a, **k: {'state': {'loader': {'modules': {}}}}))
        # stop() reaches the ship before quitting; the fake game has no build offsets to navigate,
        # so these tests stand in for the trip (navigation has its own tests).
        self.ship_trips = []

        def to_ship(paths, manifest, note=print, **kwargs):
            self.ship_trips.append(manifest['session'])
            State(paths.state).update(reached_ship='test')
            return {'state': 3}
        self.stack.enter_context(mock.patch('hd2h.navigate.to_ship', to_ship))
        self.notes = []
        self.data_before = deploy.inventory(self.paths.data)
        self.main_before = self.paths.settings_file.read_bytes()
        self.profile_before = (self.paths.saves / 'abc_user_settings.config').read_bytes()

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def assert_restored(self):
        self.assertEqual(deploy.inventory(self.paths.data), self.data_before)
        self.assertEqual(self.paths.settings_file.read_bytes(), self.main_before)
        self.assertEqual((self.paths.saves / 'abc_user_settings.config').read_bytes(), self.profile_before)
        self.assertIsNone(State(self.paths.state).load())
        self.assertFalse((self.paths.mailbox / 'session.txt').exists())

    def test_mods_session_prepares_and_restores(self):
        manifest = session.start(self.paths, preset='light', note=self.notes.append)
        self.assertEqual(manifest['pid'], PID)
        agent = self.paths.data / '9ba626afa44a3aa3.patch_2'
        self.assertTrue(archive.contains(agent.read_bytes(), archive.resource_hash(archive.AGENT_NAME)))
        self.assertEqual(settings.privacy(self.paths), '2')
        self.assertIn('framerate_limit_enabled = true', self.paths.settings_file.read_text())
        self.assertIn('id=%s' % manifest['session'], (self.paths.mailbox / 'session.txt').read_text())
        self.assertIsNone(manifest['facts']['build'], 'dummy binaries are no known build')
        with self.assertRaises(session.HarnessError):
            session.finish(self.paths)  # still running
        # The session writes the loader's log; a mod that did not run left an old one behind.
        (self.paths.logs / 'BingusSharedLoader.log').write_text('Bingus Shared Loader loader-v18; API 1\nrun\n')
        old = self.paths.logs / 'FastLobbyScanner.log'
        old.write_text('from an earlier session\n')
        four_days_ago = time.time() - 4 * 86400
        os.utime(old, (four_days_ago, four_days_ago))
        self.game.exit()
        summary = session.finish(self.paths, note=self.notes.append)
        self.assertEqual(summary['outcome'], 'exited')
        self.assertEqual(summary['restore_problems'], [])
        self.assertEqual(summary['logs'], ['BingusSharedLoader.log'])
        self.assertEqual(summary['stale_logs'], ['FastLobbyScanner.log'], 'old logs are listed apart')
        self.assertTrue(Path(manifest['run'], 'logs-stale', 'FastLobbyScanner.log').exists())
        self.assertFalse(Path(manifest['run'], 'logs', 'FastLobbyScanner.log').exists())
        self.assert_restored()
        run = json.loads(Path(manifest['run'], 'run.json').read_text())
        self.assertEqual(run['session'], manifest['session'])

    def test_vanilla_session_stashes_every_patch(self):
        session.start(self.paths, condition='vanilla', agent=False, note=self.notes.append)
        self.assertEqual(deploy.inventory(self.paths.data), {})
        self.assertEqual(settings.privacy(self.paths), '2')
        self.game.exit()
        self.assertEqual(session.finish(self.paths)['restore_problems'], [])
        self.assert_restored()

    def test_loader_only_moves_a_late_loader_to_patch_0(self):
        # The usual install: mods first, the loader last. The game stops at the first missing patch
        # number, so a loader left at its own number would never load once the mods are stashed.
        data = self.paths.data
        loader_bytes = (data / '9ba626afa44a3aa3.patch_0').read_bytes()
        for suffix in ('', '.gpu_resources', '.stream'):
            (data / ('9ba626afa44a3aa3.patch_0' + suffix)).replace(data / ('9ba626afa44a3aa3.patch_2' + suffix))
            (data / ('9ba626afa44a3aa3.patch_0' + suffix)).write_bytes(b'other mod' if not suffix else b'')
        before = deploy.inventory(data)
        session.start(self.paths, preset='light', condition='loader-only', note=self.notes.append)
        self.assertEqual((data / '9ba626afa44a3aa3.patch_0').read_bytes(), loader_bytes, 'the loader is read first')
        agent = data / '9ba626afa44a3aa3.patch_1'
        self.assertTrue(archive.contains(agent.read_bytes(), archive.resource_hash(archive.AGENT_NAME)))
        self.assertEqual(sorted(n for n in deploy.inventory(data) if deploy.PATCH.match(n)['sidecar'] is None),
                         ['9ba626afa44a3aa3.patch_0', '9ba626afa44a3aa3.patch_1'], 'loader and agent only')
        self.game.exit()
        self.assertEqual(session.finish(self.paths)['restore_problems'], [])
        self.assertEqual(deploy.inventory(data), before, 'the install is back, loader at its own number')

    def test_packages_session_replaces_the_install_and_restores(self):
        from test_deploy import package_zip
        loader = archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0y'})
        first = package_zip(self.tmp.name, 'loader-v19.zip', {0: loader})
        second = package_zip(self.tmp.name, 'mod.zip', {0: b'changed mod'}, include='Addon')
        manifest = session.start(self.paths, preset='light', condition='packages', packages=(first, second),
                                 note=self.notes.append)
        self.assertEqual(manifest['facts']['packages'], ['loader-v19.zip', 'mod.zip'])
        import hashlib
        self.assertEqual(manifest['facts']['package_sha256'],
                         {Path(z).name: hashlib.sha256(Path(z).read_bytes()).hexdigest().upper() for z in (first, second)})
        data = self.paths.data
        self.assertEqual((data / '9ba626afa44a3aa3.patch_0').read_bytes(), loader)
        self.assertEqual((data / '9ba626afa44a3aa3.patch_1').read_bytes(), b'changed mod')
        agent = data / '9ba626afa44a3aa3.patch_2'
        self.assertTrue(archive.contains(agent.read_bytes(), archive.resource_hash(archive.AGENT_NAME)))
        self.assertEqual(len([n for n in deploy.inventory(data) if deploy.PATCH.match(n)['sidecar'] is None]), 3)
        self.game.exit()
        self.assertEqual(session.finish(self.paths)['restore_problems'], [])
        self.assert_restored()

    def test_packages_need_the_condition_and_a_loader(self):
        from test_deploy import package_zip
        mod = package_zip(self.tmp.name, 'mod.zip', {0: b'changed mod'})
        with self.assertRaises(session.HarnessError):
            session.start(self.paths, condition='packages', packages=(), note=self.notes.append)
        with self.assertRaises(session.HarnessError):
            session.start(self.paths, condition='mods', packages=(mod,), note=self.notes.append)
        with self.assertRaises(session.HarnessError):
            session.start(self.paths, condition='packages', packages=(mod,), note=self.notes.append)
        self.assert_restored()

    def test_a_session_that_never_reached_the_ship_is_not_clean(self):
        def no_ship(paths, manifest, note=print, **kwargs):
            from hd2h import navigate
            raise navigate.NavigationError('could not press continue')
        with mock.patch('hd2h.navigate.to_ship', no_ship):
            session.start(self.paths, preset='light', note=self.notes.append)
            summary = session.stop(self.paths, note=self.notes.append)
        self.assertEqual(summary['exit']['how'], 'kill', 'terminated rather than closed mid-startup')
        self.assertFalse(summary['clean'])
        self.assertIn('never reached the ship', summary['not_clean_because'])
        self.assert_restored()

    def test_a_crash_dump_makes_the_session_not_clean(self):
        session.start(self.paths, preset='light', note=self.notes.append)
        (self.paths.dumps / 'dump-late.dmp').write_bytes(
            minidump(builds.ACCESS_VIOLATION, EXE_BASE + 0xC948A, GAME_25480438))
        self.game.exit(code=1)
        summary = session.finish(self.paths)
        self.assertFalse(summary['clean'])
        self.assertTrue(any(reason.startswith('crash dump: quit-during-boot') for reason in summary['not_clean_because']),
                        summary['not_clean_because'])
        self.assert_restored()

    def test_failed_launch_undoes_everything(self):
        with mock.patch('hd2h.session.launch', side_effect=session.HarnessError('steam is gone')):
            with self.assertRaises(session.HarnessError):
                session.start(self.paths, preset='light', note=self.notes.append)
        self.assert_restored()

    def test_preflight_refusals(self):
        self.game.pids = [999]
        with self.assertRaisesRegex(session.HarnessError, 'running'):
            session.preflight(self.paths)
        self.game.pids = []
        State(self.paths.state).save({'session': 'old'})
        with self.assertRaisesRegex(session.HarnessError, 'active.json'):
            session.preflight(self.paths)
        State(self.paths.state).clear()
        (self.paths.data / '9ba626afa44a3aa3.patch_0').write_bytes(b'not the loader any more')
        with self.assertRaisesRegex(session.HarnessError, 'Shared Loader'):
            session.preflight(self.paths)
        self.assertIsNone(session.preflight(self.paths, agent=False)['build'])

    def test_stop_kill_records_how_it_ended(self):
        session.start(self.paths, note=self.notes.append)
        summary = session.stop(self.paths, kill=True, note=self.notes.append)
        self.assertEqual(summary['exit']['how'], 'kill')
        self.assertEqual(summary['exit']['code'], 1)
        self.assertEqual(summary['outcome'], 'ended-by-harness-kill')
        self.assert_restored()

    def test_stop_ends_this_sessions_crash_reporter_only(self):
        # Reported case (2026-10-13): after a crash the game's crs-handler.exe stayed up and Steam blocked the
        # next two launches. The session's reporter (created after its game) goes; an older one (the user's) stays.
        manifest = session.start(self.paths, note=self.notes.append)
        self.game.add_handler(7001, manifest['created'] + 90_000_000)  # 9 s after the game
        self.game.add_handler(7002, manifest['created'] - 10_000_000_000)  # the user's own, from earlier
        self.game.exit(code=0xC0000005)  # the crash
        summary = session.stop(self.paths, note=self.notes.append)
        self.assertEqual(list(self.game.handlers), [7002])
        self.assertEqual(summary.get('crash_handlers_ended'), [7001])
        self.assert_restored()

    def test_a_new_dump_makes_the_session_a_classified_crash(self):
        manifest = session.start(self.paths, note=self.notes.append)
        (self.paths.dumps / 'dump-crash.dmp').write_bytes(
            minidump(builds.ACCESS_VIOLATION, EXE_BASE + 0x6C07A6, GAME_25480438))
        self.game.exit()
        summary = session.finish(self.paths)
        self.assertEqual(summary['outcome'], 'crash')
        self.assertEqual([d['kind'] for d in summary['dumps']], ['gpu-watchdog'])
        self.assertTrue(Path(manifest['run'], 'dumps', 'dump-crash.dmp').exists())
        self.assert_restored()

    def test_restore_problem_keeps_the_manifest_for_recover(self):
        manifest = session.start(self.paths, note=self.notes.append)
        agent = next(name for name in manifest['agent']['files'] if name.endswith('patch_2'))
        (self.paths.data / agent).write_bytes(b'a mod manager deployed into this slot')
        self.game.exit()
        summary = session.finish(self.paths, note=self.notes.append)
        self.assertTrue(summary['restore_problems'])
        self.assertIsNotNone(State(self.paths.state).load(), 'manifest kept for recover')
        self.assertEqual(settings.privacy(self.paths), '1', 'settings still restored')

    def _packages_session_then_outside_redeploy(self, contents, legacy=False):
        """A packages session (patch_0..2 from the ZIPs, the agent at patch_3), then the user's mod
        manager redeploys `contents` (archive bytes, numbered from patch_0, empty sidecars) into data/.
        Returns (manifest, the manager's files, their {name: (sha256, identity)})."""
        from test_deploy import package_zip, redeploy
        loader = archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0y'})
        first = package_zip(self.tmp.name, 'loader-v19.zip', {0: loader})
        second = package_zip(self.tmp.name, 'mod.zip', {0: b'changed mod', 1: b'another mod'}, include='Addon')
        manifest = session.start(self.paths, condition='packages', packages=(first, second), note=self.notes.append)
        self.assertEqual(set(manifest['agent']['identity']), set(manifest['agent']['files']))
        if legacy:  # a manifest written before file identities were recorded, as on 2026-10-09
            State(self.paths.state).update(**{key: {'files': manifest[key]['files']} for key in ('packages', 'agent')})
        users = {}
        for number, content in enumerate(loader if content is None else content for content in contents):
            name = '9ba626afa44a3aa3.patch_%d' % number
            users.update({name: content, name + '.gpu_resources': b'', name + '.stream': b''})
        redeploy(self.paths.data, users)
        self.game.exit()
        kept = {name: (deploy.digest(self.paths.data / name), deploy.identity(self.paths.data / name)) for name in users}
        return manifest, users, kept

    def assert_redeploy_untouched(self, manifest, users, kept):
        data = self.paths.data
        self.assertEqual(sorted(deploy.inventory(data)), sorted(users), 'data/ holds exactly the manager\'s files')
        for name, (sha, identity) in kept.items():
            self.assertEqual((deploy.digest(data / name), deploy.identity(data / name)), (sha, identity), name)
        stash_dir = Path(manifest['stash']['dir'])
        for name, sha in manifest['stash']['files'].items():
            self.assertEqual(deploy.digest(stash_dir / name), sha, 'still stashed: %s' % name)
        self.assertTrue(State(self.paths.state).load()['outside_redeploy'], 'manifest kept, redeploy recorded')
        self.assertEqual(settings.privacy(self.paths), '1', 'settings still restored')

    def _outside_redeploy_over_the_same_names(self, legacy):
        # Reported case (2026-10-09, a packages run): the user's mod manager redeployed their mods over
        # the harness's patch_0..3 mid-session. Their empty sidecars hash the same as the harness's, and
        # `recover` deleted 8 of the user's own. Here the manager's patch_0 is even the same loader
        # build as the first package's, byte for byte.
        manifest, users, kept = self._packages_session_then_outside_redeploy(
            [None, b'user mod one', b'user mod two', b'user mod three', b'user mod four'], legacy)
        summary = session.finish(self.paths, note=self.notes.append)
        self.assertFalse(summary['clean'])
        self.assertTrue(any('inventory_before' in problem for problem in summary['restore_problems']),
                        summary['restore_problems'])
        self.assert_redeploy_untouched(manifest, users, kept)
        again = session.recover(self.paths, note=self.notes.append)
        self.assertTrue(any('recover removes and unstashes nothing' in problem for problem in again['restore_problems']),
                        again['restore_problems'])
        self.assert_redeploy_untouched(manifest, users, kept)

    def test_outside_redeploy_over_the_same_names_keeps_the_users_files(self):
        self._outside_redeploy_over_the_same_names(legacy=False)

    def test_outside_redeploy_keeps_the_users_files_with_a_manifest_from_before_identities(self):
        self._outside_redeploy_over_the_same_names(legacy=True)

    def test_a_manifest_from_before_identities_still_restores(self):
        from test_deploy import package_zip
        loader = archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0y'})
        manifest = session.start(self.paths, condition='packages', packages=(package_zip(self.tmp.name, 'l.zip', {0: loader}),),
                                 note=self.notes.append)
        State(self.paths.state).update(**{key: {'files': manifest[key]['files']} for key in ('packages', 'agent')})
        self.game.exit()
        self.assertEqual(session.finish(self.paths)['restore_problems'], [])
        self.assert_restored()

    def test_outside_redeploy_of_fewer_patches_removes_only_the_harness_files(self):
        manifest, users, kept = self._packages_session_then_outside_redeploy([None, b'user mod one'])
        self.assertTrue((self.paths.data / '9ba626afa44a3aa3.patch_3').exists(), 'the agent, still the harness\'s')
        session.finish(self.paths, note=self.notes.append)
        self.assert_redeploy_untouched(manifest, users, kept)  # patch_2 (package) and patch_3 (agent) are gone
        session.recover(self.paths, note=self.notes.append)
        self.assert_redeploy_untouched(manifest, users, kept)

    def test_outside_redeploy_during_vanilla_moves_nothing_back(self):
        from test_deploy import redeploy
        manifest = session.start(self.paths, condition='vanilla', agent=False, note=self.notes.append)
        users = {'9ba626afa44a3aa3.patch_0': b'user mod', '9ba626afa44a3aa3.patch_0.gpu_resources': b'',
                 '9ba626afa44a3aa3.patch_0.stream': b''}
        redeploy(self.paths.data, users)
        self.game.exit()
        kept = {name: (deploy.digest(self.paths.data / name), deploy.identity(self.paths.data / name)) for name in users}
        session.finish(self.paths, note=self.notes.append)
        self.assert_redeploy_untouched(manifest, users, kept)  # the stashed patch_1 stays stashed too
        session.recover(self.paths, note=self.notes.append)
        self.assert_redeploy_untouched(manifest, users, kept)

    def test_start_closes_a_game_started_elsewhere(self):
        self.game.pids = [999]
        manifest = session.start(self.paths, note=self.notes.append)
        self.assertEqual(manifest['closed_before_start'], [{'pid': 999, 'how': 'kill', 'code': 1}])
        self.assertEqual(manifest['pid'], PID)
        self.assertTrue(Path(manifest['run'], 'logs-previous', 'BingusSharedLoader.log').exists())
        self.game.exit()
        self.assertEqual(session.finish(self.paths)['restore_problems'], [])
        self.assert_restored()

    def test_keep_running_refuses_and_changes_nothing(self):
        self.game.pids = [999]
        with self.assertRaisesRegex(session.HarnessError, 'running'):
            session.start(self.paths, take_over=False, note=self.notes.append)
        self.assertEqual(self.game.pids, [999])
        self.game.exit()
        self.assert_restored()

    def test_start_ends_a_recorded_harness_session_first(self):
        def hello(mailbox, session_id, pid, timeout=300):
            FakeAgent(self.paths.mailbox, self.game).start()
            return {}

        with mock.patch('hd2h.session.wait_for_hello', hello):
            first = session.start(self.paths, note=self.notes.append)
            second = session.start(self.paths, note=self.notes.append)
        self.assertNotEqual(first['session'], second['session'])
        earlier = json.loads(Path(first['run'], 'run.json').read_text())
        self.assertEqual(earlier['outcome'], 'clean-quit')
        self.assertTrue(earlier['clean'] and earlier['reached_ship'], earlier['not_clean_because'])
        self.assertEqual(self.ship_trips, [first['session']], 'the ship is reached before the quit')
        self.assertEqual(second['closed_before_start'], [])
        self.game.exit()
        self.assertEqual(session.finish(self.paths)['restore_problems'], [])
        self.assert_restored()


if __name__ == '__main__':
    unittest.main()
