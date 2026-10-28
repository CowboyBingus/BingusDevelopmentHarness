"""One game session: preflight, start, stop, finish (collect + restore), recover, watch."""
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from . import builds, deploy, dumps, hellforge, settings, winapi
from .mailbox import Mailbox, MailboxError
from .paths import GAME_EXE, STEAM_APP_ID
from .state import State

IS_WINDOWS = sys.platform == 'win32'


class HarnessError(RuntimeError):
    pass


# mods: the deployed mods plus the agent. loader-only: only the Shared Loader's patch plus the
# agent (a control that can still reach the ship). vanilla: no patches at all, no agent.
# packages: every installed patch is stashed and the given manager ZIPs are deployed instead.
CONDITIONS = ('mods', 'loader-only', 'vanilla', 'packages')


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def running(name=GAME_EXE):
    return winapi.processes(name)


def preflight(paths, agent=True, package_files=None):
    """Facts about the install; raises HarnessError when a session cannot start yet.

    The game must be closed here: the harness edits files it reads at launch and rewrites at
    exit. start() closes a running game first unless told to keep it.
    """
    if running():
        raise HarnessError('Helldivers 2 is running (start closes it unless --keep-running)')
    if State(paths.state).load() is not None:
        raise HarnessError('a harness session is still recorded in state/active.json; run `finish` or `recover`')
    if paths.target == 'hellforge':
        return _preflight_hellforge(paths, agent)
    for required in (paths.exe, paths.game_dll, paths.data, paths.settings_file):
        if not Path(required).exists():
            raise HarnessError('missing %s' % required)
    profile = settings.profile_file(paths)
    if not deploy.writable(paths.data):
        raise HarnessError('cannot write to %s' % paths.data)
    build, exe_sha, dll_sha = builds.identify(paths)
    loader = deploy.owner_of_loader(paths.data)
    if package_files is not None:
        if agent and not deploy.carries_loader(package_files):
            raise HarnessError('none of the packages carries Bingus Shared Loader, so the agent cannot start')
    elif agent and not loader:
        raise HarnessError('no Bingus Shared Loader patch is deployed, so the agent cannot start')
    return {'build': build.build if build else None, 'exe_sha256': exe_sha, 'game_dll_sha256': dll_sha,
            'loader_patches': loader, 'profile': str(profile), 'privacy': settings.privacy(paths),
            'patches': len([name for name in deploy.inventory(paths.data) if deploy.PATCH.match(name)['sidecar'] is None])}


def _preflight_hellforge(paths, agent):
    """Facts about a HellForge installation (hd2h/hellforge.py). The copy itself is the runner's to verify."""
    try:
        facts = hellforge.facts(paths)
    except hellforge.HellForgeError as error:
        raise HarnessError(str(error))
    for required in (paths.exe, paths.game_dll, paths.settings_file):
        if not Path(required).exists():
            raise HarnessError('missing %s' % required)
    if agent and not facts['installation_loader'] and not hellforge.LOADER_ZIP.exists():
        raise HarnessError('no Bingus Shared Loader among the mods and no %s to add, so the agent cannot start'
                           % hellforge.LOADER_ZIP)
    build, exe_sha, dll_sha = builds.identify(paths)
    profiles = settings.profile_files(paths)
    facts.update({'target': 'hellforge', 'build': build.build if build else None, 'exe_sha256': exe_sha,
                  'game_dll_sha256': dll_sha, 'profile': str(settings.profile_file(paths)),
                  'profiles': [str(path) for path in profiles], 'privacy': settings.privacy(paths)})
    return facts


_GAME_BASES = {}  # pid -> game.dll base, so a scan (~0.1 s) runs once per process


def game_dll_base(pid, build):
    """game.dll's base in the running game, found once per process (image scan, ~0.1 s)."""
    if pid not in _GAME_BASES:
        with winapi.Reader(pid) as reader:
            base = reader.image_base(build.dll_timestamp)
        if not base:
            return None
        _GAME_BASES[pid] = base
    return _GAME_BASES[pid]


