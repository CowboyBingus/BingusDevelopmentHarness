"""The Linux implementation of the harness's platform layer.

Processes are read from /proc, exit is waited on with pidfd where the kernel has it, memory is
read from /proc/<pid>/mem, windows are found through Xlib, and keep_awake uses systemd-inhibit.
The public names match _winapi_windows; hd2h/winapi.py chooses one of the two by sys.platform.
"""
import atexit
import contextlib
import ctypes
import os
from pathlib import Path
import select
import shutil
import signal
import struct
import subprocess
import threading
import time

PROC = '/proc'
CLK_TCK = os.sysconf('SC_CLK_TCK')
FILETIME_EPOCH_OFFSET = 11644473600


class WinError(RuntimeError):
    pass


def _proc(pid, name):
    return '%s/%d/%s' % (PROC, pid, name)


def _read_bytes(path):
    try:
        with open(path, 'rb') as file:
            return file.read()
    except OSError:
        return None


def _read_text(path):
    data = _read_bytes(path)
    return None if data is None else data.decode('utf-8', 'replace')


def _readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def command_line(pid):
    """The process's arguments, or [] when they cannot be read."""
    data = _read_bytes(_proc(pid, 'cmdline'))
    if not data:
        return []
    return [part.decode('utf-8', 'replace') for part in data.rstrip(b'\0').split(b'\0')]


def environment(pid):
    """The process's environment as {name: value}, or {} when it cannot be read."""
    data = _read_bytes(_proc(pid, 'environ'))
    if not data:
        return {}
    entries = {}
    for part in data.rstrip(b'\0').split(b'\0'):
        name, separator, value = part.partition(b'=')
        if separator:
            entries[name.decode('utf-8', 'replace')] = value.decode('utf-8', 'replace')
    return entries


def mapped_paths(pid):
    """File-backed paths in the process's mapping list, or [] when it cannot be read."""
    text = _read_text(_proc(pid, 'maps'))
    if not text:
        return []
    found = []
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[5].startswith('/'):
            found.append(fields[5])
    return found


def _basename(path):
    return path.strip('"').replace('\\', '/').rsplit('/', 1)[-1]


def _matches_name(requested, comm, argv0, executable):
    """Whether a process's own identity names the requested executable. Arguments after argv[0] are deliberately not
    searched: a bash wrapper carries the Windows executable path in its arguments but is not that process."""
    wanted = requested.lower()
    if comm:
        folded = comm.lower()
        if folded == wanted:
            return True
        # Linux comm is truncated to 15 bytes (TASK_COMM_LEN - 1), so HellForge.Session.exe is HellForge.Sessi.
        if len(comm.encode('utf-8')) == 15 and wanted.startswith(folded):
            return True
    for value in (argv0, executable):
        if value and _basename(value).lower() == wanted:
            return True
    return False


def processes(name):
    """PIDs of running processes whose executable name matches (case-insensitive)."""
    found = []
    try:
        entries = os.listdir(PROC)
    except OSError:
        return found
    for entry in sorted(entries, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else 0)):
        if not entry.isdigit():
            continue
        pid = int(entry)
        comm = (_read_text(_proc(pid, 'comm')) or '').strip()
        arguments = command_line(pid)
        executable = _readlink(_proc(pid, 'exe'))
        if _matches_name(name, comm, arguments[0] if arguments else None, executable):
            found.append(pid)
    return found


def _prefix_from_root(root):
    """The Steam compat-data folder whose pfx holds this installation, or None."""
    if not root:
        return None
    parts = Path(root).resolve().parts
    for index in range(len(parts) - 1):
        if parts[index] == 'pfx' and parts[index + 1] == 'drive_c':
            return Path(*parts[:index])
    return None


def _prefix_for_pid(pid, root=None):
    prefix = _prefix_from_root(root)
    if prefix is not None:
        return prefix
    entries = environment(pid)
    wineprefix = entries.get('WINEPREFIX')
    if wineprefix:
        candidate = Path(wineprefix)
        return candidate.parent if candidate.name == 'pfx' else candidate
    compat = entries.get('STEAM_COMPAT_DATA_PATH')
    if compat:
        return Path(compat)
    return _prefix_from_root(entries.get('INSTALL'))


