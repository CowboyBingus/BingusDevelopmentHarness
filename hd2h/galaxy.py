"""The Galactic Map's planet view without input, for watching the lobby scanner.

The map's planet view runs a lobby search every recharge (20 s from the server config, 10 s compiled in): the game
shows the lobbies it finds on the planet. This module opens it and watches the scanner:

- Opening: the Hologram presenter (15) through the PresenterManager entry, only while no menu is open, as Ship
  Station Hotkeys v1.0 does.
- The planet: the war board's own commit with no operation (board_select(ship planet, planet, -1, -1, 0)), as the
  mission steps focus a planet (no operation is created). The map view follows it ([[game+0x3326AA0]+0x55F628]).
- The scanner: its enabled byte (+0x10BD48), which the map screen sets when it zooms into a planet (0x1489CF2), set
  by the agent. That is the only data write; the scanner's own per-frame update (0x13374B0) then activates on the
  focused planet and searches it every recharge with (war id, planet index) (live 2026-10-22: two
  PFMultiplayerFindLobbies per scan, every ~10 s on a HellForge copy).
- Closing is not supported yet: open the map last in a session and let `stop` quit from it. A deploy after a map
  opened this way crashed in 2026-09 (the presenter skips the war table's own interaction), and `mission` refuses
  while a menu is open.

Reads are external (ReadProcessMemory); the agent makes the two native calls, each after a prologue check, and the
one write.
"""
import struct
import time

from .mission import MissionError, _context, choose_planet

HOLOGRAM = 15
SCANNER_ENABLED, SCANNER_ACTIVE, SCANNER_COUNT, SCANNER_RECHARGE = 0x10BD48, 0x10BD49, 0x10BD38, 0x10BD74


def scanner(probe):
    """The lobby scanner's fields: enabled, active (planet view open), last result count, recharge seconds."""
    m = probe.g('scanner')
    if not m:
        return None
    raw = probe.read(m + SCANNER_RECHARGE, 4)
    return {'enabled': probe.u8(m + SCANNER_ENABLED), 'active': probe.u8(m + SCANNER_ACTIVE),
            'results': probe.i32(m + SCANNER_COUNT),
            'recharge': round(struct.unpack('<f', raw)[0], 2) if raw else None}


def presenter(probe):
    """The menu presenter on top (15 = the Galactic Map) and the menu depth."""
    ui = probe.g('ui')
    if not ui:
        return None, None
    return probe.u32(ui + 17032 + 12), probe.u32(ui + 17032 + 40)


def open_planet_view(deployer, planet=None, faction=None, pick=None, timeout=20.0, sleep=time.sleep):
    """Opens the map and focuses a campaign planet until the scanner is active. Returns what it did."""
    probe, natives, note = deployer.probe, deployer.natives, deployer.note
    board = probe.board()
    if not board.get('ready') or board.get('war_loaded') != 1:
        raise MissionError('the war table is not loaded: %s' % board)
    deployer.require(natives.open_map(), 'opening the Galactic Map')
    campaigns = probe.campaign_planets()
    owners = probe.planet_owners(campaigns) if faction else None
    planet = choose_planet(board, campaigns, owners, planet, faction, pick)
    deployer.require(natives.board_select(board['request_ship_planet'], planet, -1, -1, 0),
                     'focusing planet %s' % planet)
    if not (scanner(probe) or {}).get('enabled'):
        deployer.require(natives.enable_scanner(), 'enabling the lobby scanner')
    note('Galactic Map open on planet %s; waiting for the lobby scanner' % planet)
    deadline, state = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        state = scanner(probe)
        if state and state['active']:
            return {'planet': planet, 'presenter': presenter(probe)[0], 'scanner': state}
        sleep(0.25)
    raise MissionError('the lobby scanner never became active on planet %s (%s, presenter %s)'
                       % (planet, state, presenter(probe)[0]))


def watch_scanner(probe, seconds, note=print, sleep=time.sleep, poll=0.25):
    """Samples the scanner for a while. A scan restarts the recharge countdown: each jump up is counted as one."""
    scans, samples, last = [], [], None
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        state = scanner(probe)
        if state:
            samples.append(state)
            if last and state['recharge'] is not None and last['recharge'] is not None \
                    and state['recharge'] > last['recharge'] + 1:
                scans.append({'at_s': round(seconds - (end - time.monotonic()), 1), 'results': state['results'],
                              'recharge': state['recharge']})
                note('lobby scan %d: %d results, next in %.0f s' % (len(scans), state['results'], state['recharge']))
            last = state
        sleep(poll)
    return {'scans': scans, 'last': last, 'samples': len(samples)}


def planet_view(paths, manifest, planet=None, faction=None, pick=None, watch=0, note=print):
    """`map`: open the Galactic Map's planet view in a running session and (watch > 0) watch its lobby scans."""
    _, _, reader, deployer = _context(paths, manifest, note)
    try:
        opened = open_planet_view(deployer, planet, faction, pick)
        if watch:
            opened['watch'] = watch_scanner(deployer.probe, watch, note)
        return opened
    finally:
        reader.close()