def live_reads(pid, build):
    """Game state, mission flag and lobby privacy read from the game's memory (known builds only)."""
    if build is None:
        return {'note': 'unknown game build: no live reads'}
    try:
        base = game_dll_base(pid, build)
        if not base:
            return {'note': 'game.dll not mapped yet'}
        with winapi.Reader(pid) as reader:
            mode = reader.pointer(base + build.mode_rva)
            mission = reader.u32(mode + 8) if mode else None
            game = reader.pointer(base + build.game_rva)
            privacy = reader.u32(game + build.privacy_offset) if game else None
            state = reader.u32(game + build.state_offset) if game and build.state_offset else None
    except winapi.WinError as error:
        return {'note': str(error)}
    return {'state': state, 'state_name': builds.STATES.get(state), 'mission': mission,
            'in_mission': mission == 1 if mission is not None else None,
            'privacy': privacy, 'privacy_name': builds.PRIVACY.get(privacy)}


def _end(pid, process, note, close_timeout, kill_timeout, graceful=True):
    """WM_CLOSE (if graceful), then terminate (TerminateProcess on Windows, SIGKILL on Linux), then taskkill on
    Windows. Returns how the process ended."""
    if graceful and winapi.post_close(pid):
        note('asked pid %d to close (WM_CLOSE)' % pid)
        if process.wait(close_timeout):
            return 'close'
    note('terminating pid %d' % pid)
    if not winapi.terminate(pid, 1) and IS_WINDOWS:
        subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
    if not process.wait(kill_timeout):
        raise HarnessError(('pid %d did not exit after TerminateProcess and taskkill' if IS_WINDOWS else
                            'pid %d did not exit after terminate') % pid)
    return 'kill'


CRASH_HANDLER_EXE = 'crs-handler.exe'


def end_crash_handlers(manifest, note=print, timeout=10):
    """Ends the game's crash reporter of this session. The game starts crs-handler.exe a few seconds after itself; it
    quits with a clean game but outlives a crash or a kill, waiting on its report dialog, and Steam counts it as the
    game still running: every later launch "completes" without starting the game (reported case 2026-10-13: after
    a crash the next two starts failed with "the game did not start within 180 s"). Only reporters created with or
    after this session's game (manifest 'created') are ended, never one from the user's own earlier play. The crash
    dump stays in the game's dumps folder. Returns the pids ended."""
    created = manifest.get('created')
    if created is None:
        return []
    ended = []
    for pid in running(CRASH_HANDLER_EXE):
        try:
            process = winapi.Process(pid)
        except winapi.WinError:
            continue  # gone meanwhile
        with process:
            born = process.creation_time()
            if born is None or born < created:
                continue
            note('ending the crash reporter pid %d left by this session' % pid)
            _end(pid, process, note, timeout, timeout)
        ended.append(pid)
    return ended


def close_running(note=print, close_timeout=60, kill_timeout=30):
    """Closes every running game, gracefully first. Returns [{pid, how, code}]."""
    closed = []
    for pid in running():
        try:
            process = winapi.Process(pid)
        except winapi.WinError:
            if pid not in running():
                continue
            raise
        with process:
            how = _end(pid, process, note, close_timeout, kill_timeout)
            closed.append({'pid': pid, 'how': how, 'code': process.exit_code()})
    if closed:
        time.sleep(5)  # Steam ignores a launch while it still counts the old process as running
    return closed


def launch(timeout=180):
    """Starts the game through Steam and returns its PID (asks Steam a second time halfway)."""
    if not IS_WINDOWS:
        raise HarnessError('the steam target runs on Windows only; use --target hellforge')
    os.startfile('steam://rungameid/%d' % STEAM_APP_ID)
    started, asked_again = time.monotonic(), False
    while time.monotonic() - started < timeout:
        found = running()
        if found:
            return found[0]
        if not asked_again and time.monotonic() - started > timeout / 2:
            os.startfile('steam://rungameid/%d' % STEAM_APP_ID)
            asked_again = True
        time.sleep(1)
    raise HarnessError('the game did not start within %d s' % timeout)


