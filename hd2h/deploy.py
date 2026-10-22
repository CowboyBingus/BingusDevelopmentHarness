"""Changes to the game's data folder, each recorded so it can be undone exactly.

- The agent goes into the next free 9ba626afa44a3aa3.patch_<n> slot, with empty sidecars.
  Removal deletes only those files, and only while they are still the files we wrote.
- A patch number is one unit: the archive and its .gpu_resources and .stream sidecars deploy,
  stay and go together. Empty sidecars hash the same in every deployment, so their bytes never
  prove a unit is ours; the archive's bytes or the recorded file identity do.
- The vanilla condition moves every *.patch_* file into the harness's stash and back,
  checking each file's SHA-256 both ways.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import zipfile

from . import archive

PATCH = re.compile(r'^(?P<family>[0-9a-f]{16})\.patch_(?P<number>\d+)(?P<sidecar>\.gpu_resources|\.stream)?$')
SIDECARS = ('.gpu_resources', '.stream')


class DeployError(RuntimeError):
    pass


def digest(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(1 << 20), b''):
            hasher.update(block)
    return hasher.hexdigest().upper()


def identity(path):
    """[file id, last write time in ns]. A manager that redeploys the same bytes writes a new file
    or at least a new write time, so this tells the file we wrote from an identical copy."""
    info = os.stat(path)
    return [info.st_ino, info.st_mtime_ns]


def unit_of(name):
    """The archive name ('<family>.patch_<n>') for an archive or either of its sidecars."""
    match = PATCH.match(name)
    return '%s.patch_%s' % (match['family'], match['number']) if match else name


def _units(names):
    """{unit: [names]}, each unit's sidecars before its archive."""
    units = {}
    for name in sorted(names, key=lambda name: (unit_of(name), name == unit_of(name))):
        units.setdefault(unit_of(name), []).append(name)
    return units


def inventory(data):
    """{file name: sha256} of every patch file (archives and sidecars) in the data folder."""
    return {entry.name: digest(entry) for entry in Path(data).iterdir()
            if entry.is_file() and PATCH.match(entry.name)}


def next_slot(data, family=archive.FAMILY):
    numbers = [int(match['number']) for match in map(PATCH.match, os.listdir(data))
               if match and match['family'] == family]
    return max(numbers) + 1 if numbers else 0


def agent_files(data, source, family=archive.FAMILY):
    """{file name: bytes} for the agent in the next free slot."""
    name = '%s.patch_%d' % (family, next_slot(data, family))
    return {name: archive.agent_archive(source), name + '.gpu_resources': b'', name + '.stream': b''}


def package_files(packages, start=0, family=archive.FAMILY):
    """{file name: bytes} for the patch archives in manager ZIPs, numbered from `start`.

    Each entry is a ZIP path, or 'path::Option A|Option B' to deploy only those options. By
    default every option is deployed, as a player enables a pack's components (the Megapack ships
    each component as an option). Each option keeps its archives, with .gpu_resources/.stream
    sidecars, in the folders it includes (data/, Addon/, options/<name>/). Archives are numbered in
    package order, then option order, then patch number, so a later one overrides an earlier one,
    as when a manager deploys them. A ZIP without manifest.json deploys every archive it holds.
    """
    files, slot = {}, start
    for entry in packages:
        path, _, wanted = str(entry).partition('::')
        selected = [name for name in wanted.split('|') if name] if wanted else None
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            # Without a manifest: every folder that holds archives, in name order.
            folders = sorted({name.rsplit('/', 1)[0] + '/' if '/' in name else '' for name in names})
            if 'manifest.json' in names:
                options = json.loads(bundle.read('manifest.json')).get('Options') or []
                if selected is not None:
                    known = {option.get('Name') for option in options}
                    missing = [name for name in selected if name not in known]
                    if missing:
                        raise DeployError('%s has no option %s' % (Path(path).name, ', '.join(missing)))
                    options = [option for option in options if option.get('Name') in selected]
                folders = [folder.strip('/') + '/' for option in options for folder in option.get('Include') or ()]
            deployed = 0
            for folder in folders:
                groups = {}
                for name in names:
                    if not name.startswith(folder) or '/' in name[len(folder):]:
                        continue
                    match = PATCH.match(name[len(folder):])
                    if match and match['family'] == family:
                        groups.setdefault(int(match['number']), {})[match['sidecar'] or ''] = bundle.read(name)
                for number in sorted(groups):
                    parts = groups[number]
                    if '' not in parts:
                        raise DeployError('%s: %spatch_%d has sidecars but no archive' % (Path(path).name, folder, number))
                    name = '%s.patch_%d' % (family, slot)
                    files[name] = parts['']
                    files[name + '.gpu_resources'] = parts.get('.gpu_resources', b'')
                    files[name + '.stream'] = parts.get('.stream', b'')
                    slot += 1
                    deployed += 1
        if not deployed:
            raise DeployError('%s holds no %s patch archives' % (Path(path).name, family))
    return files