def _host_path(value, prefix):
    """A host path for a Windows path (C:\\... through the prefix, Z:\\... as /...) or a host path; None when it
    cannot be resolved without guessing."""
    value = value.strip('"')
    if len(value) >= 3 and value[0].upper() in ('C', 'Z') and value[1] == ':' and value[2] in ('\\', '/'):
        drive, rest = value[0].upper(), value[3:].replace('\\', '/')
        if drive == 'C':
            if not prefix:
                return None
            target = Path(prefix) / 'pfx' / 'drive_c' / rest
        else:
            target = Path('/') / rest
        return os.path.realpath(str(target))
    if value.startswith('/'):
        return os.path.realpath(value)
    return None


def image_path(pid, root=None):
    """The host path of the executable a process runs, or None. For a Wine process /proc/<pid>/exe is the Wine loader,
    so the Windows executable is resolved from argv[0] or the file-backed mappings instead."""
    arguments = command_line(pid)
    raw = arguments[0] if arguments else ''
    wanted = _basename(raw) if raw else ''
    resolved = _host_path(raw, _prefix_for_pid(pid, root))
    if resolved:
        return resolved
    mappings = mapped_paths(pid)
    if wanted:
        for mapping in mappings:
            if _basename(mapping) == wanted:
                return os.path.realpath(mapping)
    executable = _readlink(_proc(pid, 'exe'))
    if executable:
        name = _basename(executable)
        if not wanted or name == wanted or name.split('.')[0] == wanted.split('.')[0]:
            return os.path.realpath(executable)
        # A Windows executable is run by wine, whose own executable is not the program's path.
        if not wanted.lower().endswith('.exe'):
            return os.path.realpath(executable)
    return None


def _stat_fields(pid):
    data = _read_bytes(_proc(pid, 'stat'))
    if not data:
        return None
    end = data.rfind(b')')
    if end < 0:
        return None
    return data[end + 2:].split()


def _field(fields, number):
    try:
        return int(fields[number - 3])
    except (IndexError, TypeError, ValueError):
        return None


def _starttime(pid):
    return _field(_stat_fields(pid) or [], 22)


def _boot_time():
    text = _read_text('%s/stat' % PROC)
    if text:
        for line in text.splitlines():
            if line.startswith('btime '):
                try:
                    return int(line.split()[1])
                except (IndexError, ValueError):
                    return None
    return None


def _creation_filetime(starttime):
    boot = _boot_time()
    if starttime is None or boot is None:
        return None
    return int((boot + starttime / CLK_TCK + FILETIME_EPOCH_OFFSET) * 10_000_000)