def wait_for_hello(mailbox, session_id, pid, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hello = mailbox.read('hello')
        if hello and hello.get('session') == session_id:
            return hello
        if pid not in running():
            raise HarnessError('the game exited before the agent said hello')
        time.sleep(1)
    raise HarnessError('no hello from the agent within %d s (is the loader deployed and discovery working?)' % timeout)


def _install(paths, state, key, files):
    """Deploys files under manifest[key]: their hashes are recorded before writing, then which files
    hold them, so teardown can tell them from an identical copy a mod manager writes later."""
    written = deploy.install(paths.data, files, lambda hashes: state.update(**{key: {'files': hashes}}))
    state.update(**{key: dict(state.load()[key], identity=written)})


def _check_public(paths, multiplayer):
    """A Public lobby is allowed only where it cannot reach other players: a HellForge copy whose lobby service is
    the stand-ins (Local PlayFab and the multiplayer stand-ins both on). The Steam target's lobbies are real PlayFab
    lobbies, and a HellForge session with Local PlayFab off searches the app's Steam lobbies, which vanilla games
    publish too, under the same build string (seen 2026-10-22)."""
    if paths.target != 'hellforge':
        raise HarnessError('a Public lobby is only for HellForge sessions: on Steam it would be open to real players')
    arguments = hellforge.session_arguments(paths, multiplayer)
    missing = [flag for flag in ('--local-playfab', '--multiplayer') if flag not in arguments]
    if missing:
        raise HarnessError('a Public HellForge lobby needs Local PlayFab and the multiplayer stand-ins on (missing %s)'
                           % ', '.join(missing))


def _launch_steam(paths, state, session_id, mailbox, condition, agent, package_files, launch_timeout, preset, note):
    """Prepares data/ for the condition (stash, loader copy, packages, agent) and launches through Steam."""
    if condition in ('vanilla', 'loader-only', 'packages'):
        loader = deploy.loader_copy(paths.data) if condition == 'loader-only' else {}
        if condition == 'loader-only' and not loader:
            raise HarnessError('loader-only: no Bingus Shared Loader patch is deployed')
        deploy.stash(paths.data, paths.state / 'stash',
                     lambda files: state.update(stash={'dir': str(paths.state / 'stash'), 'files': files}))
        if loader:
            _install(paths, state, 'loader_copy', loader)
    if package_files:
        _install(paths, state, 'packages', package_files)
    if agent:
        _install(paths, state, 'agent', deploy.agent_files(paths.data, paths.agent_source.read_bytes()))
        mailbox.begin(session_id)
    note('session %s prepared (%s, preset %s); launching through Steam' % (session_id, condition, preset))
    return launch(launch_timeout)


def _launch_hellforge(paths, state, session_id, mailbox, run_dir, multiplayer, war, timeout, preset, note):
    """Adds the agent (and Bingus Shared Loader, unless the installation has it) as packs in mods/, backs up the
    Mods page's settings, and starts the session runner (hd2h/hellforge.py)."""
    state.update(mods_settings=hellforge.backup_mods_settings(paths, paths.state / 'settings-backup'))
    packs = hellforge.pack_files(paths, paths.agent_source.read_bytes())
    hellforge.install_packs(paths, packs, lambda hashes: state.update(packs=hashes))
    state.update(agent={'pack': hellforge.AGENT_FOLDER})
    mailbox.begin(session_id)
    note('session %s prepared (HellForge %s, profile %s, preset %s; packs %s); starting its session runner'
         % (session_id, paths.hellforge, paths.profile, preset, ', '.join(sorted(packs))))
    try:
        _runner, pid, run_id, _run = hellforge.launch(paths, run_dir, multiplayer, war, timeout, note,
                                                      record=lambda **facts: state.update(**facts))
    except hellforge.HellForgeError as error:
        raise HarnessError(str(error))
    note('HellForge session %s running' % run_id)
    return pid


def start(paths, preset='private', condition='mods', agent=True, run_dir=None, note=print,
          launch_timeout=180, hello_timeout=300, take_over=True, packages=(), multiplayer=None, war=None,
          privacy='invite'):
    """Prepares the install and launches the game. The session stays recorded until finish().

    take_over: end a harness session still recorded, and close a game started by anyone else,
    before preparing; otherwise refuse while either exists.
    HellForge target (paths.target): the session runner launches the copy; multiplayer and war override the
    launcher's settings (hd2h/hellforge.session_arguments).
    privacy: 'invite' (always on the Steam target) or 'public' (HellForge only, see _check_public).
    """
    if condition not in CONDITIONS:
        raise HarnessError('condition must be one of %s' % ', '.join(CONDITIONS))
    if privacy == 'public':
        _check_public(paths, multiplayer)
    if paths.target == 'hellforge' and (condition != 'mods' or packages or not agent):
        raise HarnessError('HellForge sessions run the mods condition with the agent (its packs go into mods/)')
    if condition == 'vanilla' and agent:
        raise HarnessError('the vanilla condition has no loader, so it cannot run the agent')
    if (condition == 'packages') != bool(packages):
        raise HarnessError('package ZIPs go with the packages condition, and it needs at least one')
    package_files = deploy.package_files(packages) if packages else None
    closed = []
    if take_over:
        if State(paths.state).load() is not None:
            note('a harness session is still recorded: stopping it first')
            stop(paths, note=note)
        closed = close_running(note)
        if paths.target == 'hellforge':
            for runner in hellforge.runners():  # a closed copy's runner still restores the copy
                if not hellforge.wait_runner(runner, 90):
                    raise HarnessError('the HellForge session runner pid %d did not end after its game closed' % runner)
    facts = preflight(paths, agent=agent, package_files=package_files)
    if packages:
        facts['packages'] = [Path(package).name for package in packages]
        # Each ZIP's SHA-256, so a run record names the exact bytes it played (the loader's
        # scripts/pin_tested.py reads it).
        facts['package_sha256'] = {Path(package).name: hashlib.sha256(Path(package).read_bytes()).hexdigest().upper()
                                   for package in packages}
    state, mailbox = State(paths.state), Mailbox(paths.mailbox)
    session_id = uuid.uuid4().hex[:12]
    run_dir = Path(run_dir) if run_dir else paths.runs / ('%s-%s%s' % (
        _stamp(), condition, '-hellforge' if paths.target == 'hellforge' else ''))
    target = paths.target
    manifest = {'session': session_id, 'run': str(run_dir), 'condition': condition, 'preset': preset,
                'agent': None, 'stash': None, 'settings': None, 'pid': None, 'created': None,
                'started': _now(), 'started_epoch': datetime.datetime.now().timestamp(), 'facts': facts,
                'dumps_before': dumps.listing(paths.dumps),
                # A HellForge session stages its mods into the copy's data/ itself: the harness never touches it.
                'inventory_before': deploy.inventory(paths.data) if target == 'steam' else None,
                'exit': None, 'quit_requested': False, 'closed_before_start': closed, 'target': target,
                'privacy': privacy}
    if target == 'hellforge':
        manifest.update(hellforge_root=str(paths.hellforge), profile=paths.profile, packs=None, mods_settings=None,
                        runner_pid=None, hellforge_run=None, hellforge_run_id=None)
    state.save(manifest)
    # The next launch truncates every mod log; keep the previous session's copies with this run.
    _copy_tree(paths.logs, run_dir / 'logs-previous')
    try:
        settings.apply(paths, preset, paths.state / 'settings-backup', lambda entry: state.update(settings=entry),
                       privacy)
        if target == 'hellforge':
            pid = _launch_hellforge(paths, state, session_id, mailbox, run_dir, multiplayer, war,
                                    max(launch_timeout, 300), preset, note)
        else:
            pid = _launch_steam(paths, state, session_id, mailbox, condition, agent, package_files, launch_timeout,
                                preset, note)
    except BaseException:
        current = state.load() or {}
        if current.get('runner_pid') and not hellforge.abort_runner(current, note):
            note('the HellForge session runner pid %d did not end; its mods stay until `recover`' % current['runner_pid'])
            raise
        problems = teardown(paths)
        if problems:
            note('could not fully undo the preparation (manifest kept for `recover`): %s' % problems)
        else:
            state.clear()
        raise
    with winapi.Process(pid) as process:
        created = process.creation_time()
    state.update(pid=pid, created=created, launched=_now())
    note('game running, pid %d' % pid)
    if agent:
        hello = wait_for_hello(mailbox, session_id, pid, hello_timeout)
        state.update(hello=_now())
        note('agent connected (loader modules: %d)' % len(((hello.get('state') or {}).get('loader') or {})
                                                             .get('modules') or {}))
    return state.load()


def _alive(manifest):
    pid = manifest.get('pid')
    return bool(pid) and pid in running()


def _wait_for_title(paths, manifest, note, timeout=240):
    """Without the agent the ship is out of reach: wait until the title screen (or later) is up."""
    build = builds.KNOWN.get((manifest.get('facts') or {}).get('build') or '')
    if build is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _alive(manifest):
        if (live_reads(manifest['pid'], build).get('state') or 0) in (2, 3, 4, 5, 6):
            State(paths.state).update(reached_title=_now())
            return True
        time.sleep(2)
    return False


def _before_quit(paths, manifest, note):
    """Closing the game during startup crashes it (vanilla helldivers2+0xC948A), so a graceful end
    first reaches the ship (with the agent) or the title screen (without it). Returns False when that
    failed and the game must be terminated."""
    if not manifest.get('agent'):
        if _wait_for_title(paths, manifest, note):
            return True
        note('the title screen never came up; terminating instead of closing mid-startup')
        return False
    build = builds.KNOWN.get((manifest.get('facts') or {}).get('build') or '')
    if build is not None and (live_reads(manifest['pid'], build).get('state') or 0) in (4, 6):
        # Quitting inside a mission is the vanilla teardown crash path (+0x626296): go back to the ship
        # first, and terminate instead if that fails.
        from . import mission  # mission imports this module
        try:
            mission.leave(paths, manifest, note=note)
        except (mission.MissionError, MailboxError, HarnessError, winapi.WinError) as error:
            note('could not leave the mission (%s); terminating instead of quitting inside it' % error)
            return False
    from . import navigate  # navigate imports this module
    try:
        navigate.to_ship(paths, manifest, note=note)
        return True
    except (navigate.NavigationError, MailboxError, HarnessError) as error:
        note('could not reach the ship before quitting (%s); terminating instead of closing mid-startup' % error)
        return False


# How long finish waits for the crash handler's dump after a nonzero exit, and how long the dump
# folder must stay unchanged.
DUMP_WAIT, DUMP_QUIET = 20.0, 3.0


def _settle_dumps(paths, manifest, crashed):
    """The game's crash handler can still be writing its dump when the process is gone."""
    wait, quiet = DUMP_WAIT, DUMP_QUIET
    deadline, last, still_since = time.monotonic() + (wait if crashed else quiet), None, time.monotonic()
    while time.monotonic() < deadline:
        listing = dumps.listing(paths.dumps)
        if listing != last:
            last, still_since = listing, time.monotonic()
        elif time.monotonic() - still_since >= quiet and (not crashed or listing != manifest.get('dumps_before')):
            return
        time.sleep(0.5)


def stop(paths, kill=False, quit_timeout=90, note=print):
    """Ends the game (agent quit, then WM_CLOSE, then terminate) and finishes the session.

    A graceful end first reaches the ship (or, without the agent, the title screen): closing the
    game during startup crashes it. If that fails the game is terminated instead.
    """
    state = State(paths.state)
    manifest = state.load()
    if manifest is None:
        raise HarnessError('no active session')
    if _alive(manifest) and not kill and not _before_quit(paths, manifest, note):
        kill = True
    manifest = state.load()
    if _alive(manifest):
        pid = manifest['pid']
        with winapi.Process(pid) as process:
            how = None
            if not kill and manifest.get('agent'):
                try:
                    values = Mailbox(paths.mailbox).call('return agent.quit()', timeout=15)
                    if values and values[0] is True:
                        state.update(quit_requested=True)
                        note('asked the game to quit')
                        if process.wait(quit_timeout):
                            how = 'quit'
                    else:
                        note('agent cannot quit: %s' % (values[1] if len(values) > 1 else values))
                except MailboxError as error:
                    note('agent did not answer: %s' % error)
            if how is None:
                how = _end(pid, process, note, 30, 30, graceful=not kill)
            code = process.exit_code()
            state.update(exit={'code': code, 'how': how, 'at': _now()})
        _settle_dumps(paths, manifest, crashed=code not in (0, None) and how != 'kill')
    ended = end_crash_handlers(manifest, note)
    if ended:
        state.update(crash_handlers_ended=ended)
    return finish(paths, note=note)


# File times can trail the clock slightly; a log written in the session's first seconds still counts.
LOG_CLOCK_SLACK = 2


def _copy_tree(source, target):
    source, target = Path(source), Path(target)
    if not source.exists():
        return []
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for entry in source.iterdir():
        if entry.is_file():
            shutil.copy2(entry, target / entry.name)
            copied.append(entry.name)
    return copied


def _copy_logs(source, run_dir, since):
    """Copies the mod logs to run_dir/logs, except those not written since the session started
    (mods that did not run this time leave their old logs behind), which go to logs-stale/.
    Returns (fresh names, stale names)."""
    source = Path(source)
    if not source.exists():
        return [], []
    fresh, stale = [], []
    for entry in sorted(source.iterdir()):
        if not entry.is_file():
            continue
        written = since is None or entry.stat().st_mtime >= since - LOG_CLOCK_SLACK
        target = Path(run_dir) / ('logs' if written else 'logs-stale')
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target / entry.name)
        (fresh if written else stale).append(entry.name)
    return fresh, stale