def carries_loader(files):
    """True if any archive in {name: bytes} carries the loader's startup override."""
    for name, content in files.items():
        match = PATCH.match(name)
        if match and not match['sidecar']:
            try:
                if archive.contains(content, archive.LOADER_CALLBACKS):
                    return True
            except ValueError:
                continue
    return False


def install(data, files, record):
    """Writes files that must not exist yet. record(files) is saved first, so a crash can be undone.
    Returns {name: identity} of the written files, for the caller to record as well."""
    data = Path(data)
    for name in files:
        if (data / name).exists():
            raise DeployError('%s already exists; refusing to overwrite it' % name)
    record({name: hashlib.sha256(content).hexdigest().upper() for name, content in files.items()})
    for name, content in files.items():
        with open(data / name, 'xb') as file:
            file.write(content)
    return {name: identity(data / name) for name in files}


def _foreign_file(path, expected, recorded):
    """Why the file at path is not the one we wrote, or None if it still is (as far as we know)."""
    if digest(path) != expected:
        return '%s changed' % path.name
    if recorded is not None and identity(path) != list(recorded):
        return '%s was replaced by another file with the same bytes' % path.name
    return None


def judge(data, written, identities=None):
    """Sorts the files we wrote ({name: sha256}) by patch number, archive and sidecars together.

    identities: {name: identity} recorded right after writing (missing for older manifests).
    Returns (ours, foreign); a unit with nothing left is in neither:
    - ours: [(unit, names still present, sidecars first, proven)]: units holding only files we
      wrote. proven: each of them still has its recorded identity; otherwise the archive's bytes
      vouch for the unit.
    - foreign: [(unit, reason)]: units that hold a file we did not write, or whose archive is gone
      while the files left prove nothing by their bytes (empty sidecars hash the same everywhere).
      Every file of such a unit is someone else's as far as we can tell.
    """
    data, identities = Path(data), identities or {}
    ours, foreign = [], []
    for unit, names in _units(written).items():
        present = [name for name in names if (data / name).exists()]
        if not present:
            continue
        reasons = (_foreign_file(data / name, written[name], identities.get(name)) for name in present)
        reason = next((reason for reason in reasons if reason), None)
        proven = all(name in identities for name in present)
        if reason is None and unit not in present and not proven:
            reason = '%s is gone and the files left cannot be told apart from another deployment' % unit
        if reason:
            foreign.append((unit, reason))
        else:
            ours.append((unit, present, proven))
    return ours, foreign


def delete(data, ours, proven_only=False):
    """Deletes units judge() found ours, sidecars first so the archive that vouches for a unit goes
    last. proven_only: keep units that only their bytes vouch for. Returns [(unit, reason)] kept."""
    kept = []
    for unit, names, proven in ours:
        if proven_only and not proven:
            kept.append((unit, 'no file identity was recorded, and its bytes alone cannot prove the harness wrote it'))
            continue
        for name in names:
            (Path(data) / name).unlink()
    return kept


def remove(data, written, identities=None):
    """Deletes the files we wrote ({name: sha256}), a patch number at a time (see judge). Returns the
    units left in place whole because they cannot be told apart from someone else's files."""
    ours, foreign = judge(data, written, identities)
    delete(data, ours)
    return [unit for unit, _ in foreign]


