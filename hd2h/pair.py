"""Two HellForge installations on one PC, joined to each other without input (KB hellforge-multi-instance-plan).

Each installation runs a sandboxed session with the several-instances switches (HELLFORGE_MULTI_INSTANCE=1: its own
SteamID, the backends on a loopback link, the brokers keeping the game's own Steam lobby and Steam P2P between them):

1. Both installations get the agent and Bingus Shared Loader as packs in mods/ (as `start` adds them), their Mods
   settings backed up, and the agent's session.txt in <root>/userdata/environment, which a sandboxed session copies
   into its run folder (<run>/game/environment) as it starts. The agent then answers there.
2. The host's session starts and goes title -> ship through its agent. Its broker keeps its Steam lobby in the shared
   folder (<directory>/steam-lobbies/<lobby>.lobby, "owner <its SteamID>").
3. The joiner's session starts with "+connect_lobby <that lobby>" among the game's options and goes title -> ship: the
   game's own Steam deep link joins the host.
4. The host's backend says whether the friend got into its lobby and Party network, and how much Party traffic each
   side sends (a join that stalls shows the host sending almost nothing).

Everything changed is recorded in state/pair.json before it is made; `stop` ends both sessions, removes the packs and
puts the Mods settings back. Nothing is written into either game copy, and only games these runners started are touched.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from . import builds, hellforge, navigate, session
from .mailbox import Mailbox, MailboxError
from .paths import HARNESS, Paths

RECORD = HARNESS / 'state' / 'pair.json'
IDENTITIES = (76561199000000002, 76561199000000003)   # host, joiner
LINK_PORTS = (47002, 47003)
DEFAULT_DIRECTORY = Path(r'C:\HellForge-mi-directory')
CREATE_NO_WINDOW = 0x08000000


class PairError(RuntimeError):
    pass


def _record(update=None):
    data = json.loads(RECORD.read_text(encoding='utf-8')) if RECORD.exists() else {}
    if update:
        data.update(update)
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        RECORD.write_text(json.dumps(data, indent=2), encoding='utf-8')
    return data


def _instance(record, role, **facts):
    data = record.get(role) or {}
    data.update(facts)
    return _record({role: data})


def _environment(root):
    return Path(root) / 'userdata' / 'environment' / 'Local' / 'CowboyBingus' / 'Helldivers2' / 'TestAgent'


def _sandbox_mailbox(run):
    return Mailbox(Path(run) / 'game' / 'environment' / 'Local' / 'CowboyBingus' / 'Helldivers2' / 'TestAgent')


def prepare(paths, role, note):
    """The agent and loader packs, the Mods settings backup, and the agent's session for one installation."""
    record = _record()
    backup = hellforge.backup_mods_settings(paths, RECORD.parent / ('pair-settings-' + role))
    record = _instance(record, role, root=str(paths.hellforge), mods_settings=backup)
    packs = hellforge.pack_files(paths, paths.agent_source.read_bytes())
    hellforge.install_packs(paths, packs, lambda hashes: _instance(_record(), role, packs=hashes))
    session_id = '%s-%s' % (role, uuid.uuid4().hex[:8])
    Mailbox(_environment(paths.hellforge)).begin(session_id)
    _instance(_record(), role, session=session_id)
    note('%s: %s prepared (packs %s, agent session %s)' % (role, paths.hellforge, ', '.join(sorted(packs)), session_id))
    return session_id


def _hex(value):
    return '%X' % value


