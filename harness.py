"""BingusDevelopmentHarness: launch, drive, stop and test Helldivers 2 while it is not the active window.

  python harness.py preflight                  install facts (the game must be closed for this one)
  python harness.py close                      close a running game (WM_CLOSE, then terminate)
  python harness.py start [--preset light]     close any running game, prepare (Invite Only, agent), launch
  python harness.py lua "return agent.state()" run Lua inside the game (the agent's helpers are `agent`)
  python harness.py state                      agent state plus live game state, mission and privacy
  python harness.py ship                       press continue on the title screen, wait for the ship
  python harness.py mission [--difficulty N] [--planet ID | --faction F [--pick N]] [--shots]
                                               ship -> a solo mission until the player has control
                                               (real account traffic: Operation/Create, Mission/Start)
  python harness.py leave                      mission -> ship through the host abandon (abandon report)
  python harness.py map [--planet ID | --faction F [--pick N]] [--watch S]
                                               open the Galactic Map's planet view and watch its lobby scans
                                               (no closing yet: open it last, then stop)
  python harness.py shot [name]                capture the game window into the run's shots/, scored
  python harness.py stop [--kill]              (leave a mission,) quit through the engine (or kill), collect, restore
  python harness.py watch                      block until the game exits, then collect and restore
  python harness.py finish | recover           collect and restore after the game has exited
  python harness.py smoke [--ship] [--mission] launch -> settle -> snapshot (on the ship; --mission: into a
                                               mission and back) -> quit -> verify
  python harness.py soak --conditions mods,loader-only --where ship --minutes 30 --cycles 8
                                               ABBA-alternated background sessions; crashes per hour
  python harness.py classify <dump.dmp>...     name known crash signatures
  python harness.py build-agent <folder>       write the agent patch files without deploying them

Targets: --target steam (the default; or HD2H_TARGET) drives the Steam install; --target hellforge drives a
HellForge installation's game copy (--hellforge-root, default HD2H_HELLFORGE_ROOT or the platform's installation;
--profile, default the launcher's selected profile): its session runner launches the copy, and the agent and Bingus
Shared Loader go into the installation's mods/ as packs (hd2h/hellforge.py). Commands after start follow the recorded
session's target. On Linux only the hellforge target runs; see README.md.

start, smoke and soak close a running game first (gracefully, then by force) unless given
--keep-running. Files are only changed while the game is closed. Every change is recorded in
state/active.json before it is made and undone by finish/recover, verified by SHA-256 (and, for
deployed files, file identity). If data/ was redeployed from outside meanwhile, recover changes
nothing there and says how to resolve it. Results go to runs/. See README.md.
"""
import argparse
import json
from pathlib import Path
import sys

from hd2h import archive, capture, deploy, dumps, galaxy, hellforge, mission, navigate, scenarios, session, settings, winapi
from hd2h.mailbox import Mailbox, MailboxError
from hd2h.paths import TARGETS, Paths
from hd2h.state import State

# Commands that start sessions of their own: they take the asked-for target and refuse while a session of the other
# target is recorded.
STARTING = ('start', 'smoke', 'soak')
MULTIPLAYER = {'launcher': None, 'on': True, 'off': False}


def emit(value):
    print(json.dumps(value, indent=2, default=str))


def command_lua(paths, args):
    source = Path(args.file).read_text(encoding='utf-8') if args.file else args.code
    if not source:
        raise SystemExit('give Lua code or --file')
    return Mailbox(paths.mailbox).send(source, timeout=args.timeout)


def command_state(paths, args):
    manifest = State(paths.state).load()
    result = {'session': manifest and {k: manifest.get(k) for k in ('session', 'condition', 'preset', 'pid',
                                                                       'started', 'run')},
              'running': session.running()}
    if manifest and manifest.get('agent'):
        mailbox = Mailbox(paths.mailbox)
        result['heartbeat_age_s'] = mailbox.age('heartbeat')
        try:
            result['agent'] = mailbox.call('return agent.state()', timeout=args.timeout)[0]
        except MailboxError as error:
            result['agent_error'] = str(error)
    if manifest and manifest.get('pid') in result['running']:
        from hd2h import builds
        result['live'] = session.live_reads(manifest['pid'], builds.KNOWN.get(manifest['facts'].get('build') or ''))
    return result


