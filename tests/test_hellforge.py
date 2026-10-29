"""The HellForge target on a fake installation: the session runner, the game and the agent are simulated."""
from contextlib import ExitStack
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

import support  # noqa: F401
from hd2h import archive, hellforge, session, settings
from hd2h.paths import Paths
from hd2h.state import State
from fakes import PID, FakeGame, patch_game
from test_settings import MAIN, PROFILE
import harness

RUNNER = 5151
AGENT = b'-- HD2-Addon: mods/cowboybingus/bingus_test_agent\nreturn 1\n'
LOADER_MANIFEST = b'{"Name": "Bingus Shared Loader", "Builds": ["25480438"]}'
LOADER_PATCH = archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0x'})


def fake_hellforge(tmp, own_loader=False, mods_settings=True, inside_prefix=False, wrapper=None):
    """A HellForge installation as Setup leaves it, with two profile files (the game's own and an imported one)."""
    if inside_prefix:
        root = (Path(tmp) / 'hellforge-prefix' / 'pfx' / 'drive_c' / 'users' / 'steamuser' / 'Documents' /
                'HellForge')
    else:
        root = Path(tmp) / 'HellForge'
    (root / 'game' / 'bin').mkdir(parents=True)
    (root / 'game' / 'bin' / 'helldivers2.exe').write_bytes(b'exe')
    (root / 'game' / 'data' / 'game').mkdir(parents=True)
    (root / 'game' / 'data' / 'game' / 'game.dll').write_bytes(b'dll')
    (root / 'app' / 'session').mkdir(parents=True)
    (root / 'app' / 'session' / 'HellForge.Session.exe').write_bytes(b'runner')
    (root / 'app' / 'linux').mkdir(parents=True)
    (root / 'app' / 'linux' / 'hellforge-linux.sh').write_bytes(
        Path(wrapper).read_bytes() if wrapper else b'#!/bin/sh\nexit 0\n')
    (root / 'hellforge-install.json').write_text('{"schemaVersion": 1, "buildId": "25480438"}')
    (root / 'launcher-settings.json').write_text(json.dumps(
        {'selectedProfile': 'alpha', 'localPlayFab': True, 'sdkShims': True, 'war': 'live', 'engineLog': True}))
    (root / 'profiles' / 'alpha').mkdir(parents=True)
    (root / 'profiles' / 'alpha' / 'launcher.json').write_text('{"template": "unlock-all"}')
    profile = root / 'userdata' / 'profiles' / 'alpha'
    (profile / 'saves').mkdir(parents=True)
    (profile / 'user_settings.config').write_bytes(MAIN.encode())
    (profile / 'saves' / '000000000000a01_user_settings.config').write_bytes(PROFILE.encode())
    (profile / 'saves' / '000000000000b02_user_settings.config').write_bytes(PROFILE.replace('= 1', '= 0').encode())
    (root / 'userdata' / 'dumps').mkdir(parents=True)
    (root / 'logs' / 'sessions').mkdir(parents=True)
    (root / 'mods' / 'vanilla-plus').mkdir(parents=True)
    (root / 'mods' / 'vanilla-plus' / 'hellforge-mod.json').write_text('{"id": "example.vanilla-plus"}')
    if mods_settings:
        (root / 'mods' / 'hellforge-mods.json').write_text('{"format":1,"disabled":[],"order":["example.vanilla-plus"]}')
    if own_loader:
        (root / 'mods' / 'bingus').mkdir()
        (root / 'mods' / 'bingus' / 'BingusSharedLoader-manifest.json').write_bytes(LOADER_MANIFEST)
    paths = Paths.for_hellforge(root, Path(tmp) / 'harness')
    paths.logs.mkdir(parents=True)
    return paths


def fake_loader_zip(tmp):
    path = Path(tmp) / 'Bingus-Shared-Loader-v20.zip'
    with zipfile.ZipFile(path, 'w') as bundle:
        bundle.writestr('BingusSharedLoader-manifest.json', LOADER_MANIFEST)
        bundle.writestr('manifest.json', '{"Version": 1, "Options": []}')
        bundle.writestr('data/9ba626afa44a3aa3.patch_0', LOADER_PATCH)
        bundle.writestr('data/9ba626afa44a3aa3.patch_0.gpu_resources', b'')
        bundle.writestr('data/9ba626afa44a3aa3.patch_0.stream', b'')
    return path


