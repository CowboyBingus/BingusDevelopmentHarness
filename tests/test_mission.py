"""Ship -> mission -> ship steps (hd2h/mission.py) against a simulated game, plus decoding and the Lua they send."""
from contextlib import ExitStack
import ctypes
from pathlib import Path
import struct
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import support
from hd2h import builds, mission, session
from hd2h.state import State

BUILD = builds.KNOWN['25480438']
LIVE_IMAGE = support.GAME_DLL_IMAGE


class Clock:
    """Simulated time: sleep() advances it, so timeouts pass instantly."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)


def operation_bytes(planet, difficulty, records, race=2, states=(1, 0, 0), valid=1, op_id=0):
    r = bytearray(92)
    struct.pack_into('<I', r, 4, op_id)
    struct.pack_into('<H', r, 16, planet)
    r[32], r[52] = difficulty, valid
    struct.pack_into('<I', r, 36, race)
    struct.pack_into('<3I', r, 72, *states)
    r[85:85 + len(records)] = bytes(records)
    r[88] = len(records)
    return bytes(r)


def record_bytes(op, mission_id, difficulty, state=1, in_op=0, kind=86):
    r = bytearray(76)
    struct.pack_into('<IiIIII', r, 40, op, mission_id, kind, 1234, state, difficulty)
    struct.pack_into('<I', r, 68, in_op)
    return bytes(r)


class World:
    """A solo host's game on the ship that reacts to the native calls the way the live runs did.

    Reads advance it (events fire after a number of reads); the native calls are recorded in `calls`."""

    def __init__(self, active_op=None):
        self.state, self.calls, self.events = 3, [], []
        self.board_ = {'ready': True, 'war_loaded': 1, 'board_host': 7, 'local_peer': 7, 'host': True,
                       'hosting': 1, 'transition': 0, 'active_op': 0, 'active_op_index': 0, 'active_op_id': 0,
                       'active_planet': 0, 'active_difficulty': 0, 'create_state': 0, 'op_array_planet': 0,
                       'request_ship_planet': 0, 'sel_planet': 0}
        self.campaigns, self.owners = [7, 270], {7: 3, 270: 2}
        self.ops, self.records = [], {}
        self.pod = {'players': [1, 1], 'pod': 60, 'mode': 0, 'state': 0, 'target': 0}
        self.loadout_, self.seated, self.menu = None, False, 0
        self.view_ = {'cut': 0, 'cut_queued': 0, 'transition': 0, 'drop': 0, 'mission_active': 0, 'mission_type': 0,
                      'stack': [], 'popup': 0, 'control': {'avatar': 229, 'spawning': False}}
        self.refuse = set()  # native calls that answer false
        self.stall = set()   # steps that never progress: 'pod', 'launch', 'control', 'leave'
        if active_op:
            self.generate(270)
            self.board_.update(active_op=1, active_op_index=active_op, active_op_id=184, active_planet=270,
                               active_difficulty=self.ops[active_op]['difficulty'], op_array_planet=270)

    def later(self, reads, apply):
        self.events.append([reads, apply])

    def tick(self):
        for event in list(self.events):
            event[0] -= 1
            if event[0] <= 0:
                self.events.remove(event)
                event[1]()

    def generate(self, planet):
        self.ops, self.records = [], {}
        for d in range(1, 11):
            for s in range(3):
                index = 3 * (d - 1) + s
                self.ops.append(mission.parse_operations(operation_bytes(planet, d, [index], self.owners[planet]))[0])
                self.ops[-1]['index'] = index
                self.records[index] = mission.parse_mission_record(record_bytes(index, 100 + index, d), index)
        self.board_['op_array_planet'] = planet

    # Probe -----------------------------------------------------------------------------------------------------
    def board(self):
        self.tick()
        return dict(self.board_)

    def operations(self):
        self.tick()
        return [dict(o) for o in self.ops]

    def mission_record(self, k):
        return dict(self.records[k])

    def campaign_planets(self):
        return list(self.campaigns)

    def planet_owners(self, planets):
        return {p: self.owners.get(p) for p in planets}

    def mission_tags(self, kind):
        return getattr(self, 'tags', {}).get(kind)  # region tags per mission type; unknown unless a test sets them

    def hellpod(self):
        self.tick()
        return dict(self.pod)

    def loadout(self):
        self.tick()
        return dict(self.loadout_) if self.loadout_ else None

    def avatar(self):
        return 229

    def seater(self, avatar):
        return {'entity': 0x1234, 'seat': 60 if self.seated else 0, 'free': not self.seated}

    def view(self, game_state):
        self.tick()
        return dict(self.view_, state=game_state, menu=self.menu)

    # Natives ---------------------------------------------------------------------------------------------------
    def answer(self, name, values):
        self.calls.append(name)
        return [False, 'refused'] if name in self.refuse else values

    def board_select(self, ship, view, op, mission_id, difficulty):
        self.calls.append(('board_select', ship, view, op, mission_id, difficulty))
        if 'board_select' in self.refuse:
            return [False, 'not the host']
        if op < 0:
            self.generate(view)
            self.board_['sel_view_planet'] = view
            return [True]
        if not self.board_['active_op']:
            self.board_.update(active_op=1, active_op_index=op, active_op_id=185, active_planet=view,
                               active_difficulty=difficulty, create_state=3)
            self.later(1, lambda: self.board_.update(create_state=2))
        if 'pod' not in self.stall:
            self.later(3, lambda: self.pod.update(mode=2))
            self.later(6, lambda: self.pod.update(state=3, target=3))
        return [True, view, mission_id, difficulty, 1, 3, 185]

    def open_briefing(self):
        self.menu, self.loadout_ = 14, {'phase': 1, 'launching': 0}
        return self.answer('open_briefing', [True, 14])

    def skip_intro(self):
        self.later(1, lambda: self.loadout_.update(phase=0))
        return self.answer('skip_intro', [True])

    def seat(self, entity, pod):
        self.seated = True
        self.later(1, lambda: self.pod.update(state=4, target=4))
        return self.answer('seat', [True])

    def drop_confirm(self):
        if 'drop_confirm' not in self.refuse:
            self.view_['drop'] = 1
        return self.answer('drop_confirm', [True, 0.0, 0.0])

    def drop_at(self, x, y):
        self.view_['drop'] = 1
        return self.answer('drop_at', [True])

    def ready(self):
        if 'launch' not in self.stall:
            self.later(2, lambda: setattr(self, 'menu', 0))
            self.later(4, lambda: (setattr(self, 'state', 6), self.view_.update(cut=6, transition=2)))
            self.later(8, lambda: (setattr(self, 'state', 4), self.view_.update(
                cut=0, transition=0, mission_active=1, mission_type=1,
                control={'avatar': 451, 'spawning': True})))
            if 'control' not in self.stall:
                self.later(12, lambda: self.view_.update(control={'avatar': 451, 'spawning': False}))
        return self.answer('ready', [True, 267])

    def squad_abandon(self):
        if 'leave' not in self.stall:
            self.later(1, lambda: self.view_.update(transition=1, menu=24))
            self.later(3, lambda: (setattr(self, 'state', 5), self.view_.update(transition=2, mission_active=0)))
            self.later(6, lambda: (setattr(self, 'state', 3), self.view_.update(transition=0)))
        return self.answer('squad_abandon', [True])


class Decoding(unittest.TestCase):
    def test_operations_keep_valid_records_only(self):
        raw = operation_bytes(270, 3, [6, 7], op_id=184) + operation_bytes(270, 3, [8], valid=0)
        ops = mission.parse_operations(raw)
        self.assertEqual(len(ops), 1)
        self.assertEqual({k: ops[0][k] for k in ('index', 'id', 'planet', 'difficulty', 'race', 'records')},
                         {'index': 0, 'id': 184, 'planet': 270, 'difficulty': 3, 'race': 2, 'records': [6, 7]})

    def test_mission_record_and_campaigns(self):
        record = mission.parse_mission_record(record_bytes(0, 203, 1, in_op=2), 5)
        self.assertEqual((record['k'], record['op'], record['mission'], record['difficulty'], record['in_op']),
                         (5, 0, 203, 1, 2))
        raw = b''.join(struct.pack('<IIIII', 1, 0, planet, 0, 0) for planet in (7, 270))
        self.assertEqual(mission.parse_campaign_planets(raw, 2), [7, 270])

    def test_pick_skips_done_missions_and_other_difficulties(self):
        ops = mission.parse_operations(operation_bytes(270, 1, [0]) + operation_bytes(270, 1, [1], states=(3, 0, 0))
                                       + operation_bytes(270, 2, [2]))
        records = {0: mission.parse_mission_record(record_bytes(0, 10, 1, state=4), 0),
                   1: mission.parse_mission_record(record_bytes(1, 11, 1), 1),
                   2: mission.parse_mission_record(record_bytes(2, 12, 2), 2)}
        self.assertEqual(mission.pick_mission(ops, records.get, 270, 1), (None, None), 'record done, slot done')
        op, record = mission.pick_mission(ops, records.get, 270, 2)
        self.assertEqual((op['index'], record['mission']), (2, 12))
        self.assertEqual(mission.pick_mission(ops, records.get, 270, 2, index=0), (None, None))
        self.assertEqual(mission.pick_mission(ops, records.get, 7, 2), (None, None))
        # nth: the open mission number N (wrapping); the first by default (city maps lie only in some regions)
        ops2 = mission.parse_operations(operation_bytes(270, 1, [0]) + operation_bytes(270, 1, [1])
                                        + operation_bytes(270, 2, [2]) + operation_bytes(270, 2, [3, 4], states=(3, 0, 0)))
        records2 = {**records, 3: mission.parse_mission_record(record_bytes(3, 13, 2), 3),
                    4: mission.parse_mission_record(record_bytes(3, 14, 2, in_op=1), 4)}
        opened = mission.open_missions(ops2, records2.get, 270, 2)
        self.assertEqual([r['mission'] for _, r in opened], [12, 14], 'mission 13 is in a done slot')
        self.assertEqual([mission.pick_mission(ops2, records2.get, 270, 2, nth=n)[1]['mission'] for n in (None, 1, 2)],
                         [12, 14, 12], 'first by default, then wrapping')
        # tag: only missions whose definition carries the region tag (planet 158: megacity types carry only tag 3,
        # and they came after every tag-1 mission in board order, so "first" never reached them)
        records3 = {**records2, 4: mission.parse_mission_record(record_bytes(3, 14, 2, in_op=1, kind=36), 4)}
        tags = {86: [1], 36: [3]}.get
        op, record = mission.pick_mission(ops2, records3.get, 270, 2, tags_of=tags, tag=3)
        self.assertEqual((op['index'], record['mission']), (3, 14))
        self.assertEqual(mission.pick_mission(ops2, records3.get, 270, 2, tags_of=tags, tag=2), (None, None))
        self.assertEqual(mission.pick_mission(ops2, records3.get, 270, 2, tags_of=lambda kind: None, tag=3),
                         (None, None), 'unknown tags never match')

    def test_choose_planet(self):
        board = {'sel_planet': 0}
        self.assertEqual(mission.choose_planet(board, [7, 270]), 7)
        self.assertEqual(mission.choose_planet({'sel_planet': 270}, [7, 270]), 270, "the ship's planet first")
        self.assertEqual(mission.choose_planet(board, [7, 270], {7: 3, 270: 1}, faction='terminid'), 270,
                         'owner 1 counts as Terminid')
        self.assertEqual(mission.choose_planet(board, [7, 270], planet=7), 7)
        with self.assertRaises(mission.MissionError):
            mission.choose_planet(board, [7, 270], planet=5)
        with self.assertRaises(mission.MissionError):
            mission.choose_planet(board, [7], {7: 3}, faction='illuminate')
        # pick: the faction's campaign planet number N, wrapping, even when the ship's planet fits (other biomes)
        owners = {7: 3, 157: 3, 200: 2, 230: 3}
        self.assertEqual([mission.choose_planet({'sel_planet': 157}, [7, 157, 200, 230], owners, faction='automaton',
                                                pick=n) for n in range(4)], [7, 157, 230, 7])
        with self.assertRaises(mission.MissionError):
            mission.choose_planet(board, [7], {7: 3}, faction='illuminate', pick=0)

    def test_in_control(self):
        view = {'state': 4, 'mission_active': 1, 'transition': 0, 'cut': 0, 'cut_queued': 0, 'menu': 0, 'stack': [],
                'control': {'spawning': False}}
        self.assertTrue(mission.in_control(view))
        for change in ({'state': 6}, {'transition': 2}, {'cut': 6}, {'menu': 4}, {'stack': [2]},
                       {'control': {'spawning': True}}, {'control': None}, {'mission_active': 0}):
            self.assertFalse(mission.in_control(dict(view, **change)), change)


class NativeSources(unittest.TestCase):
    def setUp(self):
        self.natives = mission.Natives(None, 0x7FFB00000000, BUILD.mission)
        self.sources = {
            'board_select': self.natives.source(*self.body('board_select', 270, 270, 0, 203, 1)),
            'open_briefing': self.natives.source(*self.body('open_briefing')),
            'skip_intro': self.natives.source(*self.body('skip_intro')),
            'seat': self.natives.source(*self.body('seat', 0x1234, 60)),
            'drop_confirm': self.natives.source(*self.body('drop_confirm')),
            'drop_at': self.natives.source(*self.body('drop_at', 0.0, 0.0)),
            'ready': self.natives.source(*self.body('ready')),
            'squad_abandon': self.natives.source(*self.body('squad_abandon')),
        }

    def body(self, name, *args):
        captured = {}
        with mock.patch.object(mission.Natives, 'call', lambda self, body, *functions: captured.update(
                body=body, functions=functions)):
            getattr(self.natives, name)(*args)
        return (captured['body'],) + captured['functions']

    def test_every_call_checks_its_function_and_uses_build_offsets(self):
        for name, function in (('board_select', 'board_select'), ('open_briefing', 'open_briefing'),
                               ('seat', 'instant_seat'), ('drop_confirm', 'drop_confirm'), ('drop_at', 'drop_set'),
                               ('ready', 'set_ready'), ('squad_abandon', 'squad_abandon')):
            rva, prologue = BUILD.mission.functions[function]
            source = self.sources[name]
            self.assertIn("[%d] = '%s'" % (rva, prologue), source, name)
            self.assertIn('base + %d' % rva, source, name)
        self.assertIn('local base = %d' % 0x7FFB00000000, self.sources['skip_intro'])
        self.assertIn('board = %d' % BUILD.mission.globals['board'], self.sources['skip_intro'])
        self.assertIn('select(board, 270, 270, op, 203, difficulty, 0, 0)', self.sources['board_select'])
        self.assertIn("if p(board + 0x1F8078) ~= p(ctx + 0xB398) then return false, 'not the host' end",
                      self.sources['board_select'])

    def test_sources_compile_in_every_lua_vm(self):
        checked = 0
        if support.LUAJIT.exists():
            for name, source in self.sources.items():
                result = subprocess.run([str(support.LUAJIT), '-e', 'local f, e = loadstring(io.read("*a")) '
                                         'if not f then io.stderr:write(e) os.exit(1) end'],
                                        input=source, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, '%s: %s' % (name, result.stderr))
            checked += 1
        if support.GAME_DLL.exists():
            dll = ctypes.CDLL(str(support.GAME_DLL))
            dll.luaL_newstate.restype = ctypes.c_void_p
            dll.luaL_loadbuffer.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
            dll.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            dll.lua_tolstring.restype = ctypes.c_char_p
            dll.lua_settop.argtypes = [ctypes.c_void_p, ctypes.c_int]
            dll.lua_close.argtypes = [ctypes.c_void_p]
            state = dll.luaL_newstate()
            try:
                for name, source in self.sources.items():
                    data = source.encode()
                    code = dll.luaL_loadbuffer(state, data, len(data), name.encode())
                    self.assertEqual(code, 0, '%s: %s' % (name, dll.lua_tolstring(state, -1, None)))
                    dll.lua_settop(state, 0)
            finally:
                dll.lua_close(state)
            checked += 1
        if not checked:
            self.skipTest('no Lua VM available')

    @unittest.skipUnless(support.present(LIVE_IMAGE), 'captured game.dll image not available (HD2H_GAME_DLL_IMAGE)')
    def test_prologues_match_the_captured_game_dll(self):
        image = LIVE_IMAGE.read_bytes()
        for name, (rva, prologue) in BUILD.mission.functions.items():
            expected = bytes.fromhex(prologue)
            self.assertEqual(image[rva:rva + len(expected)], expected, name)


class Steps(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stack = ExitStack()
        self.stack.enter_context(mock.patch('hd2h.mission.time', types.SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=self.clock.sleep)))
        self.notes, self.shots = [], []

    def tearDown(self):
        self.stack.close()

    def deployer(self, world):
        return mission.Deployer(world, world, lambda: world.state, note=self.notes.append, shoot=self.shots.append)

    def round_trip(self, world, **select):
        deployer = self.deployer(world)
        chosen = deployer.select(**select)
        deployer.brief_and_seat()
        deployer.drop()
        deployer.launch()
        deployer.wait_control()
        left = deployer.leave(settle=0)
        return chosen, left

    def test_new_operation_round_trip(self):
        world = World()
        chosen, left = self.round_trip(world, difficulty=3)
        self.assertEqual(chosen['planet'], 7, 'first campaign planet')
        self.assertEqual((chosen['difficulty'], chosen['operation'], chosen['continued']), (3, 185, False))
        focus, commit = [c for c in world.calls if c[0] == 'board_select']
        self.assertEqual(focus, ('board_select', 0, 7, -1, -1, 0), 'the galaxy view focus comes first')
        self.assertEqual(commit, ('board_select', 7, 7, 6, 106, 3))
        names = [c for c in world.calls if isinstance(c, str)]
        self.assertEqual(names, ['open_briefing', 'skip_intro', 'seat', 'drop_confirm', 'ready', 'squad_abandon'])
        self.assertEqual(world.state, 3)
        self.assertEqual(self.shots, ['briefing', 'landed', 'ship-after'])
        self.assertEqual(left['to_ship_s'] >= 0, True)

    def test_active_operation_is_continued_without_a_focus(self):
        world = World(active_op=4)
        chosen, _ = self.round_trip(world, difficulty=9)
        self.assertTrue(chosen['continued'])
        self.assertEqual((chosen['planet'], chosen['difficulty'], chosen['operation_index']), (270, 2, 4))
        self.assertEqual([c for c in world.calls if c[0] == 'board_select'], [('board_select', 270, 270, 4, 104, 2)])

    def test_region_tag_picks_a_city_mission(self):
        # Planet 158 (2026-10-13): its megacity operations' mission types carry only region tag 3 and came after every
        # tag-1 (open terrain) mission, so the first open mission was never a city map.
        world = World()
        world.tags = {86: [1], 36: [3]}
        plain = world.generate

        def generate(planet):
            plain(planet)
            world.records[8] = dict(world.records[8], type=36)  # D3's third operation: a megacity type
        world.generate = generate
        chosen, _ = self.round_trip(world, difficulty=3, region_tag=3)
        self.assertEqual((chosen['operation_index'], chosen['mission'], chosen['region_tags']), (8, 108, [3]))
        self.assertTrue(any('106/86/[1], 107/86/[1], 108/36/[3]' in n for n in self.notes), self.notes)
        unknown = World()  # tags unreadable: nothing matches, and the error says which tag was asked
        with self.assertRaisesRegex(mission.MissionError, 'with region tag 3'):
            self.deployer(unknown).select(difficulty=3, region_tag=3)

    def test_region_tag_prefers_missions_with_only_that_tag(self):
        # Reported case (2026-10-14, planet 7): --region-tag 2 picked type 129 (tags [2, 1]) first and got an
        # open-terrain map with no colony buildings; types carrying only tag 2 (134, 136, 137) were open too.
        world = World()
        world.tags = {86: [2, 1], 36: [2]}
        plain = world.generate

        def generate(planet):
            plain(planet)
            world.records[8] = dict(world.records[8], type=36)  # D3's third operation: a colony-only type
        world.generate = generate
        chosen, _ = self.round_trip(world, difficulty=3, region_tag=2)
        self.assertEqual((chosen['mission'], chosen['region_tags']), (108, [2]))
        # Without a colony-only mission, a mixed one is still used.
        mixed = World()
        mixed.tags = {86: [2, 1]}
        chosen, _ = self.round_trip(mixed, difficulty=3, region_tag=2)
        self.assertEqual(chosen['region_tags'], [2, 1])

    def test_faction_and_region_tag_find_a_planet_with_such_missions(self):
        # Two Terminid planets; only the second has a city mission at D3. The first is focused, found without one,
        # and the second is used (no operation is created while looking).
        world = World()
        world.campaigns, world.owners = [7, 270, 300], {7: 3, 270: 2, 300: 2}
        world.tags = {86: [1], 36: [3]}
        plain = world.generate

        def generate(planet):
            plain(planet)
            if planet == 300:
                world.records[8] = dict(world.records[8], type=36)
        world.generate = generate
        chosen, _ = self.round_trip(world, difficulty=3, faction='terminid', region_tag=3)
        self.assertEqual((chosen['planet'], chosen['mission'], chosen['region_tags']), (300, 108, [3]))
        focuses = [c for c in world.calls if c[0] == 'board_select' and c[3] == -1]
        self.assertEqual([f[2] for f in focuses], [270, 300], 'each candidate focused once, in campaign order')
        self.assertTrue(any('planet 300 has open missions with region tag 3 (tried [270, 300])' in n
                            for n in self.notes), self.notes)
        none = World()
        none.tags = {86: [1]}
        with self.assertRaisesRegex(mission.MissionError, r'no terminid campaign planet has an open D3 mission with '
                                                          r'region tag 3 \(tried \[270\]\)'):
            self.deployer(none).select(difficulty=3, faction='terminid', region_tag=3)

    def test_faction_picks_a_planet_of_that_race(self):
        world = World()
        chosen, _ = self.round_trip(world, faction='terminid')
        self.assertEqual((chosen['planet'], chosen['race']), (270, 2))

    def test_drop_falls_back_to_the_map_centre(self):
        world = World()
        world.refuse.add('drop_confirm')
        self.round_trip(world)
        self.assertIn('drop_at', world.calls)

    def test_refusals_and_stalls_name_the_step(self):
        for setup, expected in ((lambda w: w.refuse.add('board_select'), 'focusing planet 7 refused'),
                                (lambda w: w.stall.add('pod'), 'never got the mission'),
                                (lambda w: w.refuse.add('ready'), 'ready refused'),
                                (lambda w: w.stall.add('launch'), 'the launch did not start'),
                                (lambda w: w.stall.add('control'), 'no control within'),
                                (lambda w: w.stall.add('leave'), 'did not reach the ship')):
            world = World()
            setup(world)
            with self.assertRaises(mission.MissionError) as raised:
                self.round_trip(world)
            self.assertIn(expected, str(raised.exception))

    def test_selection_refuses_a_squad_or_a_guest(self):
        world = World()
        world.pod['players'] = [2, 1]
        with self.assertRaisesRegex(mission.MissionError, 'solo sessions only'):
            self.deployer(world).select()
        world = World()
        world.board_['host'] = False
        with self.assertRaisesRegex(mission.MissionError, 'host of a loaded war table'):
            self.deployer(world).select()

    def test_leave_waits_for_a_loading_mission_first(self):
        world = World()
        world.state = 6
        world.later(3, lambda: (setattr(world, 'state', 4), world.view_.update(mission_active=1)))
        self.deployer(world).leave(settle=0)
        self.assertEqual((world.state, world.calls), (3, ['squad_abandon']))

    def test_leave_on_the_ship_does_nothing(self):
        world = World()
        self.deployer(world).leave(settle=0)
        self.assertEqual(world.calls, [])


class SessionCommands(unittest.TestCase):
    """deploy()/leave() record every step in the session manifest; stop leaves a mission before quitting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = types.SimpleNamespace(state=Path(self.tmp.name) / 'state', mailbox=Path(self.tmp.name) / 'mb')
        State(self.paths.state).save({'session': 's', 'pid': 42, 'agent': {'files': {}},
                                      'facts': {'build': '25480438'}})
        self.clock = Clock()
        self.world = World()
        self.privacy, self.notes = 2, []
        self.stack = ExitStack()
        self.stack.enter_context(mock.patch('hd2h.mission.time', types.SimpleNamespace(
            monotonic=self.clock.monotonic, sleep=self.clock.sleep)))
        reads = lambda pid, build: {'state': self.world.state, 'privacy': self.privacy,
                                    'privacy_name': {2: 'Invite Only', 0: 'Public'}[self.privacy]}
        fake_session = types.SimpleNamespace(live_reads=reads)
        self.stack.enter_context(mock.patch('hd2h.mission._context', lambda paths, manifest, note, shoot=None: (
            fake_session, BUILD, types.SimpleNamespace(close=lambda: None),
            mission.Deployer(self.world, self.world, lambda: self.world.state, note=note, shoot=shoot))))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_deploy_and_leave_record_the_mission(self):
        manifest = State(self.paths.state).load()
        entry = mission.deploy(self.paths, manifest, difficulty=2, note=self.notes.append)
        saved = State(self.paths.state).load()
        self.assertTrue(saved['in_mission'])
        self.assertEqual(saved['missions'][-1]['operation'], 185)
        self.assertEqual(sorted(entry['steps']), ['briefing', 'control', 'drop', 'launch', 'select'])
        self.assertIn('reached_mission', entry)
        self.assertIn('in_control', entry)
        left = mission.leave(self.paths, saved, settle=0, note=self.notes.append)
        self.assertEqual(self.notes[-1], 'mission: back on the ship')
        saved = State(self.paths.state).load()
        self.assertEqual(left['state'], 3)
        self.assertFalse(saved['in_mission'])
        self.assertIn('left', saved['missions'][-1])

    def test_deploy_refuses_a_public_lobby_and_records_errors(self):
        self.privacy = 0
        with self.assertRaisesRegex(mission.MissionError, 'not Invite Only'):
            mission.deploy(self.paths, State(self.paths.state).load(), note=self.notes.append)
        self.privacy = 2
        self.world.stall.add('launch')
        with self.assertRaises(mission.MissionError):
            mission.deploy(self.paths, State(self.paths.state).load(), note=self.notes.append)
        self.assertIn('the launch did not start', State(self.paths.state).load()['missions'][-1]['error'])

    def test_a_public_hellforge_session_may_deploy_in_its_public_lobby(self):
        self.privacy = 0
        manifest = State(self.paths.state).update(privacy='public', target='hellforge')
        self.world.stall.add('launch')  # stop after the privacy check: it passed if the error is the launch's
        with self.assertRaises(mission.MissionError) as raised:
            mission.deploy(self.paths, manifest, note=self.notes.append)
        self.assertNotIn('not Invite Only', str(raised.exception))
        self.privacy = 2  # an opted-in Public session refuses a lobby that is not Public
        with self.assertRaisesRegex(mission.MissionError, 'not Public'):
            mission.deploy(self.paths, manifest, note=self.notes.append)
        State(self.paths.state).update(target='steam')  # a Public request never applies to the Steam target
        self.privacy = 0
        with self.assertRaisesRegex(mission.MissionError, 'not Invite Only'):
            mission.deploy(self.paths, State(self.paths.state).load(), note=self.notes.append)

    def test_stop_leaves_a_mission_before_quitting(self):
        manifest = dict(State(self.paths.state).load(), agent={'files': {}})
        left = []
        with mock.patch('hd2h.session.live_reads', lambda pid, build: {'state': 4}), \
                mock.patch('hd2h.mission.leave', lambda paths, manifest, note: left.append(True)), \
                mock.patch('hd2h.navigate.to_ship', lambda paths, manifest, note: {'state': 3}):
            self.assertTrue(session._before_quit(self.paths, manifest, note=lambda text: None))
        self.assertEqual(left, [True])

    def test_stop_terminates_when_the_mission_cannot_be_left(self):
        manifest = State(self.paths.state).load()
        notes = []

        def fail(paths, manifest, note):
            raise mission.MissionError('did not reach the ship')
        with mock.patch('hd2h.session.live_reads', lambda pid, build: {'state': 4}), \
                mock.patch('hd2h.mission.leave', fail):
            self.assertFalse(session._before_quit(self.paths, manifest, note=notes.append))
        self.assertIn('terminating instead of quitting inside it', notes[-1])


if __name__ == '__main__':
    unittest.main()
