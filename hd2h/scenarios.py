"""Scenarios built on sessions: a short smoke test and the background soak."""
import datetime
import json
from pathlib import Path
import time

from . import builds, capture, mission, navigate, session, settings, winapi
from .mailbox import Mailbox, MailboxError
from .state import State

STUCK_STATUSES = ('loading',)
FAILED_STATUSES = ('load failed', 'lookup failed')
# Words in a mod's own status line that mean it stopped or refused to run.
BAD_MOD_WORDS = ('error', 'failed', 'stopped', 'unsupported', 'disabled', 'restore_failed')
# Units in the busiest world: the title's worlds hold one unit each, the ship's main world thousands.
SHIP_UNITS_LUA = ('local best = 0 for _, w in ipairs(stingray.Application.worlds()) do '
                  'local ok, n = pcall(stingray.World.num_units, w) if ok and n > best then best = n end end '
                  'return best')


def _stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def wait_ready(mailbox, pid, timeout=600, settle=20, note=print):
    """Polls agent.state() until no loader module is still loading, then waits `settle` seconds."""
    deadline, last_frames = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        if pid not in session.running():
            raise session.HarnessError('the game exited while waiting for it to settle')
        try:
            state = mailbox.call('return agent.state()', timeout=20)[0]
        except MailboxError as error:
            note('waiting: %s' % error)
            time.sleep(2)
            continue
        modules = ((state.get('loader') or {}).get('modules')) or {}
        loading = [name for name, status in modules.items() if status in STUCK_STATUSES]
        advancing = last_frames is not None and state.get('frames', 0) > last_frames
        last_frames = state.get('frames', 0)
        if modules and not loading and advancing:
            note('loader settled (%d modules); waiting %d s' % (len(modules), settle))
            time.sleep(settle)
            return mailbox.call('return agent.state()', timeout=20)[0]
        time.sleep(2)
    raise session.HarnessError('the game did not settle within %d s' % timeout)


def _check(checks, name, passed, detail=None):
    checks.append({'check': name, 'passed': bool(passed), 'detail': detail})


def mod_problems(state):
    """Known mod globals whose status line says they stopped or refused to run."""
    problems = {}
    for name, value in ((state or {}).get('mods') or {}).items():
        status = value.get('status') if isinstance(value, dict) else None
        if isinstance(status, str) and any(word in status.lower() for word in BAD_MOD_WORDS):
            problems[name] = status
    return problems


def _shot(paths, name, note):
    """A capture into the run's shots/, or None when the window cannot be captured."""
    try:
        return capture.run_shot(paths, name)
    except (capture.CaptureError, winapi.WinError, OSError) as error:
        note('capture %s failed: %s' % (name, error))
        return None


MISSION_ERRORS = (mission.MissionError, MailboxError, navigate.NavigationError, session.HarnessError,
                  winapi.WinError)


def _mission_round_trip(paths, manifest, mailbox, build, checks, note, difficulty=1, planet=None, faction=None):
    """Ship -> mission (in control) -> health checks and captures there -> ship. Adds six checks."""
    shoot = lambda name: _shot(paths, name, note)
    try:
        entry = mission.deploy(paths, manifest, difficulty=difficulty, planet=planet, faction=faction, note=note,
                               shoot=shoot)
    except MISSION_ERRORS as error:
        _check(checks, 'deployed into a mission and got control', False, str(error))
        return
    _check(checks, 'deployed into a mission and got control', True,
           {key: entry.get(key) for key in ('operation', 'planet', 'difficulty', 'mission', 'race')})
    time.sleep(5)
    shoot('mission-5s')
    reads = session.live_reads(manifest['pid'], build)
    in_mission = mailbox.call('return agent.state()', timeout=20)[0]
    _check(checks, 'lobby is Invite Only in the mission', reads.get('privacy') == 2, reads)
    _check(checks, 'no mod reports a stop or failure in the mission', not mod_problems(in_mission),
           mod_problems(in_mission) or None)
    shots = (State(paths.state).load() or {}).get('shots') or []
    seen = [s for s in shots if s.get('name') in ('landed', 'mission-5s')]
    # Two captures: one can be a bright sky or cloud that scores like the gray no-world frame.
    _check(checks, 'a mission capture shows the 3D world', any(s.get('world') for s in seen),
           [{key: s.get(key) for key in ('name', 'saturation', 'detail', 'file')} for s in seen])
    try:
        left = mission.leave(paths, manifest, note=note, shoot=shoot)
    except MISSION_ERRORS as error:
        _check(checks, 'back on the ship after the mission', False, str(error))
        return
    _check(checks, 'back on the ship after the mission', left.get('state') == 3, left)
    after = [s for s in (State(paths.state).load() or {}).get('shots') or [] if s.get('name') == 'ship-after']
    _check(checks, 'ship capture after the mission shows the 3D world', bool(after) and after[-1].get('world'),
           after[-1] if after else None)


