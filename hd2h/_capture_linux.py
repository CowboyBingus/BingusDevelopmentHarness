"""Linux screenshots of the game window through X11 and ImageMagick.

There is no portable replacement for PrintWindow: a specific X11 window can be copied with `import -window`, but an
obscured window depends on the compositor. Wayland has no cross-client window capture; when neither a display nor an
ImageMagick tool is available the caller gets a CaptureError that names what is missing, never a blank image.
"""
import os
import shutil
import subprocess

from . import winapi
from ._winapi_linux import window_size


class CaptureError(RuntimeError):
    pass


def _capture_tool():
    """The command prefix that writes a window's pixels as raw BGRA to stdout."""
    if not os.environ.get('DISPLAY'):
        raise CaptureError('window capture needs an X11 display (DISPLAY is not set); Wayland has no portable '
                           'cross-client window capture')
    if shutil.which('import'):
        return ['import']
    if shutil.which('magick'):
        return ['magick', 'import']
    raise CaptureError("window capture needs ImageMagick's `import` (or `magick import`), which was not found")


def game_window(pid):
    """The game's largest visible X11 window, or None."""
    best, best_area = None, 0
    for window in winapi.windows(pid):
        width, height = window_size(window)
        area = width * height
        if area > best_area:
            best, best_area = window, area
    return best


def grab(pid):
    """(width, height, top-down BGRA bytes) of the game's client area."""
    tool = _capture_tool()
    window = game_window(pid)
    if not window:
        raise CaptureError('no visible game window')
    width, height = window_size(window)
    if width <= 0 or height <= 0:
        raise CaptureError('the game window has no client area')
    try:
        result = subprocess.run(tool + ['-window', str(window), '-depth', '8', 'bgra:-'],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureError('the window capture failed: %s' % error) from error
    if result.returncode != 0:
        detail = result.stderr.decode('utf-8', 'replace').strip()
        raise CaptureError('the window capture failed: %s' % (detail or 'exit code %d' % result.returncode))
    expected = width * height * 4
    if len(result.stdout) != expected:
        raise CaptureError('the window capture returned %d bytes, expected %d' % (len(result.stdout), expected))
    return width, height, result.stdout