# What start() deploys, by manifest key, and how restore problems name it.
_DEPLOYED = (('agent', 'agent'), ('packages', 'package'), ('loader_copy', 'loader copy'))


def _redeploy_advice(paths, manifest, done):
    """How to resolve a session whose data/ was redeployed from outside."""
    active, run, stash = State(paths.state).path, Path(manifest['run']), (manifest.get('stash') or {}).get('dir')
    return ('data/ was redeployed from outside the harness during this session, so the harness cannot tell its '
            'files there from yours: %s. Resolve it by hand: compare data/ with inventory_before in %s (a '
            '"data/ differs" problem lists the differences), put data/ right with your mod manager%s, then move %s '
            'to %s%s.'
            % (done, active, ' or from the originals in %s' % stash if stash else '', active,
               run / 'active-manifest-stale.json', ' and %s to %s' % (stash, run / 'stash-kept') if stash else ''))


def _restore_data(paths, state, manifest):
    """Removes the patch files the session deployed and unstashes the originals. Returns problems.

    If data/ was redeployed from outside meanwhile (a patch number the session deployed or stashed
    holds files the harness did not write), its files there cannot be told from the user's: only
    units proven to be the harness's own (by file identity) are removed, the stash stays, and the
    manifest records it, so every later teardown (recover) changes nothing in data/.
    """
    found = manifest.get('outside_redeploy')
    if found:
        return ['outside redeploy found %s: %s' % (found['at'], '; '.join(found['evidence'])),
                _redeploy_advice(paths, manifest, 'recover removes and unstashes nothing')]
    judged = [(label, deploy.judge(paths.data, manifest[key]['files'], manifest[key].get('identity')))
              for key, label in _DEPLOYED if manifest.get(key)]
    outside = ['%s %s left in place: %s' % (label, unit, reason)
               for label, (_, foreign) in judged for unit, reason in foreign]
    stash = manifest.get('stash')
    if stash and not outside:
        leaving = [name for _, (ours, _) in judged for _, names, _ in ours for name in names]
        outside = ['stashed %s left in the stash: %s' % (unit, reason)
                   for unit, reason in deploy.stash_blockers(paths.data, stash['dir'], stash['files'], leaving)]
    if outside:
        state.update(outside_redeploy={'at': _now(), 'evidence': outside})
    problems = list(outside)
    for label, (ours, _) in judged:
        problems += ['%s %s left in place: %s' % (label, unit, reason)
                     for unit, reason in deploy.delete(paths.data, ours, proven_only=bool(outside))]
    if stash and not outside:
        problems += deploy.unstash(paths.data, stash['dir'], stash['files'])
    if outside:
        problems.append(_redeploy_advice(paths, manifest, 'only files it could prove it wrote were removed, '
                                                          'and the stash was left as it is'))
    return problems


