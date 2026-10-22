"""Shared paths and runners for the BingusDevelopmentHarness tests.

Optional inputs come from environment variables (see README.md, Tests). Tests that need one that is missing skip.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

HARNESS = Path(__file__).resolve().parents[1]
TESTS = HARNESS / 'tests'
# The folder holding the sibling checkouts some defaults below point into (the harness's parent folder).
WORKSPACE = Path(os.environ.get('HD2H_WORKSPACE', HARNESS.parent))


def _optional(variable, default=None):
    """The path in the environment variable, else the default; None when neither is given."""
    value = os.environ.get(variable)
    if value:
        return Path(value)
    return Path(default) if default else None


def _luajit():
    """HD2_LUAJIT, else a LuaJIT build in the sibling tools/ folder, else luajit on PATH."""
    built = WORKSPACE / 'tools' / 'src' / 'LuaJIT' / 'src' / 'luajit.exe'
    found = os.environ.get('HD2_LUAJIT') or (str(built) if built.exists() else shutil.which('luajit'))
    return Path(found or built)


LUAJIT = _luajit()
# A runner that executes a Lua script in the game's lua51.dll: `python <runner> <script.lua> [args]`.
GAME_LUA = _optional('HD2_GAME_LUA', WORKSPACE / 'PerformanceBaseline' / 'game_lua.py')
GAME_DLL = Path(os.environ.get('HD2_LUA51_DLL',
                               r'C:\Program Files (x86)\Steam\steamapps\common\Helldivers 2\bin\lua51.dll'))
# A Bingus Shared Loader checkout (its scripts/archive.py), and a checkout of its published v18 release (its
# src/discover.lua).
LOADER_CHECKOUT = _optional('BINGUS_SHARED_LOADER', WORKSPACE / 'BingusSharedLoader')
PUBLISHED_LOADER = _optional('HD2H_PUBLISHED_LOADER')
# Captured data, never part of the repository: the window captures of a mission deploy (deploy3/ and deploy4/), and
# build 25480438's game.dll image as mapped in the running game (file offset = RVA).
CAPTURES = _optional('HD2H_CAPTURES')
GAME_DLL_IMAGE = _optional('HD2H_GAME_DLL_IMAGE')

sys.path.insert(0, str(HARNESS))
# Scratch folders the Lua VMs write into. A luajit.exe that runs at low integrity gets "Permission denied" for any
# file under %TEMP% (Python itself can write there), so they live in the harness (gitignored).
LUA_TMP = HARNESS / '.test-tmp'


def present(path):
    """True when an optional input is given and exists."""
    return path is not None and Path(path).exists()


def lua_tmp():
    """A temporary directory every Lua VM can write to."""
    import tempfile
    LUA_TMP.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=LUA_TMP)


def runners():
    """(label, command prefix) for every Lua VM available: LuaJIT and the game's lua51.dll."""
    found = []
    if LUAJIT.exists():
        found.append(('LuaJIT', [str(LUAJIT)]))
    if present(GAME_LUA) and GAME_DLL.exists():
        found.append(("game's lua51.dll", [sys.executable, '-B', str(GAME_LUA)]))
    return found


def run_lua(prefix, script, *args, timeout=120):
    return subprocess.run(prefix + [str(script)] + [str(a) for a in args], capture_output=True, text=True,
                          timeout=timeout)


def popen_lua(prefix, script, *args):
    return subprocess.Popen(prefix + [str(script)] + [str(a) for a in args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
