"""Stingray patch archives for discoverable Lua addons.

resource_hash and make_archive are copies of BingusSharedLoader/scripts/archive.py;
tests/test_archive.py checks that both produce identical bytes.
"""
import struct

FAMILY = '9ba626afa44a3aa3'
LUA_TYPE = 0xA14E8DFA2CD117E2
MAGIC = 0xF0000011
AGENT_NAME = 'mods/cowboybingus/bingus_test_agent'
# The loader's own startup override (core/wwise/lua/wwise_flow_callbacks).
LOADER_CALLBACKS = 0x7251FDD9BB62480A


def resource_hash(name):
    data = name.encode('utf-8')
    mask, mix = (1 << 64) - 1, 0xC6A4A7935BD1E995
    value = len(data) * mix & mask
    end = len(data) // 8 * 8
    for (word,) in struct.iter_unpack('<Q', data[:end]):
        word = word * mix & mask
        word ^= word >> 47
        value = (value ^ (word * mix & mask)) * mix & mask
    if data[end:]:
        value = (value ^ int.from_bytes(data[end:], 'little')) * mix & mask
    value ^= value >> 47
    value = value * mix & mask
    return value ^ (value >> 47)


def make_archive(resources):
    if not resources:
        raise ValueError('An archive needs at least one resource')
    count = len(resources)
    offset = (104 + 80 * count + 15) & ~15
    entries, body = bytearray(), bytearray(offset)
    for index, (name, resource) in enumerate(sorted(resources.items())):
        entries += struct.pack('<7Q6I', name, LUA_TYPE, offset, 0, 0, 0, 0,
                               len(resource), 0, 0, 16, 16, index)
        body += resource
        body += b'\0' * (-len(body) % 16)
        offset = len(body)
    header = struct.pack('<III20sQQ24s', MAGIC, 1, count, b'', offset, 0, b'')
    types = struct.pack('<IIQIIII', 0, 0, LUA_TYPE, count, 0, 16, 16)
    body[:104 + len(entries)] = header + types + entries
    return bytes(body)


def addon_resource(name, source):
    """The Lua resource body for a plaintext addon whose first line declares `name`."""
    declaration = ('-- HD2-Addon: ' + name).encode('utf-8')
    first = source.split(b'\n', 1)[0].rstrip(b'\r')
    if first != declaration:
        raise ValueError('The first line must be "-- HD2-Addon: %s"' % name)
    if source.startswith((b'\xef\xbb\xbf', b'\x1b')) or b'\0' in source:
        raise ValueError('Addon source must be plaintext UTF-8 Lua without a BOM')
    source.decode('utf-8')
    return struct.pack('<II', len(source), 2) + source


def agent_archive(source):
    return make_archive({resource_hash(AGENT_NAME): addon_resource(AGENT_NAME, source)})


def entries(archive):
    """(name hash, type hash, offset, size) for every resource in an archive's table."""
    if len(archive) < 72:
        raise ValueError('truncated archive header')
    magic, types, count = struct.unpack_from('<III', archive, 0)
    if magic != MAGIC:
        raise ValueError('not a Stingray archive')
    start = 72 + 32 * types
    if start + 80 * count > len(archive):
        raise ValueError('truncated archive tables')
    result = []
    for index in range(count):
        name, kind, offset = struct.unpack_from('<QQQ', archive, start + 80 * index)
        size = struct.unpack_from('<I', archive, start + 80 * index + 56)[0]
        result.append((name, kind, offset, size))
    return result


def contains(archive, name_hash):
    return any(entry[0] == name_hash for entry in entries(archive))
