"""Two HellForge installations on one PC, joined to each other without input.

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
import sys
import time
import uuid

from . import builds, capture, galaxy, hellforge, navigate, session
from .mailbox import Mailbox, MailboxError
from .paths import HARNESS, Paths

RECORD = HARNESS / 'state' / 'pair.json'
IDENTITIES = (76561199000000002, 76561199000000003)   # host, joiner
LINK_PORTS = (47002, 47003)
DEFAULT_DIRECTORY = (Path(r'C:\HellForge-mi-directory') if sys.platform == 'win32' else
                     Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'hellforge-mi-directory')
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


def _sha(data):
    import hashlib
    return hashlib.sha256(data).hexdigest().upper()


def set_window(paths, role, size, note):
    """Sets the installation's game window and render size (user_settings.config) to size, for captures the marker can be
    told apart in: the instances otherwise draw at 427x240 inside a 1280x720 window. The file is backed up first and put
    back by `stop`. The render size never exceeds the window (a larger upscaler input leaves the world gray, see
    settings.scale_render_resolution)."""
    from . import settings
    path = Path(paths.settings_file)
    data = path.read_bytes()
    backup = RECORD.parent / ('pair-window-' + role) / 'user_settings.config.backup'
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(data)
    if _sha(backup.read_bytes()) != _sha(data):
        raise PairError('backup of %s did not verify' % path)
    _instance(_record(), role, window={'path': str(path), 'backup': str(backup), 'original_sha256': _sha(data)})
    text = settings.set_resolution(data.decode('utf-8'), *size)
    if len(settings.RENDER_RESOLUTION.findall(text)) != 1:
        raise PairError('render_resolution block not found once in %s' % path)
    text = settings.RENDER_RESOLUTION.sub(lambda m: '\trender_resolution = [%s\t\t%d%s\t\t%d%s\t]' % (
        m.group(1), size[0], m.group(1), size[1], m.group(1)), text)
    path.write_bytes(text.encode('utf-8'))
    note('%s: window and render size %dx%d (settings backed up)' % (role, size[0], size[1]))


def restore_window(entry):
    """Puts user_settings.config back as set_window found it. Returns problems."""
    if not entry:
        return []
    path, backup = Path(entry['path']), Path(entry['backup'])
    if path.exists() and _sha(path.read_bytes()) == entry['original_sha256']:
        backup.unlink(missing_ok=True)
        return []
    if not backup.exists() or _sha(backup.read_bytes()) != entry['original_sha256']:
        return ['%s: window backup missing or changed' % path]
    path.write_bytes(backup.read_bytes())
    backup.unlink()
    return []


def _hex(value):
    return '%X' % value


def _machine_module_path():
    """The machine's own PSModulePath (Windows PowerShell 5.1's modules), as a fresh session would inherit it."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment') as key:
        return os.path.expandvars(winreg.QueryValueEx(key, 'PSModulePath')[0])


def start(paths, role, directory, game_options, note, timeout=420, strangers=False):
    """Starts one installation's sandboxed session with the several-instances switches; returns (game pid, run).
    strangers: HELLFORGE_TEST_STRANGERS=1, the two are not friends (no presence; the lobby directory's stranger path)."""
    me, other = (0, 1) if role == 'host' else (1, 0)
    arguments = hellforge.session_arguments(paths, multiplayer=True)
    if sys.platform == 'win32':
        arguments.append('--sandbox')
    arguments += ['--game-options', game_options]
    switches = {
        'HELLFORGE_MULTI_INSTANCE': '1',
        'HELLFORGE_IDENTITY': str(IDENTITIES[me]),
        'HELLFORGE_PEER_LINK': 'udp:%s:%d:%s=%d' % (_hex(IDENTITIES[me]), LINK_PORTS[me], _hex(IDENTITIES[other]), LINK_PORTS[other]),
        'HELLFORGE_DIRECTORY_FOLDER': str(directory) if sys.platform == 'win32' else hellforge.to_wine_path(directory),
        'HELLFORGE_TEST_STRANGERS': '1' if strangers else '0',
    }
    if sys.platform == 'win32':
        environment = dict(os.environ)
        environment.update(switches)
        # the runner's own checks run Windows PowerShell 5.1; an inherited PowerShell 7 module path breaks them
        environment['PSModulePath'] = (os.path.expandvars(r'%USERPROFILE%\Documents\WindowsPowerShell\Modules;') +
                                       _machine_module_path())
        temp = Path(paths.hellforge) / 'temp'
        temp.mkdir(exist_ok=True)
        environment['TEMP'] = environment['TMP'] = str(temp)
        command = arguments
    else:
        environment = hellforge.linux_environment(paths, switches)
        command = hellforge.linux_command(arguments, paths)
    before, _ = hellforge.current_run(paths)
    if sys.platform == 'win32':
        runner = subprocess.Popen(command, cwd=str(paths.hellforge), env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=CREATE_NO_WINDOW).pid
    else:
        runner = subprocess.Popen(command, cwd=str(paths.hellforge), env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True).pid
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
        if hellforge.wait_runner(runner, 0):
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


