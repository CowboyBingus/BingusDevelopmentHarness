"""Read-only look at the war board of the running harness session: the operations and mission records on one
planet, and where the planet's region hashes appear (ReadProcessMemory only; no mailbox, no game call).

    python tools/board_regions.py <planet> <region hash> [<region hash> ...]

Used to find which field ties a mission to a planet region (city, megacity), so `mission` can pick a city map.
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hd2h import builds, mission, session, winapi  # noqa: E402
from hd2h.paths import Paths  # noqa: E402
from hd2h.state import State  # noqa: E402

BOARD_SCAN = 0x200000  # bytes of the board searched for the region hashes


def main():
    planet = int(sys.argv[1])
    hashes = [int(h, 0) for h in sys.argv[2:]]
    paths = Paths.from_environment()
    manifest = State(paths.state).load() or {}
    build = builds.KNOWN.get((manifest.get('facts') or {}).get('build') or '')
    pid = manifest.get('pid')
    if not build or not pid or pid not in session.running():
        raise SystemExit('no running harness session')
    base = session.game_dll_base(pid, build)
    reader = winapi.Reader(pid)
    try:
        probe = mission.Probe(reader, base, build.mission)
        board = probe.g('board')
        print('board', hex(board), 'state', {k: v for k, v in probe.board().items()
                                             if k.startswith(('active', 'sel_', 'op_array'))})
        for op in probe.operations():
            if op['planet'] != planet:
                continue
            raw = probe.read(board + 0xF7280 + mission.OPERATION_SIZE * op['index'], mission.OPERATION_SIZE)
            print('operation', op)
            print('   raw', raw.hex())
            for k in op['records']:
                rec = probe.mission_record(k)
                rraw = probe.read(board + 0xF9A10 + mission.RECORD_SIZE * k, mission.RECORD_SIZE)
                print('   record', rec)
                print('      raw', rraw.hex())
        blob = reader.read(board, BOARD_SCAN) or b''
        for h in hashes:
            needle = struct.pack('<I', h)
            at, found = 0, []
            while True:
                at = blob.find(needle, at)
                if at < 0:
                    break
                found.append(hex(at))
                at += 1
            print('region hash 0x%08x at board +' % h, found[:20])
    finally:
        reader.close()


if __name__ == '__main__':
    main()
