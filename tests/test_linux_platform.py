"""The Linux platform layer: /proc scanning, process lifetime, memory reads, path conversion, the wrapper command,
capture fallbacks and systemd-inhibit. No game, Steam or HellForge process is started; the few real processes are
Python children of the test."""
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import support  # noqa: F401
from hd2h import capture, hellforge, session, winapi
from hd2h.paths import Paths
from test_hellforge import fake_hellforge

IS_WINDOWS = sys.platform == 'win32'
FAKE_WRAPPER = Path(__file__).with_name('fake_hellforge_linux.sh')
if not IS_WINDOWS:
    from hd2h import _winapi_linux


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class Identity(unittest.TestCase):
    def test_comm_is_truncated_to_fifteen_bytes(self):
        self.assertTrue(_winapi_linux._matches_name('HellForge.Session.exe', 'HellForge.Sessi', None, None))
        self.assertFalse(_winapi_linux._matches_name('HellForge.Session.exe', 'HellForge', None, None))

    def test_a_bash_wrapper_is_not_its_windows_argument(self):
        self.assertTrue(_winapi_linux._matches_name('helldivers2.exe', 'wine64', 'C:\\game\\helldivers2.exe',
                                                    '/usr/bin/wine64'))
        self.assertFalse(_winapi_linux._matches_name('helldivers2.exe', 'bash', '/bin/bash', '/usr/bin/bash'))

    def test_a_bare_executable_name_is_not_a_host_exe(self):
        with mock.patch('hd2h._winapi_linux.command_line', lambda pid: ['helldivers2.exe']), \
                mock.patch('hd2h._winapi_linux.mapped_paths', lambda pid: []), \
                mock.patch('hd2h._winapi_linux.environment', lambda pid: {}), \
                mock.patch('hd2h._winapi_linux._readlink', lambda path: '/usr/bin/wine64'):
            self.assertIsNone(_winapi_linux.image_path(4242, root=None))


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class Lifetime(unittest.TestCase):
    def test_exit_code_survives_as_a_zombie(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(0.3); sys.exit(7)'])
        with winapi.Process(child.pid) as process:
            self.assertGreater(process.creation_time(), 11644473600 * 10_000_000)
            self.assertFalse(process.wait(0))
            self.assertIsNone(process.exit_code())
            self.assertTrue(process.wait(10))
            self.assertEqual(process.exit_code(), 7)
        child.wait()

    def test_terminate_uses_a_signal_and_cannot_choose_a_code(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        with winapi.Process(child.pid) as process:
            self.assertTrue(winapi.terminate(child.pid, 5))
            self.assertTrue(process.wait(10))
            self.assertEqual(process.exit_code(), -signal.SIGKILL)
        child.wait()
        self.assertFalse(winapi.terminate(child.pid, 5))

    def test_wait_runner_waits_on_the_wrapper_pid(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        self.assertTrue(hellforge.wait_runner(child.pid, 10))
        child.wait()


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class Memory(unittest.TestCase):
    def test_image_table_finds_pe_headers_in_a_fake_maps(self):
        stamp, size, image_size = 0x12345678, 0x5000, 0x8000
        header = bytearray(0x400)
        header[:2] = b'MZ'
        struct.pack_into('<I', header, 0x3C, 0x80)
        header[0x80:0x84] = b'PE\0\0'
        struct.pack_into('<I', header, 0x88, stamp)
        struct.pack_into('<I', header, 0x80 + 24 + 56, image_size)
        maps = ('00400000-00480000 r-xp 00000000 08:01 1234 /tmp/fake.exe\n'
                '00401000-00402000 r--p 00001000 08:01 1234 /tmp/fake.exe\n'
                '7ff000000000-7ff000001000 rw-p 00000000 00:00 0 [stack]\n')
        reads = []

        def read(address, count):
            reads.append((address, count))
            return bytes(header) if address == 0x400000 else None

        self.assertEqual(_winapi_linux.images_from_maps(maps, read), [(0x400000, stamp, image_size)])
        self.assertEqual(reads, [(0x400000, 0x400)], 'one header read per image base')

    def test_self_reader_reads_its_own_buffer(self):
        import ctypes
        buffer = ctypes.create_string_buffer(b'\x11\x22\x33\x44')
        with winapi.Reader(os.getpid()) as reader:
            self.assertEqual(reader.read(ctypes.addressof(buffer), 4), b'\x11\x22\x33\x44')
            self.assertIsNone(reader.read(0x10, 4))


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class PathsLinux(unittest.TestCase):
    def test_wine_paths_round_trip_inside_and_outside_the_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'prefix'
            root = prefix / 'pfx' / 'drive_c' / 'users' / 'steamuser' / 'Documents' / 'HellForge'
            self.assertEqual(hellforge.wine_prefix(root), prefix.resolve())
            wine = hellforge.to_wine_path(root, prefix)
            self.assertEqual(wine, 'C:\\users\\steamuser\\Documents\\HellForge')
            self.assertEqual(Path(hellforge.to_host_path(wine, prefix)).resolve(), root.resolve())
            outside = Path(tmp) / 'elsewhere'
            self.assertTrue(hellforge.to_wine_path(outside, prefix).startswith('Z:'))
            self.assertEqual(Path(hellforge.to_host_path('Z:' + str(outside).replace('/', '\\'), prefix)).resolve(),
                             outside.resolve())

    def test_current_run_converts_a_wine_run_to_a_host_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'prefix'
            root = prefix / 'pfx' / 'drive_c' / 'users' / 'steamuser' / 'Documents' / 'HellForge'
            run = root / 'logs' / 'sessions' / 'run1'
            run.mkdir(parents=True)
            paths = Paths.for_hellforge(root, Path(tmp) / 'harness')
            paths.play_session.write_text(json.dumps({'runId': 'run1', 'run': hellforge.to_wine_path(run, prefix)}))
            self.assertEqual(hellforge.current_run(paths), ('run1', run))

    def test_steam_environment_comes_from_the_compat_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / 'compatdata'
            game = Path(tmp) / 'game'
            with mock.patch.dict(os.environ, {'STEAM_COMPAT_DATA_PATH': str(prefix), 'HD2_GAME_ROOT': str(game)},
                                 clear=False):
                paths = Paths.from_environment('steam')
            user = prefix / 'pfx' / 'drive_c' / 'users' / 'steamuser' / 'AppData'
            self.assertEqual((paths.game, paths.appdata, paths.localappdata), (game, user / 'Roaming', user / 'Local'))


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class SpawnLinux(unittest.TestCase):
    class Popen:
        calls = []

        def __init__(self, arguments, **kwargs):
            self.pid, self.arguments, self.kwargs = 4242, arguments, kwargs
            SpawnLinux.Popen.calls.append(self)

    def setUp(self):
        SpawnLinux.Popen.calls = []

    def test_wrapper_command_and_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            wrapper = paths.hellforge / 'app' / 'linux' / 'hellforge-linux.sh'
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text('#!/bin/sh\n')
            with mock.patch('hd2h.hellforge.subprocess.Popen', SpawnLinux.Popen), \
                    mock.patch.dict(os.environ, {'PSModulePath': 'C:\\PowerShell\\Modules'}, clear=False):
                runner = hellforge._spawn(hellforge.session_arguments(paths), paths, None)
            call = SpawnLinux.Popen.calls[-1]
            self.assertEqual(runner, 4242)
            self.assertEqual(call.arguments[:4], ['bash', str(wrapper), str(paths.session_runner), 'play'])
            self.assertEqual(call.arguments[5], hellforge.to_wine_path(paths.hellforge))
            self.assertIn('--sandbox', call.arguments, 'the launcher default is on, on Linux too')
            self.assertEqual(call.kwargs['cwd'], str(paths.hellforge))
            self.assertTrue(call.kwargs['start_new_session'])
            self.assertIs(call.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(call.kwargs['env']['INSTALL'], str(paths.hellforge))
            self.assertEqual(call.kwargs['env']['STEAM_COMPAT_DATA_PATH'], str(hellforge.wine_prefix(paths.hellforge)))
            self.assertEqual(call.kwargs['env']['TEMP'], str(paths.hellforge / 'temp'))
            self.assertNotIn('PSModulePath', call.kwargs['env'])
            self.assertTrue((paths.hellforge / 'temp').is_dir())

    def test_missing_wrapper_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            (paths.hellforge / 'app' / 'linux' / 'hellforge-linux.sh').unlink()
            with self.assertRaisesRegex(hellforge.HellForgeError, 'Linux session wrapper is missing'):
                hellforge._spawn(hellforge.session_arguments(paths), paths, None)

    def test_steam_pid_mismatch_does_not_read_a_registry(self):
        self.assertIsNone(hellforge.steam_pid_mismatch())

    def test_copy_games_compares_real_paths_not_a_string_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_hellforge(tmp)
            images = {11: str(paths.game / 'bin' / 'helldivers2.exe'),
                      12: str(Path(tmp) / 'Steam' / 'helldivers2.exe'),
                      13: str(Path(str(paths.game) + '-2') / 'bin' / 'helldivers2.exe')}
            with mock.patch('hd2h.hellforge.winapi.processes', lambda name: list(images)), \
                    mock.patch('hd2h.hellforge.winapi.image_path', lambda pid, root=None: images[pid]):
                self.assertEqual(hellforge.copy_games(paths), [11])


def _process_gone(pid):
    """True once a PID has no /proc entry or is a zombie (an orphan's parent has not reaped it yet)."""
    try:
        fields = Path('/proc/%d/stat' % pid).read_text().rpartition(')')[2].split()
    except OSError:
        return True
    return bool(fields) and fields[0] == 'Z'


@unittest.skipIf(IS_WINDOWS, 'the Linux wrapper')
class WrapperLinux(unittest.TestCase):
    """The installation's wrapper is tests/fake_hellforge_linux.sh; it starts no Proton, Steam or game."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = fake_hellforge(self.tmp.name, inside_prefix=True, wrapper=FAKE_WRAPPER)
        self.runner = self.child = None

    def tearDown(self):
        for pid in (self.child, self.runner):
            if pid and not _process_gone(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.tmp.cleanup()

    def start(self, **environment):
        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch('hd2h.hellforge.copy_games', lambda paths: [999999]):
            runner, _game, run_id, run = hellforge.launch(self.paths, None, timeout=15)
        self.runner = runner
        return runner, run_id, Path(run)

    def test_a_c_run_path_comes_through_the_installations_own_prefix(self):
        runner, run_id, run = self.start()
        expected = self.paths.hellforge / 'logs' / 'sessions' / run_id
        self.assertEqual(run, expected)
        pointer = json.loads(self.paths.play_session.read_text())
        self.assertEqual(pointer['run'][:2], 'C:', 'the fake wrapper publishes a C: run path')
        self.assertNotIn('/', pointer['run'])
        self.assertEqual(hellforge.current_run(self.paths), (run_id, expected))
        self.assertNotIn('/Steam/steamapps/compatdata', str(run), 'not the Steam default prefix')
        self.assertEqual(hellforge.end_runner({'runner_pid': runner, 'hellforge_run': str(run)},
                                              wait=0, after_signal=10), 'signalled')
        self.assertTrue(hellforge.wait_runner(runner, 5))
        self.assertEqual(hellforge.result(run).get('status'), 'observed')
        self.assertTrue(Path(run, hellforge.STOP_SIGNAL).exists())

    def test_a_wrapper_that_ignores_stop_is_ended_with_its_group(self):
        runner, _run_id, run = self.start(HD2H_FAKE_IGNORE_STOP='1', HD2H_FAKE_CHILD='1')
        child_file = self.paths.hellforge / 'logs' / 'fake-child.pid'
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(child_file.exists(), 'the fake wrapper did not start its child')
        self.child = int(child_file.read_text())
        self.assertEqual(hellforge.end_runner({'runner_pid': runner, 'hellforge_run': str(run)},
                                              wait=0, after_signal=0.2), 'killed')
        self.assertTrue(hellforge.wait_runner(runner, 5))
        deadline = time.monotonic() + 5
        while not _process_gone(self.child) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(_process_gone(self.child), 'the wrapper child survived the process-group kill')
        self.child = None

    def test_discovery_names_both_missing_files(self):
        marker = self.paths.hellforge / 'hellforge-install.json'
        wrapper = self.paths.hellforge / 'app' / 'linux' / 'hellforge-linux.sh'
        marker.unlink()
        wrapper.unlink()
        with self.assertRaises(hellforge.HellForgeError) as raised:
            hellforge.facts(self.paths)
        self.assertIn(str(marker), str(raised.exception))
        self.assertIn(str(wrapper), str(raised.exception))

    def test_discovery_names_a_missing_wrapper_when_the_marker_is_there(self):
        wrapper = self.paths.hellforge / 'app' / 'linux' / 'hellforge-linux.sh'
        wrapper.unlink()
        with self.assertRaises(hellforge.HellForgeError) as raised:
            hellforge.facts(self.paths)
        self.assertIn(str(wrapper), str(raised.exception))
        self.assertNotIn('hellforge-install.json', str(raised.exception))

    def test_a_launch_failure_surfaces_the_wrapper_log(self):
        (self.paths.hellforge / 'logs' / 'linux-session-wrapper.log').write_text('no bridge sockets found\n')
        self.assertIn('no bridge sockets found', hellforge._launch_failure(self.paths, None))


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class CaptureLinux(unittest.TestCase):
    def test_without_imagemagick_the_error_names_the_missing_tool(self):
        with mock.patch.dict(os.environ, {'DISPLAY': ':0'}, clear=False), \
                mock.patch('hd2h._capture_linux.shutil.which', lambda name: None):
            with self.assertRaisesRegex(capture.CaptureError, 'ImageMagick'):
                capture.grab(1)

    def test_without_a_display_the_error_names_x11(self):
        environment = dict(os.environ)
        environment.pop('DISPLAY', None)
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(capture.CaptureError, 'X11 display'):
                capture.grab(1)

    def test_a_windowless_process_has_no_game_window(self):
        with mock.patch('hd2h._capture_linux.winapi.windows', lambda pid: []):
            self.assertIsNone(capture.game_window(4242))


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class KeepAwakeLinux(unittest.TestCase):
    class Inhibitor:
        last = None

        def __init__(self, command, **kwargs):
            self.command, self.kwargs = command, kwargs
            self.terminated = self.waited = False
            KeepAwakeLinux.Inhibitor.last = self

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def kill(self):
            pass

    def test_starts_and_stops_systemd_inhibit(self):
        with mock.patch.object(_winapi_linux, '_INHIBITOR', None), \
                mock.patch('hd2h._winapi_linux.shutil.which', lambda name: '/usr/bin/' + name), \
                mock.patch('hd2h._winapi_linux.subprocess.Popen', KeepAwakeLinux.Inhibitor):
            self.assertTrue(winapi.keep_awake(True))
            inhibitor = KeepAwakeLinux.Inhibitor.last
            self.assertEqual(inhibitor.command[:2], ['systemd-inhibit', '--what=idle:sleep'])
            self.assertTrue(winapi.keep_awake(False))
            self.assertTrue(inhibitor.terminated and inhibitor.waited)
            self.assertIsNone(_winapi_linux._INHIBITOR)

    def test_without_systemd_inhibit_it_is_a_noop(self):
        with mock.patch.object(_winapi_linux, '_INHIBITOR', None), \
                mock.patch('hd2h._winapi_linux.shutil.which', lambda name: None):
            self.assertFalse(winapi.keep_awake(True))
            self.assertTrue(winapi.keep_awake(False))


@unittest.skipIf(IS_WINDOWS, 'the Linux platform layer')
class SessionLinux(unittest.TestCase):
    class Process:
        def wait(self, seconds):
            return True

    def test_the_steam_launch_says_it_is_windows_only(self):
        with self.assertRaisesRegex(session.HarnessError, 'steam target runs on Windows only'):
            session.launch()

    def test_end_does_not_call_taskkill(self):
        with mock.patch('hd2h.session.winapi.post_close', lambda pid: 0), \
                mock.patch('hd2h.session.winapi.terminate', lambda pid, code: False), \
                mock.patch('hd2h.session.subprocess.run') as taskkill:
            self.assertEqual(session._end(123, SessionLinux.Process(), lambda text: None, 1, 1), 'kill')
        taskkill.assert_not_called()


if __name__ == '__main__':
    unittest.main()
