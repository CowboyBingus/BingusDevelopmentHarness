"""The HellForge target: sessions of a HellForge installation's game copy instead of the Steam install.

A HellForge session is owned by its session runner (<root>/app/session/HellForge.Session.exe play ...), which the
launcher normally starts: it verifies and locks the copy, checks Steam ownership and the network block, starts the
local backend, stages the enabled mods into the copy's data/ and runs the game inside a kill-on-close job. So the
harness never writes into the copy:

- The agent and Bingus Shared Loader go into <root>/mods as two cosmetic packs (folders with game patches at their
  root, or the loader's manager ZIP unpacked), which the runner stages like any mod. Both folders are recorded with
  every file's SHA-256 before they are written, and removed only while every file in them is still the harness's.
  An installation that already has Bingus Shared Loader among its mods keeps its own.
- The Mods page's settings (mods/hellforge-mods.json) are backed up and put back byte for byte: the launcher may
  record the harness's packs in its order while they are there.
- The runner starts detached from the harness, so `start` can return while the session runs. A session ends when the
  game exits (the agent's quit, WM_CLOSE or a kill); the runner then stops the backend, restores the copy and
  exits. stop-observation.signal in its run folder asks it to end the game itself.
- Its run folder (logs/sessions/<run>: result.json, backend/requests.jsonl, multiplayer.log, stand-in logs) is
  copied into the harness run as hellforge-session/.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

from . import archive, winapi
from .paths import GAME_EXE, HARNESS

RUNNER_EXE = 'HellForge.Session.exe'
AGENT_FOLDER = 'zz-harness-agent'
LOADER_FOLDER = 'zz-harness-loader'
LOADER_MANIFEST = 'BingusSharedLoader-manifest.json'
MODS_SETTINGS = 'hellforge-mods.json'
# Bingus Shared Loader v20 supports build 25480438, the build HellForge copies run. HD2H_LOADER_ZIP names its manager
# ZIP; the default is releases/ next to the harness folder.
LOADER_ZIP = Path(os.environ.get('HD2H_LOADER_ZIP') or HARNESS.parent / 'releases' / 'Bingus-Shared-Loader-v20.zip')
STOP_SIGNAL = 'stop-observation.signal'
CREATE_NO_WINDOW = 0x08000000


class HellForgeError(RuntimeError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def runners():
    """PIDs of running session runners (any installation)."""
    return winapi.processes(RUNNER_EXE)


def copy_games(paths):
    """PIDs of running games started from this installation's copy (not the Steam install)."""
    root = os.path.normcase(str(Path(paths.game).resolve())) + os.sep
    return [pid for pid in winapi.processes(GAME_EXE)
            if os.path.normcase(winapi.image_path(pid) or '').startswith(root)]


def facts(paths):
    """The installation's facts; raises HellForgeError when a session cannot start."""
    if paths.hellforge is None or not Path(paths.hellforge).is_dir():
        raise HellForgeError('no HellForge installation at %s' % paths.hellforge)
    install = _json(paths.hellforge / 'hellforge-install.json')
    if install is None:
        raise HellForgeError('%s is not a HellForge installation (no hellforge-install.json)' % paths.hellforge)
    for required in (paths.session_runner, paths.hellforge_profile):
        if not Path(required).exists():
            raise HellForgeError('missing %s' % required)
    if runners():
        raise HellForgeError('a HellForge session is running; end it from the launcher (or `close`) first')
    mismatch = steam_pid_mismatch()
    if mismatch:
        seen, running = mismatch
        raise HellForgeError(
            'Steam is not running as this process sees it (registry ActiveProcess pid %s, steam.exe running as %s), '
            'so the game would stop at "STEAM NOT DETECTED". %s' % (
                seen, running or 'nothing',
                'Start Steam first.' if not running else
                'This process reads a different registry than Steam wrote: the container of a packaged (MSIX) '
                'desktop app keeps a private copy of the key. Run the harness from a normal terminal, or correct the copy '
                'from inside the container (PowerShell, once per Steam restart): Set-ItemProperty '
                "'HKCU:\\Software\\Valve\\Steam\\ActiveProcess' -Name pid -Value %s -Type DWord" % running[0]))
    launcher = _json(paths.hellforge / 'launcher-settings.json') or {}
    return {'hellforge_root': str(paths.hellforge), 'hellforge_build': install.get('buildId'),
            'hellforge_profile': paths.profile, 'launcher_settings': launcher,
            'installation_loader': installed_loader(paths)}


