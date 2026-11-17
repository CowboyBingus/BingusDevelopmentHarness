"""Is the session still playable, and a watchdog that says when it stops being so.

A scripted test drives a mission for minutes; when the Helldiver dies without coming back, the mission ends, or the
game drops to a menu, every later step waits out its own timeout against a session that can no longer do anything.
`playable` answers once from the same external reads the mission steps wait on (mission.in_control); `guard` polls it
and returns as soon as the session has been unplayable for `grace` seconds in a row (after it was playable once), or
the game exited, so the test can stop and move on instead of sitting at a menu.
"""
import time

from . import mission
from .mission import MISSION, PREPARE_MISSION, SHIP, PREPARE_SHIP, MissionError


def _reason(view):
    """Why a view is not in control, in words (the first failing condition of mission.in_control)."""
    state = view.get('state')
    if state != MISSION:
        names = {SHIP: 'on the ship', PREPARE_SHIP: 'loading the ship', PREPARE_MISSION: 'loading a mission'}
        return 'not in a mission (%s; state %s, menu %s)' % (names.get(state, 'unknown state'), state, view.get('menu'))
    if not view.get('mission_active'):
        return 'the mission is over (menu %s)' % view.get('menu')
    if view.get('transition'):
        return 'in a transition (%s)' % view.get('transition')
    if view.get('cut') or view.get('cut_queued'):
        return 'in a cutscene'
    if view.get('menu') in mission.LOAD_SCREENS or any(s in mission.LOAD_SCREENS for s in view.get('stack') or []):
        return 'on a loading screen (menu %s)' % view.get('menu')
    control = view.get('control')
    if not control:
        return 'no local Helldiver (dead and not reinforced, or not spawned; menu %s)' % view.get('menu')
    if control.get('spawning') is not False:
        return 'the Helldiver is spawning or being reinforced'
    return 'not in control'


def _summary(view):
    control = view.get('control') or {}
    return {k: view.get(k) for k in ('state', 'mission_active', 'menu', 'popup', 'stack', 'transition', 'cut')} | \
        {'avatar': control.get('avatar'), 'spawning': control.get('spawning')}


def playable(paths, manifest, note=print):
    """{'playable': bool, 'reason': str, 'view': {...}}; 'the game exited' when it is gone."""
    try:
        _, _, _, deployer = mission._context(paths, manifest, note)
        view = deployer.view()
    except MissionError as error:
        return {'playable': False, 'reason': str(error), 'view': None}
    ok = mission.in_control(view)
    return {'playable': ok, 'reason': 'in control' if ok else _reason(view), 'view': _summary(view)}


def guard(paths, manifest, grace=45.0, interval=5.0, arm_timeout=900.0, note=print):
    """Poll `playable` until the session has been unplayable for `grace` seconds after it was playable once (or never
    became playable within `arm_timeout`), or the game exited. Returns the verdict with how long it watched."""
    started, armed, bad_since, last = time.monotonic(), False, None, None
    while True:
        last = playable(paths, manifest, note)
        now = time.monotonic()
        gone = last['view'] is None and ('exited' in last['reason'] or 'not running' in last['reason'])
        if gone:
            return {'verdict': 'exited', 'reason': last['reason'], 'watched_s': round(now - started)}
        if last['playable']:
            if not armed:
                note('guard: playable; watching (grace %d s)' % grace)
            armed, bad_since = True, None
        elif armed:
            if bad_since is None:
                bad_since = now
                note('guard: not playable: %s' % last['reason'])
            if now - bad_since >= grace:
                return {'verdict': 'stalled', 'reason': last['reason'], 'unplayable_s': round(now - bad_since),
                        'watched_s': round(now - started), 'view': last['view']}
        elif now - started >= arm_timeout:
            return {'verdict': 'never playable', 'reason': last['reason'], 'watched_s': round(now - started),
                    'view': last['view']}
        time.sleep(interval)


# ---- the session watchdog (user 2026-11-07: "The harness failed to catch a stalled run. Fix the harness.") ----------
# A held session driven step by step (no script-level guard) sat 9 minutes at a menu after the Helldiver died: every
# later `lua` / `input` call ran against a mission that was over. So the harness itself watches every mission it
# deploys: `mission` starts a detached `guard --mark --stop` for the session; when the session stalls (or the game
# exits) the watchdog writes a stall mark (guard-stall.json, keyed to the session) and, with --stop, ends the game; from
# then on `lua` and `input` refuse at once (exit 3, the reason) instead of acting on a dead session. `start` and `stop`
# clear the mark and end the watchdog. HD2H_GUARD=off leaves a deployed mission unwatched.
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


def _mark_path(paths):
    return paths.harness / 'guard-stall.json'


def _pid_path(paths):
    return paths.harness / 'guard.pid'


def mark(paths, manifest, verdict):
    record = dict(verdict, session=(manifest or {}).get('session'), at=time.strftime('%Y-%m-%d %H:%M:%S'))
    _mark_path(paths).write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


def stall_mark(paths, manifest):
    """The stall mark of this session, or None."""
    try:
        record = json.loads(_mark_path(paths).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if record.get('session') == (manifest or {}).get('session') else None


def clear(paths):
    try:
        _mark_path(paths).unlink()
    except OSError:
        pass


def end_watchdog(paths):
    """Ends a running watchdog (a deliberate stop must not be reported as a stall)."""
    try:
        pid = int(_pid_path(paths).read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        _pid_path(paths).unlink()
    except OSError:
        pass


def forget_pid(paths):
    """The watchdog's own exit: its pid file goes (a later end_watchdog must not signal a reused pid)."""
    try:
        if int(_pid_path(paths).read_text().strip()) == os.getpid():
            _pid_path(paths).unlink()
    except (OSError, ValueError):
        pass


def start_watchdog(paths, grace=45.0, note=print):
    """Starts the detached watchdog for the launched session (after a deploy reached control)."""
    if os.environ.get('HD2H_GUARD', '').lower() in ('0', 'off', 'no'):
        note('guard: HD2H_GUARD=off: this mission is not watched')
        return None
    end_watchdog(paths)
    clear(paths)
    script = Path(__file__).resolve().parent.parent / 'harness.py'
    log = open(paths.harness / 'guard.log', 'a', encoding='utf-8')
    process = subprocess.Popen([sys.executable, str(script), 'guard', '--grace', str(grace), '--mark', '--stop'],
                               cwd=str(script.parent), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                               start_new_session=True)
    _pid_path(paths).write_text(str(process.pid))
    note('guard: watching this mission (pid %d, grace %d s); a stall ends the game and later steps refuse' % (process.pid, grace))
    return process.pid