def _begin(host_root, joiner_root, directory, options, note, strangers=False):
    """Checks, prepares both installations and brings the host to its ship. Returns (host, joiner, build, host pid,
    host run)."""
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
    host_pid, host_run = start(host, 'host', directory, options, note, strangers=strangers)
    to_ship(host_pid, build, _sandbox_mailbox(host_run), 'host', note)
    return host, joiner, build, host_pid, host_run


def run(host_root, joiner_root, directory=DEFAULT_DIRECTORY, options='--toaster_mode', note=print):
    """The whole pairing; returns the join's verdict. Leaves both sessions running for a look (stop ends them)."""
    _, joiner, build, _, host_run = _begin(host_root, joiner_root, directory, options, note)
    lobby = host_lobby(directory)
    note('host: its Steam lobby is %d' % lobby)
    _record({'lobby': lobby})
    joiner_pid, joiner_run = start(joiner, 'joiner', directory, '%s +connect_lobby %d' % (options, lobby), note)
    to_ship(joiner_pid, build, _sandbox_mailbox(joiner_run), 'joiner', note)
    verdict = watch_join(host_run, note=note)
    _record({'verdict': verdict})
    return verdict


def _deployer(pid, build, run, note):
    """The mission module's steps on one sandboxed game, through its run folder's mailbox. Returns (reader, deployer);
    the caller closes the reader."""
    from . import winapi
    from .mission import Deployer, Natives, Probe
    base = session.game_dll_base(pid, build)
    if not base:
        raise PairError('game.dll is not mapped yet')
    reader = winapi.Reader(pid)
    deployer = Deployer(Probe(reader, base, build.mission), Natives(_sandbox_mailbox(run), base, build.mission),
                        lambda: session.live_reads(pid, build).get('state'), note=note)
    return reader, deployer


def travel(deployer, planet, timeout=30.0, arrive=25.0):
    """Moves the ship to planet (None: the ship's or the first campaign planet) with the war board's commit and no
    operation, as a deploy travels. Returns the planet. The map's planet view (and so its lobby scanner) follows only
    the ship's planet when the map opens: focusing another planet on an open map leaves the view on the galaxy
    (live 2026-10-23, [game+0x3326AA0]+0x55F628 stayed -1)."""
    from .mission import choose_planet
    probe = deployer.probe
    planet = choose_planet(probe.board(), probe.campaign_planets(), planet=planet)
    if probe.board().get('request_ship_planet') == planet:
        return planet
    deployer.require(deployer.natives.board_select(planet, planet, -1, -1, 0), 'moving the ship to %s' % planet)
    deadline = time.monotonic() + timeout
    while probe.board().get('request_ship_planet') != planet:
        if time.monotonic() > deadline:
            raise PairError('the ship did not move to planet %s within %d s' % (planet, timeout))
        time.sleep(0.25)
    # The board says the new planet at once, but the trip takes 15-18 s; a map opened
    # during it stayed on the galaxy (live 2026-10-23). No arrival field is known: wait out the trip.
    deployer.note('the ship is travelling to planet %s; waiting %.0f s for it to arrive' % (planet, arrive))
    time.sleep(arrive)
    return planet


def open_map(pid, build, run, planet, role, note):
    """Moves the ship to planet, then opens the Galactic Map's planet view (its lobby scanner) there. Returns the
    planet."""
    reader, deployer = _deployer(pid, build, run, note)
    try:
        planet = travel(deployer, planet)
        opened = galaxy.open_planet_view(deployer, planet=planet)
    finally:
        reader.close()
    note('%s: Galactic Map open on planet %s (scanner %s)' % (role, opened['planet'], opened['scanner']))
    return opened['planet']


def pick_mission(pid, build, run, planet, difficulty, note):
    """Moves the host's ship to planet and selects an open mission there at difficulty, without opening its map. Its
    lobby advertises the difficulty only with a mission selected (number_key5 stays 0 otherwise, and the map's search
    asks for its own difficulty window), and opening the map clears a selected mission;
    a mission committed on an open map sent the host back to the title screen (live 2026-10-23). Returns the planet."""
    from .mission import pick_mission as first_open
    reader, deployer = _deployer(pid, build, run, note)
    try:
        planet = travel(deployer, planet)
        probe = deployer.probe
        op, record = first_open(probe.operations(), probe.mission_record, planet, difficulty)
        if not op:
            raise PairError('no open mission at D%s on planet %s' % (difficulty, planet))
        deployer.require(deployer.natives.board_select(planet, planet, op['index'], record['mission'], difficulty),
                         'selecting mission %s' % record['mission'])
    finally:
        reader.close()
    note('host: mission %s selected on planet %s at D%s' % (record['mission'], planet, difficulty))
    return planet