def _machine_module_path():
    """The machine's own PSModulePath (Windows PowerShell 5.1's modules), as a fresh session would inherit it."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment') as key:
        return os.path.expandvars(winreg.QueryValueEx(key, 'PSModulePath')[0])


def start(paths, role, directory, game_options, note, timeout=420):
    """Starts one installation's sandboxed session with the several-instances switches; returns (game pid, run)."""
    me, other = (0, 1) if role == 'host' else (1, 0)
    arguments = hellforge.session_arguments(paths, multiplayer=True) + ['--sandbox', '--game-options', game_options]
    environment = dict(os.environ)
    environment.update({
        'HELLFORGE_MULTI_INSTANCE': '1',
        'HELLFORGE_IDENTITY': str(IDENTITIES[me]),
        'HELLFORGE_PEER_LINK': 'udp:%s:%d:%s=%d' % (_hex(IDENTITIES[me]), LINK_PORTS[me], _hex(IDENTITIES[other]), LINK_PORTS[other]),
        'HELLFORGE_DIRECTORY_FOLDER': str(directory),
        # the runner's own checks run Windows PowerShell 5.1; an inherited PowerShell 7 module path breaks them
        'PSModulePath': os.path.expandvars(r'%USERPROFILE%\Documents\WindowsPowerShell\Modules;') + _machine_module_path(),
    })
    temp = Path(paths.hellforge) / 'temp'
    temp.mkdir(exist_ok=True)
    environment['TEMP'] = environment['TMP'] = str(temp)
    before, _ = hellforge.current_run(paths)
    runner = subprocess.Popen(arguments, cwd=str(paths.hellforge), env=environment, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW).pid
    _instance(_record(), role, runner=runner)
    note('%s: session runner started (pid %d, options %s)' % (role, runner, game_options))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run_id, run = hellforge.current_run(paths)
        fresh = run_id is not None and run_id != before
        result = hellforge.result(run) if fresh else {}
        games = hellforge.copy_games(paths)
        if fresh and games and result.get('status') == 'observing':
            _instance(_record(), role, run=str(run), game=games[0])
            note('%s: game %d running (run %s)' % (role, games[0], run_id))
            return games[0], Path(run)
        if result.get('status') == 'failed':
            raise PairError('%s: the session failed: %s' % (role, result.get('failure')))
        if runner not in hellforge.runners():
            raise PairError('%s: the session runner exited before the game started' % role)
        time.sleep(2)
    raise PairError('%s: the game did not start within %d s' % (role, timeout))


def to_ship(pid, build, mailbox, role, note, press_timeout=240, ship_timeout=300):
    """Presses continue on the title screen through the agent and waits for the ship (state 3)."""
    deadline, last = time.monotonic() + press_timeout, None
    while time.monotonic() < deadline:
        if pid not in session.running():
            raise PairError('%s: the game exited on the title screen' % role)
        reads = session.live_reads(pid, build)
        if reads.get('state') == 3:
            return reads
        base = session.game_dll_base(pid, build)
        if base:
            try:
                values = mailbox.call(navigate.continue_source(build, base), timeout=20)
            except MailboxError as error:
                values = [False, str(error)]
            if values and values[0] is True:
                note('%s: title screen: %s' % (role, values[1]))
                break
            if values != last:
                note('%s: title screen: %s' % (role, values[1] if len(values) > 1 else values))
                last = values
        time.sleep(2)
    else:
        raise PairError('%s: could not press continue within %d s' % (role, press_timeout))
    deadline, seen = time.monotonic() + ship_timeout, None
    while time.monotonic() < deadline:
        if pid not in session.running():
            raise PairError('%s: the game exited on the way to the ship' % role)
        reads = session.live_reads(pid, build)
        if reads.get('state_name') != seen:
            seen = reads.get('state_name')
            note('%s: game state %s' % (role, seen))
        if reads.get('state') == 3:
            return reads
        time.sleep(1)
    raise PairError('%s: the game did not reach the ship within %d s' % (role, ship_timeout))


def host_lobby(directory, timeout=120):
    """The host's own Steam lobby id (decimal), from the shared folder its broker keeps it in."""
    owner = 'owner %016X' % IDENTITIES[0]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for file in (Path(directory) / 'steam-lobbies').glob('*.lobby'):
            try:
                if owner in file.read_text(encoding='utf-8', errors='replace').splitlines():
                    return int(file.stem, 16)
            except (OSError, ValueError):
                pass
        time.sleep(1)
    raise PairError('the host made no Steam lobby within %d s' % timeout)


