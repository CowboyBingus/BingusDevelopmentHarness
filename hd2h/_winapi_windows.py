"""The few Win32 calls the harness needs, through ctypes (no third-party packages).

Game access is read-only (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ), plus SYNCHRONIZE
for exit codes, PROCESS_TERMINATE for a kill, and WM_CLOSE posted to the game's own window.
Nothing is injected or hooked.

GameGuard injects npggNT64 into other processes and stalls EnumProcessModulesEx on the game
forever (seen 2026-10-03). Modules are therefore found by scanning mapped images with
VirtualQueryEx and matching the PE timestamp.
"""
import ctypes
from ctypes import wintypes
import struct

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
user32 = ctypes.WinDLL('user32', use_last_error=True)

PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
TH32CS_SNAPPROCESS = 0x2
WM_CLOSE = 0x0010
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
INVALID_HANDLE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD),
                ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD),
                ('szExeFile', wintypes.WCHAR * 260)]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                ('AllocationProtect', wintypes.DWORD), ('PartitionId', wintypes.WORD),
                ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD), ('Protect', wintypes.DWORD),
                ('Type', wintypes.DWORD)]


MEM_IMAGE = 0x1000000
USER_LIMIT = 0x7FFFFFFF0000


class FILETIME(ctypes.Structure):
    _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]


def _declare(function, restype, *argtypes):
    function.restype, function.argtypes = restype, list(argtypes)


_declare(kernel32.CreateToolhelp32Snapshot, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
_declare(kernel32.Process32FirstW, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_declare(kernel32.Process32NextW, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
_declare(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_declare(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
_declare(kernel32.WaitForSingleObject, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
_declare(kernel32.GetExitCodeProcess, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_declare(kernel32.TerminateProcess, wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_declare(kernel32.ReadProcessMemory, wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
         ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
_declare(kernel32.VirtualQueryEx, ctypes.c_size_t, wintypes.HANDLE, ctypes.c_void_p,
         ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t)
_declare(kernel32.GetProcessTimes, wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(FILETIME),
         ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME))
_declare(kernel32.SetThreadExecutionState, wintypes.DWORD, wintypes.DWORD)
_declare(kernel32.QueryFullProcessImageNameW, wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
         ctypes.POINTER(wintypes.DWORD))
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_declare(user32.EnumWindows, wintypes.BOOL, WNDENUMPROC, wintypes.LPARAM)
_declare(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_declare(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
_declare(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_declare(user32.GetForegroundWindow, wintypes.HWND)


class WinError(RuntimeError):
    pass


def _fail(what):
    raise WinError('%s failed (error %d)' % (what, ctypes.get_last_error()))


def processes(name):
    """PIDs of running processes whose executable name matches (case-insensitive)."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE:
        _fail('CreateToolhelp32Snapshot')
    try:
        entry, found = PROCESSENTRY32W(), []
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() == name.lower():
                found.append(entry.th32ProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return found
    finally:
        kernel32.CloseHandle(snapshot)


def image_path(pid):
    """Full path of a running process's executable, or None (gone, or not ours to query). A HellForge copy and the
    Steam install run the same helldivers2.exe name; the path tells them apart."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


class Process:
    """A handle on a running process; held from launch to exit so the exit code survives."""

    def __init__(self, pid, access=SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION):
        self.pid = pid
        self.handle = kernel32.OpenProcess(access, False, pid)
        if not self.handle:
            _fail('OpenProcess(%d)' % pid)

    def wait(self, seconds):
        """True once the process has exited."""
        milliseconds = 0xFFFFFFFF if seconds is None else max(0, int(seconds * 1000))
        return kernel32.WaitForSingleObject(self.handle, milliseconds) == WAIT_OBJECT_0

    def exit_code(self):
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            _fail('GetExitCodeProcess')
        return None if code.value == STILL_ACTIVE and not self.wait(0) else code.value

    def creation_time(self):
        """Creation time as a Windows FILETIME integer (100 ns since 1601), or None."""
        times = [FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            return None
        return times[0].high << 32 | times[0].low

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def terminate(pid, code=1):
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, code))
    finally:
        kernel32.CloseHandle(handle)


def windows(pid, visible_only=True):
    """Top-level windows owned by a process."""
    found = []

    def callback(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and (not visible_only or user32.IsWindowVisible(hwnd)):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def post_close(pid):
    """Asks the process's visible top-level windows to close (WM_CLOSE). Returns how many."""
    count = 0
    for hwnd in windows(pid):
        if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            count += 1
    return count


def foreground_pid():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return owner.value


class Reader:
    """Read-only view of another process's memory."""

    def __init__(self, pid):
        self.handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.handle:
            _fail('OpenProcess(%d) for reading' % pid)

    def read(self, address, size):
        buffer, received = ctypes.create_string_buffer(size), ctypes.c_size_t()
        ok = kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(address), buffer, size,
                                        ctypes.byref(received))
        return buffer.raw if ok and received.value == size else None

    def u32(self, address):
        raw = self.read(address, 4)
        return None if raw is None else struct.unpack('<I', raw)[0]

    def pointer(self, address):
        raw = self.read(address, 8)
        if raw is None:
            return None
        value = struct.unpack('<Q', raw)[0]
        return value if 0x10000 <= value < 0x800000000000 else None

    def images(self):
        """(base, PE timestamp, image size) of every mapped image, found region by region."""
        address, info, seen, found = 0x10000, MEMORY_BASIC_INFORMATION(), set(), []
        while address < USER_LIMIT and kernel32.VirtualQueryEx(self.handle, ctypes.c_void_p(address),
                                                               ctypes.byref(info), ctypes.sizeof(info)):
            base = info.AllocationBase or 0
            if info.Type == MEM_IMAGE and base and base not in seen:
                seen.add(base)
                header = self.read(base, 0x400)
                if header and header[:2] == b'MZ':
                    pe = struct.unpack_from('<I', header, 0x3C)[0]
                    if pe + 24 + 60 <= len(header) and header[pe:pe + 4] == b'PE\0\0':
                        found.append((base, struct.unpack_from('<I', header, pe + 8)[0],
                                      struct.unpack_from('<I', header, pe + 24 + 56)[0]))
            if not info.RegionSize:
                break
            address = (info.BaseAddress or 0) + info.RegionSize
        return found

    def image_base(self, timestamp):
        """Base of the mapped image whose PE header has this TimeDateStamp, or None."""
        for base, stamp, _size in self.images():
            if stamp == timestamp:
                return base
        return None

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def keep_awake(on):
    """Keeps the display and system awake while this thread runs (on) or releases it."""
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
    return kernel32.SetThreadExecutionState(flags) != 0

