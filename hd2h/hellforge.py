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
import signal
import subprocess
import sys
import time
import zipfile

from . import archive, winapi
from .paths import GAME_EXE, HARNESS, STEAM_APP_ID

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
IS_WINDOWS = sys.platform == 'win32'
# Popen objects of wrappers this process started, kept so their exit is reaped and a discarded object cannot warn
# "subprocess is still running" while the wrapper runs.
_LINUX_WRAPPERS = {}


class HellForgeError(RuntimeError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def _default_prefix():
    """The Steam compat-data folder a session uses when the installation is outside a prefix."""
    return Path(os.environ.get('STEAM_COMPAT_DATA_PATH') or
                Path.home() / '.local' / 'share' / 'Steam' / 'steamapps' / 'compatdata' / str(STEAM_APP_ID))


def wine_prefix(root):
    """The Steam compat-data folder whose pfx holds root, else STEAM_COMPAT_DATA_PATH or Steam's default prefix."""
    root = Path(root).resolve()
    parts = root.parts
    for index in range(len(parts) - 1):
        if parts[index] == 'pfx' and parts[index + 1] == 'drive_c':
            return Path(*parts[:index])
    return _default_prefix()


def to_wine_path(host, prefix=None):
    """A Windows path for a host path: C:\\... inside the prefix's drive_c, else Z:\\...."""
    host = Path(host)
    prefix = Path(prefix) if prefix is not None else wine_prefix(host)
    drive_c = (prefix / 'pfx' / 'drive_c').resolve()
    try:
        relative = host.resolve().relative_to(drive_c)
    except ValueError:
        return 'Z:' + str(host.resolve()).replace('/', '\\')
    return 'C:\\' if relative == Path('.') else 'C:\\' + str(relative).replace('/', '\\')


def to_host_path(value, prefix=None):
    """The host path of a Wine path (C:\\... through the prefix, Z:\\... as /...); other values are returned as they
    are.

    A C:\\ path belongs to the prefix that wrote it. Callers passing an installation's files use
    installation_host_path, which always uses that installation's own prefix; the Steam default is only a fallback for
    a path with no installation.
    """
    text = str(value)
    if not (len(text) >= 3 and text[0].upper() in ('C', 'Z') and text[1] == ':' and text[2] in ('\\', '/')):
        return text
    drive, rest = text[0].upper(), text[3:].replace('\\', '/')
    if drive == 'C':
        prefix = Path(prefix) if prefix is not None else _default_prefix()
        target = Path(prefix) / 'pfx' / 'drive_c' / rest
    else:
        target = Path('/') / rest
    return os.path.realpath(str(target))


def installation_host_path(paths, value):
    """The host path of a path a HellForge installation wrote: C:\\ through the installation's own prefix, Z:\\ as
    /...."""
    return to_host_path(value, wine_prefix(paths.hellforge))


def linux_wrapper(paths):
    """The Linux session wrapper of an installation."""
    return Path(paths.hellforge) / 'app' / 'linux' / 'hellforge-linux.sh'


def linux_environment(paths, extra=None):
    """The environment the Linux wrapper and its Proton session need."""
    environment = dict(os.environ)
    temp = Path(paths.hellforge) / 'temp'
    temp.mkdir(exist_ok=True)
    environment['TEMP'] = environment['TMP'] = str(temp)
    environment['INSTALL'] = str(paths.hellforge)
    environment['STEAM_COMPAT_DATA_PATH'] = str(wine_prefix(paths.hellforge))
    environment.pop('PSModulePath', None)
    if extra:
        environment.update(extra)
    return environment


def linux_command(arguments, paths):
    """The wrapper command the launcher uses: bash <root>/app/linux/hellforge-linux.sh <session runner> ..."""
    return ['bash', str(linux_wrapper(paths))] + list(arguments)


def runners():
    """PIDs of running session runners (any installation)."""
    return winapi.processes(RUNNER_EXE)


def _copy_games_windows(paths):
    root = os.path.normcase(str(Path(paths.game).resolve())) + os.sep
    return [pid for pid in winapi.processes(GAME_EXE)
            if os.path.normcase(winapi.image_path(pid) or '').startswith(root)]


def _under(path, root):
    if not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _copy_games_linux(paths):
    root = Path(paths.game).resolve()
    return [pid for pid in winapi.processes(GAME_EXE)
            if _under(winapi.image_path(pid, root=paths.hellforge), root)]


copy_games = _copy_games_windows if IS_WINDOWS else _copy_games_linux


def facts(paths):
    """The installation's facts; raises HellForgeError when a session cannot start."""
    if paths.hellforge is None or not Path(paths.hellforge).is_dir():
        raise HellForgeError('no HellForge installation at %s' % paths.hellforge)
    marker = Path(paths.hellforge) / 'hellforge-install.json'
    if IS_WINDOWS:
        install = _json(marker)
        if install is None:
            raise HellForgeError('%s is not a HellForge installation (no hellforge-install.json)' % paths.hellforge)
    else:
        # The Linux wrapper is the supported start route; name every file that is missing, not just the first.
        missing = [str(required) for required in (marker, linux_wrapper(paths)) if not Path(required).exists()]
        if missing:
            raise HellForgeError('%s is not a HellForge installation: missing %s'
                                 % (paths.hellforge, ', '.join(missing)))
        install = _json(marker)
        if install is None:
            raise HellForgeError('%s is not a HellForge installation (%s is not a valid install marker)'
                                 % (paths.hellforge, marker))
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
    if not IS_WINDOWS:
        # The runner is a Windows program under Proton: the launcher hands it Wine paths (SessionRunner.cs).
        prefix = wine_prefix(paths.hellforge)
        for flag in ('--install', '--profile', '--live-war'):
            if flag in arguments:
                index = arguments.index(flag)
                arguments[index + 1] = to_wine_path(arguments[index + 1], prefix)
    return arguments


def current_run(paths):
    """(run id, run folder) of the installation's latest session, from logs/play-session.json."""
    pointer = _json(paths.play_session) or {}
    run = pointer.get('run')
    if run and not IS_WINDOWS:
        run = installation_host_path(paths, run)
    return pointer.get('runId'), (Path(run) if run else None)


def result(run):
    """The run's result.json (the runner's status and facts), or {}."""
    return (_json(Path(run) / 'result.json') or {}) if run else {}


def _tail(path, lines=5):
    try:
        return ' | '.join(Path(path).read_text(encoding='utf-8', errors='replace').splitlines()[-lines:])
    except OSError:
        return ''


def _runner_log_tail(paths, lines=5):
    return _tail(Path(paths.hellforge) / 'logs' / 'session-runner.log', lines)


def _wrapper_log_tail(paths, lines=5):
    """The Linux wrapper's own log; a failure before the run folder opens is often only here."""
    return _tail(Path(paths.hellforge) / 'logs' / 'linux-session-wrapper.log', lines)


def _launch_failure(paths, run):
    """The runner's result or the logs that explain a launch that stopped before the game started."""
    failure = result(run).get('failure') if run else None
    if failure:
        return failure
    if IS_WINDOWS:
        return _runner_log_tail(paths) or 'no reason given'
    tails = []
    for name, tail in (('session-runner.log', _runner_log_tail(paths)),
                       ('linux-session-wrapper.log', _wrapper_log_tail(paths))):
        if tail:
            tails.append('%s: %s' % (name, tail))
    return ' | '.join(tails) or 'no reason given'


def _spawn_windows(arguments, paths, run_dir):
    """Starts the runner without a window, as the launcher does (SessionRunner: CreateNoWindow). The runner reports
    through its run folder and logs/session-runner.log, so nothing of its output is kept here."""
    environment = dict(os.environ)
    temp = Path(paths.hellforge) / 'temp'
    temp.mkdir(exist_ok=True)
    environment['TEMP'] = environment['TMP'] = str(temp)
    # the runner's own checks run Windows PowerShell 5.1; an inherited PowerShell 7 module path breaks them
    environment['PSModulePath'] = windows_powershell_modules()
    try:
        return subprocess.Popen(arguments, cwd=str(paths.hellforge), env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW).pid
    except OSError as error:
        raise HellForgeError('could not start %s: %s' % (arguments[0], error))


def _spawn_linux(arguments, paths, run_dir):
    """Starts the installation's Linux wrapper in its own process group, as the launcher does. The wrapper runs the
    session runner under Proton and keeps the run's whole tree together, so a timeout can end it as one group."""
    wrapper = linux_wrapper(paths)
    if not wrapper.is_file():
        raise HellForgeError('the Linux session wrapper is missing: %s' % wrapper)
    try:
        environment = linux_environment(paths)
        process = subprocess.Popen(linux_command(arguments, paths), cwd=str(paths.hellforge), env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        _LINUX_WRAPPERS[process.pid] = process
        return process.pid
    except OSError as error:
        raise HellForgeError('could not start %s: %s' % (wrapper, error))


_spawn = _spawn_windows if IS_WINDOWS else _spawn_linux


def _pid_alive_windows(pid):
    return pid in runners()


def _pid_alive_linux(pid):
    if not pid:
        return False
    try:
        with winapi.Process(pid) as process:
            return not process.wait(0)
    except winapi.WinError:
        return False


_pid_alive = _pid_alive_windows if IS_WINDOWS else _pid_alive_linux


def windows_powershell_modules():
    """Windows PowerShell 5.1's module path, as a fresh session inherits it: the user's modules, then the machine's."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment') as key:
        machine = os.path.expandvars(winreg.QueryValueEx(key, 'PSModulePath')[0])
    return os.path.expandvars(r'%USERPROFILE%\Documents\WindowsPowerShell\Modules;') + machine


def _steam_pid_mismatch_windows():
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


def _steam_pid_mismatch_linux():
    """Linux has no Windows registry and native Steam has no steam.exe ActiveProcess pid; the wrapper and the
    session runner check the Steam bridge themselves."""
    return None


steam_pid_mismatch = _steam_pid_mismatch_windows if IS_WINDOWS else _steam_pid_mismatch_linux


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
        if not _pid_alive(runner):
            failure = _launch_failure(paths, run if fresh else None)
            raise HellForgeError('the session runner exited before the game started: %s' % failure)
        time.sleep(1)
    if IS_WINDOWS:
        raise HellForgeError('the game did not start within %d s (session runner pid %d)' % (timeout, runner))
    raise HellForgeError('the game did not start within %d s (session runner pid %d): %s'
                         % (timeout, runner, _launch_failure(paths, None)))


def signal_stop(run):
    """Asks the runner to end the game and the session."""
    if run:
        Path(run, STOP_SIGNAL).write_text('stop', encoding='utf-8')


def _wait_runner_windows(pid, timeout):
    """True once the runner has exited (or cannot be opened, which means it is gone)."""
    if not pid or pid not in runners():
        return True
    try:
        with winapi.Process(pid) as process:
            return process.wait(timeout)
    except winapi.WinError:
        return pid not in runners()


def _wait_runner_linux(pid, timeout):
    """True once the wrapper (the Linux runner pid) has exited, or cannot be opened, which means it is gone."""
    if not pid:
        return True
    process = _LINUX_WRAPPERS.get(pid)
    if process is not None and hasattr(process, 'wait'):
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            pass
        _LINUX_WRAPPERS.pop(pid, None)
        return True
    try:
        with winapi.Process(pid) as process:
            return process.wait(timeout)
    except winapi.WinError:
        return True


wait_runner = _wait_runner_windows if IS_WINDOWS else _wait_runner_linux


def _signal_runner_group(pid, sig):
    """Sends a signal to the process group the Linux wrapper leads, or to the process alone when it leads none.
    Returns whether the signal was sent."""
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
        return True
    except OSError:
        return False


def _abort_runner_windows(manifest, note, wait):
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


def _abort_runner_linux(manifest, note, wait):
    """The stop signal first when the run folder exists, then the wrapper's whole group (hf-inner, Proton, the game
    and the backend), never just the wrapper."""
    pid = manifest.get('runner_pid')
    if wait_runner(pid, 0):
        return True
    if manifest.get('hellforge_run'):
        signal_stop(manifest['hellforge_run'])
        if wait_runner(pid, wait):
            return True
    note('ending the session runner pid %d and its process group' % pid)
    if _signal_runner_group(pid, signal.SIGTERM) and wait_runner(pid, 30):
        return True
    _signal_runner_group(pid, signal.SIGKILL)
    return wait_runner(pid, 30)


def abort_runner(manifest, note=print, wait=60):
    """Ends a runner whose start failed part-way: the stop signal when its run folder exists, else (before it
    started the game) a kill. On Linux the kill reaches the wrapper's process group. Returns True once it is gone."""
    if IS_WINDOWS:
        return _abort_runner_windows(manifest, note, wait)
    return _abort_runner_linux(manifest, note, wait)


def _end_runner_windows(manifest, note, wait, after_signal):
    pid = manifest.get('runner_pid')
    if wait_runner(pid, wait):
        return 'exited'
    note('the session runner is still running; asking it to stop')
    signal_stop(manifest.get('hellforge_run'))
    return 'signalled' if wait_runner(pid, after_signal) else 'still-running'


def _end_runner_linux(manifest, note, wait, after_signal):
    pid = manifest.get('runner_pid')
    if wait_runner(pid, wait):
        return 'exited'
    note('the session runner is still running; asking it to stop')
    signal_stop(manifest.get('hellforge_run'))
    if wait_runner(pid, after_signal):
        return 'signalled'
    note('the Linux session wrapper pid %d did not stop; ending its process group' % pid)
    if _signal_runner_group(pid, signal.SIGTERM) and wait_runner(pid, 30):
        return 'killed'
    _signal_runner_group(pid, signal.SIGKILL)
    return 'exited' if wait_runner(pid, 30) else 'still-running'


def end_runner(manifest, note=print, wait=120, after_signal=60):
    """Lets the runner finish once the game is gone (it stops the backend and restores the copy); asks it to stop
    when it does not. On Linux, only after the stop signal has timed out is the wrapper's process group signalled
    (SIGTERM, then SIGKILL). Returns how it ended: 'exited', 'signalled', 'killed' or 'still-running'."""
    if IS_WINDOWS:
        return _end_runner_windows(manifest, note, wait, after_signal)
    return _end_runner_linux(manifest, note, wait, after_signal)


def collect(run, target):
    """Copies the HellForge run folder into the harness run. Returns the number of files copied."""
    if not run or not Path(run).is_dir():
        return 0
    shutil.copytree(run, target, dirs_exist_ok=True)
    return sum(1 for path in Path(target).rglob('*') if path.is_file())