def host_listing(directory, planet, difficulty, timeout=90):
    """The host's directory listing once it advertises planet and difficulty (PlayFab keys number_key4 and
    number_key5)."""
    file = Path(directory) / ('%X.json' % IDENTITIES[0])
    deadline, listing = time.monotonic() + timeout, {}
    while time.monotonic() < deadline:
        try:
            listing = json.loads(file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            listing = {}
        if listing.get('number_key4') == str(planet) and listing.get('number_key5') == str(difficulty):
            return listing
        time.sleep(1)
    raise PairError('the host was not listed on planet %s at D%s within %d s (its listing: war %s, planet %s, '
                    'difficulty %s)' % (planet, difficulty, timeout, listing.get('number_key3'),
                                        listing.get('number_key4'), listing.get('number_key5')))


def watch_map(pid, build, run, seconds, note, settle=3.0, most=8):
    """Watches the searcher's lobby scanner and captures its window: once at the start and `settle` s after every
    scan (the map draws the lobbies it found once their records arrive). Returns the scans and the captures."""
    reader, deployer = _deployer(pid, build, run, note)
    shots, scans, last, due = [], [], None, [time.monotonic()]
    started, taken = time.monotonic(), len(list((Path(run) / 'shots').glob('*.png')))
    try:
        while time.monotonic() < started + seconds:
            state = galaxy.scanner(deployer.probe)
            if state and last and None not in (state['recharge'], last['recharge']) \
                    and state['recharge'] > last['recharge'] + 1:
                scans.append({'at_s': round(time.monotonic() - started, 1), 'results': state['results']})
                note('searcher: lobby scan %d: %d results' % (len(scans), state['results']))
                due.append(time.monotonic() + settle)
            last = state or last
            if due and time.monotonic() >= due[0] and len(shots) < most:
                due.pop(0)
                name = '%02d-map-scan%d.png' % (taken + len(shots) + 1, len(scans))
                shots.append(dict(capture.shoot(pid, Path(run) / 'shots' / name), scans=len(scans),
                                  results=(last or {}).get('results')))
                note('searcher: captured %s' % name)
            time.sleep(0.25)
    finally:
        reader.close()
    return {'scans': scans, 'shots': shots}


def searches(run):
    """The searcher backend's directory searches: (listed, shown) per search, from its multiplayer.log."""
    return [(int(listed), int(shown)) for listed, shown in re.findall(r'Directory: search .*?: (\d+) listed, (\d+) shown',
                                                                      _log(run))]


def run_map(host_root, searcher_root, directory=DEFAULT_DIRECTORY, options='--toaster_mode', planet=None, seconds=60,
            difficulty=1, searcher_options='', window=(1920, 1080), note=print):
    """The Galactic Map test: the host selects a mission on a planet (its lobby is then listed there with that
    difficulty); the searcher, which does not join, opens its map on the same planet and watches its scanner, with
    captures for a look at the lobby it shows. The two are strangers (HELLFORGE_TEST_STRANGERS): the map's planet scan
    leaves out friends' lobbies (the friend-lobby pass marks them seen and a planet scan does not score them; live
    2026-10-23, the scorer 0x133B550 kept the host's lobby only with the seen-sets empty), which the game lists under
    Social instead. Leaves both sessions running (stop ends them)."""
    _, searcher, build, host_pid, host_run = _begin(host_root, searcher_root, directory, options, note, strangers=True)
    planet = pick_mission(host_pid, build, host_run, planet, difficulty, note)
    listing = host_listing(directory, planet, difficulty)
    note('host: listed on planet %s at D%s (war %s, lobby %s)' % (planet, difficulty, listing.get('number_key3'),
                                                                  listing.get('hf.lobbyId')))
    if window:
        set_window(searcher, 'joiner', window, note)
    searcher_pid, searcher_run = start(searcher, 'joiner', directory, searcher_options, note, strangers=True)
    to_ship(searcher_pid, build, _sandbox_mailbox(searcher_run), 'joiner', note)
    open_map(searcher_pid, build, searcher_run, planet, 'searcher', note)
    watched = watch_map(searcher_pid, build, searcher_run, seconds, note)
    found = searches(searcher_run)
    verdict = {'planet': planet, 'scans': len(watched['scans']),
               'scan_results': max([s['results'] for s in watched['scans']] or [0]),
               'directory_searches': len(found), 'directory_shown': max([shown for _, shown in found] or [0]),
               'shots': [s['file'] for s in watched['shots']]}
    verdict['found'] = verdict['scan_results'] > 0 and verdict['directory_shown'] > 0
    note('map: %s' % json.dumps(verdict))
    _record({'map': verdict})
    return verdict


def _launch(deployer, load_timeout, attempts=20, wait=5.0):
    """The launch step, retried while the trip's cutscene plays ("ready refused: a cutscene is playing", live
    2026-10-24, right after the drop on a ship that had just travelled)."""
    from .mission import MissionError
    for attempt in range(attempts):
        try:
            return deployer.launch(load_timeout)
        except MissionError as error:
            if 'cutscene' not in str(error) or attempt == attempts - 1:
                raise
            time.sleep(wait)


def deploy_host(pid, build, run, planet, difficulty, note, load_timeout=300, control_timeout=180):
    """Deploys the host into a mission on planet at difficulty, until it has control: the map's "Find ongoing
    missions" draws a lobby only from its decoded live data (marker +0x610, 0x133E220), which a squad on its ship does
    not have (live 2026-10-24). Returns the mission's planet."""
    reader, deployer = _deployer(pid, build, run, note)
    try:
        planet = travel(deployer, planet)
        selected = deployer.select(difficulty, planet, None, None, None, None)
        note('host: mission %s selected on planet %s at D%s' % (selected.get('mission'), planet, difficulty))
        for name, step in (('briefing', deployer.brief_and_seat), ('drop', deployer.drop),
                           ('launch', lambda: _launch(deployer, load_timeout)),
                           ('control', lambda: deployer.wait_control(control_timeout))):
            step()
            note('host: %s done' % name)
    finally:
        reader.close()
    return planet


def run_ongoing(host_root, searcher_root, directory=DEFAULT_DIRECTORY, options='--toaster_mode', planet=None,
                seconds=25, difficulty=1, searcher_options='', window=(1920, 1080), note=print):
    """The map test with the host in a mission (an ongoing mission), not on its ship. The searcher opens its map on
    the same planet as in run_map. Leaves both sessions running (stop ends them)."""
    _, searcher, build, host_pid, host_run = _begin(host_root, searcher_root, directory, options, note, strangers=True)
    planet = deploy_host(host_pid, build, host_run, planet, difficulty, note)
    if window:
        set_window(searcher, 'joiner', window, note)
    searcher_pid, searcher_run = start(searcher, 'joiner', directory, searcher_options, note, strangers=True)
    to_ship(searcher_pid, build, _sandbox_mailbox(searcher_run), 'joiner', note)
    open_map(searcher_pid, build, searcher_run, planet, 'searcher', note)
    watched = watch_map(searcher_pid, build, searcher_run, seconds, note)
    found = searches(searcher_run)
    verdict = {'planet': planet, 'host_in_mission': True, 'scans': len(watched['scans']),
               'scan_results': max([s['results'] for s in watched['scans']] or [0]),
               'directory_shown': max([shown for _, shown in found] or [0]),
               'shots': [s['file'] for s in watched['shots']]}
    note('ongoing: %s' % json.dumps(verdict))
    _record({'ongoing': verdict})
    return verdict


def stop(note=print, wait=120):
    """Ends both sessions (their runners' stop signal), removes the packs, puts the Mods settings back and ends the
    agent sessions. Returns the problems left."""
    record, problems = _record(), []
    for role in ('joiner', 'host'):
        data = record.get(role)
        if not data:
            continue
        runner = data.get('runner')
        if data.get('run') and not hellforge.wait_runner(runner, 0):
            hellforge.signal_stop(data['run'])
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not hellforge.wait_runner(runner, 0):
                time.sleep(2)
            if not hellforge.wait_runner(runner, 0):
                problems.append('%s: the session runner %d did not end' % (role, runner))
                continue
            note('%s: session ended (%s)' % (role, hellforge.result(data['run']).get('status')))
        paths = Paths.for_hellforge(data['root'], HARNESS)
        problems += hellforge.remove_packs(paths, data.get('packs') or {})
        if data.get('mods_settings'):
            problems += hellforge.restore_mods_settings(data['mods_settings'])
        problems += restore_window(data.get('window'))
        Mailbox(_environment(paths.hellforge)).end()
    if not problems and RECORD.exists():
        RECORD.unlink()
    for problem in problems:
        note('problem: ' + problem)
    return problems
