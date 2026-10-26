"""Mods changed in a running HellForge session without a restart (HellForge's live mod sync, docs/mods.md "Changing
mods while you play"): what the launcher's Mods page does while a session runs, driven by the harness.

- setup (game closed): copies mod folders into <root>/mods and sets which mods are on for the next session, as the Mods
  page's settings (mods/hellforge-mods.json) say. Every copied file's SHA-256 and the settings' original bytes are
  recorded in state/live-mods/<installation>.json before anything is written; teardown removes the folders (only while
  every file is still the harness's) and puts the settings back byte for byte. `start` then backs up the settings setup wrote and
  puts them back at `stop`, before teardown.
- change (session running): switches mods in the settings, as a switch on the Mods page does, and asks the session's
  backend to make the change (POST /hellforge/live-mods; GET previews it). The backend's address is the run's
  result.json "backendOrigin". The answer comes back with what the backend (backend/live-mods.log), the loader
  (game/modloader.log) and every mod's own log (game/mod-<id>.log) wrote meanwhile, and whether the game still runs.
- scenario: a JSON file of changes with the answers they must give, run in order; one verdict per step.
- pair_sync: mod sync between two installations on one PC (hd2h/pair.py): the joiner, whose mods differ, joins the
  host's lobby, accepts the MOD SYNC offer and takes the host's mods live with APPLY AND JOIN, answered as the launcher
  answers (a file the backend reads, no input to any window); then the rejoin is watched in both backends' logs, with
  captures of the joiner's window (which take no focus) at every stage.

During a session HellForge holds every mod file read-only (ModsHold), so a change can only switch mods (or reorder
them); a new version of a mod comes through mod sync (mods/.synced/), which the pair harness drives.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error
import urllib.request

from . import hellforge, session
from .paths import HARNESS

RECORDS = HARNESS / 'state' / 'live-mods'
SETTINGS = hellforge.MODS_SETTINGS
MANIFEST = 'hellforge-mod.json'
ENDPOINT = '/hellforge/live-mods'
# Building a change's source mods and its hot package may take minutes (the backend's PrepareTimeout is 5 minutes, and
# a change with game data then waits up to 120 s for the game).
REQUEST_TIMEOUT = 480
ANSWER_KEYS = ('live', 'applied', 'changes', 'blocking', 'detail', 'digest')


class LiveModsError(RuntimeError):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def _record_file(root):
    """state/live-mods/<the installation's path as a name>.json"""
    return RECORDS / (re.sub(r'[^A-Za-z0-9]+', '-', str(root)).strip('-') + '.json')


def _record(root, update=None, clear=False):
    path = _record_file(root)
    if clear:
        path.unlink(missing_ok=True)
        return {}
    data = _json(path) or {}
    if update:
        data.update(update)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding='utf-8')
    return data


def mod_id(folder):
    """The id in a mod folder's manifest; raises LiveModsError when it has none."""
    data = _json(Path(folder) / MANIFEST)
    if not data or not isinstance(data.get('id'), str):
        raise LiveModsError('%s has no %s with an id' % (folder, MANIFEST))
    return data['id']


def installed_ids(paths):
    """{mod id: folder name} of the installation's mod folders with a manifest (packs have none)."""
    found = {}
    for folder in sorted(Path(paths.mods).iterdir()) if Path(paths.mods).is_dir() else []:
        if folder.is_dir() and not folder.name.startswith('.') and (folder / MANIFEST).exists():
            try:
                found[mod_id(folder)] = folder.name
            except LiveModsError:
                pass
    return found


def settings(paths):
    """The Mods page's settings (mods/hellforge-mods.json), or a fresh one."""
    return _json(Path(paths.mods) / SETTINGS) or {'format': 1, 'disabled': [], 'order': []}