def _mods_state(paths):
    return _json(Path(paths.mods) / MODS_SETTINGS) or {}


def installed_loader(paths, skip=(AGENT_FOLDER, LOADER_FOLDER)):
    """The mods folder holding the installation's own enabled Bingus Shared Loader, or None."""
    disabled = set(_mods_state(paths).get('disabled') or [])
    if not Path(paths.mods).is_dir():
        return None
    for folder in sorted(Path(paths.mods).iterdir()):
        if folder.is_dir() and folder.name not in skip and (folder / LOADER_MANIFEST).exists():
            # A pack's id is its folder's slug; a disabled one does not load.
            if folder.name not in disabled and folder.name.lower() not in disabled:
                return folder.name
    return None


def agent_pack(source):
    """{file name: bytes}: the agent as a pack (one patch at the pack's root, with empty sidecars)."""
    name = '%s.patch_0' % archive.FAMILY
    return {name: archive.agent_archive(source), name + '.gpu_resources': b'', name + '.stream': b''}


def loader_pack(zip_path):
    """{relative path: bytes}: Bingus Shared Loader's manager ZIP, unpacked as it ships."""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise HellForgeError('Bingus Shared Loader ZIP not found: %s' % zip_path)
    with zipfile.ZipFile(zip_path) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist() if not name.endswith('/')}
    if LOADER_MANIFEST not in files:
        raise HellForgeError('%s is not a Bingus Shared Loader release (no %s)' % (zip_path.name, LOADER_MANIFEST))
    return files


def pack_files(paths, source, loader_zip=None):
    """{folder: {relative path: bytes}} the harness adds to <root>/mods: the agent, and the loader unless the
    installation has its own."""
    packs = {AGENT_FOLDER: agent_pack(source)}
    if installed_loader(paths) is None:
        packs[LOADER_FOLDER] = loader_pack(loader_zip or LOADER_ZIP)
    return packs


def install_packs(paths, packs, record):
    """Writes the packs into <root>/mods after record({folder: {path: sha256}}) has saved their hashes."""
    for folder in packs:
        if (Path(paths.mods) / folder).exists():
            raise HellForgeError('%s already exists (a previous harness session? run `recover`)' % (Path(paths.mods) / folder))
    hashes = {folder: {name: _sha(data) for name, data in files.items()} for folder, files in packs.items()}
    record(hashes)
    for folder, files in packs.items():
        for name, data in files.items():
            target = Path(paths.mods) / folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return hashes


def remove_packs(paths, recorded):
    """Removes the recorded packs while every file in them is still the harness's. Returns problems."""
    problems = []
    for folder, files in (recorded or {}).items():
        root = Path(paths.mods) / folder
        if not root.exists():
            continue
        present = {path.relative_to(root).as_posix(): path for path in root.rglob('*') if path.is_file()}
        foreign = sorted(name for name in present if name not in files)
        changed = sorted(name for name, path in present.items() if name in files and _sha(path.read_bytes()) != files[name])
        if foreign or changed:
            problems.append('mods/%s left in place: %s' % (folder, '; '.join(
                (['not written by the harness: %s' % ', '.join(foreign)] if foreign else []) +
                (['changed since: %s' % ', '.join(changed)] if changed else []))))
            continue
        shutil.rmtree(root)
        if root.exists():
            problems.append('mods/%s could not be removed' % folder)
    return problems


