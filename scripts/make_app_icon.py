"""Draws the app's icon with nothing but the standard library: a rounded blue square with three candlesticks.

Writes tradingagents/assets/app-icon.ico (several sizes, for the Windows shortcut) and app-icon.png (256 px, for Linux).
To restyle it, change the colors or the CANDLES list below and run:  python scripts/make_app_icon.py
"""

import struct
import zlib
from pathlib import Path

BACKGROUND = (11, 61, 145)                     # the blue of the app's buttons
RED, GREEN = (229, 83, 75), (47, 191, 113)
# (center x, wick top, wick bottom, body top, body bottom, color), in a 100 x 100 drawing box
CANDLES = [(30, 30, 78, 44, 66, RED), (50, 22, 70, 32, 56, GREEN), (70, 14, 60, 22, 46, GREEN)]
WICK, BODY, CORNER, MARGIN = 3.0, 14.0, 20.0, 2.0
SIZES = (16, 24, 32, 48, 64, 128, 256)
ASSETS = Path(__file__).resolve().parent.parent / "tradingagents" / "assets"


def _in_rounded_square(x: float, y: float) -> bool:
    lo, hi = MARGIN, 100 - MARGIN
    if not (lo <= x <= hi and lo <= y <= hi):
        return False
    cx = min(max(x, lo + CORNER), hi - CORNER)                 # the nearest point of the inner square: a corner is a circle around it
    cy = min(max(y, lo + CORNER), hi - CORNER)
    return (x - cx) ** 2 + (y - cy) ** 2 <= CORNER ** 2


def _color_at(x: float, y: float):
    """The color of the drawing at one point of the 100 x 100 box, or None outside the icon."""
    if not _in_rounded_square(x, y):
        return None
    for cx, wick_top, wick_bottom, body_top, body_bottom, color in CANDLES:
        if abs(x - cx) <= BODY / 2 and body_top <= y <= body_bottom:
            return color
        if abs(x - cx) <= WICK / 2 and wick_top <= y <= wick_bottom:
            return color
    return BACKGROUND


def render(size: int, samples: int = 4) -> bytes:
    """RGBA rows for a size x size picture, each pixel the average of samples x samples points (smooth edges)."""
    rows = bytearray()
    for py in range(size):
        rows.append(0)                                        # PNG filter type 0 for this row
        for px in range(size):
            r = g = b = hit = 0
            for sy in range(samples):
                for sx in range(samples):
                    c = _color_at((px + (sx + 0.5) / samples) / size * 100, (py + (sy + 0.5) / samples) / size * 100)
                    if c:
                        r, g, b, hit = r + c[0], g + c[1], b + c[2], hit + 1
            total = samples * samples
            if hit:
                rows += bytes((round(r / hit), round(g / hit), round(b / hit), round(255 * hit / total)))
            else:
                rows += bytes((0, 0, 0, 0))
    return bytes(rows)


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)       # 8 bits per channel, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b"")


def ico(sizes) -> bytes:
    images = [(s, png(s)) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)      # 256 is stored as 0
        offset += len(data)
    return out + b"".join(data for _, data in images)


if __name__ == "__main__":
    (ASSETS / "app-icon.ico").write_bytes(ico(SIZES))
    (ASSETS / "app-icon.png").write_bytes(png(256))
    print("wrote", ASSETS / "app-icon.ico", "and", ASSETS / "app-icon.png")
