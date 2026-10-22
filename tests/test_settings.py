"""Settings presets: changed for a session, restored byte-for-byte."""
from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401
from hd2h import settings
from hd2h.paths import Paths

MAIN = ('audio_device = 1\naudio_volume = 1\nmaximized_window = false\nframerate_limit_enabled = false\n'
        'fullscreen = false\nborderless_fullscreen = true\nrender_settings = {\n\taudio_volume = 7\n'
        '\tframerate_limit = 99\n}\nscreen_resolution = [\n\t2560\n\t1440\n]\nframerate_limit = 30\n'
        'vsync = false\n')
PROFILE = 'crossplay_enabled = true\nlanguage = "us"\nprivacy_mode = 1\nvoice_mode = 4\n'


def fake_paths(tmp, newline='\n'):
    tmp = Path(tmp)
    paths = Paths(tmp / 'game', tmp / 'appdata', tmp / 'local', tmp / 'harness')
    paths.saves.mkdir(parents=True)
    paths.settings_file.write_bytes(MAIN.replace('\n', newline).encode())
    (paths.saves / '000000000000b02_user_settings.config').write_bytes(PROFILE.replace('\n', newline).encode())
    (paths.saves / '000000000000b02_user_settings.config.old').write_bytes(b'privacy_mode = 0\n')
    return paths


class Presets(unittest.TestCase):
    def test_privacy_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(tmp)
            entry = settings.apply(paths, 'private', Path(tmp) / 'backup', lambda entry: None, privacy='public')
            self.assertEqual(settings.privacy(paths), '0')
            self.assertEqual(settings.restore(entry), [])
            self.assertEqual(settings.privacy(paths), '1')
            with self.assertRaises(settings.SettingsError):
                settings.apply(paths, 'private', Path(tmp) / 'backup', lambda entry: None, privacy='friends')

    def check_round_trip(self, newline):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(tmp, newline)
            main_before = paths.settings_file.read_bytes()
            profile = settings.profile_file(paths)
            profile_before = profile.read_bytes()
            recorded = {}
            entry = settings.apply(paths, 'light', Path(tmp, 'backup'), recorded.update)
            self.assertEqual(recorded, entry)
            main = paths.settings_file.read_bytes().decode()  # read_text() would fold CRLF into LF
            self.assertIn('framerate_limit_enabled = true', main)
            self.assertIn('audio_volume = 0' + newline, main)
            self.assertIn('\taudio_volume = 7', main, 'indented keys inside blocks stay')
            self.assertIn('borderless_fullscreen = false', main)
            self.assertIn('screen_resolution = [%s\t1280%s\t720%s]' % (newline, newline, newline), main)
            self.assertEqual(settings.privacy(paths), '2')
            old_copy = paths.saves / '000000000000b02_user_settings.config.old'
            old_before = old_copy.read_bytes()
            self.assertEqual(old_copy.read_bytes(), b'privacy_mode = 0\n', 'extra save files are not edited')
            old_copy.write_bytes(b'privacy_mode = 2\n')  # the game rotates its .old copy mid-session
            (paths.saves / 'new_by_game.config').write_bytes(b'x')
            self.assertEqual(settings.new_save_files(paths, entry), ['new_by_game.config'])
            self.assertEqual(settings.restore(entry), [])
            self.assertEqual(old_copy.read_bytes(), old_before)
            self.assertEqual(paths.settings_file.read_bytes(), main_before)
            self.assertEqual(profile.read_bytes(), profile_before)
            self.assertEqual(list(Path(tmp, 'backup').iterdir()), [])

    def test_round_trip_lf(self):
        self.check_round_trip('\n')

    def test_round_trip_crlf(self):
        self.check_round_trip('\r\n')

    def check_render_resolution(self, newline):
        # Reported case 2026-10-13: the stored upscaler input (2/3 of the full-screen size) fed a 1280x720 window
        # and the world rendered flat gray; scaled by the window's share of the screen it drew. Here the screen is
        # 2560x1440, so 1706x960 becomes 853x480.
        main = MAIN.replace('\tframerate_limit = 99\n', '\tframerate_limit = 99\n\trender_resolution = [\n'
                            '\t\t1706\n\t\t960\n\t]\n')
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(tmp, newline)
            paths.settings_file.write_bytes(main.replace('\n', newline).encode())
            before = paths.settings_file.read_bytes()
            entry = settings.apply(paths, 'light', Path(tmp, 'backup'), lambda e: None)
            text = paths.settings_file.read_bytes().decode()
            self.assertIn('\trender_resolution = [%s\t\t853%s\t\t480%s\t]' % (newline, newline, newline), text)
            self.assertIn('screen_resolution = [%s\t1280%s\t720%s]' % (newline, newline, newline), text)
            self.assertEqual(settings.restore(entry), [])
            self.assertEqual(paths.settings_file.read_bytes(), before)

    def test_light_scales_render_resolution_lf(self):
        self.check_render_resolution('\n')

    def test_light_scales_render_resolution_crlf(self):
        self.check_render_resolution('\r\n')

    def test_render_resolution_edge_cases(self):
        self.assertEqual(settings.scale_render_resolution('a = 1\n', (2560, 1440), (1280, 720)), 'a = 1\n',
                         'no block: unchanged')
        block = '\trender_resolution = [\n\t\t4000\n\t\t2000\n\t]\n'
        self.assertEqual(settings.scale_render_resolution(block, None, (1280, 720)), block, 'no screen size')
        self.assertIn('\t\t1280\n\t\t720\n', settings.scale_render_resolution(block, (2560, 1440), (1280, 720)),
                      'supersampling is capped at the window size')

    def test_private_preset_changes_only_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(tmp)
            main_before = paths.settings_file.read_bytes()
            entry = settings.apply(paths, 'private', Path(tmp, 'backup'), lambda e: None)
            self.assertEqual(paths.settings_file.read_bytes(), main_before)
            self.assertEqual(settings.privacy(paths), '2')
            self.assertEqual(settings.restore(entry), [])
            self.assertEqual(settings.privacy(paths), '1')

    def test_refuses_ambiguous_or_missing_keys(self):
        with self.assertRaises(settings.SettingsError):
            settings.set_scalar('a = 1\na = 2\n', 'a', '3')
        with self.assertRaises(settings.SettingsError):
            settings.set_scalar('b = 1\n', 'a', '3')
        with self.assertRaises(settings.SettingsError):
            settings.set_resolution('screen_resolution = [\n\t1\n]\n', 1, 2)
        with self.assertRaises(settings.SettingsError):
            settings.apply(None, 'nope', '.', lambda e: None)

    def test_needs_exactly_one_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(tmp)
            (paths.saves / 'other_user_settings.config').write_text(PROFILE)
            with self.assertRaises(settings.SettingsError):
                settings.profile_file(paths)

    def test_restore_reports_a_damaged_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = fake_paths(tmp)
            entry = settings.apply(paths, 'light', Path(tmp, 'backup'), lambda e: None)
            Path(entry['files']['main']['backup']).write_bytes(b'damaged')
            problems = settings.restore(entry)
            self.assertEqual(len(problems), 1)
            self.assertIn('main', problems[0])
            self.assertEqual(settings.privacy(paths), '1', 'the intact profile is still restored')


if __name__ == '__main__':
    unittest.main()