def backup_mods_settings(paths, backup_dir):
    """Backs up mods/hellforge-mods.json; returns the record restore_mods_settings takes."""
    path = Path(paths.mods) / MODS_SETTINGS
    entry = {'path': str(path), 'backup': None, 'original_sha256': None}
    if path.exists():
        data = path.read_bytes()
        backup = Path(backup_dir) / 'hellforge-mods.json.backup'
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(data)
        if _sha(backup.read_bytes()) != _sha(data):
            raise HellForgeError('backup of %s did not verify' % path)
        entry.update(backup=str(backup), original_sha256=_sha(data))
    return entry


def restore_mods_settings(entry):
    """Puts mods/hellforge-mods.json back as it was (absent if it was absent). Returns problems."""
    if not entry:
        return []
    path = Path(entry['path'])
    if entry.get('original_sha256') is None:
        if path.exists():
            path.unlink()
        return []
    backup = Path(entry['backup'])
    if path.exists() and _sha(path.read_bytes()) == entry['original_sha256']:
        if backup.exists():
            backup.unlink()
        return []
    if not backup.exists() or _sha(backup.read_bytes()) != entry['original_sha256']:
        return ['mods/%s: backup missing or changed' % MODS_SETTINGS]
    shutil.copyfile(backup, path)
    if _sha(path.read_bytes()) != entry['original_sha256']:
        return ['mods/%s: restore did not verify' % MODS_SETTINGS]
    backup.unlink()
    return []


def session_arguments(paths, multiplayer=None, war=None):
    """The runner's command line, as the launcher builds it from its settings (SessionRunner.Start).

    multiplayer: None follows the launcher (Local PlayFab and the stand-ins), True or False sets both.
    war: None follows the launcher; 'live', 'open' or 'frozen'.
    """
    launcher = _json(paths.hellforge / 'launcher-settings.json') or {}
    template = (_json(paths.hellforge_profile / 'launcher.json') or {}).get('template') or 'unlock-all'
    arguments = [str(paths.session_runner), 'play', '--install', str(paths.hellforge),
                 '--profile', str(paths.hellforge_profile), '--template', template]
    local_playfab = launcher.get('localPlayFab', True) is not False if multiplayer is None else multiplayer
    shims = launcher.get('sdkShims', True) is not False if multiplayer is None else multiplayer
    if local_playfab:
        arguments.append('--local-playfab')
    if shims:
        arguments.append('--multiplayer')
    war = war or launcher.get('war') or 'frozen'
    if war == 'live':
        arguments += ['--live-war', str(paths.hellforge / 'live-war.json')]
    elif war == 'open':
        arguments.append('--open-war')
    if launcher.get('engineLog'):
        arguments.append('--engine-log')
    return arguments


def current_run(paths):
    """(run id, run folder) of the installation's latest session, from logs/play-session.json."""
    pointer = _json(paths.play_session) or {}
    return pointer.get('runId'), (Path(pointer['run']) if pointer.get('run') else None)


def result(run):
    """The run's result.json (the runner's status and facts), or {}."""
    return (_json(Path(run) / 'result.json') or {}) if run else {}


def _runner_log_tail(paths, lines=5):
    log = paths.hellforge / 'logs' / 'session-runner.log'
    try:
        return ' | '.join(log.read_text(encoding='utf-8', errors='replace').splitlines()[-lines:])
    except OSError:
        return ''


def _spawn(arguments, paths, run_dir):
    """Starts the runner without a window, as the launcher does (SessionRunner: CreateNoWindow). The runner reports
    through its run folder and logs/session-runner.log, so nothing of its output is kept here."""
    environment = dict(os.environ)
    temp = Path(paths.hellforge) / 'temp'
    temp.mkdir(exist_ok=True)
    environment['TEMP'] = environment['TMP'] = str(temp)
    try:
        return subprocess.Popen(arguments, cwd=str(paths.hellforge), env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW).pid
    except OSError as error:
        raise HellForgeError('could not start %s: %s' % (arguments[0], error))


