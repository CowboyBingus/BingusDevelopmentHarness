"""Two HellForge installations on one PC, joined to each other without input (hd2h/pair.py).

  python pair.py run --host C:\\HellForge-2 --joiner C:\\HellForge-3 [--directory C:\\HellForge-mi-directory] [--options "--toaster_mode"]
  python pair.py map --host C:\\HellForge-2 --joiner C:\\HellForge-3 [--planet N] [--seconds 60]
  python pair.py stop

Both installations need an engine built from HellForge's feature/multi-instance branch (the several-instances
switches, the brokers' local Steam lobby and P2P) and their own network block (one session from each first).
"""
import argparse
import json
import sys

from hd2h import capture, pair, winapi


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--host', required=True)
    run.add_argument('--joiner', required=True)
    run.add_argument('--directory', default=str(pair.DEFAULT_DIRECTORY))
    run.add_argument('--options', default='--toaster_mode', help="the game's launch options for both")
    shown = sub.add_parser('map', help="the Galactic Map test: the joiner does not join; it opens its map on the "
                                       "host's planet and captures its window after every lobby scan")
    shown.add_argument('--host', required=True)
    shown.add_argument('--joiner', required=True)
    shown.add_argument('--directory', default=str(pair.DEFAULT_DIRECTORY))
    shown.add_argument('--options', default='--toaster_mode')
    shown.add_argument('--planet', type=int, help="a campaign planet (default: the host's ship or first campaign planet)")
    shown.add_argument('--seconds', type=float, default=60, help="how long the joiner's scanner is watched")
    sub.add_parser('stop')
    args = parser.parse_args()
    try:
        if args.command == 'run':
            verdict = pair.run(args.host, args.joiner, args.directory, args.options)
            print(json.dumps(verdict, indent=2))
            return 0 if verdict.get('joined') else 1
        if args.command == 'map':
            verdict = pair.run_map(args.host, args.joiner, args.directory, args.options, args.planet, args.seconds)
            print(json.dumps(verdict, indent=2))
            return 0 if verdict.get('found') else 1
        return 1 if pair.stop() else 0
    except (pair.PairError, pair.hellforge.HellForgeError, capture.CaptureError, winapi.WinError) as error:
        print('pair: ' + str(error) + ' (pair.py stop undoes what was done)', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
