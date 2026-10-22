"""The agent in real Lua VMs: unit tests, and an end-to-end mailbox session from Python."""
from pathlib import Path
import tempfile
import time
import unittest

import support
from hd2h.mailbox import Mailbox


def mailbox_root(tmp):
    folder = Path(tmp) / 'CowboyBingus' / 'Helldivers2' / 'TestAgent'
    folder.mkdir(parents=True)
    return folder


class AgentInLua(unittest.TestCase):
    def test_unit_tests_in_every_vm(self):
        runners = support.runners()
        self.assertTrue(runners, 'no Lua VM found')
        for label, prefix in runners:
            with self.subTest(vm=label), support.lua_tmp() as tmp:
                mailbox_root(tmp)
                result = support.run_lua(prefix, support.TESTS / 'test_agent.lua', support.HARNESS, tmp)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('passed', output)
                self.assertNotIn('ERROR', output)


class AgentEndToEnd(unittest.TestCase):
    def test_python_client_drives_the_real_agent(self):
        for label, prefix in support.runners():
            with self.subTest(vm=label), support.lua_tmp() as tmp:
                mailbox = Mailbox(mailbox_root(tmp))
                mailbox.begin('e2e01', poll=0.05, heartbeat=0.5)
                process = support.popen_lua(prefix, support.TESTS / 'agent_driver.lua', support.HARNESS, tmp)
                try:
                    deadline = time.monotonic() + 20
                    hello = None
                    while time.monotonic() < deadline and not hello:
                        hello = mailbox.read('hello')
                        time.sleep(0.05)
                    self.assertIsNotNone(hello, 'no hello')
                    self.assertEqual(hello['session'], 'e2e01')
                    self.assertEqual(hello['state']['loader']['modules']['mods/cowboybingus/bingus_test_agent'],
                                     'loaded')

                    state = mailbox.call('return agent.state()')[0]
                    self.assertEqual(state['session'], 'e2e01')
                    self.assertGreater(state['frames'], 0)

                    self.assertEqual(mailbox.call('return 6 * 7, nil, "x"'), [42, None, 'x'])
                    failed = mailbox.send('error("boom")')
                    self.assertFalse(failed['ok'])
                    self.assertEqual(failed['stage'], 'run')
                    self.assertIn('boom', failed['error'])
                    self.assertEqual(mailbox.call('return agent.keys(agent, "^quit")'), [['quit: function']])

                    time.sleep(1.2)
                    self.assertIsNotNone(mailbox.read('heartbeat'))
                    self.assertLess(mailbox.age('heartbeat'), 5)

                    # A client killed after the agent took its command (live 2026-10-13): the command is answered
                    # but next-seq.txt never moves; the next call must catch up from the heartbeat, not wait forever.
                    seq = int(mailbox.path('next-seq.txt').read_text().strip())
                    mailbox.path('cmd-%d.lua' % seq).write_text('return "orphan"', encoding='utf-8')
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and (mailbox.read('heartbeat') or {}).get('seq') != seq + 1:
                        time.sleep(0.05)
                    self.assertEqual(mailbox.read('heartbeat')['seq'], seq + 1, 'the agent never took the orphan')
                    self.assertEqual(mailbox.call('return "after"', timeout=5), ['after'])
                    self.assertFalse(mailbox.path('res-%d.json' % seq).exists(), "the orphan's reply is cleared")

                    self.assertEqual(mailbox.call('return agent.quit()'), [True])
                    output, errors = process.communicate(timeout=20)
                    self.assertEqual(process.returncode, 0, output + errors)
                    self.assertIn('driver: quit after', output)
                    bye = mailbox.read('bye')
                    self.assertEqual(bye['reason'], 'shutdown')
                    self.assertEqual(bye['session'], 'e2e01')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_a_command_nobody_reads_is_withdrawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Mailbox(mailbox_root(tmp))
            mailbox.begin('lonely')
            with self.assertRaisesRegex(Exception, 'did not pick up command 1'):
                mailbox.send('return 1', timeout=0.3)
            self.assertFalse(mailbox.path('cmd-1.lua').exists())
            self.assertEqual(mailbox.path('next-seq.txt').read_text().strip(), '1')

    def test_the_sequence_catches_up_with_the_agent_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            mailbox = Mailbox(mailbox_root(tmp))
            mailbox.begin('resync')
            mailbox.path('next-seq.txt').write_text('3\n')
            mailbox.path('res-3.json').write_text('{"seq": 3}\nEND 3\n')
            beat = mailbox.path('heartbeat.json')
            # another session's heartbeat, or one behind us, never moves the sequence
            for session, seq in (('other', 9), ('resync', 2), ('resync', 3)):
                beat.write_text('{"session": "%s", "seq": %d}\nEND heartbeat\n' % (session, seq))
                with self.assertRaisesRegex(Exception, 'did not pick up command 3'):
                    mailbox.send('return 1', timeout=0.2)
            # the agent is ahead (a killed client's command was taken): catch up and clear its leftovers
            beat.write_text('{"session": "resync", "seq": 5}\nEND heartbeat\n')
            with self.assertRaisesRegex(Exception, 'did not pick up command 5'):
                mailbox.send('return 1', timeout=0.2)
            self.assertEqual(mailbox.path('next-seq.txt').read_text().strip(), '5')
            self.assertFalse(mailbox.path('res-3.json').exists())


if __name__ == '__main__':
    unittest.main()