def smoke(paths, preset='light', settle=20, note=print, take_over=True, ship=True, packages=(), mission_run=False,
          difficulty=1, planet=None, faction=None, multiplayer=None, war=None, privacy='invite'):
    """Launch with the agent, settle, snapshot health (on the ship if asked), quit, collect, restore.

    mission_run: after the ship checks, deploy into a mission, check health and captures there, and come back
    (real account traffic; see hd2h/mission.py; on the HellForge target, the local backend's).
    multiplayer, war, privacy='public': HellForge target only, see session.start."""
    manifest = session.start(paths, preset=preset, condition='packages' if packages else 'mods', agent=True,
                             note=note, take_over=take_over, packages=packages, multiplayer=multiplayer, war=war,
                             privacy=privacy,
                             run_dir=paths.runs / ('%s-smoke%s%s' % (_stamp(), '-mission' if mission_run else
                                                                     '-ship' if ship else '',
                                                                     '-hellforge' if paths.target == 'hellforge'
                                                                     else '')))
    run_dir, checks = Path(manifest['run']), []
    expected = int(settings.PRIVACY[privacy])
    wanted = builds.PRIVACY[expected]
    build = builds.KNOWN.get(manifest['facts'].get('build') or '')
    mailbox, snapshot, reads, ship_reads = Mailbox(paths.mailbox), None, None, None
    try:
        snapshot = wait_ready(mailbox, manifest['pid'], settle=settle, note=note)
        reads = session.live_reads(manifest['pid'], build)
        modules = (snapshot.get('loader') or {}).get('modules') or {}
        failed = {name: status for name, status in modules.items()
                  if any(status.startswith(prefix) for prefix in FAILED_STATUSES)}
        _check(checks, 'agent connected', True)
        _check(checks, 'no loader module failed', not failed, failed or None)
        _check(checks, 'agent loaded through discovery', modules.get('mods/cowboybingus/bingus_test_agent') == 'loaded')
        # No lobby exists before the ship loads, so an unreadable privacy value is not a failure;
        # any readable value other than Invite Only is.
        _check(checks, 'lobby is %s (or no lobby yet)' % wanted, reads.get('privacy') in (expected, None), reads)
        _check(checks, 'not in a mission', reads.get('mission') in (0, None), reads)
        if ship:
            ship_reads = navigate.to_ship(paths, manifest, note=note)
            units = mailbox.call(SHIP_UNITS_LUA, timeout=20)[0]
            on_ship = mailbox.call('return agent.state()', timeout=20)[0]
            snapshot = {'title': snapshot, 'ship': on_ship, 'ship_units': units}
            _check(checks, 'reached the ship', ship_reads.get('state') == 3, ship_reads)
            _check(checks, 'ship scene loaded (busiest world > 500 units)', (units or 0) > 500, units)
            _check(checks, 'lobby is %s on the ship' % wanted, ship_reads.get('privacy') == expected, ship_reads)
            _check(checks, 'no mod reports a stop or failure', not mod_problems(on_ship),
                   mod_problems(on_ship) or None)
            if mission_run:
                _mission_round_trip(paths, manifest, mailbox, build, checks, note, difficulty, planet, faction)
    except (session.HarnessError, MailboxError, navigate.NavigationError) as error:
        _check(checks, 'game settled with the agent answering', False, str(error))
    finally:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / 'snapshot.json').write_text(json.dumps({'state': snapshot, 'live': reads, 'ship': ship_reads},
                                                          indent=2), encoding='utf-8')
        summary = session.stop(paths, note=note)
    _check(checks, 'quit through the engine', summary['exit'].get('how') == 'quit', summary['exit'])
    _check(checks, 'agent saw shutdown (bye)', summary['agent_bye'])
    _check(checks, 'no crash dump', not summary['dumps'], summary['dumps'] or None)
    _check(checks, 'install and settings restored', not summary['restore_problems'],
           summary['restore_problems'] or None)
    if summary.get('hellforge') is not None:
        _check(checks, 'HellForge session ended as observed', summary['hellforge'].get('status') == 'observed',
               summary['hellforge'])
    verdict = {'scenario': 'smoke', 'target': paths.target, 'ship': ship, 'mission': mission_run,
               'passed': all(c['passed'] for c in checks), 'checks': checks, 'run': str(run_dir)}
    (run_dir / 'verdict.json').write_text(json.dumps(verdict, indent=2), encoding='utf-8')
    return verdict


