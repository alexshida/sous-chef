#!/usr/bin/env python3
"""Generate the home-screen icons, using only the standard library.

Run from the repo root:  python tools/make_icons.py

A bowl with chopsticks on the app's accent colour. Rendered at 4× and averaged
down for anti-aliasing — enough for artwork this simple, and no imaging
library for a handful of PNGs written once. iOS rounds the corners itself, so
these are full opaque squares.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent.parent / "src" / "sous_chef" / "web" / "static" / "icons"
SIZES = (180, 192, 512)
SS = 4

BG_TOP = (234, 88, 12)       # orange-600
BG_BOTTOM = (194, 65, 12)    # --accent
WHITE = (255, 255, 255)
CREAM = (254, 215, 170)


def write_png(path: Path, width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> None:
    raw = b"".join(b"\x00" + b"".join(struct.pack("3B", *px) for px in row) for row in pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def _seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def colour_at(x: float, y: float) -> tuple[int, int, int]:
    """The artwork in unit coordinates, (0,0) top-left."""
    bg = tuple(round(a + (b - a) * y) for a, b in zip(BG_TOP, BG_BOTTOM))
    # chopsticks, behind the bowl
    for ax, ay, bx, by in ((0.50, 0.50, 0.80, 0.17), (0.57, 0.52, 0.89, 0.24)):
        if _seg_dist(x, y, ax, ay, bx, by) < 0.024:
            return CREAM
    # rim
    if 0.47 <= y <= 0.53 and 0.16 <= x <= 0.84:
        return WHITE
    # bowl: lower half-disc
    if y >= 0.50 and (x - 0.5) ** 2 + (y - 0.50) ** 2 <= 0.31 ** 2:
        return WHITE
    # foot
    if 0.80 <= y <= 0.85 and 0.39 <= x <= 0.61:
        return WHITE
    return bg


def render(size: int) -> list[list[tuple[int, int, int]]]:
    big = size * SS
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            acc = [0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = colour_at((px * SS + sx + 0.5) / big, (py * SS + sy + 0.5) / big)
                    acc[0] += c[0]
                    acc[1] += c[1]
                    acc[2] += c[2]
            n = SS * SS
            row.append((acc[0] // n, acc[1] // n, acc[2] // n))
        rows.append(row)
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT / f"icon-{size}.png"
        write_png(path, size, size, render(size))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
