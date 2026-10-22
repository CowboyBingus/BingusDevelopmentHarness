"""Screenshots of the game window without focus, standard library only.

PrintWindow with PW_RENDERFULLCONTENT copies DWM's image of the client area, so the window may sit behind
others. A capture is scored for whether it shows the 3D world: a session whose upscaler input is larger than
its window draws only the HUD over a flat gray world (2026-10-13, see settings.scale_render_resolution).
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import struct
import zlib

from . import winapi

gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
user32 = winapi.user32

PW_CLIENT_FULL = 0x1 | 0x2  # PW_CLIENTONLY | PW_RENDERFULLCONTENT
# Calibrated 2026-10-13 against PIL on 142 bench captures with the 3D world (centre saturation 50-140, detail
# 2.4-18.7) and 11 gray ones (saturation <= 22.8, detail <= 2.14). Bright cloud frames (the hellpod drop) score
# like gray ones: judge a step by several captures.
MIN_SATURATION, MIN_DETAIL = 40.0, 3.0
SAMPLE_STEP = 2


class CaptureError(RuntimeError):
    pass


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


def _declare(function, restype, *argtypes):
    function.restype, function.argtypes = restype, argtypes


_declare(user32.GetClientRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_declare(user32.GetDC, wintypes.HDC, wintypes.HWND)
_declare(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
_declare(user32.PrintWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC, wintypes.UINT)
_declare(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_declare(gdi32.CreateCompatibleBitmap, wintypes.HBITMAP, wintypes.HDC, ctypes.c_int, ctypes.c_int)
_declare(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_declare(gdi32.GetDIBits, ctypes.c_int, wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
         ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT)
_declare(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_declare(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)


def game_window(pid):
    """The game's largest visible window, or None."""
    best, best_area = None, 0
    for hwnd in winapi.windows(pid):
        rect = wintypes.RECT()
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            area = rect.right * rect.bottom
            if area > best_area:
                best, best_area = hwnd, area
    return best


def grab(pid):
    """(width, height, top-down BGRA bytes) of the game's client area."""
    hwnd = game_window(pid)
    if not hwnd:
        raise CaptureError('no visible game window')
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    width, height = rect.right, rect.bottom
    if width <= 0 or height <= 0:
        raise CaptureError('the game window has no client area')
    window_dc = user32.GetDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    try:
        gdi32.SelectObject(memory_dc, bitmap)
        if not user32.PrintWindow(hwnd, memory_dc, PW_CLIENT_FULL):
            raise CaptureError('PrintWindow failed')
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0) != height:
            raise CaptureError('GetDIBits failed')
        return width, height, buffer.raw
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def bgra_to_rgb(bgra):
    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return bytes(rgb)


def png(width, height, rgb):
    """An 8-bit RGB PNG of top-down rows."""
    stride = width * 3
    rows = b''.join(b'\0' + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b''))


def score(width, height, rgb, step=SAMPLE_STEP):
    """Does the capture show the 3D world? Centre region (HUD-free on most screens): mean HSV saturation
    (0-255) and fine detail (mean luminance difference to the pixel up and left), sampled every `step` pixels."""
    x0, x1, y0, y1 = int(width * 0.2), int(width * 0.8), int(height * 0.2), int(height * 0.8)
    stride, saturation, detail, count = width * 3, 0.0, 0.0, 0
    for y in range(max(y0, 1), y1, step):
        row, above = y * stride, (y - 1) * stride
        for x in range(max(x0, 1), x1, step):
            i = row + 3 * x
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            high, low = max(r, g, b), min(r, g, b)
            saturation += (high - low) * 255.0 / high if high else 0.0
            j = above + 3 * (x - 1)
            detail += abs((r * 299 + g * 587 + b * 114) - (rgb[j] * 299 + rgb[j + 1] * 587 + rgb[j + 2] * 114)) / 1000.0
            count += 1
    saturation, detail = (saturation / count, detail / count) if count else (0.0, 0.0)
    return {'saturation': round(saturation, 1), 'detail': round(detail, 2),
            'world': saturation >= MIN_SATURATION or detail >= MIN_DETAIL}


def shoot(pid, path):
    """Captures the game window to a PNG at path; returns the score."""
    width, height, bgra = grab(pid)
    rgb = bgra_to_rgb(bgra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png(width, height, rgb))
    return dict(score(width, height, rgb), file=str(path), size=[width, height])


def run_shot(paths, name):
    """Captures into <run>/shots/<nn>-<name>.png and records it in the session (run.json `shots`)."""
    from .state import State
    state = State(paths.state)
    manifest = state.load()
    if manifest is None or not manifest.get('pid'):
        raise CaptureError('no launched session')
    shots = list(manifest.get('shots') or [])
    safe = ''.join(c if c.isalnum() or c in '-_' else '-' for c in name)[:40] or 'shot'
    path = Path(manifest['run']) / 'shots' / ('%02d-%s.png' % (len(shots) + 1, safe))
    result = dict(shoot(manifest['pid'], path), name=name)
    state.update(shots=shots + [result])
    return result
