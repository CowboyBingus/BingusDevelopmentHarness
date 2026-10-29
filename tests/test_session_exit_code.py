"""HellForge's result gives the game's exit code as text; the session summary must judge it as a number."""
import unittest

from hd2h import session


class ExitCodeNumberTest(unittest.TestCase):
    def test_hex_text_is_a_number(self):
        self.assertEqual(session.exit_code_number('0x00000000'), 0)
        self.assertEqual(session.exit_code_number('0xC0000005'), 0xC0000005)

    def test_numbers_and_unknown_text_are_kept(self):
        self.assertEqual(session.exit_code_number(1), 1)
        self.assertIsNone(session.exit_code_number(None))
        self.assertEqual(session.exit_code_number('unknown'), 'unknown')


if __name__ == '__main__':
    unittest.main()
