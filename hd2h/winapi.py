"""The platform layer the harness calls: Win32 through ctypes on Windows, /proc and X11 on Linux.

The implementation is chosen once, here, so callers keep importing hd2h.winapi and never branch. The
public names are the same on both platforms: WinError, processes, image_path, Process, terminate,
windows, post_close, foreground_pid, Reader and keep_awake.
"""
import sys

if sys.platform == 'win32':
    from ._winapi_windows import (kernel32, user32, PROCESS_TERMINATE, PROCESS_VM_READ,
                                  PROCESS_QUERY_INFORMATION, PROCESS_QUERY_LIMITED_INFORMATION, SYNCHRONIZE,
                                  STILL_ACTIVE, WAIT_OBJECT_0, TH32CS_SNAPPROCESS, WM_CLOSE, ES_CONTINUOUS,
                                  ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED, INVALID_HANDLE, PROCESSENTRY32W,
                                  MEMORY_BASIC_INFORMATION, MEM_IMAGE, USER_LIMIT, FILETIME, WNDENUMPROC,
                                  WinError, processes, image_path, Process, terminate, windows, post_close,
                                  foreground_pid, Reader, keep_awake)
else:
    from ._winapi_linux import (WinError, processes, image_path, Process, terminate, windows, post_close,
                                foreground_pid, Reader, keep_awake)
