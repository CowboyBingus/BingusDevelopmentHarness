"""Ship -> mission -> ship without input, through the game's own functions.

Every address is for build 25480438 (hd2h/builds.py, MissionOffsets), live-verified on 2026-10-13. Decisions come
from external reads (ReadProcessMemory through winapi.Reader); the in-game agent only makes the native calls, each
after checking the function's prologue bytes and re-checking its guard in the same frame. Nothing patches code. The
only data writes are the briefing intro timer (as Ship Station Hotkeys does) and, through the game's own setter, the
local ready bit.

Steps: select (0x12D18F0, the Galactic Map's commit) -> briefing (0x661CF0) and instant seat (0x639830) -> drop zone
(the briefing map's confirm 0x18C0BC0) -> ready (0x607230) -> the game launches by itself (state 6, then 4) -> the
avatar's spawn flag clears (in control). Leaving: the host abandon 0x18FB720 (state 4 -> 5 -> 3).

Account traffic, the same as a player's own deploy: Operation/Create when no operation is active,
Account/ReportPosition when the ship travels, Operation/Mission/Start at launch, and the Mission/Session abandon
report on leaving. Solo sessions only; the lobby must be Invite Only.
"""
import datetime
import struct
import time

from . import builds, winapi
from .mailbox import Mailbox
from .state import State

SHIP, MISSION, PREPARE_SHIP, PREPARE_MISSION = 3, 4, 5, 6
FACTIONS = {'terminid': 2, 'automaton': 3, 'illuminate': 4}
POD_MISSION_READY, POD_OPEN, POD_OCCUPIED = 2, 3, 4
BRIEFING_PRESENTER, LOADOUT_SCREEN, LOAD_SCREENS = 14, 11, (2, 4)
OPERATIONS, OPERATION_SIZE, RECORD_SIZE = 110, 92, 76
DONE_STATES = (3, 4)  # mission and operation-slot states: succeeded, failed
INVITE_ONLY, PUBLIC = 2, 0


class MissionError(RuntimeError):
    """A step did not happen; the message names the step and the last values read."""


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _u32(raw, at):
    return struct.unpack_from('<I', raw, at)[0]


def _i32(raw, at):
    return struct.unpack_from('<i', raw, at)[0]


# Decoding (pure) ------------------------------------------------------------------------------------------------

