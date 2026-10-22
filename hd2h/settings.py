"""The game's settings files during a harness session: backed up, changed, restored byte-for-byte.

Every session forces Invite Only lobby privacy (privacy_mode = 2 in the profile file), unless a HellForge session
opts in to Public (session.start checks when that is allowed).
The `light` preset also caps the frame rate at 30, mutes audio and runs windowed at
1280x720, so a background session costs less and covers less of the screen. It scales the
upscaler's stored input size to the window as well (see scale_render_resolution).
"""
import hashlib
from pathlib import Path
import re
import shutil

INVITE_ONLY, PUBLIC = '2', '0'
PRIVACY = {'invite': INVITE_ONLY, 'public': PUBLIC}
PRESETS = {
    'private': {'profile': {'privacy_mode': INVITE_ONLY}, 'main': {}, 'resolution': None},
    'light': {'profile': {'privacy_mode': INVITE_ONLY},
              'main': {'framerate_limit_enabled': 'true', 'framerate_limit': '30', 'audio_volume': '0',
                       'borderless_fullscreen': 'false', 'fullscreen': 'false', 'maximized_window': 'false'},
              'resolution': (1280, 720)},
}


class SettingsError(RuntimeError):
    pass


def profile_files(paths):
    """Every saves/<id>_user_settings.config file a session's preset is written into.

    The Steam install has one. A HellForge profile can hold two: the game's own, named after the HellForge peer id,
    and one imported from the normal game (the launcher's profile import), so both get the preset.
    """
    found = sorted(Path(paths.saves).glob('*_user_settings.config'))
    if getattr(paths, 'target', 'steam') == 'hellforge':
        if not found:
            raise SettingsError('no saves/*_user_settings.config in %s' % paths.saves)
        return found
    if len(found) != 1:
        raise SettingsError('expected one saves/*_user_settings.config, found %d' % len(found))
    return found


def profile_file(paths):
    """The profile file the game uses: the single one, or (HellForge) the one it wrote last."""
    return max(profile_files(paths), key=lambda path: path.stat().st_mtime)


def _line_pattern(key):
    # Top-level lines only (indented keys belong to blocks); LF or CRLF endings.
    return re.compile(r'^(%s = )([^\r\n]*)(?=\r?$)' % re.escape(key), re.MULTILINE)


def read_scalar(text, key):
    match = _line_pattern(key).search(text)
    return match.group(2) if match else None


def set_scalar(text, key, value):
    """Replaces the value of one top-level `key = value` line; the key must occur exactly once."""
    pattern = _line_pattern(key)
    if len(pattern.findall(text)) != 1:
        raise SettingsError('setting %s is not a single top-level line' % key)
    return pattern.sub(lambda match: match.group(1) + value, text)


SCREEN_RESOLUTION = re.compile(r'^screen_resolution = \[(\r?\n)\t(\d+)\r?\n\t(\d+)\r?\n\]', re.MULTILINE)
RENDER_RESOLUTION = re.compile(r'^\trender_resolution = \[(\r?\n)\t\t(\d+)\r?\n\t\t(\d+)\r?\n\t\]', re.MULTILINE)


def set_resolution(text, width, height):
    if len(SCREEN_RESOLUTION.findall(text)) != 1:
        raise SettingsError('screen_resolution block not found once')
    return SCREEN_RESOLUTION.sub(lambda m: 'screen_resolution = [%s\t%d%s\t%d%s]' % (
        m.group(1), width, m.group(1), height, m.group(1)), text)


def read_resolution(text):
    """(width, height) of the top-level screen_resolution block, or None."""
    found = SCREEN_RESOLUTION.findall(text)
    return (int(found[0][1]), int(found[0][2])) if len(found) == 1 else None


def scale_render_resolution(text, screen, window):
    """Scales render_settings.render_resolution by window/screen.

    The game stores the upscaler's input size in pixels, not as a factor. A window smaller than that input
    leaves the 3D world undrawn and only the HUD shows (2026-10-13: an input of 2/3 of the full-screen size
    into a 1280x720 window gave a flat gray world; 2/3 of the window drew it). No block or no screen size: the
    text is returned unchanged.
    """
    found = RENDER_RESOLUTION.findall(text)
    if len(found) != 1 or not screen or not screen[0] or not screen[1]:
        return text
    size = [min(window[i], max(1, round(int(found[0][i + 1]) * window[i] / screen[i]))) for i in (0, 1)]
    return RENDER_RESOLUTION.sub(lambda m: '\trender_resolution = [%s\t\t%d%s\t\t%d%s\t]' % (
        m.group(1), size[0], m.group(1), size[1], m.group(1)), text)


