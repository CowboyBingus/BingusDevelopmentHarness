"""The agent archive: identical to the loader's writer, and found by the loader's own discovery."""
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

import support
from hd2h import archive

LOADER_ARCHIVE = support.LOADER_CHECKOUT / 'scripts' / 'archive.py' if support.LOADER_CHECKOUT else None
PUBLISHED_DISCOVER = support.PUBLISHED_LOADER / 'src' / 'discover.lua' if support.PUBLISHED_LOADER else None


def loader_archive_module():
    spec = importlib.util.spec_from_file_location('loader_archive', LOADER_ARCHIVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArchiveWriter(unittest.TestCase):
    @unittest.skipUnless(support.present(LOADER_ARCHIVE), 'no Bingus Shared Loader checkout (BINGUS_SHARED_LOADER)')
    def test_matches_the_loader_writer_byte_for_byte(self):
        loader = loader_archive_module()
        for name in ('mods/cowboybingus/bingus_test_agent', 'mods/a/b', 'mods/example_author/example_mod', 'x'):
            self.assertEqual(archive.resource_hash(name), loader.resource_hash(name))
        resources = {archive.resource_hash('mods/a/b'): b'\x05\0\0\0\x02\0\0\0hello',
                     archive.resource_hash('mods/c/d'): b'\x01\0\0\0\x02\0\0\0x' * 7}
        self.assertEqual(archive.make_archive(resources), loader.make_archive(resources))
        self.assertEqual(archive.LUA_TYPE, loader.TYPE)

    def test_agent_archive_layout(self):
        source = support.HARNESS.joinpath('agent', 'bingus_test_agent.lua').read_bytes()
        data = archive.agent_archive(source)
        [(name, kind, offset, size)] = archive.entries(data)
        self.assertEqual(name, archive.resource_hash(archive.AGENT_NAME))
        self.assertEqual(kind, archive.LUA_TYPE)
        length, version = struct.unpack_from('<II', data, offset)
        self.assertEqual((length, version), (len(source), 2))
        self.assertEqual(size, len(source) + 8)
        self.assertTrue(data[offset + 8:].startswith(b'-- HD2-Addon: mods/cowboybingus/bingus_test_agent\n'))

    def test_rejects_sources_without_the_declaration(self):
        with self.assertRaises(ValueError):
            archive.addon_resource(archive.AGENT_NAME, b'print(1)\n')
        with self.assertRaises(ValueError):
            archive.addon_resource(archive.AGENT_NAME, b'\xef\xbb\xbf-- HD2-Addon: ' + archive.AGENT_NAME.encode())
        with self.assertRaises(ValueError):
            archive.entries(b'not an archive' * 10)

    def test_contains(self):
        data = archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0x'})
        self.assertTrue(archive.contains(data, archive.LOADER_CALLBACKS))
        self.assertFalse(archive.contains(data, archive.resource_hash(archive.AGENT_NAME)))


@unittest.skipUnless(support.present(PUBLISHED_DISCOVER), 'no published loader v18 checkout (HD2H_PUBLISHED_LOADER)')
class LoaderDiscovery(unittest.TestCase):
    def test_published_loader_discovers_the_agent(self):
        agent = archive.agent_archive(support.HARNESS.joinpath('agent', 'bingus_test_agent.lua').read_bytes())
        example = archive.make_archive({archive.resource_hash('mods/example_author/example_mod'):
                                        archive.addon_resource('mods/example_author/example_mod',
                                                               b'-- HD2-Addon: mods/example_author/example_mod\n')})
        undeclared_body = b'print("no declaration")\n'
        undeclared = archive.make_archive({archive.resource_hash('mods/x/y'):
                                           struct.pack('<II', len(undeclared_body), 2) + undeclared_body})
        for label, prefix in support.runners():
            with self.subTest(vm=label), tempfile.TemporaryDirectory() as tmp:
                files = {'9ba626afa44a3aa3.patch_3': example, '9ba626afa44a3aa3.patch_5': undeclared,
                         '9ba626afa44a3aa3.patch_7': agent}
                for name, data in files.items():
                    Path(tmp, name).write_bytes(data)
                result = support.run_lua(prefix, support.TESTS / 'discover_check.lua', PUBLISHED_DISCOVER, tmp,
                                         *sorted(files))
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('entries=mods/cowboybingus/bingus_test_agent,mods/example_author/example_mod',
                              output)
                self.assertIn('warnings=\n', output + '\n')


if __name__ == '__main__':
    unittest.main()