class FakeRunner:
    """Stands in for HellForge.Session.exe: a run folder, result.json, and the game it starts."""

    def __init__(self, paths, game):
        self.paths, self.game, self.alive, self.run, self.calls = paths, game, False, None, []
        self.sandboxed, self.sandbox_mode, self.sandbox_notice = None, None, None

    def runners(self):
        return [RUNNER] if self.alive else []

    def launch(self, paths, run_dir, multiplayer=None, war=None, timeout=300, note=print, record=lambda **k: None,
               sandbox=None):
        self.calls.append({'multiplayer': multiplayer, 'war': war, 'timeout': timeout, 'sandbox': sandbox})
        self.alive = True
        record(runner_pid=RUNNER)
        self.run = paths.hellforge / 'logs' / 'sessions' / 'run1'
        (self.run / 'backend').mkdir(parents=True)
        self.result({'status': 'observing'})
        (self.run / 'backend' / 'requests.jsonl').write_text('{"path": "/lobby"}\n')
        record(hellforge_run=str(self.run), hellforge_run_id='run1')
        return RUNNER, self.game.launch(), 'run1', self.run

    def result(self, outcome):
        for key, value in (('sandboxed', self.sandboxed), ('sandboxMode', self.sandbox_mode),
                           ('sandboxNotice', self.sandbox_notice)):
            if value is not None:
                outcome[key] = value
        (self.run / 'result.json').write_text(json.dumps(outcome))

    def finish(self, status='observed'):
        """The game is gone: the runner writes its result and exits."""
        self.result({'status': status})
        self.alive = False


class Paths_(unittest.TestCase):
    def test_paths_follow_the_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            root = paths.hellforge
            self.assertEqual(paths.target, 'hellforge')
            self.assertEqual(paths.profile, 'alpha', "the launcher's selected profile")
            self.assertEqual(paths.exe, root / 'game' / 'bin' / 'helldivers2.exe')
            self.assertEqual(paths.settings_file, root / 'userdata' / 'profiles' / 'alpha' / 'user_settings.config')
            self.assertEqual(paths.dumps, root / 'userdata' / 'dumps')
            self.assertEqual(paths.mailbox, root / 'userdata' / 'environment' / 'Local' / 'CowboyBingus' /
                             'Helldivers2' / 'TestAgent', 'the game runs with LOCALAPPDATA in the installation')
            self.assertEqual(Paths.for_hellforge(root, paths.harness, 'beta').profile, 'beta')

    def test_environment_selects_the_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            with mock.patch.dict('os.environ', {'HD2H_TARGET': 'hellforge', 'HD2H_HELLFORGE_ROOT': str(paths.hellforge)}):
                self.assertEqual(Paths.from_environment().hellforge, paths.hellforge)
            self.assertEqual(Paths.from_environment('steam').target, 'steam')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows hands the runner host paths')
    def test_session_arguments_follow_the_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            arguments = hellforge.session_arguments(paths)
            self.assertEqual(arguments[:8], [str(paths.session_runner), 'play', '--install', str(paths.hellforge),
                                             '--profile', str(paths.hellforge_profile), '--template', 'unlock-all'])
            for flag in ('--local-playfab', '--multiplayer', '--engine-log'):
                self.assertIn(flag, arguments)
            self.assertEqual(arguments[arguments.index('--live-war') + 1], str(paths.hellforge / 'live-war.json'))
            solo = hellforge.session_arguments(paths, multiplayer=False, war='open')
            self.assertNotIn('--multiplayer', solo)
            self.assertNotIn('--local-playfab', solo)
            self.assertIn('--open-war', solo)
            self.assertNotIn('--live-war', hellforge.session_arguments(paths, war='frozen'))

    @unittest.skipIf(sys.platform == 'win32', 'Linux hands the runner Wine paths')
    def test_session_arguments_use_wine_paths_on_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            arguments = hellforge.session_arguments(paths)
            self.assertEqual(arguments[:2], [str(paths.session_runner), 'play'])
            self.assertEqual(arguments[3], hellforge.to_wine_path(paths.hellforge))
            self.assertEqual(arguments[5], hellforge.to_wine_path(paths.hellforge_profile))
            self.assertEqual(arguments[arguments.index('--live-war') + 1],
                             hellforge.to_wine_path(paths.hellforge / 'live-war.json'))
            self.assertTrue(arguments[3].startswith('Z:'), 'the temporary install is outside a Proton prefix')

    def test_sandbox_arguments_follow_the_setting_or_force_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            settings_file = paths.hellforge / 'launcher-settings.json'
            launcher = json.loads(settings_file.read_text())
            for value, wanted in ((None, True), (True, True), (False, False)):
                configured = dict(launcher)
                configured.pop('sandboxed', None)
                if value is not None:
                    configured['sandboxed'] = value
                settings_file.write_text(json.dumps(configured))
                arguments = hellforge.session_arguments(paths)
                self.assertEqual('--sandbox' in arguments, wanted, 'sandboxed=%r' % value)
                self.assertLessEqual(arguments.count('--sandbox'), 1)
            settings_file.write_text(json.dumps(dict(launcher, sandboxed=False)))
            self.assertIn('--sandbox', hellforge.session_arguments(paths, sandbox='on'))
            self.assertIn('--sandbox', hellforge.session_arguments(paths, sandbox=True))
            settings_file.write_text(json.dumps(dict(launcher, sandboxed=True)))
            self.assertNotIn('--sandbox', hellforge.session_arguments(paths, sandbox='off'))
            self.assertNotIn('--sandbox', hellforge.session_arguments(paths, sandbox=False))

    def test_sandbox_reason_falls_back_to_the_session_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            run.mkdir()
            (run / 'result.json').write_text(json.dumps({'status': 'observing', 'sandboxed': False}))
            (run / 'session.log').write_text(
                '12:00:00 This session is not sandboxed (the log gives the reason).\n')
            answer = hellforge.sandbox_answer(run)
            self.assertIs(answer['sandboxed'], False)
            self.assertEqual(answer['sandboxNotice'],
                             'This session is not sandboxed (the log gives the reason).')