def steam_pid_mismatch():
    """(the Steam pid this process reads, the running steam.exe pids) when the first is not among the second, else
    None. The game asks SteamAPI_IsSteamRunning() once at start-up, which reads
    HKCU\\Software\\Valve\\Steam\\ActiveProcess\\pid and checks that process; a stale value makes it stop at
    "STEAM NOT DETECTED". A container with its own registry layer can hold such a value: a packaged (MSIX) desktop
    app runs the programs it starts with HKCU merged over a private hive, which on 2026-10-21 kept the pid of a
    Steam instance gone for a week. A game launched from a normal terminal or the launcher reads the real one."""
    import winreg
    running = winapi.processes('steam.exe')
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess') as key:
            pid = winreg.QueryValueEx(key, 'pid')[0]
    except OSError:
        pid = None
    return None if pid in running else (pid, running)


def launch(paths, run_dir, multiplayer=None, war=None, timeout=300, note=print, record=lambda **facts: None):
    """Starts a session; returns (runner pid, game pid, run id, run folder) once the copy's game runs.

    record(runner_pid=...) runs as soon as the runner exists and record(hellforge_run=..., hellforge_run_id=...) as
    soon as its run folder does, so a start that fails part-way can still stop the runner. The runner reports
    through its run folder. It fails before the game starts when the copy, Steam ownership or the network block is
    not right; that failure is raised with the runner's own words.
    """
    before, _ = current_run(paths)
    runner = _spawn(session_arguments(paths, multiplayer, war), paths, run_dir)
    record(runner_pid=runner)
    note('session runner started (pid %d)' % runner)
    deadline, recorded = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        run_id, run = current_run(paths)
        fresh = run_id is not None and run_id != before
        if fresh and recorded != run_id:
            record(hellforge_run=str(run), hellforge_run_id=run_id)
            recorded = run_id
        status = result(run).get('status') if fresh else None
        if status == 'awaiting_network_block_install':
            raise HellForgeError('the session waits for the network block to be installed; start one session from '
                                 'the HellForge launcher once, then try again')
        games = copy_games(paths)
        if fresh and games:
            return runner, games[0], run_id, run
        if runner not in runners():
            failure = result(run).get('failure') if fresh else None
            raise HellForgeError('the session runner exited before the game started: %s'
                                 % (failure or _runner_log_tail(paths) or 'no reason given'))
        time.sleep(1)
    raise HellForgeError('the game did not start within %d s (session runner pid %d)' % (timeout, runner))


def signal_stop(run):
    """Asks the runner to end the game and the session."""
    if run:
        Path(run, STOP_SIGNAL).write_text('stop', encoding='utf-8')


def wait_runner(pid, timeout):
    """True once the runner has exited (or cannot be opened, which means it is gone)."""
    if not pid or pid not in runners():
        return True
    try:
        with winapi.Process(pid) as process:
            return process.wait(timeout)
    except winapi.WinError:
        return pid not in runners()


def abort_runner(manifest, note=print, wait=60):
    """Ends a runner whose start failed part-way: the stop signal when its run folder exists, else (before it
    started the game) a kill. Returns True once it is gone."""
    pid = manifest.get('runner_pid')
    if wait_runner(pid, 0):
        return True
    if manifest.get('hellforge_run'):
        signal_stop(manifest['hellforge_run'])
        if wait_runner(pid, wait):
            return True
    note('ending the session runner pid %d' % pid)
    winapi.terminate(pid, 1)
    return wait_runner(pid, 30)


def end_runner(manifest, note=print, wait=120, after_signal=60):
    """Lets the runner finish once the game is gone (it stops the backend and restores the copy); asks it to stop
    when it does not. Returns how it ended: 'exited', 'signalled' or 'still-running'."""
    pid = manifest.get('runner_pid')
    if wait_runner(pid, wait):
        return 'exited'
    note('the session runner is still running; asking it to stop')
    signal_stop(manifest.get('hellforge_run'))
    return 'signalled' if wait_runner(pid, after_signal) else 'still-running'


def collect(run, target):
    """Copies the HellForge run folder into the harness run. Returns the number of files copied."""
    if not run or not Path(run).is_dir():
        return 0
    shutil.copytree(run, target, dirs_exist_ok=True)
    return sum(1 for path in Path(target).rglob('*') if path.is_file())
