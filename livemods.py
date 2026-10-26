"""Live mod changes in a running HellForge session (hd2h/livemods.py).

  python livemods.py setup --hellforge-root C:\\HellForge-2 [--copy FOLDER]... [--on ID]... [--off ID]...
                                     before `harness.py start`: copy test mods in, set the next session's switches
  python livemods.py change [--on ID]... [--off ID]... [--order ID,ID] [--preview] [--requests N]
                                     switch mods in the running session (POST /hellforge/live-mods; --preview: GET)
  python livemods.py scenario FILE.json
                                     a list of changes with expected answers, one verdict per step
  python livemods.py teardown --hellforge-root C:\\HellForge-2
                                     after `harness.py stop`: remove the copies, put the Mods settings back
  python livemods.py pair-sync --host C:\\HellForge-3 --joiner C:\\HellForge-2
                                     mod sync on one PC: the joiner (other mods; `setup` both first) joins the host,
                                     downloads its mods, takes them live (APPLY AND JOIN) and is invited back;
                                     `pair.py stop` ends both sessions

change and scenario follow the session `harness.py start --target hellforge` recorded. Results go to the harness run's
live-mods/ folder.
"""
import argparse
import json
from pathlib import Path
import sys
import time

from hd2h import hellforge, livemods, session
from hd2h.paths import HARNESS, Paths
from hd2h.state import State


def recorded_session():
    paths = Paths.from_environment('steam')
    manifest = State(paths.state).load()
    if not manifest or manifest.get('target') != 'hellforge' or not manifest.get('pid'):
        raise livemods.LiveModsError('no HellForge session is recorded (harness.py --target hellforge start)')
    return Paths.for_hellforge(manifest['hellforge_root'], HARNESS, manifest.get('profile')), manifest


def output(manifest, name):
    folder = Path(manifest['run']) / 'live-mods'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / ('%s-%s.json' % (name, time.strftime('%H%M%S')))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    setup = sub.add_parser('setup')
    setup.add_argument('--hellforge-root', required=True)
    setup.add_argument('--copy', action='append', default=[], metavar='FOLDER', help='a mod folder to copy into mods/')
    setup.add_argument('--on', action='append', default=[], metavar='ID', help='a copied mod to leave on')
    setup.add_argument('--off', action='append', default=[], metavar='ID', help="an installation's mod to switch off")
    teardown = sub.add_parser('teardown')
    teardown.add_argument('--hellforge-root', required=True)
    change = sub.add_parser('change')
    change.add_argument('--on', action='append', default=[], metavar='ID')
    change.add_argument('--off', action='append', default=[], metavar='ID')
    change.add_argument('--order', help='comma-separated mod ids: the Mods page order')
    change.add_argument('--preview', action='store_true', help='GET: what the change would do, without making it')
    change.add_argument('--requests', type=int, default=1, help='send this many POSTs at once')
    run = sub.add_parser('scenario')
    run.add_argument('file')
    paired = sub.add_parser('pair-sync')
    paired.add_argument('--host', required=True)
    paired.add_argument('--joiner', required=True)
    paired.add_argument('--options', default='--toaster_mode', help="the game's launch options for both")
    args = parser.parse_args(argv)
    note = lambda text: print('[livemods] ' + text, file=sys.stderr, flush=True)
    try:
        if args.command == 'setup':
            paths = Paths.for_hellforge(args.hellforge_root, HARNESS)
            print(json.dumps(livemods.setup(paths, args.copy, args.on, args.off), indent=2))
        elif args.command == 'teardown':
            problems = livemods.teardown(Paths.for_hellforge(args.hellforge_root, HARNESS))
            print(json.dumps({'problems': problems}, indent=2))
            return 1 if problems else 0
        elif args.command == 'change':
            paths, manifest = recorded_session()
            order = [o.strip() for o in args.order.split(',') if o.strip()] if args.order else None
            outcome = livemods.change(paths, manifest, args.on, args.off, order, args.preview, requests=args.requests)
            output(manifest, 'change').write_text(json.dumps(outcome, indent=2), encoding='utf-8')
            print(json.dumps(outcome, indent=2))
        elif args.command == 'scenario':
            paths, manifest = recorded_session()
            steps = json.loads(Path(args.file).read_text(encoding='utf-8'))
            steps = steps.get('steps', steps) if isinstance(steps, dict) else steps
            verdict = livemods.scenario(paths, manifest, steps, note, output(manifest, Path(args.file).stem))
            print(json.dumps({k: verdict[k] for k in ('passed', 'failed', 'steps', 'ran')}, indent=2))
            return 0 if verdict['failed'] == 0 and verdict['ran'] == verdict['steps'] else 1
        elif args.command == 'pair-sync':
            from hd2h import pair
            try:
                verdict = livemods.pair_sync(args.host, args.joiner, options=args.options, note=note)
            except pair.PairError as error:
                note('error: %s (pair.py stop undoes what was done)' % error)
                return 2
            print(json.dumps(verdict, indent=2, default=str))
            return 0 if verdict.get('live') and verdict.get('rejoined') else 1
    except (livemods.LiveModsError, hellforge.HellForgeError, session.HarnessError) as error:
        note('error: %s' % error)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