def write_settings(paths, data):
    path = Path(paths.mods) / SETTINGS
    temporary = path.with_suffix('.harness-tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def switch(data, on=(), off=()):
    """The settings with these mods on and off (by id: the Mods page keeps a list of the disabled ones)."""
    disabled = [mod for mod in data.get('disabled') or [] if mod not in set(on)]
    disabled += [mod for mod in off if mod not in disabled]
    return dict(data, disabled=disabled)


def _busy(paths):
    if hellforge.copy_games(paths) or any(str(paths.hellforge).lower() in str(r).lower() for r in hellforge.runners()):
        return True
    return bool(hellforge.runners())


def setup(paths, folders=(), on=(), off=()):
    """Copies the mod folders into <root>/mods and sets the next session's switches: the copied mods are off unless named
    in `on`, the installation's own named in `off` are switched off. Returns the record."""
    if _record(paths.hellforge).get('settings'):
        raise LiveModsError('a live mods setup is still recorded in %s: run `livemods.py teardown` first'
                            % _record_file(paths.hellforge))
    if _busy(paths):
        raise LiveModsError('a HellForge session or game is running; end it first')
    folders = [Path(f) for f in folders]
    ids = {folder: mod_id(folder) for folder in folders}
    installed = installed_ids(paths)
    for folder, identity in ids.items():
        if (Path(paths.mods) / folder.name).exists():
            raise LiveModsError('mods/%s already exists' % folder.name)
        if identity in installed:
            raise LiveModsError('%s: the installation has %s already (mods/%s)' % (folder, identity, installed[identity]))
    path = Path(paths.mods) / SETTINGS
    original = path.read_bytes() if path.exists() else None
    backup = _record_file(paths.hellforge).with_suffix('.settings.backup')
    if original is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        if _sha(backup.read_bytes()) != _sha(original):
            raise LiveModsError('backup of %s did not verify' % path)
    copies = {folder.name: {p.relative_to(folder).as_posix(): _sha(p.read_bytes()) for p in folder.rglob('*') if p.is_file()}
              for folder in folders}
    record = _record(paths.hellforge, {'root': str(paths.hellforge), 'copies': copies, 'settings': {
        'path': str(path), 'backup': str(backup) if original is not None else None,
        'original_sha256': _sha(original) if original is not None else None}})
    for folder in folders:
        shutil.copytree(folder, Path(paths.mods) / folder.name)
    data = switch(settings(paths), on=on, off=[i for i in ids.values() if i not in set(on)] + list(off))
    write_settings(paths, data)
    return dict(record, disabled=data['disabled'])


def teardown(paths):
    """Removes the copied folders while every file is the harness's and puts the settings back. Returns problems."""
    record = _record(paths.hellforge)
    if not record.get('settings'):
        return []
    if _busy(paths):
        raise LiveModsError('a HellForge session or game is running; end it first')
    problems = hellforge.remove_packs(paths, record.get('copies'))
    entry = record['settings']
    path = Path(entry['path'])
    if entry.get('original_sha256') is None:
        path.unlink(missing_ok=True)
    else:
        backup = Path(entry['backup'])
        if not backup.exists() or _sha(backup.read_bytes()) != entry['original_sha256']:
            problems.append('mods/%s: backup missing or changed' % SETTINGS)
        else:
            shutil.copyfile(backup, path)
            if _sha(path.read_bytes()) != entry['original_sha256']:
                problems.append('mods/%s: restore did not verify' % SETTINGS)
            else:
                backup.unlink()
    if not problems:
        _record(paths.hellforge, clear=True)
    return problems


def run_folder(manifest):
    run = manifest.get('hellforge_run')
    if not run or not Path(run).is_dir():
        raise LiveModsError('the session has no HellForge run folder yet')
    return Path(run)


def backend(run):
    """The session backend's origin, once result.json names it."""
    origin = (hellforge.result(run) or {}).get('backendOrigin')
    if not origin:
        raise LiveModsError('the session has no backend yet (result.json has no backendOrigin)')
    return origin.rstrip('/')


def ask(origin, preview=False, timeout=REQUEST_TIMEOUT):
    """The backend's answer to POST (make the Mods page's change) or GET (preview it), as a dict."""
    request = urllib.request.Request(origin + ENDPOINT, data=None if preview else b'{}', method='GET' if preview else 'POST',
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as reply:
            status, body = reply.status, reply.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        raise LiveModsError('the backend did not answer %s %s: %s' % ('GET' if preview else 'POST', ENDPOINT, error))
    try:
        answer = json.loads(body.decode('utf-8'))
    except ValueError:
        answer = {'body': body.decode('utf-8', errors='replace')[:2000]}
    if not isinstance(answer, dict):
        answer = {'body': answer}
    answer['http_status'] = status
    answer['seconds'] = round(time.monotonic() - started, 2)
    return answer


class Logs:
    """Where each of the run's logs stood, so a change's new lines can be told apart."""

    def __init__(self, run):
        self.run = Path(run)
        self.marks = {name: self._size(path) for name, path in self._files().items()}

    def _files(self):
        # The game's logs are in the run folder, or in its game/ folder when the session is sandboxed.
        game = self.run / 'game' if (self.run / 'game' / 'modloader.log').exists() else self.run
        files = {'live-mods': self.run / 'backend' / 'live-mods.log', 'modloader': game / 'modloader.log'}
        for log in sorted(game.glob('mod-*.log')):
            files['mod:' + log.stem[4:]] = log
        return files

    @staticmethod
    def _size(path):
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def new_lines(self):
        """{log name: [lines written since]} for every log with new lines."""
        lines = {}
        for name, path in self._files().items():
            start = self.marks.get(name, 0)
            try:
                with open(path, 'rb') as handle:
                    handle.seek(start)
                    text = handle.read().decode('utf-8', errors='replace')
            except OSError:
                continue
            if text.strip():
                lines[name] = text.splitlines()
        return lines


def session_order(run):
    """The ids the session runs in load order, from its session-mods.json (written at the start and after every
    change)."""
    data = _json(Path(run) / 'session-mods.json') or {}
    mods = data.get('mods') or data.get('Mods') or []
    return [m.get('id') or m.get('Id') for m in mods if isinstance(m, dict)]


def session_mods(run):
    return sorted(session_order(run))


def alive(manifest):
    return manifest.get('pid') in session.running()


def change(paths, manifest, on=(), off=(), order=None, preview=False, settle=1.0, requests=1):
    """Switches mods (and sets the order when given; 'reverse': the session's load order reversed), asks the backend,
    and returns what happened. requests > 1 sends that many POSTs at once (the backend makes one change at a time)."""
    run = run_folder(manifest)
    origin = backend(run)
    logs = Logs(run)
    before = settings(paths)
    data = switch(before, on, off)
    if order == 'reverse':
        # the Mods page's order is top first and the session loads bottom first: the load order as the page's order
        # turns the session's order around
        order = session_order(run)
    if order is not None:
        data['order'] = list(order)
    if data != before:
        write_settings(paths, data)
    answers = [None] * requests
    if requests == 1:
        answers[0] = ask(origin, preview)
    else:
        def one(index):
            try:
                answers[index] = ask(origin, preview)
            except LiveModsError as error:
                answers[index] = {'error': str(error)}
        threads = [threading.Thread(target=one, args=(i,)) for i in range(requests)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(REQUEST_TIMEOUT + 10)
    time.sleep(settle)  # the loader's last lines (modresult, the mods' logs) reach their files
    return {'answer': answers[0] if requests == 1 else answers, 'logs': logs.new_lines(),
            'session_mods': session_mods(run), 'disabled': data.get('disabled'), 'game_running': alive(manifest)}


def revert(paths, previous):
    """Puts the switches (and order) back as they were before a refused step, without asking the backend."""
    write_settings(paths, previous)


def _contains(texts, wanted):
    texts = texts if isinstance(texts, list) else [texts]
    return any(wanted.lower() in str(text).lower() for text in texts)


def check(step, outcome):
    """The step's failed expectations (an empty list when it passed)."""
    expect = step.get('expect') or {}
    answer = outcome['answer'] if isinstance(outcome['answer'], dict) else (outcome['answer'] or [{}])[0]
    failed = []
    for key in ('applied', 'live'):
        if key in expect and answer.get(key) != expect[key]:
            failed.append('%s is %r, expected %r' % (key, answer.get(key), expect[key]))
    for key in ('detail', 'blocking', 'changes'):
        for wanted in ([expect[key]] if isinstance(expect.get(key), str) else expect.get(key) or []):
            if not _contains(answer.get(key) or [], wanted):
                failed.append('%s does not mention %r (%r)' % (key, wanted, answer.get(key)))
    for log, wanted_lines in (step.get('logs') or {}).items():
        for wanted in wanted_lines:
            if not _contains(outcome['logs'].get(log) or [], wanted):
                failed.append('%s has no line with %r' % (log, wanted))
    for log, unwanted_lines in (step.get('not_logs') or {}).items():
        for unwanted in unwanted_lines:
            if _contains(outcome['logs'].get(log) or [], unwanted):
                failed.append('%s has a line with %r' % (log, unwanted))
    for identity in step.get('runs') or []:
        if identity not in outcome['session_mods']:
            failed.append('the session does not run %s' % identity)
    for identity in step.get('runs_not') or []:
        if identity in outcome['session_mods']:
            failed.append('the session still runs %s' % identity)
    if not outcome['game_running']:
        failed.append('the game is no longer running')
    return failed


def scenario(paths, manifest, steps, note=print, out=None):
    """Runs the steps in order. A step: {"name", "on": [ids], "off": [ids], "order": [ids], "preview": bool,
    "requests": n, "expect": {"applied", "live", "detail", "blocking", "changes"}, "logs": {log: [text]},
    "not_logs": {log: [text]}, "runs": [ids], "runs_not": [ids], "revert": bool (put the switches back afterwards;
    default: when the change was not applied), "wait": seconds before}. Stops when the game is gone."""
    results = []
    for index, step in enumerate(steps):
        name = step.get('name') or 'step %d' % (index + 1)
        if step.get('wait'):
            time.sleep(step['wait'])
        previous = settings(paths)
        try:
            outcome = change(paths, manifest, step.get('on') or (), step.get('off') or (), step.get('order'),
                             bool(step.get('preview')), requests=step.get('requests') or 1)
        except LiveModsError as error:
            outcome = {'answer': {'error': str(error)}, 'logs': {}, 'session_mods': [], 'disabled': None,
                       'game_running': alive(manifest)}
        failed = check(step, outcome)
        answer = outcome['answer'] if isinstance(outcome['answer'], dict) else (outcome['answer'] or [{}])[0]
        applied = bool(answer.get('applied'))
        if step.get('revert', not applied and not step.get('preview')):
            revert(paths, previous)
        result = {'step': name, 'passed': not failed, 'failed': failed, **outcome}
        results.append(result)
        note('%s %s: %s' % ('PASS' if not failed else 'FAIL', name, '; '.join(failed) or
                            ', '.join('%s=%s' % (k, answer.get(k)) for k in ('applied', 'detail') if k in answer)))
        if out:
            Path(out).write_text(json.dumps(results, indent=2), encoding='utf-8')
        if not outcome['game_running']:
            note('the game is gone; stopping the scenario')
            break
    return {'passed': sum(r['passed'] for r in results), 'failed': sum(not r['passed'] for r in results),
            'steps': len(steps), 'ran': len(results), 'results': results}


# ---- mod sync between two installations (hd2h/pair.py) ------------------------------------------------------------

SYNC_OFFER = r"Mod sync: (?P<host>.+)'s lobby: (?P<plan>.+)"
SYNC_STORED = r'Mod sync: every mod is here'
SYNC_LIVE = r"Mod sync: the running game plays .+'s mods now"
SYNC_RESTART = r"Mod sync: the running game could not take .+|Mod sync: the next session plays .+"
# What follows each answer: the download starting, then the change being made.
SYNC_AFTER = {'offer': r'Mod sync: (?:downloading|every mod is here|.+ to download)', 'stored': SYNC_LIVE + '|' + SYNC_RESTART}
SYNC_ANSWERS = {'offer': 'accept', 'stored': 'restart'}  # download; APPLY AND JOIN (made live when it can be)


def _wait(text, pattern, timeout, alive, every=1.0):
    """The first match of pattern in text() within timeout s, or None (also once alive() is false)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = re.search(pattern, text())
        if found or not alive():
            return found
        time.sleep(every)
    return None


def _answer(run, answer, note):
    """Answers the joiner's MOD SYNC offer as the launcher does (ModSyncOffer.Answer): <run>/backend/mod-sync-answer.json
    with the offer's version, which the backend takes within a second. No window is touched."""
    backend = Path(run) / 'backend'
    offer = _json(backend / 'mod-sync.json') or {}
    version = offer.get('version')
    if not isinstance(version, int) or version < 0:
        note('joiner: no offer to answer %s' % answer)
        return {'error': 'no offer'}
    temporary = backend / 'mod-sync-answer.json.tmp'
    temporary.write_text(json.dumps({'version': version, 'answer': answer}), encoding='utf-8')
    temporary.replace(backend / 'mod-sync-answer.json')
    return {'version': version, 'answer': answer, 'state': offer.get('state')}


def _shot(pid, run, name, note):
    from . import capture, winapi
    try:
        return capture.shoot(pid, Path(run) / 'shots' / name)
    except (capture.CaptureError, winapi.WinError, OSError) as error:
        note('joiner: capture %s failed: %s' % (name, error))
        return None


def _rejoin(verdict, pid, run, build, joiner_log, host_log, alive, timeout, note):
    """After the live change: the joiner brought toward its ship through its agent when it is not there (a join refused
    at boot leaves it on the title screen), then its way back into the host's lobby awaited: the game's own join (a Steam
    invitation's, tried again) or the host's invitation. Nothing is pressed: when the invitation is handed over and no
    join follows within 15 s, the window is captured (an invitation popup would need the player). Rejoined: the host's
    backend saw the friend join its Party network."""
    from . import pair
    state = session.live_reads(pid, build).get('state_name')
    verdict['state_after_change'] = state
    if state != 'ship':
        note('joiner: on %s after the change; going to the ship through its agent' % state)
        verdict['shots'].append(_shot(pid, run, 'after-change.png', note))
        try:
            pair.to_ship(pid, build, pair._sandbox_mailbox(run), 'joiner', note)
        except pair.PairError as error:
            note('joiner: %s' % error)
    joined = r'A friend joined Party network'
    back = _wait(lambda: joiner_log() + host_log(), r'the game is invited|Rejoin: back in|' + joined, timeout, alive, every=2.0)
    verdict['rejoin_way'] = back.group(0) if back else None
    note('joiner: %s' % (verdict['rejoin_way'] or 'no way back within %d s' % timeout))
    if back and back.group(0) == 'the game is invited' and not _wait(host_log, joined, 15, alive):
        verdict['shots'].append(_shot(pid, run, 'invitation.png', note))
    verdict['rejoined'] = bool(back and _wait(host_log, joined, timeout, alive, every=2.0))
    verdict['shots'].append(_shot(pid, run, 'rejoin.png', note))


def pair_sync(host_root, joiner_root, directory=None, options='--toaster_mode', note=print, offer_timeout=300,
              store_timeout=300, apply_timeout=240, rejoin_timeout=240):
    """The host comes to its ship; the joiner, whose mods differ, joins its lobby (+connect_lobby), takes the MOD SYNC
    prompt's first answer (Enter: download), then APPLY AND JOIN (Enter), and the rejoin is watched. Both sessions stay
    up for a look (`pair.py stop` ends them). Returns what each stage showed."""
    from . import pair
    directory = directory or pair.DEFAULT_DIRECTORY
    _, joiner, build, host_pid, host_run = pair._begin(host_root, joiner_root, directory, options, note)
    lobby = pair.host_lobby(directory)
    pair._record({'lobby': lobby})
    joiner_pid, joiner_run = pair.start(joiner, 'joiner', directory, '%s +connect_lobby %d' % (options, lobby), note)
    verdict = {'lobby': lobby, 'host_run': str(host_run), 'joiner_run': str(joiner_run), 'shots': []}
    alive = lambda: joiner_pid in session.running() and host_pid in session.running()
    joiner_log = lambda: pair._log(joiner_run)
    host_log = lambda: pair._log(host_run)
    # No wait for the ship first: a Steam invitation's join (+connect_lobby) is refused on the title screen, where the
    # prompt shows, and a player answers it there; _rejoin brings the game to the ship afterwards.
    stages = [('offer', SYNC_OFFER, offer_timeout), ('stored', SYNC_STORED, store_timeout),
              ('applied', SYNC_LIVE + '|' + SYNC_RESTART, apply_timeout)]
    for stage, pattern, timeout in stages:
        found = _wait(joiner_log, pattern, timeout, alive)
        verdict[stage] = found.group(0) if found else None
        verdict['shots'].append(_shot(joiner_pid, joiner_run, '%s.png' % stage, note))
        note('joiner: %s: %s' % (stage, verdict[stage] or 'not within %d s' % timeout))
        if not found:
            break
        if stage in SYNC_ANSWERS:  # once more only when nothing followed (the offer's version may have moved on)
            verdict['%s_answer' % stage] = _answer(joiner_run, SYNC_ANSWERS[stage], note)
            if not _wait(joiner_log, SYNC_AFTER[stage], 25, alive):
                note('joiner: nothing followed the %s answer; answering again' % stage)
                verdict['%s_answer_again' % stage] = _answer(joiner_run, SYNC_ANSWERS[stage], note)
    verdict['live'] = bool(verdict.get('applied') and re.search(SYNC_LIVE, verdict['applied']))
    if verdict['live']:
        _rejoin(verdict, joiner_pid, joiner_run, build, joiner_log, host_log, alive, rejoin_timeout, note)
    verdict['joiner_rejoin_log'] = [l for l in joiner_log().splitlines() if 'Rejoin' in l or 'invit' in l.lower()][-12:]
    verdict['host_lobby_log'] = [l for l in host_log().splitlines() if 'friend' in l.lower() or 'Mod sync' in l][-12:]
    verdict['joiner_live_mods'] = (Path(joiner_run) / 'backend' / 'live-mods.log').read_text(encoding='utf-8', errors='replace').splitlines()[-12:] \
        if (Path(joiner_run) / 'backend' / 'live-mods.log').exists() else []
    verdict['joiner_session_mods'] = session_mods(joiner_run)
    verdict['games_running'] = alive()
    pair._record({'sync_verdict': {k: v for k, v in verdict.items() if k != 'shots'}})
    return verdict