def teardown(paths):
    """Undoes everything recorded in the manifest. Returns problems (empty when all verified)."""
    state = State(paths.state)
    manifest = state.load()
    if manifest is None:
        return []
    if manifest.get('agent'):
        Mailbox(paths.mailbox).end()
    if manifest.get('target') == 'hellforge':
        problems = hellforge.remove_packs(paths, manifest.get('packs'))
        problems += hellforge.restore_mods_settings(manifest.get('mods_settings'))
    else:
        problems = _restore_data(paths, state, manifest)
    if manifest.get('settings'):
        problems += settings.restore(manifest['settings'])
    before = manifest.get('inventory_before')
    after = deploy.inventory(paths.data) if before is not None else None
    if before is not None and after != before:
        added = sorted(set(after) - set(before))
        missing = sorted(set(before) - set(after))
        changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
        problems.append('data/ differs from before the session: added %s, missing %s, changed %s'
                        % (added, missing, changed))
    return problems


@contextlib.contextmanager
def _finish_lock(paths, wait=120):
    """One finisher at a time (stop and a background watch can both reach finish)."""
    paths.state.mkdir(parents=True, exist_ok=True)
    lock = paths.state / 'finish.lock'
    deadline = time.monotonic() + wait
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise HarnessError('state/finish.lock is held; delete it if no harness is running')
            time.sleep(0.5)
    try:
        os.close(handle)
        yield
    finally:
        lock.unlink()