def parse_operations(raw):
    """Valid local operation records (92 bytes each; byte +52 marks a valid record)."""
    found = []
    for index in range(len(raw) // OPERATION_SIZE):
        r = raw[index * OPERATION_SIZE:(index + 1) * OPERATION_SIZE]
        if r[52] == 0:
            continue
        found.append({'index': index, 'id': _u32(r, 4), 'planet': struct.unpack_from('<H', r, 16)[0],
                      'type': _u32(r, 28), 'difficulty': r[32], 'race': _u32(r, 36), 'create': r[53],
                      'states': list(struct.unpack_from('<3I', r, 72)), 'records': list(r[85:85 + min(r[88], 3)])})
    return found


def parse_mission_record(raw, k):
    """One 76-byte mission record: its operation, the mission (territory) id the commit takes, type, state."""
    return {'k': k, 'op': _u32(raw, 40), 'mission': _i32(raw, 44), 'type': _u32(raw, 48), 'seed': _u32(raw, 52),
            'state': _u32(raw, 56), 'difficulty': _u32(raw, 60), 'in_op': _u32(raw, 68)}


def parse_campaign_planets(raw, count):
    """Planets with a campaign entry (20-byte entries, planet at +8): only these get operations."""
    return [_u32(raw, 20 * i + 8) for i in range(count)]


def open_missions(operations, record_of, planet, difficulty, index=None, tags_of=None, tag=None):
    """Every (operation, mission record) open at this difficulty on planet, in board order. With tag, only missions
    whose definition (tags_of(type)) carries that region tag."""
    found = []
    for op in operations:
        if op['planet'] != planet or op['difficulty'] != difficulty or (index is not None and op['index'] != index):
            continue
        for k in op['records']:
            record = record_of(k)
            if (record['op'] == op['index'] and record['difficulty'] == difficulty
                    and record['state'] not in DONE_STATES and record['in_op'] < 3
                    and op['states'][record['in_op']] not in DONE_STATES
                    and (tag is None or tag in (tags_of(record['type']) or ()))):
                found.append((op, record))
    return found


def pick_mission(operations, record_of, planet, difficulty, index=None, nth=None, tags_of=None, tag=None):
    """(operation, mission record) of the first open mission at this difficulty on planet, or (None, None). With nth,
    open mission number nth (from 0, wrapping). With tag, only missions whose definition carries that region tag:
    the generator picks the map's region variant among the planet's variants with one of the definition's tags, so
    the tag decides whether a city map is possible (planet 158: the megacity operations' types carry only tag 3,
    every other Automaton type tag 1 or 1-2; 2026-10-13). Missions carrying only that tag come first (board order
    kept within each group): a mission with tags [2, 1] may get an open-terrain map, as planet 7's type 129 did for a
    colony test on 2026-10-14 (rocks and ruins only)."""
    found = open_missions(operations, record_of, planet, difficulty, index, tags_of, tag)
    if not found:
        return None, None
    if tag is not None:
        found.sort(key=lambda pair: tuple(tags_of(pair[1]['type']) or ()) != (tag,))  # stable: board order kept
    return found[(nth or 0) % len(found)]


def choose_planet(board, campaigns, owners=None, planet=None, faction=None, pick=None):
    """The planet to deploy on: the asked one; else, with pick, the faction's campaign planet number pick (counted
    from 0, wrapping around); else the ship's planet when it has a campaign (and the faction); else the first
    campaign planet owned by the faction (owners: planet -> race; 1 counts as 2, as the generator does); else the
    first campaign planet."""
    if planet is not None:
        if planet not in campaigns:
            raise MissionError('planet %s has no campaign (campaign planets: %s)' % (planet, campaigns))
        return planet
    race = FACTIONS[faction] if faction else None
    def fits(candidate):
        return race is None or ({1: 2}.get((owners or {}).get(candidate), (owners or {}).get(candidate)) == race)
    if pick is not None:  # other planets, other biomes and maps (Automaton D1 maps on one planet had 1-3 spots)
        candidates = [c for c in campaigns if fits(c)]
        if not candidates:
            raise MissionError('no campaign planet%s' % (' for %s' % faction if faction else ''))
        return candidates[pick % len(candidates)]
    if board.get('sel_planet') in campaigns and fits(board['sel_planet']):
        return board['sel_planet']
    for candidate in campaigns:
        if fits(candidate):
            return candidate
    raise MissionError('no campaign planet%s' % (' for %s' % faction if faction else ''))


def in_control(view):
    """In the mission, no transition, loading screen or cutscene, and the local avatar's spawn flag clear."""
    control = view.get('control') or {}
    return (view.get('state') == MISSION and bool(view.get('mission_active')) and view.get('transition') == 0
            and view.get('cut') == 0 and view.get('cut_queued') == 0 and view.get('menu') not in LOAD_SCREENS
            and not any(s in LOAD_SCREENS for s in view.get('stack') or []) and control.get('spawning') is False)


# External reads -------------------------------------------------------------------------------------------------

class Probe:
    """Reads the running game from outside (ReadProcessMemory only)."""

    def __init__(self, reader, base, offsets):
        self.reader, self.base, self.offsets = reader, base, offsets

    def read(self, address, size):
        raw = self.reader.read(address, size)
        if raw is None:
            raise MissionError('unreadable memory at 0x%x (%d bytes)' % (address, size))
        return raw

    def u8(self, address):
        return self.read(address, 1)[0]

    def u32(self, address):
        return _u32(self.read(address, 4), 0)

    def i32(self, address):
        return _i32(self.read(address, 4), 0)

    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def ptr(self, address):
        return self.reader.pointer(address)

    def g(self, name):
        """The pointer stored in game.dll's global `name` (see builds.MissionOffsets), or None."""
        return self.ptr(self.base + self.offsets.globals[name])

    def board(self):
        board, ctx, backend = self.g('board'), self.g('ctx'), self.g('backend')
        if not board or not ctx:
            return {'ready': False}
        out = {'ready': True, 'war_loaded': self.u8(board + 0x1F8011),
               # Peer ids, not pointers (both can be 0 in a solo session); the commit only needs them equal.
               'board_host': self.u64(board + 0x1F8078), 'local_peer': self.u64(ctx + 0xB398),
               'hosting': self.u32(ctx + 0x159C8), 'transition': self.u32(ctx + 0x159CC),
               'backend_ready': [self.u8(backend + 0x702F8), self.u32(backend + 0x702FC)] if backend else None,
               'active_op': self.u8(board + 0x78EBC), 'active_op_index': self.u32(board + 0x78E88),
               'active_op_id': self.u32(board + 0x78E8C),
               'active_planet': struct.unpack('<H', self.read(board + 0x78E98, 2))[0],
               'active_difficulty': self.u8(board + 0x78EA8), 'create_state': self.u8(board + 0x78EBD),
               'op_array_planet': self.i32(board + 0xF9A08), 'request_ship_planet': self.i32(board + 0x78E60),
               'sel_planet': self.i32(board + 0x17A298), 'sel_view_planet': self.i32(board + 0x17A29C),
               'sel_op': self.i32(board + 0x17A2A0), 'sel_mission': self.i32(board + 0x17A2A4),
               'sel_difficulty': self.i32(board + 0x17A2A8)}
        out['host'] = out['board_host'] == out['local_peer']
        return out

    def operations(self):
        return parse_operations(self.read(self.g('board') + 0xF7280, OPERATION_SIZE * OPERATIONS))

    def mission_record(self, k):
        return parse_mission_record(self.read(self.g('board') + 0xF9A10 + RECORD_SIZE * k, RECORD_SIZE), k)

    def campaign_planets(self):
        board = self.g('board')
        count = self.u32(board + 0x173C84)
        if count > 512:
            raise MissionError('campaign table count %d out of range' % count)
        return parse_campaign_planets(self.read(board + 0x171484, 20 * count), count) if count else []

    def planet_owners(self, planets):
        board = self.g('board')
        return {planet: self.u32(board + 0x14747C + 304 * planet) for planet in planets}

    def mission_tags(self, kind):
        """The region tags of mission definition `kind` (a mission record's type): game.dll's static table, row
        +0x368 a pointer to the tag bytes, +0x370 their count. None when the build has no table or it looks wrong."""
        table = (self.offsets.tables or {}).get('mission_definitions')
        if not table or not 0 <= kind < 512:
            return None
        row = self.base + table[0] + table[1] * kind
        try:
            pointer, count = self.u64(row + 0x368), self.u32(row + 0x370)
            if not pointer or count > 16:
                return None
            return list(self.read(pointer, count)) if count else []
        except MissionError:  # unreadable: the tags are unknown, never guessed
            return None

    def hellpod(self):
        """The local player's pod (deployment slot 0 when solo): mode 2 = mission selected, state 3 open, 4 taken."""
        system, manager = self.g('hellpod_system'), self.g('hellpod_manager')
        if not system or not manager:
            return None
        out = {'players': [self.u32(system + 32), self.u32(system + 36)], 'pod': self.u32(system + 72984)}
        for i in range(min(self.u32(manager + 8), 4)):
            entity = self.ptr(manager + 104 + 8 * i)
            if entity and self.u32(entity + 8) == out['pod']:
                out.update(mode=self.u32(manager + 136 + 12 * i), state=self.u32(manager + 184 + 20 * i),
                           target=self.u32(manager + 264 + 4 * i))
                return out
        return out

    def presenter(self):
        ui = self.g('ui')
        return (self.u32(ui + 17032 + 12), self.u32(ui + 17032 + 40)) if ui else (None, None)

    def loadout(self):
        """The briefing's loadout screen (top screen type 11): phase 1 intro, 0 idle, 6 launching."""
        menu = self.g('menu')
        if not menu or self.u32(menu) != LOADOUT_SCREEN:
            return None
        screen = self.ptr(menu + 176)
        if not screen:
            return None
        flags, timers = self.read(screen + 10236, 13), self.read(screen + 2570632, 24)
        return {'screen': screen, 'phase': _u32(timers, 4), 'exiting': flags[0], 'aborted': flags[1],
                'host': flags[2], 'launching': flags[12]}

    def _hashed(self, header, key, limit=1048576):
        """Value for key in one of the game's open-addressed maps (8-byte buckets {key, value}), or None."""
        buckets, capacity, empty, multiplier = struct.unpack('<QIII', self.read(header, 20))
        if not (0x10000 <= buckets < 0x800000000000) or not 0 < capacity <= limit or capacity & (capacity - 1):
            return None
        product = (key * multiplier) & 0xFFFFFFFF
        for probe in range(min(capacity, 128)):
            found, value = struct.unpack('<II', self.read(buckets + 8 * ((product + probe) & (capacity - 1)), 8))
            if found == key:
                return None if value == 0xFFFFFFFF else value
            if found == empty:
                return None
        return None

    def avatar(self):
        """The local player's avatar entity id (Ship Station Hotkeys' chain), or None."""
        players, entities = self.g('players'), self.g('entities')
        if not players or not entities:
            return None
        if not (1 <= self.u32(players + 0x84) <= 4 and 1 <= self.u32(players + 0x88) <= 4):
            return None
        record = self.ptr(players + 0xE8)
        if not record or not self.read(record, 24)[20] & 1:
            return None
        ref = self.u32(players + 0x3A8)
        if ref == 0x7FFF:
            return None
        index = self._hashed(entities + 0xF22EC8, ref)
        if index is None or index >= 262144:
            return None
        avatar = self.read(entities + 0xF32F18 + 24 * index, 24)
        if avatar[:8] != bytes.fromhex('97fa4d294d331c4d') or not avatar[20] & 1:
            return None
        return _u32(avatar, 8) or None

    def seater(self, avatar_id):
        """The avatar's seater entity pointer and whether it is free to enter a seat."""
        component = self.g('seaters')
        index = component and self._hashed(component + 32, avatar_id)
        if index is None or index >= self.u32(component + 12):
            return None
        entities, records = self.ptr(component + 56), self.ptr(component + 72)
        entity = entities and self.ptr(entities + 8 * index)
        if not entity or not records or self.u32(entity + 8) != avatar_id:
            return None
        record = self.read(records + 64 * index, 64)
        return {'entity': entity, 'seat': _u32(record, 0), 'free': _u32(record, 0) == 0 and record[48] == 0}

    def control(self):
        """The avatar's spawn-sequence flag: bit 23 of u32 [ctl+0xF8C]; clear = the player has control."""
        avatar = self.avatar()
        if not avatar:
            return None
        avatars = self.g('avatars')
        index = avatars and self._hashed(avatars + 0xF8, avatar, 64)
        if index is None or index >= self.u32(avatars + 0x6C):
            return {'avatar': avatar, 'spawning': None}
        controller = avatars + 0x53D900 + index * 0x1238
        return {'avatar': avatar, 'spawning': bool(self.u32(controller + 0xF8C) & 0x00800000),
                'sequence': self.u32(controller + 0xDEC)}

    def view(self, game_state):
        """What the launch, landing and leave steps wait on, in one dict."""
        out = {'state': game_state}
        ui = self.g('ui')
        if ui:
            out['menu'], out['popup'] = self.u32(ui + 17044), self.u32(ui + 17048)
            words = struct.unpack('<6I', self.read(ui + 0x429C, 24))
            out['stack'] = list(words[:min(words[5], 5)])
        cutscenes = self.g('cutscenes')
        if cutscenes:
            out['cut'], out['cut_queued'] = self.u32(cutscenes + 6412), self.u32(cutscenes + 6416)
        ctx = self.g('ctx')
        if ctx:
            out['transition'] = self.u32(ctx + 0x159CC)
        board = self.g('board')
        if board:
            out['drop'] = self.u8(board + 0x17A2B0)
        mode = self.g('mode')
        if mode:
            out['mission_active'], out['mission_type'] = self.u32(mode + 8), self.u32(mode + 0x40)
        loadout = self.loadout()
        if loadout:
            out['phase'], out['launching'] = loadout['phase'], loadout['launching']
        out['control'] = self.control()
        return out


# Native calls through the agent ---------------------------------------------------------------------------------

LUA_PRELUDE = '''local ffi = require('ffi')
local base = %d
local function u8(a) return ffi.cast('uint8_t *', a)[0] end
local function u32(a) return tonumber(ffi.cast('uint32_t *', a)[0]) end
local function i32(a) return tonumber(ffi.cast('int32_t *', a)[0]) end
local function p(a) return ffi.cast('uint64_t *', a)[0] end
local PROLOGUES = {%s}
for rva, hex in pairs(PROLOGUES) do
  for i = 0, #hex / 2 - 1 do
    if u8(base + rva + i) ~= tonumber(hex:sub(2 * i + 1, 2 * i + 2), 16) then
      error(string.format('game.dll changed at 0x%%x: not the expected function', rva), 0)
    end
  end
end
local G = {%s}
'''


class Natives:
    """The game functions the steps call, run by the agent on the game's main thread (one frame each)."""

    def __init__(self, mailbox, base, offsets, timeout=20):
        self.mailbox, self.base, self.offsets, self.timeout = mailbox, base, offsets, timeout

    def source(self, body, *functions):
        """Lua for one call: prologue checks of the named functions, globals as G.<name>, then body."""
        checks = ', '.join('[%d] = %r' % self.offsets.functions[name] for name in functions)
        names = ', '.join('%s = %d' % item for item in sorted(self.offsets.globals.items()))
        rvas = {name: self.offsets.functions[name][0] for name in self.offsets.functions}
        return LUA_PRELUDE % (self.base, checks, names) + body % rvas

    def call(self, body, *functions):
        return self.mailbox.call(self.source(body, *functions), timeout=self.timeout)

    def board_select(self, ship_planet, view_planet, op, mission, difficulty):
        """The map's commit 0x12D18F0 (op -1 and mission -1: focus a planet). Host-checked in the same frame."""
        return self.call('''
local board, ctx = p(base + G.board), p(base + G.ctx)
if board == 0 or ctx == 0 then return false, 'no board or session' end
if p(board + 0x1F8078) ~= p(ctx + 0xB398) then return false, 'not the host' end
local op, difficulty = %d, %d
if op >= 0 then
  local record = board + 0xF7280 + 92 * op
  if u8(record + 52) == 0 then return false, 'operation record not valid' end
  if u8(record + 32) ~= difficulty then return false, 'operation difficulty differs' end
end
local select = ffi.cast('uint8_t (*)(uint64_t, int32_t, int32_t, int32_t, int32_t, uint32_t, uint8_t, uint64_t)',
  base + %%(board_select)d)
local ok = select(board, %d, %d, op, %d, difficulty, 0, 0)
return tonumber(ok) == 1, i32(board + 0x17A298), i32(board + 0x17A2A4), i32(board + 0x17A2A8),
  u8(board + 0x78EBC), u8(board + 0x78EBD), u32(board + 0x78E8C)
''' % (op, difficulty, ship_planet, view_planet, mission), 'board_select')

    def open_map(self):
        """The Galactic Map (Hologram presenter 15) through the PresenterManager entry, only while no menu is open."""
        return self.call('''
local ui = p(base + G.ui)
if ui == 0 then return false, 'no UI state' end
local manager = ui + 17032
if u32(manager + 12) == 15 then return true, 15, 'already open' end
if u32(manager + 12) ~= 0 or u32(manager + 40) ~= 0 then return false, 'a menu is open', u32(manager + 12) end
ffi.cast('void (*)(uint64_t, int32_t, void *)', base + %(open_presenter)d)(manager, 15, nil)
return u32(manager + 12) == 15, u32(manager + 12)
''', 'open_presenter')

    def enable_scanner(self):
        """The lobby scanner's enabled byte, which the map screen sets when it zooms into a planet (0x1489CF2); the
        scanner's own update then activates and searches the focused planet. Only while the map is on top."""
        return self.call('''
local ui, m = p(base + G.ui), p(base + G.scanner)
if ui == 0 or m == 0 then return false, 'no UI state or scanner' end
if u32(ui + 17032 + 12) ~= 15 then return false, 'the Galactic Map is not open', u32(ui + 17032 + 12) end
ffi.cast('uint8_t *', m + 0x10BD48)[0] = 1
return true
''')

    def open_briefing(self):
        return self.call('''
local ui = p(base + G.ui)
if ui == 0 then return false, 'no UI state' end
local manager = ui + 17032
if u32(manager + 12) ~= 0 or u32(manager + 40) ~= 0 then return false, 'a menu is open', u32(manager + 12) end
ffi.cast('void (*)(void *, uint32_t)', base + %(open_briefing)d)(nil, 0)
return u32(manager + 12) == 14, u32(manager + 12)
''', 'open_briefing')

    def skip_intro(self):
        """Expires the briefing's 2 s intro timer (what Ship Station Hotkeys does)."""
        return self.call('''
local menu = p(base + G.menu)
if menu == 0 or u32(menu) ~= 11 then return false, 'briefing not on top' end
local screen = p(menu + 176)
if screen == 0 then return false, 'no loadout screen' end
if u32(screen + 2570636) ~= 1 then return false, 'intro not running', u32(screen + 2570636) end
ffi.cast('float *', screen + 2570632)[0] = 0
return true
''')

    def seat(self, entity, pod):
        """Instant seat entry into the own pod (Ship Station Hotkeys' F8 path)."""
        return self.call('''
local placement = ffi.new('uint32_t[409]')
placement[406] = %d
local request = ffi.new('uint8_t[80]')
request[0] = 1
ffi.cast('uint32_t **', request + 72)[0] = placement
ffi.cast('void (*)(void *, void *, void *)', base + %%(instant_seat)d)(nil, ffi.cast('void *', %d), request)
return true
''' % (pod, entity), 'instant_seat')

    def drop_confirm(self):
        """The briefing map's own drop confirm at the local cursor (the map centre when untouched)."""
        return self.call('''
local menu = p(base + G.menu)
if menu == 0 or u32(menu) ~= 11 then return false, 'briefing not on top' end
local screen = p(menu + 176)
if screen == 0 or u32(screen + 2570636) ~= 0 then return false, 'briefing not idle' end
ffi.cast('void *(*)(uint64_t)', base + %(drop_confirm)d)(screen + 10888)
local board = p(base + G.board)
local position = ffi.cast('float *', board + 0x17A2B4)
return u8(board + 0x17A2B0) == 1, tonumber(position[0]), tonumber(position[1])
''', 'drop_confirm')

    def drop_at(self, x, y):
        return self.call('''
local xy = ffi.new('float[2]', %r, %r)
ffi.cast('int64_t (*)(void *, uint8_t, uint64_t)', base + %%(drop_set)d)(nil, 1, ffi.cast('uint64_t *', xy)[0])
local board = p(base + G.board)
return u8(board + 0x17A2B0) == 1
''' % (float(x), float(y)), 'drop_set')

    def ready(self):
        """The local ready bit through the game's own guarded setter; the game launches a solo host next frame."""
        return self.call('''
local players = p(base + G.players)
local menu = p(base + G.menu)
if players == 0 or menu == 0 or u32(menu) ~= 11 then return false, 'briefing not on top' end
local screen = p(menu + 176)
if screen == 0 or u32(screen + 2570636) ~= 0 then return false, 'briefing not idle' end
if u8(screen + 10248) ~= 0 or u8(screen + 10237) ~= 0 or u8(screen + 10236) ~= 0 then return false, 'briefing busy' end
local cutscenes = p(base + G.cutscenes)
if cutscenes ~= 0 and u32(cutscenes + 6412) ~= 0 then return false, 'a cutscene is playing' end
ffi.cast('void (*)(uint64_t, uint8_t)', base + %(set_ready)d)(players, 1)
return true, u32(players + 940)
''', 'set_ready')

    def squad_abandon(self):
        """The host's abandon (once): Load screen, transition to the ship, the abandon report."""
        return self.call('''
local game, ctx = p(base + G.game), p(base + G.ctx)
if game == 0 or ctx == 0 then return false, 'no game or session' end
if u32(game + 0xAC21C) ~= 4 then return false, 'not in a mission' end
local host = p(ctx + 0xB3A8)
if host ~= 0 and host ~= p(ctx + 0xB398) then return false, 'not the host' end
if u32(ctx + 0x159CC) ~= 0 then return false, 'a transition is running' end
ffi.cast('int64_t (*)(void)', base + %(squad_abandon)d)()
return true
''', 'squad_abandon')


# Steps ----------------------------------------------------------------------------------------------------------

def wait_until(read, done, timeout, interval=0.25):
    """Polls read() until done(value); returns (True, value, seconds) or (False, last value, seconds)."""
    started = time.monotonic()
    value = read()
    while not done(value):
        if time.monotonic() - started > timeout:
            return False, value, round(time.monotonic() - started, 2)
        time.sleep(interval)
        value = read()
    return True, value, round(time.monotonic() - started, 2)


class Deployer:
    """The steps, on a Probe-like reader and a Natives-like caller (tests pass fakes)."""

    def __init__(self, probe, natives, game_state, note=print, shoot=None):
        self.probe, self.natives, self.game_state, self.note = probe, natives, game_state, note
        self.shoot = shoot or (lambda name: None)

    def view(self):
        return self.probe.view(self.game_state())

    def require(self, values, step):
        if not values or values[0] is not True:
            raise MissionError('%s refused: %s' % (step, values))
        return values

    def focus(self, board, planet):
        """The galaxy view on planet, so its operations exist on the board (one planet's at a time)."""
        if board['op_array_planet'] != planet or not any(o['planet'] == planet for o in self.probe.operations()):
            self.require(self.natives.board_select(board['request_ship_planet'], planet, -1, -1, 0),
                         'focusing planet %s' % planet)
            ok, _, _ = wait_until(self.probe.operations, lambda ops: any(o['planet'] == planet for o in ops), 10)
            if not ok:
                raise MissionError('no operations were generated for planet %s' % planet)
            board['op_array_planet'] = planet

    def planet_with_tag(self, board, campaigns, owners, faction, difficulty, tag):
        """The faction's first campaign planet with an open mission at the difficulty whose definition carries the
        region tag (city missions exist only on planets with an open city region; 2026-10-13). Each candidate is
        focused in the galaxy view (no operation is created)."""
        race = FACTIONS[faction]
        tried = []
        for candidate in campaigns:
            if {1: 2}.get(owners.get(candidate), owners.get(candidate)) != race:
                continue
            self.focus(board, candidate)
            tried.append(candidate)
            op, _ = pick_mission(self.probe.operations(), self.probe.mission_record, candidate, difficulty,
                                 tags_of=self.probe.mission_tags, tag=tag)
            if op:
                self.note('planet %s has open missions with region tag %s (tried %s)' % (candidate, tag, tried))
                return candidate
        raise MissionError('no %s campaign planet has an open D%s mission with region tag %s (tried %s)' % (
            faction, difficulty, tag, tried))

    def select(self, difficulty=1, planet=None, faction=None, pick=None, mission_pick=None, region_tag=None):
        board = self.probe.board()
        if not board.get('ready') or not board.get('host') or board.get('war_loaded') != 1 \
                or board.get('hosting') != 1:
            raise MissionError('selection needs the host of a loaded war table: %s' % board)
        pod = self.probe.hellpod() or {}
        if pod.get('players') != [1, 1]:
            raise MissionError('solo sessions only (deployment players %s)' % pod.get('players'))
        index = None
        if board['active_op']:
            planet, difficulty, index = board['active_planet'], board['active_difficulty'], board['active_op_index']
            self.note('continuing the active operation %s (planet %s, D%s)' % (board['active_op_id'], planet,
                                                                               difficulty))
        else:
            campaigns = self.probe.campaign_planets()
            owners = self.probe.planet_owners(campaigns) if faction else None
            if faction:
                race = FACTIONS[faction]
                self.note('campaign planets of %s: %s' % (faction, [c for c in campaigns if
                                                                    {1: 2}.get(owners.get(c), owners.get(c)) == race]))
            if faction and region_tag is not None and planet is None and pick is None:
                planet = self.planet_with_tag(board, campaigns, owners, faction, difficulty, region_tag)
            else:
                planet = choose_planet(board, campaigns, owners, planet, faction, pick)
        self.focus(board, planet)
        tags_of = self.probe.mission_tags  # None when unknown: then no mission matches a region tag
        opened = open_missions(self.probe.operations(), self.probe.mission_record, planet, difficulty, index)
        self.note('open missions at D%s on planet %s (mission/type/region tags): %s' % (
            difficulty, planet, ', '.join('%s/%s/%s' % (r['mission'], r['type'], tags_of(r['type'])) for _, r in opened)))
        op, record = pick_mission(self.probe.operations(), self.probe.mission_record, planet, difficulty, index,
                                  mission_pick, tags_of, region_tag)
        if op:
            self.note('picked mission %s (type %s, region tags %s)' % (record['mission'], record['type'],
                                                                     tags_of(record['type'])))
        if not op:
            raise MissionError('no open mission at D%s on planet %s%s%s' % (
                difficulty, planet, ' with region tag %s' % region_tag if region_tag is not None else '',
                ' in the active operation (finish or abandon it by hand)' if index is not None
                else ''))
        if faction and op['race'] != FACTIONS[faction]:
            raise MissionError('planet %s has %s operations, not %s' % (planet, op['race'], faction))
        values = self.require(self.natives.board_select(planet, planet, op['index'], record['mission'], difficulty),
                              'the mission commit')
        # Travelling to another planet delays mode 2 (18 s live vs 2-3 s on the ship's own planet).
        ok, pod, seconds = wait_until(self.probe.hellpod, lambda p: (p or {}).get('mode') == POD_MISSION_READY, 45)
        if not ok:
            raise MissionError('the hellpod never got the mission (mode %s)' % (pod or {}).get('mode'))
        ok, pod, _ = wait_until(self.probe.hellpod, lambda p: (p or {}).get('state') == POD_OPEN
                                and (p or {}).get('target') == POD_OPEN, 30)
        if not ok:
            raise MissionError('the hellpod did not open (%s)' % pod)
        board = self.probe.board()
        return {'planet': planet, 'difficulty': difficulty, 'operation': board['active_op_id'],
                'operation_index': op['index'], 'mission': record['mission'], 'mission_type': record['type'],
                'region_tags': tags_of(record['type']),
                'race': op['race'], 'continued': index is not None, 'commit': values, 'pod_ready_s': seconds}

    def brief_and_seat(self):
        self.require(self.natives.open_briefing(), 'opening the briefing')
        skipped, deadline = False, time.monotonic() + 10
        while time.monotonic() < deadline:
            loadout = self.probe.loadout()
            phase = loadout and loadout['phase']
            if phase == 1 and not skipped:
                skipped = bool((self.natives.skip_intro() or [False])[0])
            elif phase == 0:
                break
            time.sleep(0.05)
        else:
            raise MissionError('the briefing never became idle')
        avatar = self.probe.avatar()
        seater = avatar and self.probe.seater(avatar)
        pod = self.probe.hellpod() or {}
        if not seater or not seater['free'] or pod.get('state') != POD_OPEN:
            raise MissionError('cannot seat: seater %s, pod %s' % (seater, pod))
        self.require(self.natives.seat(seater['entity'], pod['pod']), 'the seat')
        ok, pod, _ = wait_until(self.probe.hellpod, lambda p: (p or {}).get('state') == POD_OCCUPIED, 5)
        if not ok:
            raise MissionError('the pod was not taken (%s)' % pod)
        self.shoot('briefing')
        return {'intro_skipped': skipped}

    def drop(self):
        values = self.natives.drop_confirm()
        if not values or values[0] is not True:
            self.note('drop confirm gave %s; dropping at the map centre' % (values,))
            values = self.require(self.natives.drop_at(0.0, 0.0), 'the drop zone')
        return {'drop': values}

    def launch(self, load_timeout=300):
        self.require(self.natives.ready(), 'ready')
        ok, view, seconds = wait_until(self.view, lambda v: v.get('state') in (PREPARE_MISSION, MISSION), 60)
        if not ok:
            raise MissionError('the launch did not start (%s)' % view)
        ok, view, load = wait_until(self.view, lambda v: v.get('state') == MISSION, load_timeout)
        if not ok:
            raise MissionError('the mission did not load within %d s (%s)' % (load_timeout, view))
        return {'launch_s': seconds, 'load_s': load}

    def wait_control(self, timeout=180):
        ok, view, seconds = wait_until(self.view, in_control, timeout)
        if not ok:
            raise MissionError('no control within %d s (%s)' % (timeout, view))
        stable = 0
        for _ in range(4):  # held about 1 s
            time.sleep(0.25)
            stable += in_control(self.view())
        if stable < 4:
            raise MissionError('control did not hold (%d/4 polls)' % stable)
        self.shoot('landed')
        return {'control_s': seconds, 'mission_type': view.get('mission_type')}

    def leave(self, load_timeout=300, settle=10):
        """From state 6 (loading) or 4 back to the ship, through the host abandon."""
        view = self.view()
        if view.get('state') == PREPARE_MISSION:
            ok, view, _ = wait_until(self.view, lambda v: v.get('state') == MISSION and v.get('transition') == 0,
                                     load_timeout)
            if not ok:
                raise MissionError('the mission never finished loading (%s)' % view)
        if view.get('state') == MISSION:
            wait_until(self.view, lambda v: v.get('transition') == 0, 30)
            self.require(self.natives.squad_abandon(), 'the abandon')
        elif view.get('state') != SHIP:
            raise MissionError('not in a mission or on the ship (state %s)' % view.get('state'))
        ok, view, seconds = wait_until(self.view, lambda v: v.get('state') == SHIP and v.get('transition') == 0,
                                       load_timeout)
        if not ok:
            raise MissionError('did not reach the ship (%s)' % view)
        time.sleep(settle)
        self.shoot('ship-after')
        return {'to_ship_s': seconds}


# Session commands -----------------------------------------------------------------------------------------------

def _context(paths, manifest, note, shoot=None):
    from . import session  # session imports this module for stop
    build = builds.KNOWN.get((manifest.get('facts') or {}).get('build') or '')
    if build is None or build.mission is None:
        raise MissionError('no mission offsets for this game build')
    if not manifest.get('agent'):
        raise MissionError('missions need the agent (any condition but vanilla)')
    pid = manifest.get('pid')
    if not pid or pid not in session.running():
        raise MissionError('the game is not running')
    base = session.game_dll_base(pid, build)
    if not base:
        raise MissionError('game.dll is not mapped yet')
    reader = winapi.Reader(pid)
    state_of = lambda: session.live_reads(pid, build).get('state')
    deployer = Deployer(Probe(reader, base, build.mission), Natives(Mailbox(paths.mailbox), base, build.mission),
                        state_of, note=note, shoot=shoot)
    return session, build, reader, deployer


def _record(paths, entry):
    state = State(paths.state)
    missions = list((state.load() or {}).get('missions') or [])
    if missions and missions[-1].get('started') == entry.get('started'):
        missions[-1] = entry
    else:
        missions.append(entry)
    state.update(missions=missions)


def deploy(paths, manifest, difficulty=1, planet=None, faction=None, note=print, shoot=None, load_timeout=300,
           control_timeout=180, pick=None, mission_pick=None, region_tag=None):
    """From the ship (navigating there first if needed) into a mission, until the player has control.

    Every step is recorded in state/active.json `missions` (and run.json) as it completes."""
    from . import navigate
    session, build, reader, deployer = _context(paths, manifest, note, shoot)
    entry = {'started': _now(), 'steps': {},
             'asked': {'difficulty': difficulty, 'planet': planet, 'faction': faction, 'pick': pick,
                       'mission_pick': mission_pick, 'region_tag': region_tag}}
    try:
        reads = session.live_reads(manifest['pid'], build)
        if reads.get('state') in (MISSION, PREPARE_MISSION):
            raise MissionError('already in a mission')
        if reads.get('state') != SHIP:
            navigate.to_ship(paths, manifest, note=note)
            reads = session.live_reads(manifest['pid'], build)
        # Invite Only, unless a HellForge session opted in to Public (session._check_public allowed it).
        wanted = PUBLIC if manifest.get('privacy') == 'public' and manifest.get('target') == 'hellforge' else INVITE_ONLY
        if reads.get('privacy') != wanted:
            raise MissionError('the lobby is not %s (%s)' % (builds.PRIVACY[wanted], reads.get('privacy_name')))
        _record(paths, entry)
        for name, step in (('select', lambda: deployer.select(difficulty, planet, faction, pick, mission_pick,
                                                              region_tag)),
                           ('briefing', deployer.brief_and_seat), ('drop', deployer.drop),
                           ('launch', lambda: deployer.launch(load_timeout)),
                           ('control', lambda: deployer.wait_control(control_timeout))):
            started = time.monotonic()
            note('mission: %s' % name)
            result = step()
            entry['steps'][name] = dict(result, seconds=round(time.monotonic() - started, 1))
            if name == 'select':
                entry.update({k: result[k] for k in ('planet', 'difficulty', 'operation', 'mission', 'race')})
            if name == 'launch':
                entry['reached_mission'] = _now()
                State(paths.state).update(in_mission=True)
            _record(paths, entry)
        entry['in_control'] = _now()
        _record(paths, entry)
        note('mission: in control (operation %s, planet %s, D%s)' % (entry.get('operation'), entry.get('planet'),
                                                                    entry.get('difficulty')))
        return entry
    except MissionError as error:
        entry['error'] = str(error)
        _record(paths, entry)
        raise
    finally:
        reader.close()


def leave(paths, manifest, note=print, shoot=None, settle=10):
    """Back to the ship from a mission (or its loading), through the host abandon; nothing to do on the ship."""
    session, build, reader, deployer = _context(paths, manifest, note, shoot)
    try:
        note('mission: leaving')
        result = deployer.leave(settle=settle)
        state = State(paths.state)
        current = state.load() or {}
        missions = list(current.get('missions') or [])
        if missions and not missions[-1].get('left'):
            missions[-1] = dict(missions[-1], left=_now(), leave=result)
        state.update(missions=missions, in_mission=False)
        note('mission: back on the ship')
        return dict(result, state=SHIP)
    finally:
        reader.close()