def _log(run):
    try:
        return (Path(run) / 'backend' / 'multiplayer.log').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def watch_join(host_run, timeout=180, note=print):
    """The host backend's view of the join: in its lobby, in its Party network, and the traffic each side sends.
    Returns a verdict once the host has sent at least 100 Party messages to the friend, or at the timeout."""
    deadline, verdict = time.monotonic() + timeout, {}
    while time.monotonic() < deadline:
        text = _log(host_run)
        verdict = {
            'lobby': 'A friend joined lobby' in text,
            'party': 'A friend joined Party network' in text,
            'left': 'A friend left Party network' in text,
        }
        counts = re.findall(r'messages so far: (\d+) from friend, (\d+) from host', text)
        if counts:
            verdict['from_friend'], verdict['from_host'] = int(counts[-1][0]), int(counts[-1][1])
        if verdict.get('from_host', 0) >= 100 or verdict['left']:
            break
        time.sleep(5)
    verdict['joined'] = verdict.get('party', False) and not verdict['left'] and verdict.get('from_host', 0) >= 100
    note('join: %s' % json.dumps(verdict))
    return verdict


def run(host_root, joiner_root, directory=DEFAULT_DIRECTORY, options='--toaster_mode', note=print):
    """The whole pairing; returns the join's verdict. Leaves both sessions running for a look (stop ends them)."""
    if _record().get('host') or _record().get('joiner'):
        raise PairError('a pairing is still recorded in %s: run `pair.py stop` first' % RECORD)
    if hellforge.steam_pid_mismatch():
        raise PairError('this process reads a stale Steam pid (see hellforge.steam_pid_mismatch)')
    host, joiner = Paths.for_hellforge(host_root, HARNESS), Paths.for_hellforge(joiner_root, HARNESS)
    for paths in (host, joiner):
        if hellforge.copy_games(paths) or any(str(paths.hellforge).lower() in str(p).lower() for p in hellforge.runners()):
            raise PairError('%s already runs a game' % paths.hellforge)
    build = builds.KNOWN.get(hellforge.facts(host).get('hellforge_build') or '')
    if build is None:
        raise PairError('unknown game build: no navigation offsets')
    (Path(directory) / 'steam-lobbies').mkdir(parents=True, exist_ok=True)
    for stale in (Path(directory) / 'steam-lobbies').glob('*.lobby'):
        stale.unlink()
    prepare(host, 'host', note)
    prepare(joiner, 'joiner', note)
    host_pid, host_run = start(host, 'host', directory, options, note)
    to_ship(host_pid, build, _sandbox_mailbox(host_run), 'host', note)
    lobby = host_lobby(directory)
    note('host: its Steam lobby is %d' % lobby)
    _record({'lobby': lobby})
    joiner_pid, joiner_run = start(joiner, 'joiner', directory, '%s +connect_lobby %d' % (options, lobby), note)
    to_ship(joiner_pid, build, _sandbox_mailbox(joiner_run), 'joiner', note)
    verdict = watch_join(host_run, note=note)
    _record({'verdict': verdict})
    return verdict


def stop(note=print, wait=120):
    """Ends both sessions (their runners' stop signal), removes the packs, puts the Mods settings back and ends the
    agent sessions. Returns the problems left."""
    record, problems = _record(), []
    for role in ('joiner', 'host'):
        data = record.get(role)
        if not data:
            continue
        if data.get('run') and data.get('runner') in hellforge.runners():
            hellforge.signal_stop(data['run'])
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and data['runner'] in hellforge.runners():
                time.sleep(2)
            if data['runner'] in hellforge.runners():
                problems.append('%s: the session runner %d did not end' % (role, data['runner']))
                continue
            note('%s: session ended (%s)' % (role, hellforge.result(data['run']).get('status')))
        paths = Paths.for_hellforge(data['root'], HARNESS)
        problems += hellforge.remove_packs(paths, data.get('packs') or {})
        if data.get('mods_settings'):
            problems += hellforge.restore_mods_settings(data['mods_settings'])
        Mailbox(_environment(paths.hellforge)).end()
    if not problems and RECORD.exists():
        RECORD.unlink()
    for problem in problems:
        note('problem: ' + problem)
    return problems
