"""Known game builds: fingerprints, the offsets the harness reads, crash signatures."""
from dataclasses import dataclass, field
import hashlib

LUA_OUT_OF_MEMORY = 0xE24C4A04
ACCESS_VIOLATION = 0xC0000005


@dataclass(frozen=True)
class Signature:
    module: str
    offset: int
    code: int | None   # None matches any exception code
    kind: str
    meaning: str


@dataclass(frozen=True)
class MissionOffsets:
    """game.dll globals and functions for ship -> mission -> ship (hd2h/mission.py).

    globals: name -> RVA of a pointer. functions: name -> (RVA, prologue bytes as hex); every call checks the
    prologue in the running game first.
    """
    globals: dict
    functions: dict
    # Static tables: name -> (RVA, row stride). mission_definitions: rows indexed by a mission record's type; region
    # tags at row +0x368 (pointer) and +0x370 (count), matched against the planet's map variants by the generator.
    tables: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Build:
    build: str
    exe_sha256: str
    dll_sha256: str
    exe_timestamp: int          # PE TimeDateStamp, also in every minidump's module list
    dll_timestamp: int          # game.dll's; how the harness finds game.dll in the running game
    mode_rva: int               # game.dll: [mode]+8 == 1 in a mission (frame probe, ECS)
    game_rva: int               # game.dll: [game] = the game object (Game::change_state's `this`)
    privacy_offset: int         # game+: lobby privacy setting
    state_offset: int = 0       # game+: current game state index (see STATES)
    title_offset: int = 0       # game+: StateTitleScreen object
    title_continue_rva: int = 0  # game.dll: StateTitleScreen "a key was pressed" (sets +17/+19)
    signatures: tuple = field(default_factory=tuple)
    mission: MissionOffsets | None = None


