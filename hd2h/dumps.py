"""Minidumps: read the exception and module list, and name known crashes.

Only the streams needed for triage are parsed (exception stream 6, module list 4);
deeper analysis stays with cdb.
"""
from pathlib import Path
import struct

from . import builds

SIGNATURE = 0x504D444D  # 'MDMP'
EXCEPTION_STREAM, MODULE_LIST_STREAM = 6, 4
MODULE_SIZE = 108


class DumpError(ValueError):
    pass


def _string(data, rva):
    (length,) = struct.unpack_from('<I', data, rva)
    return data[rva + 4:rva + 4 + length].decode('utf-16-le', errors='replace')


def parse(data):
    """{'code', 'address', 'thread', 'modules': [...], 'module', 'offset'} from minidump bytes."""
    if len(data) < 32:
        raise DumpError('truncated header')
    signature, _version, count, directory = struct.unpack_from('<IIII', data, 0)
    if signature != SIGNATURE:
        raise DumpError('not a minidump')
    streams = {}
    for index in range(count):
        kind, size, rva = struct.unpack_from('<III', data, directory + 12 * index)
        streams.setdefault(kind, (rva, size))
    result = {'code': None, 'address': None, 'thread': None, 'modules': [], 'module': None, 'offset': None}
    if MODULE_LIST_STREAM in streams:
        rva, _ = streams[MODULE_LIST_STREAM]
        (number,) = struct.unpack_from('<I', data, rva)
        for index in range(number):
            at = rva + 4 + MODULE_SIZE * index
            base, size, _checksum, stamp, name_rva = struct.unpack_from('<QIIII', data, at)
            name = _string(data, name_rva)
            result['modules'].append({'name': name.replace('/', '\\').rsplit('\\', 1)[-1], 'path': name,
                                      'base': base, 'size': size, 'timestamp': stamp})
    if EXCEPTION_STREAM in streams:
        rva, _ = streams[EXCEPTION_STREAM]
        thread, _align, code, _flags, _record, address = struct.unpack_from('<IIIIQQ', data, rva)
        result.update(code=code, address=address, thread=thread)
        for module in result['modules']:
            if module['base'] <= address < module['base'] + module['size']:
                result['module'], result['offset'] = module['name'], address - module['base']
                break
    return result


# The engine writes its crash metadata into memory the dump captures, as msgpack
# key/value records next to a 'quit_reason' key.
# Only these keys are read: the records also hold account and session ids.
METADATA_KEYS = ('quit_reason', 'init', 'recap.processDurationMillis', 'ui_history', 'build', 'engine/branch')
_RECORD = b'\x83\x00\x02\x02'
_FIXED = {0xcc: '>B', 0xcd: '>H', 0xce: '>I', 0xcf: '>Q', 0xd0: '>b', 0xd1: '>h', 0xd2: '>i', 0xd3: '>q',
          0xca: '>f', 0xcb: '>d'}


def _value(data, at):
    kind = data[at]
    if 0xa0 <= kind <= 0xbf or kind in (0xd9, 0xda):
        if kind <= 0xbf:
            length, start = kind - 0xa0, at + 1
        elif kind == 0xd9:
            length, start = data[at + 1], at + 2
        else:
            length, start = struct.unpack_from('>H', data, at + 1)[0], at + 3
        if start + length > len(data):
            raise ValueError('truncated string')
        return data[start:start + length].decode('latin1'), start + length
    if kind <= 0x7f:
        return kind, at + 1
    if kind >= 0xe0:
        return kind - 0x100, at + 1
    if kind in _FIXED:
        return struct.unpack_from(_FIXED[kind], data, at + 1)[0], at + 1 + struct.calcsize(_FIXED[kind])
    if kind in (0xc2, 0xc3):
        return kind == 0xc3, at + 1
    if kind == 0xc0:
        return None, at + 1
    raise ValueError('unsupported msgpack type 0x%x' % kind)


def _records(window):
    found = {}
    at = window.find(_RECORD)
    while at != -1:
        try:
            key, after = _value(window, at + len(_RECORD))
            if window[after] == 0x03 and key in METADATA_KEYS and key not in found:
                found[key] = _value(window, after + 1)[0]
        except (ValueError, IndexError, struct.error):
            pass
        at = window.find(_RECORD, at + 1)
    return found


def metadata(data, attempts=8):
    """{key: value} for METADATA_KEYS from the engine's crash metadata, or {} without any."""
    at = data.find(b'quit_reason')
    for _ in range(attempts):
        if at == -1:
            break
        found = _records(data[max(0, at - 0x20000):at + 0x8000])
        if 'quit_reason' in found:
            return found
        at = data.find(b'quit_reason', at + 1)
    return {}


def classify(info):
    """(kind, meaning) for a parsed dump, using the signatures of the build that crashed."""
    code = info.get('code')
    if code is None:
        return 'no-exception', 'the dump has no exception record'
    if code == builds.LUA_OUT_OF_MEMORY:
        return 'lua-out-of-memory', 'Lua ran out of memory in compiled code (0xE24C4A04)'
    exe = next((m for m in info['modules'] if m['name'].lower() == 'helldivers2.exe'), None)
    build = builds.by_exe_timestamp(exe['timestamp']) if exe else None
    module, offset = (info.get('module') or ''), info.get('offset')
    if build:
        for signature in build.signatures:
            if (module.lower() == signature.module and offset == signature.offset
                    and (signature.code is None or signature.code == code)):
                return signature.kind, signature.meaning
    where = '%s+0x%x' % (module, offset) if module else '0x%x' % (info.get('address') or 0)
    note = '' if build else ' (unknown game build: no signatures)'
    if (info.get('metadata') or {}).get('quit_reason') == 'window_closing':
        note += ' (while the game was closing)'
    return 'unclassified', 'exception 0x%08X at %s; needs cdb triage%s' % (code, where, note)


def describe(path):
    data = Path(path).read_bytes()
    info = parse(data)
    info['metadata'] = metadata(data)
    kind, meaning = classify(info)
    where = '%s+0x%x' % (info['module'], info['offset']) if info['module'] else None
    uptime = info['metadata'].get('recap.processDurationMillis')
    return {'file': Path(path).name, 'kind': kind, 'meaning': meaning,
            'code': None if info['code'] is None else '0x%08X' % info['code'], 'where': where,
            'thread': info['thread'], 'quit_reason': info['metadata'].get('quit_reason'),
            'uptime_s': round(uptime / 1000, 1) if isinstance(uptime, (int, float)) else None}


def listing(folder):
    """{name: size} of the dump folder's files."""
    folder = Path(folder)
    if not folder.exists():
        return {}
    return {entry.name: entry.stat().st_size for entry in folder.iterdir() if entry.is_file()}
