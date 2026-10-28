"""Windows screenshots of the game window without focus, through GDI (no third-party packages).

PrintWindow with PW_RENDERFULLCONTENT copies DWM's image of the client area, so the window may sit behind
others. hd2h/capture.py selects this module on Windows and keeps the shared PNG and score helpers.
"""
import ctypes
from ctypes import wintypes

from . import winapi

gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
user32 = winapi.user32

PW_CLIENT_FULL = 0x1 | 0x2  # PW_CLIENTONLY | PW_RENDERFULLCONTENT


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