class Process:
    """A handle on a running process, kept alive across exit by a pidfd where the kernel provides one."""

    def __init__(self, pid, access=None):
        self.pid = pid
        self._starttime = _starttime(pid)
        self._created = _creation_filetime(self._starttime)
        self.handle = None
        opener = getattr(os, 'pidfd_open', None)
        if opener is not None:
            try:
                self.handle = opener(pid, 0)
            except OSError:
                self.handle = None
        if not os.path.isdir(_proc(pid, '')) and self.handle is None:
            raise WinError('OpenProcess(%d) failed: no such process' % pid)

    def wait(self, seconds):
        """True once the process has exited."""
        if self.handle is not None:
            timeout = None if seconds is None else max(0, int(seconds * 1000))
            poller = select.poll()
            poller.register(self.handle, select.POLLIN)
            return bool(poller.poll(timeout))
        deadline = None if seconds is None else time.monotonic() + max(0, seconds)
        while True:
            fields = _stat_fields(self.pid)
            if not fields:
                return True
            if fields[0] == b'Z':
                return True
            if self._starttime is not None:
                current = _field(fields, 22)
                if current is not None and current != self._starttime:
                    return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def exit_code(self):
        """The exit status as waitpid would report it (negative for a signal), or None. A signal cannot choose a code
        the way TerminateProcess can, and a process its parent has already reaped has no code left to read."""
        if not self.wait(0):
            return None
        fields = _stat_fields(self.pid)
        if not fields:
            return None
        if self._starttime is not None:
            current = _field(fields, 22)
            if current is not None and current != self._starttime:
                return None
        status = _field(fields, 52)
        if status is None:
            return None
        try:
            return os.waitstatus_to_exitcode(status)
        except ValueError:
            return None

    def creation_time(self):
        """Creation time as a Windows FILETIME integer (100 ns since 1601), or None."""
        return self._created

    def close(self):
        if self.handle is not None:
            try:
                os.close(self.handle)
            except OSError:
                pass
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def terminate(pid, code=1):
    """Forcefully ends a process, or its whole process group when it leads one (a wrapper session). The exit code
    cannot be chosen; `code` is accepted for the Windows signature's sake."""
    try:
        group = os.getpgid(pid)
        if group == pid:
            os.killpg(group, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def images_from_maps(maps, read):
    """(base, PE timestamp, image size) of every file-backed PE image in a /proc/<pid>/maps text."""
    found, seen = [], set()
    for line in (maps or '').splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or not fields[5].startswith('/'):
            continue
        try:
            start = int(fields[0].split('-')[0], 16)
            offset = int(fields[2], 16)
        except (IndexError, ValueError):
            continue
        base = start - offset
        if base in seen:
            continue
        seen.add(base)
        header = read(base, 0x400)
        if not header or header[:2] != b'MZ':
            continue
        try:
            pe = struct.unpack_from('<I', header, 0x3C)[0]
        except struct.error:
            continue
        if pe + 24 + 60 <= len(header) and header[pe:pe + 4] == b'PE\0\0':
            found.append((base, struct.unpack_from('<I', header, pe + 8)[0],
                          struct.unpack_from('<I', header, pe + 24 + 56)[0]))
    return found


def _ptrace_scope():
    value = _read_text('%s/sys/kernel/yama/ptrace_scope' % PROC)
    return value.strip() if value is not None else 'unknown'


class Reader:
    """Read-only view of another process's memory through /proc/<pid>/mem."""

    def __init__(self, pid):
        self.pid = pid
        try:
            self.handle = os.open(_proc(pid, 'mem'), os.O_RDONLY)
        except OSError as error:
            raise WinError('cannot read the memory of pid %d: %s (Yama ptrace_scope is %s; the harness must be an '
                           'ancestor of the game, or run with ptrace_scope=0)'
                           % (pid, error, _ptrace_scope())) from error

    def read(self, address, size):
        try:
            data = os.pread(self.handle, size, address)
        except OSError:
            return None
        return data if len(data) == size else None

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
        """(base, PE timestamp, image size) of every mapped PE image, found through /proc/<pid>/maps."""
        text = _read_text(_proc(self.pid, 'maps'))
        if text is None:
            raise WinError('cannot read /proc/%d/maps (Yama ptrace_scope is %s)'
                           % (self.pid, _ptrace_scope()))
        return images_from_maps(text, self.read)

    def image_base(self, timestamp):
        """Base of the mapped image whose PE header has this TimeDateStamp, or None."""
        for base, stamp, _size in self.images():
            if stamp == timestamp:
                return base
        return None

    def close(self):
        if self.handle is not None:
            try:
                os.close(self.handle)
            except OSError:
                pass
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---- X11 ---------------------------------------------------------------------------------------------------------

_X11 = None
_X11_FAILED = False
_X11_LOCK = threading.RLock()
_X11_ERROR_HANDLER = None


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [('x', ctypes.c_int), ('y', ctypes.c_int), ('width', ctypes.c_int), ('height', ctypes.c_int),
                ('border_width', ctypes.c_int), ('depth', ctypes.c_int), ('visual', ctypes.c_void_p),
                ('root', ctypes.c_ulong), ('class', ctypes.c_int), ('bit_gravity', ctypes.c_int),
                ('win_gravity', ctypes.c_int), ('backing_store', ctypes.c_int),
                ('backing_planes', ctypes.c_ulong), ('backing_pixel', ctypes.c_ulong),
                ('save_under', ctypes.c_int), ('colormap', ctypes.c_ulong), ('map_installed', ctypes.c_int),
                ('map_state', ctypes.c_int), ('all_event_masks', ctypes.c_long),
                ('your_event_mask', ctypes.c_long), ('do_not_propagate_mask', ctypes.c_long),
                ('override_redirect', ctypes.c_int), ('screen', ctypes.c_void_p)]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('window', ctypes.c_ulong), ('message_type', ctypes.c_ulong),
                ('format', ctypes.c_int), ('data', ctypes.c_long * 5)]