def finish(paths, note=print):
    """Collects logs, agent files and new dumps, restores the install, writes run.json."""
    if running():
        raise HarnessError('the game is still running; stop it first')
    state = State(paths.state)
    manifest = state.load()
    if manifest and manifest.get('runner_pid') and not manifest.get('runner_end'):
        # The runner stops the backend and restores the copy once the game is gone; the packs stay until it is done.
        how = hellforge.end_runner(manifest, note)
        state.update(runner_end=how)
        if how == 'still-running':
            raise HarnessError('the HellForge session runner pid %d did not end; finish again once it has'
                               % manifest['runner_pid'])
    with _finish_lock(paths):
        return _finish(paths, note)


def _finish(paths, note):
    state = State(paths.state)
    manifest = state.load()
    if manifest is None:
        return None
    run_dir = Path(manifest['run'])
    run_dir.mkdir(parents=True, exist_ok=True)
    logs, stale_logs = _copy_logs(paths.logs, run_dir, manifest.get('started_epoch'))
    Mailbox(paths.mailbox).archive_to(run_dir / 'agent')
    hellforge_summary = None
    if manifest.get('target') == 'hellforge':
        run = manifest.get('hellforge_run')
        outcome = hellforge.result(run)
        hellforge_summary = {'run_id': manifest.get('hellforge_run_id'), 'run': run,
                             'status': outcome.get('status'), 'failure': outcome.get('failure'),
                             'runner_end': manifest.get('runner_end'),
                             'files_collected': hellforge.collect(run, run_dir / 'hellforge-session')}
        if not IS_WINDOWS:
            # The wrapper and the runner outlive the game: result.json is the reliable exit code on Linux.
            hellforge_summary['game_exit_code'] = outcome.get('gameExitCode')
    before = manifest.get('dumps_before') or {}
    new_dumps = []
    for name, size in sorted(dumps.listing(paths.dumps).items()):
        if before.get(name) == size:
            continue
        (run_dir / 'dumps').mkdir(exist_ok=True)
        shutil.copy2(paths.dumps / name, run_dir / 'dumps' / name)
        if name.endswith('.dmp'):
            try:
                new_dumps.append(dumps.describe(paths.dumps / name))
            except (OSError, ValueError) as error:
                new_dumps.append({'file': name, 'kind': 'unreadable', 'meaning': str(error)})
        else:
            new_dumps.append({'file': name, 'kind': 'other'})
    mailbox = Mailbox(paths.mailbox)
    bye = mailbox.read('bye') if manifest.get('agent') else None
    problems = teardown(paths)
    new_saves = settings.new_save_files(paths, manifest['settings']) if manifest.get('settings') else []
    exit_info = manifest.get('exit') or {}
    if hellforge_summary and not IS_WINDOWS and exit_info.get('code') is None:
        if hellforge_summary.get('game_exit_code') is not None:
            exit_info = dict(exit_info, code=hellforge_summary['game_exit_code'])
    crashed = [d for d in new_dumps if d.get('kind') not in ('other',)]
    if crashed:
        outcome = 'crash'
    elif exit_info.get('how') == 'quit' and bye:
        outcome = 'clean-quit'
    elif exit_info.get('how') in ('close', 'kill', 'quit'):
        outcome = 'ended-by-harness-%s' % exit_info['how']
    else:
        outcome = 'exited'
    # Clean means: the ship was reached (the title screen without the agent), the game ended
    # through its own quit or close with exit code 0, no dump appeared and the install restored.
    reached = manifest.get('reached_ship') if manifest.get('agent') else manifest.get('reached_title')
    reasons = []
    if not reached:
        reasons.append('never reached the ship' if manifest.get('agent') else 'never reached the title screen')
    reasons += ['crash dump: %s %s' % (d.get('kind'), d.get('where') or d.get('file')) for d in crashed]
    if exit_info.get('how') not in ('quit', 'close') or exit_info.get('code') not in (0, None):
        reasons.append('ended by %s with exit code %s' % (exit_info.get('how'), exit_info.get('code')))
    if problems:
        reasons.append('install not restored')
    if manifest.get('in_mission'):
        reasons.append('ended inside a mission')
    if hellforge_summary and hellforge_summary['status'] != 'observed':
        reasons.append('HellForge session %s%s' % (hellforge_summary['status'] or 'without a result',
                                                   ': ' + hellforge_summary['failure'] if hellforge_summary['failure']
                                                   else ''))
    summary = {'session': manifest['session'], 'condition': manifest['condition'], 'preset': manifest['preset'],
               'started': manifest['started'], 'finished': _now(), 'pid': manifest.get('pid'),
               'exit': exit_info, 'outcome': outcome, 'clean': not reasons, 'not_clean_because': reasons,
               'reached_ship': manifest.get('reached_ship'), 'agent_bye': bye is not None, 'dumps': new_dumps,
               'logs': logs, 'stale_logs': stale_logs, 'restore_problems': problems, 'new_save_files': new_saves,
               'missions': manifest.get('missions') or [], 'shots': manifest.get('shots') or [],
               'crash_handlers_ended': manifest.get('crash_handlers_ended') or [], 'facts': manifest.get('facts'),
               'target': manifest.get('target') or 'steam', 'privacy': manifest.get('privacy') or 'invite'}
    if hellforge_summary:
        summary['hellforge'] = hellforge_summary
    (run_dir / 'run.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    if problems:
        kept = 'to resolve by hand' if (state.load() or {}).get('outside_redeploy') else 'for `recover`'
        note('restore problems (manifest kept %s): %s' % (kept, problems))
    else:
        state.clear()
    return summary


def recover(paths, note=print):
    """Finishes a session left behind by a harness that stopped part-way. After an outside redeploy
    of data/ (see _restore_data) it changes nothing there and says how to resolve it."""
    return finish(paths, note=note)


def watch(paths, note=print, poll=2.0):
    """Blocks until the session's game exits, records its exit code, then finishes the session."""
    state = State(paths.state)
    manifest = state.load()
    if manifest is None or not manifest.get('pid'):
        raise HarnessError('no launched session to watch')
    winapi.keep_awake(True)
    try:
        with winapi.Process(manifest['pid']) as process:
            while not process.wait(poll):
                pass
            current = state.load() or manifest
            if not current.get('exit'):
                how = 'quit' if current.get('quit_requested') else None
                state.update(exit={'code': process.exit_code(), 'how': how, 'at': _now()})
    finally:
        winapi.keep_awake(False)
    time.sleep(2)  # let the game's dump writer and log flushes finish
    return finish(paths, note=note)