def command_build_agent(paths, args):
    target = Path(args.folder)
    target.mkdir(parents=True, exist_ok=True)
    files = {'%s.patch_0' % archive.FAMILY: archive.agent_archive(paths.agent_source.read_bytes())}
    name = next(iter(files))
    files[name + '.gpu_resources'] = b''
    files[name + '.stream'] = b''
    for file_name, content in files.items():
        (target / file_name).write_bytes(content)
    return {'folder': str(target), 'files': sorted(files), 'resource': archive.AGENT_NAME}


def launched(paths):
    manifest = State(paths.state).load()
    if manifest is None or not manifest.get('pid'):
        raise session.HarnessError('no launched session')
    return manifest


def command_mission(paths, args, note):
    shoot = None
    if args.shots:
        def shoot(name):
            try:
                return capture.run_shot(paths, name)
            except (capture.CaptureError, winapi.WinError, OSError) as error:
                note('capture %s failed: %s' % (name, error))
                return None
    if args.pick is not None and not args.faction:
        raise mission.MissionError('--pick needs --faction')
    return mission.deploy(paths, launched(paths), difficulty=args.difficulty, planet=args.planet,
                          faction=args.faction, note=note, shoot=shoot, pick=args.pick, mission_pick=args.mission_pick,
                          region_tag=args.region_tag)


def add_mission_arguments(parser):
    parser.add_argument('--difficulty', type=int, choices=range(1, 11), default=1, metavar='1-10',
                        help='difficulty of a new operation (an active operation is continued as it is)')
    where = parser.add_mutually_exclusive_group()
    where.add_argument('--planet', type=int, help='planet index (must have a campaign)')
    where.add_argument('--faction', choices=sorted(mission.FACTIONS), help='first campaign planet of this faction')
    parser.add_argument('--pick', type=int, metavar='N',
                        help='with --faction: its campaign planet number N (from 0, wrapping) instead of the first')
    parser.add_argument('--mission-pick', type=int, metavar='N',
                        help="the planet's open mission number N at the difficulty (from 0, wrapping) instead of the "
                             'first: missions lie in different regions (city maps only in some)')
    parser.add_argument('--region-tag', type=int, metavar='T',
                        help="only missions whose definition carries region tag T (the map generator's region filter; "
                             'Automaton megacity operations: 3)')


def resolve_paths(args):
    """The paths for the asked-for target. Without --target a recorded session's target is followed, so `lua`,
    `ship` and `stop` reach the session `start` began."""
    paths = Paths.from_environment(args.target, args.hellforge_root, args.profile)
    manifest = State(paths.state).load()
    recorded = (manifest or {}).get('target') or 'steam'
    if manifest is None or recorded == paths.target:
        return paths
    if args.target is None and args.command not in STARTING:
        if recorded == 'hellforge':
            return Paths.for_hellforge(manifest['hellforge_root'], paths.harness, manifest.get('profile'))
        return Paths.from_environment('steam')
    raise session.HarnessError('a %s session is recorded in state/active.json; `stop`, `finish` or `recover` it '
                               '(they follow its target) before a %s command' % (recorded, paths.target))


def add_hellforge_arguments(parser):
    parser.add_argument('--privacy', choices=sorted(settings.PRIVACY), default='invite',
                        help='lobby privacy: invite (always on Steam) or public (HellForge only, with Local PlayFab and '
                             'the multiplayer stand-ins on)')
    parser.add_argument('--multiplayer', choices=sorted(MULTIPLAYER), default='launcher',
                        help="HellForge target: Local PlayFab and the multiplayer stand-ins as the launcher has them, "
                             'or both on or off')
    parser.add_argument('--war', choices=('launcher', 'live', 'open', 'frozen'), default='launcher',
                        help="HellForge target: the galactic war the session serves (default: the launcher's)")