class Packs(unittest.TestCase):
    def test_both_profile_files_get_the_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            entry = settings.apply(paths, 'private', Path(tmp) / 'backup', lambda entry: None)
            for path in settings.profile_files(paths):
                self.assertIn('privacy_mode = 2', path.read_text())
            self.assertEqual(settings.restore(entry), [])
            self.assertIn('privacy_mode = 0', (paths.saves / '000000000000b02_user_settings.config').read_text())

    def test_installation_loader_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(set(hellforge.pack_files(fake_hellforge(tmp, own_loader=True), AGENT)),
                             {hellforge.AGENT_FOLDER})

    def test_loader_pack_comes_from_the_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            packs = hellforge.pack_files(fake_hellforge(tmp), AGENT, fake_loader_zip(tmp))
            self.assertEqual(packs[hellforge.LOADER_FOLDER]['data/9ba626afa44a3aa3.patch_0'], LOADER_PATCH)
            agent = packs[hellforge.AGENT_FOLDER]
            self.assertEqual(sorted(agent), ['9ba626afa44a3aa3.patch_0', '9ba626afa44a3aa3.patch_0.gpu_resources',
                                             '9ba626afa44a3aa3.patch_0.stream'], 'patches at the root: a pack')

    def test_a_pack_with_foreign_files_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            recorded = []
            hashes = hellforge.install_packs(paths, {hellforge.AGENT_FOLDER: {'a.patch_0': b'x'}}, recorded.append)
            self.assertEqual(recorded, [hashes], 'hashes recorded before writing')
            (paths.mods / hellforge.AGENT_FOLDER / 'extra.txt').write_text('mine')
            problems = hellforge.remove_packs(paths, hashes)
            self.assertTrue(problems and 'extra.txt' in problems[0])
            self.assertTrue((paths.mods / hellforge.AGENT_FOLDER).exists())
            with self.assertRaises(hellforge.HellForgeError):
                hellforge.install_packs(paths, {hellforge.AGENT_FOLDER: {}}, recorded.append)

    def test_mods_settings_restored_or_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            path = paths.mods / hellforge.MODS_SETTINGS
            before = path.read_bytes()
            entry = hellforge.backup_mods_settings(paths, Path(tmp) / 'backup')
            path.write_text('{"order": ["zz-harness-agent"]}')
            self.assertEqual(hellforge.restore_mods_settings(entry), [])
            self.assertEqual(path.read_bytes(), before)
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp, mods_settings=False)
            entry = hellforge.backup_mods_settings(paths, Path(tmp) / 'backup')
            (paths.mods / hellforge.MODS_SETTINGS).write_text('{}')
            self.assertEqual(hellforge.restore_mods_settings(entry), [])
            self.assertFalse((paths.mods / hellforge.MODS_SETTINGS).exists())


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = fake_hellforge(self.tmp.name)
        self.game = FakeGame()
        self.runner = FakeRunner(self.paths, self.game)
        self.stack = ExitStack()
        patch_game(self.stack, self.game, mock)
        for target, value in (('hd2h.hellforge.runners', self.runner.runners),
                              ('hd2h.hellforge.launch', self.runner.launch),
                              ('hd2h.hellforge.LOADER_ZIP', fake_loader_zip(self.tmp.name)),
                              ('hd2h.hellforge.steam_pid_mismatch', lambda: None),
                              ('hd2h.session.wait_for_hello', lambda *a, **k: {'state': {'loader': {'modules': {}}}})):
            self.stack.enter_context(mock.patch(target, value))
        self.mods_before = sorted(p.name for p in self.paths.mods.iterdir())
        self.mods_settings_before = (self.paths.mods / hellforge.MODS_SETTINGS).read_bytes()
        self.profiles_before = {p.name: p.read_bytes() for p in settings.profile_files(self.paths)}

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def assert_restored(self):
        self.assertEqual(sorted(p.name for p in self.paths.mods.iterdir()), self.mods_before)
        self.assertEqual((self.paths.mods / hellforge.MODS_SETTINGS).read_bytes(), self.mods_settings_before)
        self.assertEqual({p.name: p.read_bytes() for p in settings.profile_files(self.paths)}, self.profiles_before)
        self.assertIsNone(State(self.paths.state).load())
        self.assertFalse((self.paths.mailbox / 'session.txt').exists())

    def test_session_adds_packs_launches_and_restores(self):
        notes = []
        manifest = session.start(self.paths, preset='light', note=notes.append, war='open')
        self.assertEqual(manifest['pid'], PID)
        self.assertEqual((manifest['target'], manifest['runner_pid'], manifest['hellforge_run_id']),
                         ('hellforge', RUNNER, 'run1'))
        self.assertEqual(self.runner.calls[0]['war'], 'open')
        self.assertIs(self.runner.calls[0]['sandbox'], True, 'absent launcher setting means on')
        self.assertGreaterEqual(self.runner.calls[0]['timeout'], 300, 'a HellForge start verifies the copy first')
        self.assertIsNone(manifest['inventory_before'], "the copy's data/ is the runner's")
        agent = self.paths.mods / hellforge.AGENT_FOLDER / '9ba626afa44a3aa3.patch_0'
        self.assertTrue(archive.contains(agent.read_bytes(), archive.resource_hash(archive.AGENT_NAME)))
        self.assertTrue((self.paths.mods / hellforge.LOADER_FOLDER / 'BingusSharedLoader-manifest.json').exists())
        for path in settings.profile_files(self.paths):
            self.assertIn('privacy_mode = 2', path.read_text(), 'every profile file is Invite Only')
        self.assertIn('id=%s' % manifest['session'], (self.paths.mailbox / 'session.txt').read_text())
        self.assertTrue(manifest['run'].endswith('-mods-hellforge'))

        self.game.exit(0)
        with self.assertRaises(session.HarnessError):
            # The runner is still restoring the copy: finish waits for it, then asks it to stop, then gives up.
            with mock.patch('hd2h.hellforge.wait_runner', lambda pid, timeout: False):
                session.finish(self.paths)
        self.assertEqual(State(self.paths.state).load()['runner_end'], 'still-running')
        State(self.paths.state).update(runner_end=None)
        self.runner.finish()
        summary = session.finish(self.paths, note=notes.append)
        self.assertEqual(summary['target'], 'hellforge')
        self.assertEqual(summary['hellforge']['status'], 'observed')
        self.assertEqual(summary['hellforge']['runner_end'], 'exited')
        self.assertEqual(summary['restore_problems'], [])
        self.assertTrue(Path(manifest['run'], 'hellforge-session', 'backend', 'requests.jsonl').exists())
        self.assertNotIn('HellForge session observing', summary['not_clean_because'])
        self.assert_restored()

    def test_failed_session_is_reported(self):
        session.start(self.paths, note=lambda text: None)
        self.game.exit(0)
        self.runner.finish(status='failed')
        summary = session.finish(self.paths, note=lambda text: None)
        self.assertIn('HellForge session failed', summary['not_clean_because'])
        self.assert_restored()

    def test_a_sandboxed_runner_selects_the_runs_mailbox(self):
        notes = []
        self.runner.sandboxed, self.runner.sandbox_mode = True, 'windows-low-integrity'
        manifest = session.start(self.paths, note=notes.append, sandbox='on')
        run_mailbox = hellforge.sandbox_mailbox(manifest['hellforge_run'])
        self.assertEqual(Path(manifest['mailbox']), run_mailbox)
        self.assertEqual(session.mailbox_path(self.paths), run_mailbox)
        self.assertIs(manifest['sandboxed'], True)
        self.assertEqual(manifest['sandboxMode'], 'windows-low-integrity')
        self.assertTrue(any('harness requests one' in text for text in notes))
        self.assertTrue(any('runner reports sandboxed' in text for text in notes))
        run_mailbox.mkdir(parents=True, exist_ok=True)
        (run_mailbox / 'session.txt').write_text('id=recorded\n')
        self.game.exit(0)
        self.runner.finish()
        summary = session.finish(self.paths, note=notes.append)
        self.assertIs(summary['sandboxed'], True)
        self.assertEqual(summary['sandboxMode'], 'windows-low-integrity')
        self.assertIs(summary['hellforge']['sandboxed'], True)
        self.assertFalse((run_mailbox / 'session.txt').exists(), 'teardown ends the recorded mailbox')
        self.assertIsNone(State(self.paths.state).load())

    def test_an_unsandboxed_answer_is_reported_and_not_retried(self):
        notes = []
        notice = 'This session is not sandboxed (the test runner refused it).'
        self.runner.sandboxed, self.runner.sandbox_mode = False, 'none'
        self.runner.sandbox_notice = notice
        manifest = session.start(self.paths, note=notes.append, sandbox='on')
        self.assertEqual(len(self.runner.calls), 1, 'never retried without the sandbox')
        self.assertEqual(Path(manifest['mailbox']), self.paths.mailbox)
        self.assertIs(manifest['sandboxed'], False)
        self.assertEqual(manifest['sandboxMode'], 'none')
        self.assertEqual(manifest['sandboxNotice'], notice)
        self.assertTrue(any('runner reports unsandboxed' in text and notice in text for text in notes))
        self.game.exit(0)
        self.runner.finish()
        summary = session.finish(self.paths, note=notes.append)
        self.assertIs(summary['sandboxed'], False)
        self.assertEqual(summary['sandboxMode'], 'none')
        self.assertEqual(summary['sandboxNotice'], notice)
        self.assert_restored()

    def test_a_sandbox_refusal_reaches_the_launch_failure(self):
        run = self.paths.hellforge / 'logs' / 'sessions' / 'refused'
        run.mkdir(parents=True)
        notice = 'This session is not sandboxed (the test runner refused it).'
        (run / 'result.json').write_text(json.dumps({'status': 'failed', 'sandboxNotice': notice}))
        self.assertIn(notice, hellforge._launch_failure(self.paths, run))

    def test_a_sandbox_refusal_is_not_retried(self):
        calls = []
        notice = 'This session is not sandboxed (the test runner refused it).'

        def refuse(paths, run_dir, multiplayer=None, war=None, timeout=300, note=print, record=lambda **k: None,
                   sandbox=None):
            calls.append(sandbox)
            raise hellforge.HellForgeError('the session runner refused: %s' % notice)

        with mock.patch('hd2h.hellforge.launch', refuse):
            with self.assertRaises(session.HarnessError) as raised:
                session.start(self.paths, note=lambda text: None, sandbox='on')
        self.assertEqual(calls, [True])
        self.assertIn(notice, str(raised.exception))
        self.assert_restored()

    def test_failed_launch_undoes_the_preparation(self):
        def refuse(paths, run_dir, multiplayer=None, war=None, timeout=300, note=print, record=lambda **k: None,
                   sandbox=None):
            raise hellforge.HellForgeError('the session runner exited before the game started: copy not verified')
        with mock.patch('hd2h.hellforge.launch', refuse):
            with self.assertRaises(session.HarnessError) as raised:
                session.start(self.paths, note=lambda text: None)
        self.assertIn('copy not verified', str(raised.exception))
        self.assert_restored()

    def test_other_conditions_are_refused(self):
        for condition in ('vanilla', 'loader-only'):
            with self.assertRaises(session.HarnessError):
                session.start(self.paths, condition=condition, agent=condition != 'vanilla', note=lambda text: None)
        self.assertIsNone(State(self.paths.state).load())

    def test_a_running_session_is_refused(self):
        self.runner.alive = True
        with self.assertRaises(session.HarnessError):
            session.preflight(self.paths)

    def test_a_public_lobby_is_opt_in(self):
        manifest = session.start(self.paths, note=lambda text: None, privacy='public')
        self.assertEqual(manifest['privacy'], 'public')
        for path in settings.profile_files(self.paths):
            self.assertIn('privacy_mode = 0', path.read_text(), 'every profile file is Public')
        self.game.exit(0)
        self.runner.finish()
        summary = session.finish(self.paths, note=lambda text: None)
        self.assertEqual(summary['privacy'], 'public')
        self.assert_restored()

    def test_a_public_lobby_needs_the_stand_ins(self):
        for multiplayer in (False,):
            with self.assertRaises(session.HarnessError) as raised:
                session.start(self.paths, note=lambda text: None, privacy='public', multiplayer=multiplayer)
            self.assertIn('Local PlayFab', str(raised.exception))
        self.assertIsNone(State(self.paths.state).load())
        self.assertEqual(self.runner.calls, [], 'nothing was launched')

    def test_a_public_lobby_is_refused_on_steam(self):
        steam = Paths(self.paths.game, self.paths.appdata, self.paths.localappdata, self.paths.harness)
        with self.assertRaises(session.HarnessError) as raised:
            session.start(steam, note=lambda text: None, privacy='public')
        self.assertIn('only for HellForge', str(raised.exception))

    def test_a_stale_steam_pid_is_refused(self):
        with mock.patch('hd2h.hellforge.steam_pid_mismatch', lambda: (4321, [8765])):
            with self.assertRaises(session.HarnessError) as raised:
                session.preflight(self.paths)
        self.assertIn('4321', str(raised.exception))
        self.assertIn('STEAM NOT DETECTED', str(raised.exception))