class _XEvent(ctypes.Union):
    _fields_ = [('xclient', _XClientMessageEvent), ('pad', ctypes.c_long * 24)]


def _load_x11():
    global _X11, _X11_FAILED, _X11_ERROR_HANDLER
    if _X11 is not None or _X11_FAILED:
        return _X11
    with _X11_LOCK:
        if _X11 is not None or _X11_FAILED:
            return _X11
        for name in ('libX11.so.6', 'libX11.so'):
            try:
                library = ctypes.CDLL(name)
                break
            except OSError:
                pass
        else:
            _X11_FAILED = True
            return None
        library.XOpenDisplay.argtypes, library.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
        library.XCloseDisplay.argtypes, library.XCloseDisplay.restype = [ctypes.c_void_p], ctypes.c_int
        library.XDefaultRootWindow.argtypes, library.XDefaultRootWindow.restype = [ctypes.c_void_p], ctypes.c_ulong
        library.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        library.XInternAtom.restype = ctypes.c_ulong
        library.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int,
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
        library.XGetWindowProperty.restype = ctypes.c_int
        library.XFree.argtypes, library.XFree.restype = [ctypes.c_void_p], ctypes.c_int
        library.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XWindowAttributes)]
        library.XGetWindowAttributes.restype = ctypes.c_int
        library.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                                       ctypes.POINTER(_XEvent)]
        library.XSendEvent.restype = ctypes.c_int
        library.XFlush.argtypes, library.XFlush.restype = [ctypes.c_void_p], ctypes.c_int
        library.XSync.argtypes, library.XSync.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_int
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        _X11_ERROR_HANDLER = handler_type(lambda display, event: 0)
        library.XSetErrorHandler.argtypes, library.XSetErrorHandler.restype = [ctypes.c_void_p], ctypes.c_void_p
        library.XSetErrorHandler(ctypes.cast(_X11_ERROR_HANDLER, ctypes.c_void_p))
        _X11 = library
        return _X11


@contextlib.contextmanager
def _display():
    if not os.environ.get('DISPLAY'):
        yield None
        return
    library = _load_x11()
    if library is None:
        yield None
        return
    with _X11_LOCK:
        display = library.XOpenDisplay(None)
        if not display:
            yield None
            return
        try:
            yield library, display
        finally:
            library.XCloseDisplay(display)


def _property(library, display, window, name, length=1024):
    atom = library.XInternAtom(display, name.encode(), 0)
    actual_type, actual_format = ctypes.c_ulong(), ctypes.c_int()
    nitems, bytes_after = ctypes.c_ulong(), ctypes.c_ulong()
    prop = ctypes.POINTER(ctypes.c_ubyte)()
    status = library.XGetWindowProperty(display, window, atom, 0, length, 0, 0,
                                        ctypes.byref(actual_type), ctypes.byref(actual_format),
                                        ctypes.byref(nitems), ctypes.byref(bytes_after),
                                        ctypes.byref(prop))
    if status != 0 or not prop:
        return None
    try:
        if not nitems.value:
            return []
        if actual_format.value == 32:
            raw = ctypes.string_at(prop, nitems.value * ctypes.sizeof(ctypes.c_ulong))
            values = (ctypes.c_ulong * nitems.value).from_buffer_copy(raw)
            return [int(value) for value in values]
        raw = ctypes.string_at(prop, nitems.value)
        return list(raw)
    finally:
        library.XFree(prop)


