"""Live mod changes (hd2h/livemods.py) on a fake installation: the backend is a local HTTP server that answers as
HellForge's LiveMods does."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401
from hd2h import livemods
from test_hellforge import fake_hellforge


def test_mod(folder, mod_id):
    folder.mkdir(parents=True)
    (folder / 'hellforge-mod.json').write_text(json.dumps({'format': 1, 'id': mod_id}))
    (folder / 'hellforge-stats.json').write_text('{"format": 1}')
    return folder


class Backend(BaseHTTPRequestHandler):
    """Answers POST and GET /hellforge/live-mods; a POST appends a line to the run's live-mods.log first."""
    run = None
    seen = []

    def _answer(self, applied):
        if self.path != livemods.ENDPOINT:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({'live': True, 'applied': applied, 'changes': ['test.a added'], 'blocking': [],
                           'detail': 'Applied.' if applied else 'Applies now.', 'digest': 'abc'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        Backend.seen.append('POST')
        with open(Path(Backend.run) / 'backend' / 'live-mods.log', 'a', encoding='utf-8') as log:
            log.write('12:00:00.000 Live mods live1: test.a added. The mods\' digest is now abc.\n')
        self._answer(True)

    def do_GET(self):
        Backend.seen.append('GET')
        self._answer(False)

    def log_message(self, *args):
        pass


class Switches(unittest.TestCase):
    def test_on_leaves_the_disabled_list_and_off_joins_it_once(self):
        data = {'format': 1, 'disabled': ['a', 'b'], 'order': []}
        self.assertEqual(livemods.switch(data, on=['a'], off=['c', 'b'])['disabled'], ['b', 'c'])
        self.assertEqual(data['disabled'], ['a', 'b'])  # the original is not changed

    def test_a_step_passes_only_with_every_expectation(self):
        outcome = {'answer': {'applied': False, 'blocking': ['test.x needs a restart: code without "live"']},
                   'logs': {'live-mods': ['Live mods live2 refused at modadd']}, 'session_mods': ['test.a'],
                   'game_running': True}
        step = {'expect': {'applied': False, 'blocking': 'without "live"'}, 'logs': {'live-mods': ['refused']},
                'runs': ['test.a'], 'runs_not': ['test.x']}
        self.assertEqual(livemods.check(step, outcome), [])
        failed = livemods.check({'expect': {'applied': True}, 'runs': ['test.x']}, dict(outcome, game_running=False))
        self.assertEqual(len(failed), 3)


class SyncAnswers(unittest.TestCase):
    def test_an_answer_is_the_launchers_file_with_the_offers_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Path(tmp) / 'backend'
            backend.mkdir()
            self.assertEqual(livemods._answer(tmp, 'accept', note=lambda t: None), {'error': 'no offer'})
            (backend / 'mod-sync.json').write_text(json.dumps({'version': 3, 'state': 'offer', 'mods': []}))
            self.assertEqual(livemods._answer(tmp, 'restart', note=lambda t: None)['version'], 3)
            self.assertEqual(json.loads((backend / 'mod-sync-answer.json').read_text()), {'version': 3, 'answer': 'restart'})
            self.assertFalse((backend / 'mod-sync-answer.json.tmp').exists())


class SetupAndTeardown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = fake_hellforge(self.tmp.name)
        patcher = mock.patch.object(livemods, 'RECORDS', Path(self.tmp.name) / 'state' / 'live-mods')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = livemods._record_file(self.paths.hellforge)
        for name in ('copy_games', 'runners'):
            patcher = mock.patch.object(livemods.hellforge, name, return_value=[])
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_setup_copies_switches_and_teardown_puts_everything_back(self):
        settings = self.paths.mods / 'hellforge-mods.json'
        original = settings.read_bytes()
        a = test_mod(Path(self.tmp.name) / 'src' / 'live-a', 'test.a')
        b = test_mod(Path(self.tmp.name) / 'src' / 'live-b', 'test.b')
        result = livemods.setup(self.paths, [a, b], on=['test.b'], off=['example.vanilla-plus'])
        self.assertEqual(result['disabled'], ['test.a', 'example.vanilla-plus'])
        self.assertTrue((self.paths.mods / 'live-a' / 'hellforge-mod.json').exists())
        self.assertEqual(livemods.installed_ids(self.paths)['test.b'], 'live-b')
        with self.assertRaises(livemods.LiveModsError):
            livemods.setup(self.paths, [a])  # recorded already
        self.assertEqual(livemods.teardown(self.paths), [])
        self.assertFalse((self.paths.mods / 'live-a').exists())
        self.assertEqual(settings.read_bytes(), original)
        self.assertFalse(self.record.exists())

    def test_a_copy_changed_meanwhile_is_left_in_place(self):
        a = test_mod(Path(self.tmp.name) / 'src' / 'live-a', 'test.a')
        livemods.setup(self.paths, [a])
        (self.paths.mods / 'live-a' / 'extra.txt').write_text('mine')
        problems = livemods.teardown(self.paths)
        self.assertEqual(len(problems), 1)
        self.assertTrue((self.paths.mods / 'live-a').exists())
        self.assertTrue(self.record.exists())  # kept, so teardown can run again once resolved

    def test_a_mod_the_installation_has_is_refused(self):
        twin = test_mod(Path(self.tmp.name) / 'src' / 'twin', 'example.vanilla-plus')
        with self.assertRaises(livemods.LiveModsError):
            livemods.setup(self.paths, [twin])
        self.assertFalse(self.record.exists())


class Change(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = fake_hellforge(self.tmp.name)
        run = Path(self.tmp.name) / 'run'
        (run / 'backend').mkdir(parents=True)
        (run / 'game').mkdir()
        (run / 'backend' / 'live-mods.log').write_text('09:00:00.000 an old line\n', encoding='utf-8')
        (run / 'session-mods.json').write_text(json.dumps({'mods': [{'id': 'test.a'}]}))
        self.server = HTTPServer(('127.0.0.1', 0), Backend)
        Backend.run, Backend.seen = run, []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        (run / 'result.json').write_text(json.dumps({'backendOrigin': 'http://127.0.0.1:%d' % self.server.server_port}))
        self.manifest = {'hellforge_run': str(run), 'pid': 4242}
        patcher = mock.patch.object(livemods.session, 'running', return_value=[4242])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_a_switch_is_saved_then_posted_and_only_new_log_lines_come_back(self):
        outcome = livemods.change(self.paths, self.manifest, on=[], off=['example.vanilla-plus'], settle=0)
        self.assertEqual(Backend.seen, ['POST'])
        self.assertTrue(outcome['answer']['applied'])
        self.assertEqual(outcome['logs']['live-mods'], ["12:00:00.000 Live mods live1: test.a added. The mods' digest is now abc."])
        self.assertEqual(outcome['session_mods'], ['test.a'])
        self.assertIn('example.vanilla-plus', json.loads((self.paths.mods / 'hellforge-mods.json').read_text())['disabled'])
        self.assertTrue(outcome['game_running'])

    def test_preview_gets_and_concurrent_requests_all_answer(self):
        self.assertFalse(livemods.change(self.paths, self.manifest, preview=True, settle=0)['answer']['applied'])
        outcome = livemods.change(self.paths, self.manifest, settle=0, requests=2)
        self.assertEqual(Backend.seen.count('POST'), 2)
        self.assertEqual(len(outcome['answer']), 2)

    def test_a_refused_step_puts_the_switches_back(self):
        before = (self.paths.mods / 'hellforge-mods.json').read_text()
        with mock.patch.object(livemods, 'ask', return_value={'applied': False, 'live': False, 'blocking': ['x']}):
            verdict = livemods.scenario(self.paths, self.manifest, [
                {'name': 'refused', 'off': ['example.vanilla-plus'], 'expect': {'applied': False}}], note=lambda t: None)
        self.assertEqual(verdict['passed'], 1)
        self.assertEqual(json.loads((self.paths.mods / 'hellforge-mods.json').read_text()), json.loads(before))


if __name__ == '__main__':
    unittest.main()
