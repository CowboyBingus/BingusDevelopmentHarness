"""Minidump parsing and crash classification on synthetic dumps."""
import struct
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from hd2h import builds, dumps

EXE_BASE, DLL_BASE = 0x7FF600000000, 0x7FFA00000000


def minidump(code, address, modules):
    """Bytes of a minimal minidump: header, directory, module list, exception stream, names."""
    names, name_blobs = [], b''
    module_rva = 32 + 12 * 2
    module_size = 4 + dumps.MODULE_SIZE * len(modules)
    exception_rva = module_rva + module_size
    strings_rva = exception_rva + 168
    for name, _base, _size, _stamp in modules:
        encoded = name.encode('utf-16-le')
        names.append(strings_rva + len(name_blobs))
        name_blobs += struct.pack('<I', len(encoded)) + encoded + b'\0\0'
    header = struct.pack('<IIIIIIQ', dumps.SIGNATURE, 0xA793, 2, 32, 0, 0, 0)
    directory = struct.pack('<III', dumps.MODULE_LIST_STREAM, module_size, module_rva)
    directory += struct.pack('<III', dumps.EXCEPTION_STREAM, 168, exception_rva)
    module_list = struct.pack('<I', len(modules))
    for (name, base, size, stamp), rva in zip(modules, names):
        module_list += struct.pack('<QIIII', base, size, 0, stamp, rva) + b'\0' * (dumps.MODULE_SIZE - 24)
    exception = struct.pack('<IIIIQQII', 4242, 0, code, 0, 0, address, 0, 0) + b'\0' * 120 + b'\0' * 8
    return header + directory + module_list + exception + name_blobs


GAME_25480438 = [(r'C:\Games\Helldivers 2\bin\helldivers2.exe', EXE_BASE, 0x4000000,
                  builds.KNOWN['25480438'].exe_timestamp),
                 (r'C:\Games\Helldivers 2\data\game\game.dll', DLL_BASE, 0x5000000, 0x6AB3B43F)]


class Classify(unittest.TestCase):
    def classify(self, code, address, modules=GAME_25480438):
        info = dumps.parse(minidump(code, address, modules))
        return info, dumps.classify(info)

    def test_gpu_watchdog(self):
        info, (kind, _) = self.classify(builds.ACCESS_VIOLATION, EXE_BASE + 0x6C07A6)
        self.assertEqual((info['module'], info['offset'], info['thread']), ('helldivers2.exe', 0x6C07A6, 4242))
        self.assertEqual(kind, 'gpu-watchdog')

    def test_lua_out_of_memory_anywhere(self):
        _, (kind, _) = self.classify(builds.LUA_OUT_OF_MEMORY, 0x7FFB12345678)
        self.assertEqual(kind, 'lua-out-of-memory')

    def test_game_dll_signature(self):
        _, (kind, _) = self.classify(builds.ACCESS_VIOLATION, DLL_BASE + 0x17FF734)
        self.assertEqual(kind, 'mom-row-reset-alignment')

    def test_unknown_offset_is_unclassified(self):
        _, (kind, meaning) = self.classify(builds.ACCESS_VIOLATION, EXE_BASE + 0x123456)
        self.assertEqual(kind, 'unclassified')
        self.assertIn('helldivers2.exe+0x123456', meaning)
        self.assertNotIn('unknown game build', meaning)

    def test_quit_signatures(self):
        for offset, expected in ((0xC948A, 'quit-during-boot'), (0x626296, 'quit-render-task-pool')):
            _, (kind, meaning) = self.classify(builds.ACCESS_VIOLATION, EXE_BASE + offset)
            self.assertEqual(kind, expected)
            self.assertIn('vanilla', meaning)
        _, (kind, _) = self.classify(0xC0000094, EXE_BASE + 0xC948A)
        self.assertEqual(kind, 'unclassified')

    def test_other_builds_get_no_signatures(self):
        older = [(GAME_25480438[0][0], EXE_BASE, 0x4000000, 0x12345678)]
        _, (kind, meaning) = self.classify(builds.ACCESS_VIOLATION, EXE_BASE + 0x6C07A6, older)
        self.assertEqual(kind, 'unclassified')
        self.assertIn('unknown game build', meaning)

    def test_metadata_reads_only_allowed_keys(self):
        def record(key, value):
            def pack(item):
                if isinstance(item, str):
                    raw = item.encode('latin1')
                    return (bytes([0xa0 + len(raw)]) if len(raw) < 32 else b'\xd9' + bytes([len(raw)])) + raw
                return b'\xce' + struct.pack('>I', item)
            return b'\x83\x00\x02\x02' + pack(key) + b'\x03' + pack(value)
        blob = (b'noise' + record('user_id', 'ACCOUNT-SECRET') + record('session_id', 'SESSION-SECRET')
                + record('quit_reason', 'window_closing') + record('init', 'finished')
                + record('recap.processDurationMillis', 5893) + record('quit_reason', 'later duplicate'))
        found = dumps.metadata(b'\0' * 64 + blob + b'\0' * 64)
        self.assertEqual(found, {'quit_reason': 'window_closing', 'init': 'finished',
                                 'recap.processDurationMillis': 5893})
        self.assertEqual(dumps.metadata(b'quit_reason with no records'), {})
        self.assertEqual(dumps.metadata(b'\0' * 100), {})
        data = minidump(builds.ACCESS_VIOLATION, EXE_BASE + 0x123456, GAME_25480438) + blob
        info = dumps.parse(data)
        info['metadata'] = dumps.metadata(data)
        self.assertIn('while the game was closing', dumps.classify(info)[1])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'dump-2.dmp')
            path.write_bytes(minidump(builds.ACCESS_VIOLATION, EXE_BASE + 0xC948A, GAME_25480438) + blob)
            described = dumps.describe(path)
            self.assertEqual((described['kind'], described['quit_reason'], described['uptime_s']),
                             ('quit-during-boot', 'window_closing', 5.9))
            self.assertNotIn('SECRET', repr(described))

    def test_rejects_non_dumps(self):
        with self.assertRaises(dumps.DumpError):
            dumps.parse(b'PK\x03\x04' + b'\0' * 64)

    def test_describe_and_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'dump-1.dmp')
            path.write_bytes(minidump(builds.ACCESS_VIOLATION, EXE_BASE + 0x6F9394, GAME_25480438))
            described = dumps.describe(path)
            self.assertEqual(described['kind'], 'render-stale-texture')
            self.assertEqual(described['code'], '0xC0000005')
            self.assertEqual(described['where'], 'helldivers2.exe+0x6f9394')
            self.assertEqual((described['quit_reason'], described['uptime_s']), (None, None))
            self.assertEqual(dumps.listing(tmp), {'dump-1.dmp': path.stat().st_size})
            self.assertEqual(dumps.listing(Path(tmp, 'missing')), {})


if __name__ == '__main__':
    unittest.main()