def _monitor(paths, manifest, minutes, note, heartbeat_limit=90, poll=5):
    """Watches one soak cycle. Returns (outcome, seconds observed): outcome is 'completed',
    'exited', 'hang' or 'public-lobby'."""
    pid, agent = manifest['pid'], bool(manifest.get('agent'))
    build = builds.KNOWN.get(manifest['facts'].get('build') or '')
    mailbox, started = Mailbox(paths.mailbox), time.monotonic()
    end = started + minutes * 60
    with winapi.Process(pid) as process:
        while time.monotonic() < end:
            if process.wait(poll):
                State(paths.state).update(exit={'code': process.exit_code(), 'how': None,
                                                'at': session._now()})
                return 'exited', time.monotonic() - started
            reads = session.live_reads(pid, build)
            if reads.get('privacy') == 0:
                note('lobby privacy reads Public: ending the cycle')
                return 'public-lobby', time.monotonic() - started
            if agent:
                age = mailbox.age('heartbeat')
                if age is not None and age > heartbeat_limit:
                    note('no agent heartbeat for %.0f s: treating as a hang' % age)
                    return 'hang', time.monotonic() - started
    return 'completed', time.monotonic() - started


def cycle_order(conditions, cycles):
    """ABBA order for two conditions (time-of-day drift cancels), round-robin otherwise."""
    if len(conditions) == 2:
        pattern = (0, 1, 1, 0)
        return [conditions[pattern[i % 4]] for i in range(cycles)]
    return [conditions[i % len(conditions)] for i in range(cycles)]


def soak(paths, conditions=('mods',), minutes=30, cycles=4, preset='private', end='graceful', note=print,
         take_over=True, where='title'):
    """Repeated background sessions; each cycle's outcome and any crash signature go to soak.jsonl.

    where='ship' navigates each cycle with an agent to the ship before the clock starts (vanilla has
    no agent and stays on the title screen). The summary gives crashes per observed hour per condition.
    """
    if isinstance(conditions, str):
        conditions = (conditions,)
    folder = paths.runs / ('%s-soak-%s-%s' % (_stamp(), '-'.join(conditions), where))
    folder.mkdir(parents=True, exist_ok=True)
    results, navigation_failures = [], 0
    winapi.keep_awake(True)
    try:
        for index, condition in enumerate(cycle_order(list(conditions), cycles), start=1):
            note('cycle %d/%d (%s, %s, %g min)' % (index, cycles, condition, where, minutes))
            agent = condition != 'vanilla'
            manifest = session.start(paths, preset=preset, condition=condition, agent=agent, note=note,
                                     take_over=take_over, run_dir=folder / ('cycle-%02d-%s' % (index, condition)))
            location, outcome, seconds = 'title', None, 0.0
            if where == 'ship' and agent:
                try:
                    navigate.to_ship(paths, manifest, note=note)
                    location, navigation_failures = 'ship', 0
                except navigate.NavigationError as error:
                    note('navigation failed: %s' % error)
                    navigation_failures += 1
                    outcome = 'exited' if manifest['pid'] not in session.running() else 'navigation-failed'
            if outcome is None:
                outcome, seconds = _monitor(paths, manifest, minutes, note)
            if outcome == 'exited':
                time.sleep(3)  # the crash handler may still be closing its dump
                summary = session.finish(paths, note=note)
            else:
                # graceful: the agent's engine quit, or WM_CLOSE without an agent; force only as a fallback.
                # A public lobby or a hung game is terminated at once instead.
                urgent = outcome in ('public-lobby', 'hang')
                summary = session.stop(paths, kill=(end == 'kill' or urgent), note=note)
            record = {'cycle': index, 'condition': condition, 'where': location, 'minutes': minutes,
                      'observed_s': round(seconds, 1), 'monitor': outcome, 'outcome': summary['outcome'],
                      'exit': summary['exit'], 'dumps': summary['dumps'],
                      'restore_problems': summary['restore_problems'], 'started': summary['started'],
                      'finished': summary['finished']}
            results.append(record)
            with open(folder / 'soak.jsonl', 'a', encoding='utf-8') as file:
                file.write(json.dumps(record) + '\n')
            if summary['restore_problems']:
                note('stopping the soak: the install did not restore cleanly')
                break
            if outcome == 'public-lobby':
                note('stopping the soak: the lobby was Public')
                break
            if navigation_failures >= 2:
                note('stopping the soak: navigation to the ship failed twice in a row')
                break
            time.sleep(10)
    finally:
        winapi.keep_awake(False)
    per_condition = {}
    for condition in conditions:
        mine = [r for r in results if r['condition'] == condition]
        crashes = [r for r in mine if r['outcome'] == 'crash']
        hours = sum(r['observed_s'] for r in mine) / 3600
        per_condition[condition] = {
            'cycles': len(mine), 'observed_hours': round(hours, 2), 'crashes': len(crashes),
            'crashes_per_hour': round(len(crashes) / hours, 3) if hours else None,
            'signatures': [d.get('kind') for r in crashes for d in r['dumps']],
            'on_ship': sum(1 for r in mine if r['where'] == 'ship')}
    summary = {'conditions': list(conditions), 'where': where, 'cycles': len(results), 'minutes_each': minutes,
               'per_condition': per_condition, 'folder': str(folder)}
    (folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary
