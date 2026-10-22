"""Agent install/remove and the vanilla stash, on a throwaway data folder."""
import os
from pathlib import Path
import tempfile
import unittest

import support  # noqa: F401  (adds the harness to sys.path)
from hd2h import archive, deploy

AGENT = support.HARNESS.joinpath('agent', 'bingus_test_agent.lua').read_bytes()


def fake_data(folder):
    folder = Path(folder)
    files = {
        '9ba626afa44a3aa3.patch_0': archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0x'}),
        '9ba626afa44a3aa3.patch_1': b'mod one',
        '9ba626afa44a3aa3.patch_2': b'mod two',
        '0123456789abcdef.patch_0': b'other family',
        'game': None,  # a folder, never touched
        '9ba626afa44a3aa3': b'base archive, not a patch',
    }
    for name, data in files.items():
        if data is None:
            (folder / name).mkdir()
        else:
            (folder / name).write_bytes(data)
    for name in ('9ba626afa44a3aa3.patch_0', '9ba626afa44a3aa3.patch_1', '9ba626afa44a3aa3.patch_2',
                 '0123456789abcdef.patch_0'):
        (folder / (name + '.gpu_resources')).write_bytes(b'')
        (folder / (name + '.stream')).write_bytes(b'')
    return folder


def redeploy(data, files):
    """Writes {name: bytes} as a mod manager does: each a new file, even where the bytes are the same."""
    for name, content in files.items():
        temporary = Path(data) / (name + '.deploying')
        temporary.write_bytes(content)
        os.replace(temporary, Path(data) / name)


def package_zip(folder, name, archives, include='data', options=True):
    """A manager ZIP holding {number: bytes} archives (with sidecars) under `include`/."""
    import json
    import zipfile
    path = Path(folder) / name
    with zipfile.ZipFile(path, 'w') as bundle:
        for number, content in archives.items():
            base = '%s/9ba626afa44a3aa3.patch_%d' % (include, number)
            bundle.writestr(base, content)
            bundle.writestr(base + '.gpu_resources', b'')
            bundle.writestr(base + '.stream', b'')
        bundle.writestr('README.txt', b'readme')
        if options:
            bundle.writestr('manifest.json', json.dumps({'Options': [{'Include': [include]}]}))
    return path


class PackageFiles(unittest.TestCase):
    def test_renumbers_in_order_with_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            loader = archive.make_archive({archive.LOADER_CALLBACKS: b'\x01\0\0\0\x02\0\0\0x'})
            first = package_zip(tmp, 'loader.zip', {0: loader})
            second = package_zip(tmp, 'mod.zip', {0: b'mod a', 1: b'mod b'}, include='Addon')
            third = package_zip(tmp, 'bare.zip', {3: b'bare'}, include='data', options=False)
            files = deploy.package_files([first, second, third], start=4)
            self.assertEqual(sorted(files), sorted('9ba626afa44a3aa3.patch_%d%s' % (n, s) for n in (4, 5, 6, 7)
                                                   for s in ('', '.gpu_resources', '.stream')))
            self.assertEqual([files['9ba626afa44a3aa3.patch_%d' % n] for n in (5, 6, 7)], [b'mod a', b'mod b', b'bare'])
            self.assertTrue(deploy.carries_loader(files))
            self.assertFalse(deploy.carries_loader(deploy.package_files([second])))

    def test_every_option_by_default_or_a_selection(self):
        import json
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'pack.zip')
            with zipfile.ZipFile(path, 'w') as bundle:
                bundle.writestr('options/Main/9ba626afa44a3aa3.patch_0', b'main')
                bundle.writestr('options/Alt/9ba626afa44a3aa3.patch_0', b'alternative')
                bundle.writestr('options/Alt/9ba626afa44a3aa3.patch_1', b'alternative config')
                bundle.writestr('options/Alt/nested/9ba626afa44a3aa3.patch_0', b'not deployed')
                bundle.writestr('manifest.json', json.dumps({'Options': [
                    {'Name': 'Main', 'Include': ['options/Main']}, {'Name': 'Alt', 'Include': ['options/Alt']}]}))
            every = deploy.package_files([path])
            archives = [every['9ba626afa44a3aa3.patch_%d' % n] for n in range(3)]
            self.assertEqual(archives, [b'main', b'alternative', b'alternative config'],
                             'each option keeps its own patch_0; options deploy in order')
            self.assertEqual(len(every), 9)
            chosen = deploy.package_files(['%s::Alt' % path])
            self.assertEqual(chosen['9ba626afa44a3aa3.patch_0'], b'alternative')
            self.assertEqual(len(chosen), 6)
            with self.assertRaises(deploy.DeployError):
                deploy.package_files(['%s::Missing' % path])

    def test_rejects_packages_without_archives(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp, 'empty.zip')
            with zipfile.ZipFile(empty, 'w') as bundle:
                bundle.writestr('README.txt', b'nothing')
            with self.assertRaises(deploy.DeployError):
                deploy.package_files([empty])
            orphan = Path(tmp, 'orphan.zip')
            with zipfile.ZipFile(orphan, 'w') as bundle:
                bundle.writestr('data/9ba626afa44a3aa3.patch_0.stream', b'')
            with self.assertRaises(deploy.DeployError):
                deploy.package_files([orphan])


