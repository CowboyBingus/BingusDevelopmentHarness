"""The Win32 wrappers against real child processes and this process's own memory."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import unittest

import support  # noqa: F401
from hd2h import winapi


class Processes(unittest.TestCase):
    def test_finds_this_process(self):
        self.assertIn(os.getpid(), winapi.processes(Path(sys.executable).name))
        self.assertEqual(winapi.processes('no-such-process-hd2h.exe'), [])

    def test_exit_code_survives_through_the_held_handle(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(0.3); sys.exit(7)'])
        with winapi.Process(child.pid) as process:
            self.assertIsNotNone(process.creation_time())
            self.assertFalse(process.wait(0))
            self.assertIsNone(process.exit_code())
            self.assertTrue(process.wait(10))
            self.assertEqual(process.exit_code(), 7)
        child.wait()

    @unittest.skipUnless(sys.platform == 'win32', 'TerminateProcess can choose the exit code on Windows only')
    def test_terminate(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        with winapi.Process(child.pid) as process:
            self.assertTrue(winapi.terminate(child.pid, 5))
            self.assertTrue(process.wait(10))
            self.assertEqual(process.exit_code(), 5)
        child.wait()

    def test_window_helpers_handle_windowless_processes(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            self.assertEqual(winapi.post_close(child.pid), 0)
        finally:
            child.kill()
            child.wait()


class Memory(unittest.TestCase):
    def test_reads_this_process(self):
        buffer = ctypes.create_string_buffer(b'\x11\x22\x33\x44' + (0x123456789ABC).to_bytes(8, 'little'))
        with winapi.Reader(os.getpid()) as reader:
            address = ctypes.addressof(buffer)
            self.assertEqual(reader.read(address, 4), b'\x11\x22\x33\x44')
            self.assertEqual(reader.u32(address), 0x44332211)
            self.assertEqual(reader.pointer(address + 4), 0x123456789ABC)
            self.assertIsNone(reader.read(0x10, 4))

    @unittest.skipUnless(sys.platform == 'win32', 'kernel32 image enumeration is Windows-only')
    def test_finds_an_image_by_pe_timestamp_without_module_enumeration(self):
        kernel32 = ctypes.WinDLL('kernel32')._handle
        header = ctypes.string_at(kernel32, 0x400)
        pe = int.from_bytes(header[0x3C:0x40], 'little')
        stamp = int.from_bytes(header[pe + 8:pe + 12], 'little')
        with winapi.Reader(os.getpid()) as reader:
            images = reader.images()
            self.assertGreater(len(images), 5)
            self.assertIn(kernel32, [base for base, _, _ in images])
            self.assertEqual(reader.image_base(stamp), kernel32)
            self.assertIsNone(reader.image_base(0x0BADF00D))

    @unittest.skipUnless(sys.platform == 'win32', 'SetThreadExecutionState is Windows-only')
    def test_keep_awake_toggles(self):
        self.assertTrue(winapi.keep_awake(True))
        self.assertTrue(winapi.keep_awake(False))


if __name__ == '__main__':
    unittest.main()