def _window_pid(library, display, window):
    values = _property(library, display, window, '_NET_WM_PID')
    return values[0] if values else None


def _attributes(library, display, window):
    attributes = _XWindowAttributes()
    if not library.XGetWindowAttributes(display, window, ctypes.byref(attributes)):
        return None
    return attributes


def _windows_on(library, display, pid, visible_only):
    root = library.XDefaultRootWindow(display)
    windows = _property(library, display, root, '_NET_CLIENT_LIST')
    if windows is None:
        windows = _property(library, display, root, '_NET_CLIENT_LIST_STACKING')
    found = []
    for window in windows or []:
        if _window_pid(library, display, window) != pid:
            continue
        if visible_only:
            attributes = _attributes(library, display, window)
            if attributes is None or attributes.map_state != 2:  # IsViewable
                continue
            hidden = library.XInternAtom(display, b'_NET_WM_STATE_HIDDEN', 0)
            states = _property(library, display, window, '_NET_WM_STATE')
            if states and hidden in states:
                continue
        found.append(window)
    return found


def windows(pid, visible_only=True):
    """Top-level X11 windows owned by a process (none when there is no DISPLAY or no _NET_WM_PID)."""
    with _display() as connection:
        if connection is None:
            return []
        library, display = connection
        return _windows_on(library, display, pid, visible_only)


def window_size(window):
    """(width, height) of an X11 window, or (0, 0) when it cannot be read."""
    with _display() as connection:
        if connection is None:
            return 0, 0
        library, display = connection
        attributes = _attributes(library, display, window)
        return (attributes.width, attributes.height) if attributes is not None else (0, 0)


def post_close(pid):
    """Sends WM_DELETE_WINDOW to each visible top-level window that accepts it. Returns how many."""
    count = 0
    with _display() as connection:
        if connection is None:
            return 0
        library, display = connection
        delete = library.XInternAtom(display, b'WM_DELETE_WINDOW', 0)
        protocols = library.XInternAtom(display, b'WM_PROTOCOLS', 0)
        for window in _windows_on(library, display, pid, True):
            accepted = _property(library, display, window, 'WM_PROTOCOLS')
            if not accepted or delete not in accepted:
                continue
            event = _XEvent()
            event.xclient.type = 33  # ClientMessage
            event.xclient.send_event = 1
            event.xclient.display = display
            event.xclient.window = window
            event.xclient.message_type = protocols
            event.xclient.format = 32
            event.xclient.data[0] = delete
            if library.XSendEvent(display, window, 0, 0, ctypes.byref(event)):
                count += 1
        library.XFlush(display)
        library.XSync(display, 0)
    return count


def foreground_pid():
    with _display() as connection:
        if connection is None:
            return None
        library, display = connection
        root = library.XDefaultRootWindow(display)
        active = _property(library, display, root, '_NET_ACTIVE_WINDOW')
        if not active or not active[0]:
            return None
        return _window_pid(library, display, active[0])


# ---- keep awake --------------------------------------------------------------------------------------------------

_INHIBITOR = None


def _stop_inhibitor():
    global _INHIBITOR
    process, _INHIBITOR = _INHIBITOR, None
    if process is None:
        return
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(5)
    except Exception:
        try:
            process.kill()
        except OSError:
            pass


atexit.register(_stop_inhibitor)


def keep_awake(on):
    """Keeps the system and display awake through systemd-inhibit while on; returns False when it is unavailable."""
    global _INHIBITOR
    if not on:
        _stop_inhibitor()
        return True
    if _INHIBITOR is not None and _INHIBITOR.poll() is None:
        return True
    if shutil.which('systemd-inhibit') is None:
        return False
    try:
        _INHIBITOR = subprocess.Popen(
            ['systemd-inhibit', '--what=idle:sleep', '--who=BingusDevelopmentHarness',
             '--why=waiting for the game', '--mode=block', 'sleep', 'infinity'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        _INHIBITOR = None
        return False
    return True
