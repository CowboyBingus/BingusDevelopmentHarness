"""Moving the game between screens without input: title screen -> ship.

The title screen waits for any key (StateTitleScreen::update 0xad7cb0). Its "a key was pressed"
function (0xad6a90) sets the continuing flags; the game's own update then checks it is ready,
fades out and starts the session, and the session moves the game to prepare-ship (state 5) and
the ship (state 3). The agent calls that one function; the harness watches the state from outside.
"""
import time

from . import builds, session
from .mailbox import MailboxError
from .state import State

# Runs inside the game through the agent. Guards: the game object exists, the game is on the
# title screen (state 2), the title has finished loading (+324), and continue was not pressed (+17).
CONTINUE_LUA = '''local ffi = require('ffi')
local base, game_rva, state_offset, title_offset, continue_rva = %d, %d, %d, %d, %d
local game = ffi.cast('uint64_t *', base + game_rva)[0]
if game == 0 then return false, 'no game object yet' end
local state = tonumber(ffi.cast('uint32_t *', game + state_offset)[0])
if state ~= 2 then return false, 'not on the title screen (state ' .. state .. ')' end
local title = game + title_offset
if ffi.cast('uint8_t *', title + 324)[0] == 0 then return false, 'title screen still loading' end
if ffi.cast('uint8_t *', title + 17)[0] ~= 0 then return true, 'continue already pressed' end
ffi.cast('void (*)(uint64_t)', base + continue_rva)(title)
return true, 'continue pressed'
'''


class NavigationError(RuntimeError):
    pass


def continue_source(build, base):
    if not build.title_continue_rva:
        raise NavigationError('no title-screen offsets for build %s' % build.build)
    return CONTINUE_LUA % (base, build.game_rva, build.state_offset, build.title_offset, build.title_continue_rva)


def to_ship(paths, manifest, note=print, press_timeout=180, ship_timeout=300, settle=5):
    """Presses continue on the title screen and waits until the game reports the ship (state 3)."""
    build = builds.KNOWN.get((manifest.get('facts') or {}).get('build') or '')
    if build is None:
        raise NavigationError('unknown game build: no navigation offsets')
    if not manifest.get('agent'):
        raise NavigationError('navigation needs the agent (mods condition)')
    pid, mailbox = manifest['pid'], session.mailbox_for(paths, manifest)
    deadline, pressed, last = time.monotonic() + press_timeout, False, None
    while time.monotonic() < deadline:
        reads = session.live_reads(pid, build)
        if reads.get('state') == 3:
            note('already on the ship')
            State(paths.state).update(reached_ship=session._now())
            return reads
        if pid not in session.running():
            raise NavigationError('the game exited while navigating')
        base = session.game_dll_base(pid, build)
        if base:
            try:
                values = mailbox.call(continue_source(build, base), timeout=20)
            except MailboxError as error:
                values = [False, str(error)]
            if values and values[0] is True:
                note('title screen: %s' % values[1])
                pressed = True
                break
            if values != last:
                note('title screen: %s' % (values[1] if len(values) > 1 else values))
                last = values
        time.sleep(2)
    if not pressed:
        raise NavigationError('could not press continue within %d s' % press_timeout)
    deadline, seen = time.monotonic() + ship_timeout, None
    while time.monotonic() < deadline:
        if pid not in session.running():
            raise NavigationError('the game exited on the way to the ship')
        reads = session.live_reads(pid, build)
        if reads.get('state_name') != seen:
            seen = reads.get('state_name')
            note('game state: %s' % seen)
        if reads.get('state') == 3:
            time.sleep(settle)
            reads = session.live_reads(pid, build)
            if reads.get('state') == 3:
                State(paths.state).update(reached_ship=session._now())
            return reads
        time.sleep(1)
    raise NavigationError('the game did not reach the ship within %d s' % ship_timeout)