def _move(source, target):
    if Path(source).drive.lower() == Path(target).drive.lower():
        os.replace(source, target)
    else:
        shutil.copy2(source, target)
        os.remove(source)


def loader_copy(data, family=archive.FAMILY):
    """{file name: bytes}: the loader's patch archives and sidecars, renumbered from patch_0.

    The game reads patch_0, patch_1, ... and stops at the first missing number, so a loader left at
    its own number (often the last one, as the Arsenal instructions say) would never load once the
    patches before it are stashed. The loader-only condition stashes every patch and installs this
    copy instead; teardown removes it by hash and unstashes the originals.
    """
    files = {}
    for index, name in enumerate(owner_of_loader(data)):
        target = '%s.patch_%d' % (family, index)
        for suffix in ('', '.gpu_resources', '.stream'):
            source = Path(data) / (name + suffix)
            files[target + suffix] = source.read_bytes() if source.exists() else b''
    return files


def stash(data, stash_dir, record, keep=()):
    """Moves every patch file except `keep` out of the data folder. record(files) is saved first."""
    data, stash_dir = Path(data), Path(stash_dir)
    files = {name: sha for name, sha in inventory(data).items() if name not in set(keep)}
    stash_dir.mkdir(parents=True, exist_ok=True)
    leftovers = [entry.name for entry in stash_dir.iterdir()]
    if leftovers:
        raise DeployError('stash folder is not empty: %s' % ', '.join(sorted(leftovers)[:5]))
    record(files)
    for name in sorted(files):
        _move(data / name, stash_dir / name)
    return files


def _occupant(data, stash_dir, unit, files, leaving):
    """The first name of a stashed unit that something other than its stashed file holds in data/."""
    for name in [unit + suffix for suffix in ('',) + SIDECARS]:
        target = Path(data) / name
        if name in leaving or not target.exists():
            continue
        if name in files and not (Path(stash_dir) / name).exists() and digest(target) == files[name]:
            continue  # already back
        return name
    return None


def stash_blockers(data, stash_dir, files, leaving=()):
    """[(unit, reason)] for stashed patch numbers that something else now holds in data/, not
    counting `leaving` (our own files about to be removed). Any of them means data/ was redeployed
    from outside while the originals were stashed."""
    leaving = set(leaving)
    blocked = []
    for unit in _units(files):
        name = _occupant(data, stash_dir, unit, files, leaving)
        if name:
            blocked.append((unit, '%s: something else now occupies that name in data/' % name))
    return blocked


def unstash(data, stash_dir, files):
    """Moves stashed files back a patch number at a time: a unit with a missing, changed or blocked
    file stays in the stash whole, so no archive is put next to another deployment's sidecars.
    Returns problems."""
    data, stash_dir, problems = Path(data), Path(stash_dir), []
    for unit, names in sorted(_units(files).items()):
        occupied = _occupant(data, stash_dir, unit, files, ())
        if occupied:
            problems.append('%s: something else now occupies that name in data/' % occupied)
            continue
        moves, trouble = [], []
        for name in names:
            source, target = stash_dir / name, data / name
            if not source.exists():
                if not target.exists():
                    trouble.append('%s: missing from the stash' % name)
                continue  # otherwise already back
            if digest(source) != files[name]:
                trouble.append('%s: changed while stashed' % name)
                continue
            moves.append(name)
        problems += trouble
        if not trouble:
            for name in moves:
                _move(stash_dir / name, data / name)
    return problems


def writable(data):
    """True if a file can be created and deleted in the data folder."""
    probe = Path(data) / '.hd2h-write-probe'
    try:
        with open(probe, 'xb'):
            pass
        probe.unlink()
        return True
    except OSError:
        return False


def owner_of_loader(data):
    """Names of patch archives that carry the loader's startup override, if any."""
    found = []
    for name in sorted(os.listdir(data)):
        match = PATCH.match(name)
        if not match or match['sidecar']:
            continue
        try:
            with open(Path(data) / name, 'rb') as file:
                if archive.contains(file.read(), archive.LOADER_CALLBACKS):
                    found.append(name)
        except (OSError, ValueError):
            continue
    return found
