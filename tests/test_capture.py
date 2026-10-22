"""Captures: PNG encoding and the score that tells a rendered 3D world from the gray no-world frame."""
import math
import random
import struct
import unittest
import zlib

import support
from hd2h import capture

CAPTURES = support.CAPTURES


def gray_vignette(width, height):
    """The frame a too-small window gave on 2026-10-13: flat gray, darker towards the corners, HUD boxes."""
    rgb = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            d = math.hypot((x - width / 2) / width, (y - height / 2) / height)
            level = max(40, int(140 - 120 * max(0.0, d - 0.3)))
            if (x < 120 and y < 40) or (x > width - 140 and y > height - 40):  # HUD boxes
                level = 200
            i = 3 * (y * width + x)
            rgb[i:i + 3] = bytes((level, level + 2, level + 5))
    return bytes(rgb)


def scene(width, height, seed=1):
    """A coloured, textured frame (sky, sand, rocks)."""
    rnd, rgb = random.Random(seed), bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            base = (90, 140, 210) if y < height // 3 else (190, 140, 80)
            jitter = rnd.randint(-25, 25)
            i = 3 * (y * width + x)
            rgb[i:i + 3] = bytes(max(0, min(255, c + jitter)) for c in base)
    return bytes(rgb)


def decode_png(data):
    """(width, height, rgb) of an 8-bit RGB PNG with filter 0 rows (what capture.png writes)."""
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    at, idat, width = 8, b'', None
    while at < len(data):
        length, kind = struct.unpack('>I4s', data[at:at + 8])
        body = data[at + 8:at + 8 + length]
        crc = struct.unpack('>I', data[at + 8 + length:at + 12 + length])[0]
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, kind
        if kind == b'IHDR':
            width, height, depth, colour = struct.unpack('>IIBB', body[:10])
            assert (depth, colour) == (8, 2)
        elif kind == b'IDAT':
            idat += body
        at += 12 + length
    raw, stride = zlib.decompress(idat), width * 3
    rows = [raw[y * (stride + 1):(y + 1) * (stride + 1)] for y in range(height)]
    assert all(row[0] == 0 for row in rows)
    return width, height, b''.join(row[1:] for row in rows)


class Capture(unittest.TestCase):
    def test_png_round_trip_and_bgra_order(self):
        width, height = 7, 3
        bgra = bytes(i % 256 for i in range(width * height * 4))
        rgb = capture.bgra_to_rgb(bgra)
        self.assertEqual(rgb[:3], bytes((bgra[2], bgra[1], bgra[0])))
        self.assertEqual(decode_png(capture.png(width, height, rgb)), (width, height, rgb))

    def test_score_tells_the_world_from_the_gray_frame(self):
        width, height = 320, 180
        gray = capture.score(width, height, gray_vignette(width, height))
        world = capture.score(width, height, scene(width, height))
        self.assertFalse(gray['world'], gray)
        self.assertTrue(world['world'], world)
        self.assertLess(gray['saturation'], capture.MIN_SATURATION)

    @unittest.skipUnless(CAPTURES and (CAPTURES / 'deploy4' / '7-mission-5s.png').exists(),
                         'live captures not available (HD2H_CAPTURES)')
    def test_reported_case_scores_like_the_review(self):
        # 2026-10-13: the deploy3 captures were the gray no-world frame, the deploy4 ones the real mission.
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('PIL not installed')
        for name, expected in (('deploy3/5-mission-10s.png', False), ('deploy3/1-briefing.png', False),
                               ('deploy4/7-mission-5s.png', True), ('deploy4/1-briefing.png', True),
                               ('deploy4/9-ship-after.png', True)):
            image = Image.open(CAPTURES / name).convert('RGB')
            result = capture.score(image.width, image.height, image.tobytes())
            self.assertEqual(result['world'], expected, '%s %s' % (name, result))


if __name__ == '__main__':
    unittest.main()