class AgentDeployment(unittest.TestCase):
    def test_inventory_and_next_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(tmp)
            listing = deploy.inventory(data)
            self.assertEqual(len(listing), 12)
            self.assertNotIn('9ba626afa44a3aa3', listing)
            self.assertEqual(deploy.next_slot(data), 3)
            self.assertEqual(deploy.next_slot(data, '0123456789abcdef'), 1)
            self.assertEqual(deploy.owner_of_loader(data), ['9ba626afa44a3aa3.patch_0'])

    def test_install_records_first_then_removes_exactly_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(tmp)
            before = deploy.inventory(data)
            files = deploy.agent_files(data, AGENT)
            self.assertEqual(sorted(files), ['9ba626afa44a3aa3.patch_3', '9ba626afa44a3aa3.patch_3.gpu_resources',
                                             '9ba626afa44a3aa3.patch_3.stream'])
            recorded = {}

            def record(written):
                self.assertFalse((data / '9ba626afa44a3aa3.patch_3').exists(), 'record must come first')
                recorded.update(written)

            deploy.install(data, files, record)
            self.assertEqual(set(recorded), set(files))
            self.assertEqual((data / '9ba626afa44a3aa3.patch_3').read_bytes(), files['9ba626afa44a3aa3.patch_3'])
            with self.assertRaises(deploy.DeployError):
                deploy.install(data, files, lambda written: None)
            self.assertEqual(deploy.remove(data, recorded), [])
            self.assertEqual(deploy.inventory(data), before)

    def test_remove_leaves_files_that_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(tmp)
            files = deploy.agent_files(data, AGENT)
            recorded = {}
            deploy.install(data, files, recorded.update)
            (data / '9ba626afa44a3aa3.patch_3').write_bytes(b'a mod manager wrote this slot meanwhile')
            self.assertEqual(deploy.remove(data, recorded), ['9ba626afa44a3aa3.patch_3'])
            self.assertTrue((data / '9ba626afa44a3aa3.patch_3').exists())
            # The empty sidecars hash the same as ours but go with the manager's archive now
            # (2026-10-09: deleting them removed the user's own files).
            self.assertTrue((data / '9ba626afa44a3aa3.patch_3.stream').exists())
            self.assertTrue((data / '9ba626afa44a3aa3.patch_3.gpu_resources').exists())

    def test_remove_leaves_a_redeploy_of_the_same_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(tmp)
            recorded = {}
            identities = deploy.install(data, deploy.agent_files(data, AGENT), recorded.update)
            unit = sorted(n for n in os.listdir(data) if n.startswith('9ba626afa44a3aa3.patch_3'))
            redeploy(data, {'9ba626afa44a3aa3.patch_3.stream': b''})
            self.assertEqual(deploy.remove(data, recorded, identities), ['9ba626afa44a3aa3.patch_3'])
            self.assertEqual(sorted(n for n in os.listdir(data) if n.startswith('9ba626afa44a3aa3.patch_3')), unit,
                             'one replaced file keeps its whole patch number in place')

    def test_a_patch_number_without_its_archive_needs_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(tmp)
            recorded = {}
            identities = deploy.install(data, deploy.agent_files(data, AGENT), recorded.update)
            ours, foreign = deploy.judge(data, recorded)
            self.assertEqual((ours, foreign), ([('9ba626afa44a3aa3.patch_3', ['9ba626afa44a3aa3.patch_3.gpu_resources',
                                                                              '9ba626afa44a3aa3.patch_3.stream',
                                                                              '9ba626afa44a3aa3.patch_3'], False)], []),
                             'sidecars first, the archive last; bytes alone vouch, nothing proven')
            self.assertEqual(deploy.delete(data, ours, proven_only=True)[0][0], '9ba626afa44a3aa3.patch_3')
            self.assertTrue((data / '9ba626afa44a3aa3.patch_3').exists(), 'unproven units stay when proof is required')
            self.assertTrue(deploy.judge(data, recorded, identities)[0][0][2], 'identities prove the unit')
            (data / '9ba626afa44a3aa3.patch_3').unlink()
            self.assertEqual(deploy.remove(data, recorded), ['9ba626afa44a3aa3.patch_3'],
                             'empty sidecars without their archive prove nothing by their bytes')
            self.assertTrue((data / '9ba626afa44a3aa3.patch_3.stream').exists())
            self.assertEqual(deploy.remove(data, recorded, identities), [])
            self.assertFalse((data / '9ba626afa44a3aa3.patch_3.stream').exists())
            self.assertFalse((data / '9ba626afa44a3aa3.patch_3.gpu_resources').exists())