KNOWN = {
    '25480438': Build(
        build='25480438',
        exe_sha256='F5FEE03DCFDB2E553A4752C283590950AC13316B376D8196AA556FF0400D5F06',
        dll_sha256='2E2C3B7C2500646DADD5F2B4C6E0504DBB7E7896139F64CDDC0D1813C718F51E',
        exe_timestamp=0x6AB382E4,
        dll_timestamp=0x6AB3B43F,
        mode_rva=0x33266A0,
        game_rva=0x3326340,
        privacy_offset=0xAC550,
        # Game::change_state 0xab2fa0 switches on [game+0xAC21C]; title state at game+0x11E8
        # (on_enter 0xad5950, update 0xad7cb0, on_exit 0xad8df0); 0xad6a90 = continue pressed.
        state_offset=0xAC21C,
        title_offset=0x11E8,
        title_continue_rva=0xAD6A90,
        signatures=(
            Signature('helldivers2.exe', 0x6C07A6, ACCESS_VIOLATION, 'gpu-watchdog',
                      'renderer GPU fence watchdog (vanilla engine)'),
            Signature('helldivers2.exe', 0x6F9394, None, 'render-stale-texture',
                      'render thread used a stale texture handle'),
            Signature('helldivers2.exe', 0x5D7384, None, 'dump-writer-after-tdr',
                      "the game's dump writer after a GPU TDR"),
            Signature('helldivers2.exe', 0x5F5C33, None, 'unit-unloader-assert',
                      'engine assert in the unit resource unloader; seen with Lobby Manager v0.4 development '
                      'kicks'),
            Signature('game.dll', 0x17FF734, None, 'mom-row-reset-alignment',
                      'Mod Options Menu development build: row_reset on an 8-byte-aligned buffer, fixed before '
                      'release'),
            Signature('helldivers2.exe', 0xC948A, ACCESS_VIOLATION, 'quit-during-boot',
                      'window closed during startup: the engine UI teardown reads a null object; vanilla, no mod '
                      'frames'),
            Signature('helldivers2.exe', 0x626296, ACCESS_VIOLATION, 'quit-render-task-pool',
                      'window closed after play: the render task pool runs out during teardown; vanilla, no mod '
                      'frames'),
            Signature('helldivers2.exe', 0x5B7A43, ACCESS_VIOLATION, 'quit-freed-object',
                      'quit after play with a weapon a mod registered: the engine shutdown reads a scratch allocator '
                      'given back early (cmp [rbx+50h]); the field lists of an added network type were freed twice (fixed '
                      'in HellForge PR #64)'),
            Signature('game.dll', 0x5B3746, ACCESS_VIOLATION, 'set-destination-removed-entity',
                      'set_destination (game 0x5B36C0) called for an entity whose navigation record was removed: '
                      'a test mod ordered a squad killed or disbanded mid-trial (2026-10-14; it checks the record '
                      'first since then)'),
        ),
        # Live-verified on this build (2026-10-13), from the title screen to a mission and back.
        mission=MissionOffsets(
            globals={
                'game': 0x3326340,            # game object: state +0xAC21C (same as game_rva)
                'board': 0x347CEE8,           # galactic war board (operations, selection)
                'ctx': 0x347CEF0,             # session context (host, hosting, transition)
                'ui': 0x347CE28,              # UI state: screen stack +0x429C, presenter manager +17032
                'menu': 0x347CE38,            # menu system: top screen type +0, loadout screen +176
                'players': 0x3326468,         # player manager: local ready flags +940
                'entities': 0x346BF98,        # entity manager: entity map +0xF22EC8, records +0xF32F18
                'seaters': 0x3326D78,         # seater component
                'avatars': 0x3326D20,         # avatar manager: map +0xF8, controllers +0x53D900
                'hellpod_system': 0x33265F0,  # deployment system: players +32/+36, own pod +72984
                'hellpod_manager': 0x3326428,  # pods: entity +104, mode +136, state +184, target +264
                'cutscenes': 0x346D530,       # cutscene manager: current +6412, queued +6416
                'mode': 0x33266A0,            # mission mode: active +8, type +0x40
                'backend': 0x347CEE0,         # backend client: ready +0x702F8 / +0x702FC
                # lobby scanner of the map's planet view: enabled
                # +0x10BD48, active +0x10BD49, last result count +0x10BD38, recharge seconds (float) +0x10BD74
                'scanner': 0x347CE80,
            },
            functions={
                # (board, ship planet, view planet, op, mission, difficulty, drop flag, drop xy) -> u8, host only
                'board_select': (0x12D18F0, '4053565741564883ec58488b05'),
                'open_briefing': (0x661CF0, '48894c2408574883ec404c8b0d'),    # (nil, 0): presenter 14
                'instant_seat': (0x639830, '48895c24204889542410565741544155'),  # (nil, entity, request)
                'drop_confirm': (0x18C0BC0, '48895c2408564881ec90000000488bf1'),  # (map widget = S+10888)
                'drop_set': (0x12D1E30, '48895c24084889742410574883ec4048'),  # (nil, 1, packed xy)
                'set_ready': (0x607230, '83b98800000001722b488b81e8000000'),  # (players, 1): flags |= 8
                'squad_abandon': (0x18FB720, '4055488dac2440eeffffb8c0120000e8'),  # (): to the ship, report
                # PresenterManager entry (manager = [ui]+17032, presenter, nil); 15 = Hologram, the Galactic Map
                # (Ship Station Hotkeys calls it the same way)
                'open_presenter': (0x14C0350, '48895c24084889742410574883ec208b'),
            },
            # Region tags (0xF71220 -> region selector 0x1758D40), decoded from a game.dll capture: Automaton
            # types 35-40 carry only tag 3 (planet 158's megacity operations).
            tables={'mission_definitions': (0x3773420, 896)})),
}

PRIVACY = {0: 'Public', 1: 'Friends Only', 2: 'Invite Only', 3: 'Friends and Clan'}
# Game::change_state cases (build 25480438): 5 leads to the ship (0xac5b00), 6 into a mission.
STATES = {1: 'splash', 2: 'title', 3: 'ship', 4: 'mission', 5: 'prepare-ship', 6: 'prepare-mission'}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest().upper()


def identify(paths):
    """(Build or None, exe sha256, game.dll sha256) of the installed game."""
    exe, dll = sha256(paths.exe), sha256(paths.game_dll)
    for build in KNOWN.values():
        if build.exe_sha256 == exe and build.dll_sha256 == dll:
            return build, exe, dll
    return None, exe, dll


def by_exe_timestamp(timestamp):
    for build in KNOWN.values():
        if build.exe_timestamp == timestamp:
            return build
    return None
