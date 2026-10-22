"""Where the game, its settings and the CowboyBingus folders live.

Two targets: `steam`, the Steam install (the default), and `hellforge`, a HellForge installation's game copy
(hd2h/hellforge.py). A HellForge session runs the copy with APPDATA and LOCALAPPDATA pointed at
<root>/userdata/environment/{Roaming,Local}, so the agent's mailbox and the mod logs live there, and keeps the
game's settings and saves in <root>/userdata/profiles/<profile>.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path

STEAM_APP_ID = 553850
GAME_EXE = 'helldivers2.exe'
DEFAULT_GAME = Path(r'C:\Program Files (x86)\Steam\steamapps\common\Helldivers 2')
DEFAULT_HELLFORGE = Path(r'C:\HellForge')
HARNESS = Path(__file__).resolve().parents[1]
TARGETS = ('steam', 'hellforge')


@dataclass(frozen=True)
class Paths:
    game: Path          # install root (bin/, data/)
    appdata: Path       # %APPDATA%: the game's settings and dumps
    localappdata: Path  # %LOCALAPPDATA%: mod logs and the agent mailbox
    harness: Path       # this folder: state/ and runs/
    target: str = 'steam'
    hellforge: Path | None = None  # the HellForge installation root (target hellforge)
    profile: str = 'default'       # the HellForge profile a session plays (target hellforge)

    @classmethod
    def from_environment(cls, target=None, hellforge_root=None, profile=None):
        """target/hellforge_root/profile override HD2H_TARGET, HD2H_HELLFORGE_ROOT and HD2H_HELLFORGE_PROFILE."""
        target = target or os.environ.get('HD2H_TARGET') or 'steam'
        harness = Path(os.environ.get('HD2H_HOME', HARNESS))
        if target not in TARGETS:
            raise ValueError('target must be one of %s' % ', '.join(TARGETS))
        if target == 'steam':
            return cls(Path(os.environ.get('HD2_GAME_ROOT', DEFAULT_GAME)), Path(os.environ['APPDATA']),
                       Path(os.environ['LOCALAPPDATA']), harness)
        return cls.for_hellforge(Path(hellforge_root or os.environ.get('HD2H_HELLFORGE_ROOT') or DEFAULT_HELLFORGE),
                                 harness, profile or os.environ.get('HD2H_HELLFORGE_PROFILE'))

    @classmethod
    def for_hellforge(cls, root, harness, profile=None):
        """The paths of a HellForge installation; the profile defaults to the launcher's selected one."""
        root = Path(root)
        if not profile:
            try:
                profile = json.loads((root / 'launcher-settings.json').read_text(encoding='utf-8-sig')).get(
                    'selectedProfile')
            except (OSError, ValueError):
                profile = None
        environment = root / 'userdata' / 'environment'
        return cls(root / 'game', environment / 'Roaming', environment / 'Local', Path(harness), 'hellforge', root,
                   profile or 'default')

    @property
    def exe(self):
        return self.game / 'bin' / GAME_EXE

    @property
    def game_dll(self):
        return self.game / 'data' / 'game' / 'game.dll'

    @property
    def data(self):
        return self.game / 'data'

    @property
    def settings_root(self):
        if self.target == 'hellforge':
            return self.hellforge / 'userdata' / 'profiles' / self.profile
        return self.appdata / 'Arrowhead' / 'Helldivers2'

    @property
    def settings_file(self):
        return self.settings_root / 'user_settings.config'

    @property
    def saves(self):
        return self.settings_root / 'saves'

    @property
    def dumps(self):
        if self.target == 'hellforge':
            return self.hellforge / 'userdata' / 'dumps'
        return self.settings_root / 'dumps'

    @property
    def bingus(self):
        return self.localappdata / 'CowboyBingus' / 'Helldivers2'

    @property
    def logs(self):
        return self.bingus / 'Logs'

    @property
    def mailbox(self):
        return self.bingus / 'TestAgent'

    @property
    def state(self):
        return self.harness / 'state'

    @property
    def runs(self):
        return self.harness / 'runs'

    @property
    def agent_source(self):
        return HARNESS / 'agent' / 'bingus_test_agent.lua'

    # HellForge installation (target hellforge)

    @property
    def mods(self):
        return self.hellforge / 'mods'

    @property
    def session_runner(self):
        return self.hellforge / 'app' / 'session' / 'HellForge.Session.exe'

    @property
    def hellforge_profile(self):
        """The HellForge profile folder (backend progress), which the session runner takes as --profile."""
        return self.hellforge / 'profiles' / self.profile

    @property
    def play_session(self):
        """logs/play-session.json: the run folder of the installation's latest session."""
        return self.hellforge / 'logs' / 'play-session.json'