class VanillaStash(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(Path(tmp, 'data').mkdir() or Path(tmp, 'data'))
            stash_dir = Path(tmp, 'stash')
            before = deploy.inventory(data)
            recorded = {}
            moved = deploy.stash(data, stash_dir, recorded.update)
            self.assertEqual(moved, before)
            self.assertEqual(recorded, before)
            self.assertEqual(deploy.inventory(data), {})
            self.assertTrue((data / '9ba626afa44a3aa3').exists(), 'non-patch files stay')
            self.assertEqual(deploy.unstash(data, stash_dir, recorded), [])
            self.assertEqual(deploy.inventory(data), before)
            self.assertEqual(list(stash_dir.iterdir()), [])

    def test_unstash_reports_problems_and_keeps_going(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(Path(tmp, 'data').mkdir() or Path(tmp, 'data'))
            stash_dir = Path(tmp, 'stash')
            recorded = {}
            deploy.stash(data, stash_dir, recorded.update)
            (stash_dir / '9ba626afa44a3aa3.patch_1').write_bytes(b'tampered')
            (data / '9ba626afa44a3aa3.patch_2').write_bytes(b'someone deployed meanwhile')
            problems = deploy.unstash(data, stash_dir, recorded)
            self.assertEqual(len(problems), 2, problems)
            self.assertTrue((data / '9ba626afa44a3aa3.patch_0').exists())

    def test_unstash_moves_a_patch_number_whole_or_not_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(Path(tmp, 'data').mkdir() or Path(tmp, 'data'))
            stash_dir = Path(tmp, 'stash')
            recorded = {}
            deploy.stash(data, stash_dir, recorded.update)
            (data / '9ba626afa44a3aa3.patch_1').write_bytes(b'a manager deployed only an archive here')
            self.assertEqual(deploy.stash_blockers(data, stash_dir, recorded),
                             [('9ba626afa44a3aa3.patch_1',
                               '9ba626afa44a3aa3.patch_1: something else now occupies that name in data/')])
            self.assertEqual(deploy.stash_blockers(data, stash_dir, recorded, leaving=['9ba626afa44a3aa3.patch_1']), [])
            self.assertEqual(len(deploy.unstash(data, stash_dir, recorded)), 1)
            for suffix in deploy.SIDECARS:
                self.assertTrue((stash_dir / ('9ba626afa44a3aa3.patch_1' + suffix)).exists(),
                                'the sidecars stay with their own archive in the stash')
                self.assertFalse((data / ('9ba626afa44a3aa3.patch_1' + suffix)).exists())
            self.assertTrue((data / '9ba626afa44a3aa3.patch_2.stream').exists())
            self.assertEqual([unit for unit, _ in deploy.stash_blockers(data, stash_dir, recorded)],
                             ['9ba626afa44a3aa3.patch_1'], 'files already back do not block')

    def test_refuses_a_non_empty_stash(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = fake_data(Path(tmp, 'data').mkdir() or Path(tmp, 'data'))
            stash_dir = Path(tmp, 'stash')
            stash_dir.mkdir()
            (stash_dir / 'left over').write_bytes(b'x')
            with self.assertRaises(deploy.DeployError):
                deploy.stash(data, stash_dir, lambda files: None)
            self.assertEqual(len(deploy.inventory(data)), 12)

    def test_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(deploy.writable(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