def report_session(summary, note):
    """Prints the session summary; exit code 1 unless the session was clean (see session._finish)."""
    emit(summary)
    if summary and not summary.get('clean'):
        note('NOT CLEAN: ' + '; '.join(summary.get('not_clean_because') or ['unknown']))
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', choices=TARGETS,
                        help='steam or hellforge (default: HD2H_TARGET, else steam; later commands follow the '
                             'recorded session)')
    parser.add_argument('--hellforge-root', help='HellForge installation (default: HD2H_HELLFORGE_ROOT or the '
                                                "platform's installation)")
    parser.add_argument('--profile', help="HellForge profile (default: HD2H_HELLFORGE_PROFILE or the launcher's)")
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('preflight')
    sub.add_parser('close')
    start = sub.add_parser('start')
    start.add_argument('--keep-running', action='store_true', help='refuse instead of closing a running game')
    start.add_argument('--preset', choices=sorted(settings.PRESETS), default='private')
    start.add_argument('--package', action='append', default=[], metavar='ZIP',
                       help='with --condition packages: a manager ZIP to deploy instead of the install (repeat); '
                            'every option by default, or ZIP::Option A|Option B')
    start.add_argument('--condition', choices=session.CONDITIONS, default='mods',
                       help='mods; loader-only (only the loader patch + agent); vanilla (no patches, no agent)')
    add_hellforge_arguments(start)
    lua = sub.add_parser('lua')
    lua.add_argument('code', nargs='?')
    lua.add_argument('--file')
    lua.add_argument('--timeout', type=float, default=15)
    state = sub.add_parser('state')
    state.add_argument('--timeout', type=float, default=15)
    stop = sub.add_parser('stop')
    stop.add_argument('--kill', action='store_true', help='skip the graceful quit')
    sub.add_parser('watch')
    sub.add_parser('ship', help='press continue on the title screen and wait for the ship')
    deploy_parser = sub.add_parser('mission', help='ship -> a solo mission until the player has control')
    add_mission_arguments(deploy_parser)
    deploy_parser.add_argument('--shots', action='store_true', help='capture the briefing, landing and ship')
    sub.add_parser('leave', help='mission -> ship through the host abandon')
    map_parser = sub.add_parser('map', help="open the Galactic Map's planet view (its lobby scanner); open it last, "
                                            'closing is not supported: stop quits from it')
    where = map_parser.add_mutually_exclusive_group()
    where.add_argument('--planet', type=int, help='planet index (must have a campaign)')
    where.add_argument('--faction', choices=sorted(mission.FACTIONS), help='first campaign planet of this faction')
    map_parser.add_argument('--pick', type=int, metavar='N', help='with --faction: its campaign planet number N')
    map_parser.add_argument('--watch', type=float, default=0, metavar='S',
                            help='then watch the lobby scanner for S seconds (a scan every recharge)')
    shot = sub.add_parser('shot', help="capture the game window into the run's shots/")
    shot.add_argument('name', nargs='?', default='shot')
    sub.add_parser('finish')
    sub.add_parser('recover')
    smoke = sub.add_parser('smoke')
    smoke.add_argument('--preset', choices=sorted(settings.PRESETS), default='light')
    smoke.add_argument('--settle', type=int, default=20)
    smoke.add_argument('--keep-running', action='store_true')
    smoke.add_argument('--ship', action='store_true', help='(always on) navigate to the ship and check it there')
    smoke.add_argument('--mission', action='store_true',
                       help='then deploy into a mission, check it there and come back (real account traffic)')
    add_mission_arguments(smoke)
    smoke.add_argument('--package', action='append', default=[], metavar='ZIP',
                       help='deploy these manager ZIPs instead of the installed patches (repeat; order = priority); '
                            'every option by default, or ZIP::Option A|Option B')
    add_hellforge_arguments(smoke)
    soak = sub.add_parser('soak')
    soak.add_argument('--conditions', required=True,
                      help='comma list of mods, loader-only, vanilla; two conditions alternate ABBA')
    soak.add_argument('--where', choices=('title', 'ship'), default='title',
                      help='ship: navigate each cycle with an agent to the ship before timing')
    soak.add_argument('--minutes', type=float, default=30)
    soak.add_argument('--cycles', type=int, default=4)
    soak.add_argument('--preset', choices=sorted(settings.PRESETS), default='private')
    soak.add_argument('--end', choices=('graceful', 'kill'), default='graceful',
                      help='graceful: engine quit (mods) or WM_CLOSE (vanilla), forcing only as a fallback')
    soak.add_argument('--keep-running', action='store_true')
    classify = sub.add_parser('classify')
    classify.add_argument('files', nargs='+')
    build = sub.add_parser('build-agent')
    build.add_argument('folder')
    args = parser.parse_args(argv)

    note = lambda text: print('[harness] ' + text, file=sys.stderr, flush=True)
    try:
        paths = resolve_paths(args)
        if args.command == 'preflight':
            emit(session.preflight(paths))
        elif args.command == 'close':
            emit(session.close_running(note))
        elif args.command == 'start':
            manifest = session.start(paths, preset=args.preset, condition=args.condition,
                                     agent=args.condition != 'vanilla', note=note, take_over=not args.keep_running,
                                     packages=tuple(args.package), multiplayer=MULTIPLAYER[args.multiplayer],
                                     war=None if args.war == 'launcher' else args.war, privacy=args.privacy)
            emit({k: manifest.get(k) for k in ('session', 'pid', 'condition', 'preset', 'run')})
        elif args.command == 'lua':
            emit(command_lua(paths, args))
        elif args.command == 'ship':
            emit(navigate.to_ship(paths, launched(paths), note=note))
        elif args.command == 'mission':
            emit(command_mission(paths, args, note))
        elif args.command == 'map':
            if args.pick is not None and not args.faction:
                raise mission.MissionError('--pick needs --faction')
            emit(galaxy.planet_view(paths, launched(paths), planet=args.planet, faction=args.faction, pick=args.pick,
                                    watch=args.watch, note=note))
        elif args.command == 'leave':
            emit(mission.leave(paths, launched(paths), note=note))
        elif args.command == 'shot':
            launched(paths)
            emit(capture.run_shot(paths, args.name))
        elif args.command == 'state':
            emit(command_state(paths, args))
        elif args.command == 'stop':
            return report_session(session.stop(paths, kill=args.kill, note=note), note)
        elif args.command == 'watch':
            return report_session(session.watch(paths, note=note), note)
        elif args.command in ('finish', 'recover'):
            return report_session(session.finish(paths, note=note), note)
        elif args.command == 'smoke':
            verdict = scenarios.smoke(paths, preset=args.preset, settle=args.settle, note=note,
                                      take_over=not args.keep_running, ship=True,
                                      packages=tuple(args.package), mission_run=args.mission,
                                      difficulty=args.difficulty, planet=args.planet, faction=args.faction,
                                      multiplayer=MULTIPLAYER[args.multiplayer],
                                      war=None if args.war == 'launcher' else args.war, privacy=args.privacy)
            emit(verdict)
            return 0 if verdict['passed'] else 1
        elif args.command == 'soak':
            conditions = tuple(c.strip() for c in args.conditions.split(',') if c.strip())
            unknown = [c for c in conditions if c not in session.CONDITIONS]
            if unknown or not conditions:
                raise session.HarnessError('unknown conditions %s (choose from %s)' % (unknown, session.CONDITIONS))
            emit(scenarios.soak(paths, conditions=conditions, minutes=args.minutes, cycles=args.cycles,
                                preset=args.preset, end=args.end, note=note, take_over=not args.keep_running,
                                where=args.where))
        elif args.command == 'classify':
            emit([dumps.describe(path) for path in args.files])
        elif args.command == 'build-agent':
            emit(command_build_agent(paths, args))
    except (session.HarnessError, MailboxError, deploy.DeployError, settings.SettingsError,
            winapi.WinError, navigate.NavigationError, mission.MissionError, capture.CaptureError,
            hellforge.HellForgeError) as error:
        note('error: %s' % error)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