@unittest.skipUnless(sys.platform == 'win32', 'the Steam ActiveProcess check reads the Windows registry')
class SteamPid(unittest.TestCase):
    def check(self, registry_pid, running):
        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        with mock.patch('hd2h.hellforge.winapi.processes', lambda name: running),                 mock.patch('winreg.OpenKey', lambda *a: Key()),                 mock.patch('winreg.QueryValueEx', lambda key, name: (registry_pid, 4)):
            return hellforge.steam_pid_mismatch()

    def test_matching_pid(self):
        self.assertIsNone(self.check(8765, [8765]))

    def test_stale_pid(self):
        self.assertEqual(self.check(4321, [8765]), (4321, [8765]))

    def test_steam_not_running(self):
        self.assertEqual(self.check(4321, []), (4321, []))


class Targets(unittest.TestCase):
    def arguments(self, command, target=None):
        return argparse.Namespace(command=command, target=target, hellforge_root=None, profile=None)

    def test_later_commands_follow_the_recorded_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            with mock.patch.dict('os.environ', {'HD2H_HOME': str(paths.harness)}):
                State(paths.state).save({'target': 'hellforge', 'hellforge_root': str(paths.hellforge),
                                         'profile': 'alpha'})
                followed = harness.resolve_paths(self.arguments('stop'))
                self.assertEqual((followed.target, followed.hellforge, followed.profile),
                                 ('hellforge', paths.hellforge, 'alpha'))
                with self.assertRaises(session.HarnessError):
                    harness.resolve_paths(self.arguments('start', 'steam'))
                with self.assertRaises(session.HarnessError):
                    harness.resolve_paths(self.arguments('smoke'))

    def test_later_commands_use_the_recorded_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            recorded = Path(tmp) / 'recorded-mailbox'
            State(paths.state).save({'target': 'hellforge', 'pid': 1, 'agent': True, 'facts': {},
                                     'mailbox': str(recorded)})
            seen = []

            class FakeMailbox:
                def __init__(self, directory):
                    self.directory = Path(directory)
                    seen.append(self.directory)

                def send(self, source, timeout):
                    return ['sent']

                def age(self, name):
                    return 1.5

                def call(self, source, timeout):
                    return [{'state': 2}]

            with mock.patch('hd2h.session.Mailbox', FakeMailbox), \
                    mock.patch('hd2h.session.running', lambda: []):
                self.assertEqual(harness.command_lua(paths, argparse.Namespace(
                    file=None, code='return 1', timeout=1)), ['sent'])
                state = harness.command_state(paths, argparse.Namespace(timeout=1))
            self.assertEqual(seen, [recorded, recorded])
            self.assertEqual(state['heartbeat_age_s'], 1.5)
            self.assertEqual(state['agent'], {'state': 2})


if __name__ == '__main__':
    unittest.main()
