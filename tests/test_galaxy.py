"""The Galactic Map's planet view (hd2h/galaxy.py) against a fake game: map, war board and lobby scanner."""
import struct
import unittest

import support  # noqa: F401
from hd2h import galaxy, mission

SCANNER, UI = 0x10000000, 0x20000000


class FakeMap:
    """Probe and Natives at once: the presenter opens the map, a focus starts the scanner, the scanner recharges."""

    def __init__(self, scanner_starts=True, menu=0):
        self.memory, self.calls, self.scanner_starts = {}, [], scanner_starts
        self.set_u32(UI + 17032 + 12, menu)
        self.set_u32(UI + 17032 + 40, 0)
        self.recharge, self.results = 0.0, 0
        self.memory[SCANNER + galaxy.SCANNER_ENABLED] = 0
        self.memory[SCANNER + galaxy.SCANNER_ACTIVE] = 0
        self.focused = None

    def set_u32(self, address, value):
        for i, byte in enumerate(struct.pack('<I', value)):
            self.memory[address + i] = byte

    # Probe -----------------------------------------------------------------------------------------------------
    def g(self, name):
        return {'scanner': SCANNER, 'ui': UI}[name]

    def read(self, address, size):
        if address == SCANNER + galaxy.SCANNER_RECHARGE:
            return struct.pack('<f', self.recharge)
        return bytes(self.memory.get(address + i, 0) for i in range(size))

    def u8(self, address):
        return self.read(address, 1)[0]

    def u32(self, address):
        return struct.unpack('<I', self.read(address, 4))[0]

    def i32(self, address):
        if address == SCANNER + galaxy.SCANNER_COUNT:
            return self.results
        return struct.unpack('<i', self.read(address, 4))[0]

    def board(self):
        return {'ready': True, 'war_loaded': 1, 'request_ship_planet': 5, 'sel_planet': None}

    def campaign_planets(self):
        return [64, 261]

    def planet_owners(self, planets):
        return {64: 3, 261: 2}

    # Natives ---------------------------------------------------------------------------------------------------
    def open_map(self):
        self.calls.append('open_map')
        if self.u32(UI + 17032 + 12) not in (0, galaxy.HOLOGRAM):
            return [False, 'a menu is open', self.u32(UI + 17032 + 12)]
        self.set_u32(UI + 17032 + 12, galaxy.HOLOGRAM)
        return [True, galaxy.HOLOGRAM]

    def board_select(self, ship, view, op, mission_id, difficulty):
        self.calls.append(('board_select', ship, view, op, mission_id, difficulty))
        self.focused = view
        return [True]

    def enable_scanner(self):
        self.calls.append('enable_scanner')
        if self.u32(UI + 17032 + 12) != galaxy.HOLOGRAM:
            return [False, 'the Galactic Map is not open']
        self.memory[SCANNER + galaxy.SCANNER_ENABLED] = 1
        if self.scanner_starts and self.focused is not None:  # the game's update activates it on the focused planet
            self.memory[SCANNER + galaxy.SCANNER_ACTIVE] = 1
        return [True]


def deployer(fake):
    notes = []
    return mission.Deployer(fake, fake, lambda: 3, note=notes.append), notes


class PlanetView(unittest.TestCase):
    def test_opens_the_map_and_focuses_a_planet_without_an_operation(self):
        fake = FakeMap()
        steps, _ = deployer(fake)
        opened = galaxy.open_planet_view(steps, faction='terminid', sleep=lambda s: None)
        self.assertEqual(fake.calls, ['open_map', ('board_select', 5, 261, -1, -1, 0), 'enable_scanner'],
                         'the ship planet stays, the view moves, no operation or mission is committed')
        self.assertEqual((opened['planet'], opened['presenter']), (261, galaxy.HOLOGRAM))
        self.assertEqual(opened['scanner']['active'], 1)

    def test_a_busy_menu_is_refused(self):
        fake = FakeMap(menu=14)
        steps, _ = deployer(fake)
        with self.assertRaises(mission.MissionError):
            galaxy.open_planet_view(steps, sleep=lambda s: None)
        self.assertEqual(fake.calls, ['open_map'], 'no planet is focused when the map did not open')

    def test_a_scanner_that_never_starts_is_reported(self):
        fake = FakeMap(scanner_starts=False)
        steps, _ = deployer(fake)
        with self.assertRaises(mission.MissionError) as raised:
            galaxy.open_planet_view(steps, planet=64, timeout=0.05, sleep=lambda s: None)
        self.assertIn('never became active', str(raised.exception))

    def test_an_unknown_planet_is_refused(self):
        steps, _ = deployer(FakeMap())
        with self.assertRaises(mission.MissionError):
            galaxy.open_planet_view(steps, planet=7, sleep=lambda s: None)


class Watch(unittest.TestCase):
    def test_each_recharge_restart_counts_as_a_scan(self):
        fake = FakeMap()
        sequence = iter([(5.0, 0), (3.0, 0), (0.5, 0), (20.0, 0), (18.0, 0), (19.9, 3), (17.0, 3)])

        def step(seconds):
            try:
                fake.recharge, fake.results = next(sequence)
            except StopIteration:
                pass
        fake.recharge = 6.0
        notes = []
        watched = galaxy.watch_scanner(fake, 0.2, note=notes.append, sleep=step, poll=0)
        self.assertEqual([s['results'] for s in watched['scans']], [0, 3])
        self.assertEqual(len(notes), 2)


if __name__ == '__main__':
    unittest.main()