def _sha(data):
    return hashlib.sha256(data).hexdigest().upper()


def _change(data, values, resolution=None):
    text = data.decode('utf-8')
    for key, value in values.items():
        text = set_scalar(text, key, value)
    if resolution:
        screen = read_resolution(text)
        text = set_resolution(text, *resolution)
        text = scale_render_resolution(text, screen, resolution)
    return text.encode('utf-8')


def apply(paths, preset, backup_dir, record, privacy='invite'):
    """Backs up the settings files, writes the preset, returns the record saved via record().

    privacy: 'invite' (Invite Only, every Steam session) or 'public' (opt-in, HellForge sessions only; session.start
    checks). Every other file in saves/ (the game's own .old copy of the profile) is backed up too,
    unchanged, because the game may rotate it during the session.
    """
    if preset not in PRESETS:
        raise SettingsError('unknown preset %r (choose from %s)' % (preset, ', '.join(PRESETS)))
    if privacy not in PRIVACY:
        raise SettingsError('unknown privacy %r (choose from %s)' % (privacy, ', '.join(PRIVACY)))
    plan = PRESETS[preset]
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    profiles = profile_files(paths)
    profile_values = dict(plan['profile'], privacy_mode=PRIVACY[privacy])
    targets = {'main': (Path(paths.settings_file), plan['main'], plan['resolution'])}
    for index, profile in enumerate(profiles):
        targets['profile' if index == 0 else 'profile:' + profile.name] = (profile, profile_values, None)
    for extra in sorted(Path(paths.saves).iterdir()):
        if extra.is_file() and extra not in profiles:
            targets['saves:' + extra.name] = (extra, {}, None)
    changes, files = {}, {}
    for index, (role, (path, values, resolution)) in enumerate(targets.items()):
        original = path.read_bytes()
        changed = _change(original, values, resolution) if values or resolution else original
        changes[role] = (path, original, changed)
        files[role] = {'path': str(path), 'backup': str(backup_dir / ('%02d.backup' % index)),
                       'original_sha256': _sha(original), 'applied_sha256': _sha(changed)}
    for role, (path, original, _) in changes.items():
        Path(files[role]['backup']).write_bytes(original)
        if _sha(Path(files[role]['backup']).read_bytes()) != files[role]['original_sha256']:
            raise SettingsError('backup of %s did not verify' % path)
    entry = {'preset': preset, 'files': files, 'saves_before': sorted(p.name for p in Path(paths.saves).iterdir())}
    record(entry)
    for role, (path, original, changed) in changes.items():
        if changed != original:
            path.write_bytes(changed)
    return entry


def new_save_files(paths, entry):
    """Files the game created in saves/ during the session (reported, never deleted)."""
    before = set(entry.get('saves_before') or [])
    return sorted(p.name for p in Path(paths.saves).iterdir() if p.name not in before)


def restore(entry):
    """Puts the original bytes back. Returns problems (empty list when every file verified)."""
    problems = []
    for role, info in entry['files'].items():
        path, backup = Path(info['path']), Path(info['backup'])
        if not backup.exists():
            if path.exists() and _sha(path.read_bytes()) == info['original_sha256']:
                continue
            problems.append('%s: backup missing' % role)
            continue
        data = backup.read_bytes()
        if _sha(data) != info['original_sha256']:
            problems.append('%s: backup does not match its recorded hash' % role)
            continue
        if not path.exists() or _sha(path.read_bytes()) != info['original_sha256']:
            shutil.copyfile(backup, path)
        if _sha(path.read_bytes()) != info['original_sha256']:
            problems.append('%s: restore did not verify' % role)
            continue
        backup.unlink()
    return problems


def privacy(paths):
    """The profile's lobby privacy setting as written in its file (string), or None."""
    return read_scalar(profile_file(paths).read_text(encoding='utf-8'), 'privacy_mode')
