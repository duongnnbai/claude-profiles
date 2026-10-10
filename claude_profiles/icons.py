"""Images drawn in pure Python (no Pillow): profile icons, card backgrounds and buttons.

Shapes are described by signed distance functions (negative inside), which gives
anti-aliased edges with one sample per pixel. Output is PNG, plus ICO (Windows)
and ICNS (macOS) wrappers around PNG data.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

from .core import COLORS


def hex_to_rgb(color: str) -> tuple:
    h = color.lstrip('#')
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class Raster:
    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.buf = [0.0] * (width * height * 4)   # premultiplied RGBA, 0..1

    def draw(self, sdf, bbox, color, alpha=1.0, soft=1.0):
        """Composite a shape over the raster. ``color`` is an RGB tuple or a function (x, y) -> RGB."""
        half = soft / 2
        x0 = max(0, int(math.floor(bbox[0] - half)))
        y0 = max(0, int(math.floor(bbox[1] - half)))
        x1 = min(self.w, int(math.ceil(bbox[2] + half)))
        y1 = min(self.h, int(math.ceil(bbox[3] + half)))
        buf, w = self.buf, self.w
        solid = not callable(color)
        if solid:
            r, g, b = color
        for y in range(y0, y1):
            py = y + 0.5
            for x in range(x0, x1):
                px = x + 0.5
                d = sdf(px, py)
                if d >= half:
                    continue
                a = alpha if d <= -half else alpha * (half - d) / soft
                if not solid:
                    r, g, b = color(px, py)
                i = (y * w + x) * 4
                inv = 1 - a
                buf[i] = r * a + buf[i] * inv
                buf[i + 1] = g * a + buf[i + 1] * inv
                buf[i + 2] = b * a + buf[i + 2] * inv
                buf[i + 3] = a + buf[i + 3] * inv

    def png(self) -> bytes:
        out = bytearray(len(self.buf))
        buf = self.buf
        for i in range(0, len(buf), 4):
            a = buf[i + 3]
            if a > 0:
                out[i] = min(255, round(buf[i] / a * 255))
                out[i + 1] = min(255, round(buf[i + 1] / a * 255))
                out[i + 2] = min(255, round(buf[i + 2] / a * 255))
                out[i + 3] = min(255, round(a * 255))
        return encode_png(self.w, self.h, bytes(out))


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    stride = width * 4
    raw = b''.join(b'\x00' + rgba[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6))
            + chunk(b'IEND', b''))


# ---- Shapes ------------------------------------------------------------------

def rounded_rect(x0, y0, x1, y1, radius):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
    r = max(0.0, min(radius, hw, hh))
    ix, iy = hw - r, hh - r

    def sdf(px, py):
        qx, qy = abs(px - cx) - ix, abs(py - cy) - iy
        if qx > 0 and qy > 0:
            return math.hypot(qx, qy) - r
        return max(qx, qy) - r
    return sdf


def capsules(segments, width):
    """Union of thick line segments with round caps."""
    hw = width / 2
    segs = []
    for ax, ay, bx, by in segments:
        dx, dy = bx - ax, by - ay
        segs.append((ax, ay, dx, dy, dx * dx + dy * dy or 1.0))

    def sdf(px, py):
        best = 1e9
        for ax, ay, dx, dy, l2 in segs:
            t = ((px - ax) * dx + (py - ay) * dy) / l2
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d < best:
                best = d
        return best - hw
    return sdf


# ---- Profile icons -----------------------------------------------------------

def draw_icon(color: str, size: int) -> bytes:
    """A rounded square in the profile color (orange | blue halves for 'app') with a white spark, as PNG."""
    s = size / 256
    ras = Raster(size, size)
    square = rounded_rect(0, 0, size, size, 58 * s)
    if color == 'app':
        h = size / 2
        left, right = hex_to_rgb(COLORS['orange']), hex_to_rgb(COLORS['blue'])
        fill = lambda x, y: left if x < h else right   # noqa: E731
    else:
        fill = hex_to_rgb(COLORS[color])
    ras.draw(square, (0, 0, size, size), fill)
    c = size / 2
    segs = []
    for i in range(12):
        a = math.pi * 2 * i / 12
        length = 84 if i % 2 == 0 else 66
        segs.append((c + math.cos(a) * 16 * s, c + math.sin(a) * 16 * s,
                     c + math.cos(a) * length * s, c + math.sin(a) * length * s))
    reach = (84 + 9) * s
    ras.draw(capsules(segs, 17 * s), (c - reach, c - reach, c + reach, c + reach), (1, 1, 1))
    return ras.png()


ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)


def ico_from_pngs(pngs: dict) -> bytes:
    """An ICO from {size: PNG bytes}; PNG entries are readable since Windows Vista."""
    count = len(pngs)
    header = struct.pack('<HHH', 0, 1, count)
    entries, offset = b'', 6 + 16 * count
    for size, png in sorted(pngs.items()):
        dim = 0 if size >= 256 else size
        entries += struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return header + entries + b''.join(png for _, png in sorted(pngs.items()))


def icns_from_pngs(pngs: dict) -> bytes:
    """An ICNS file from {'ic07': 128px PNG, 'ic08': 256px PNG, ...}."""
    body = b''.join(kind.encode('ascii') + struct.pack('>I', len(data) + 8) + data for kind, data in pngs.items())
    return b'icns' + struct.pack('>I', len(body) + 8) + body


def icon_file(icon_dir: Path, color: str, ext: str = 'png') -> Path:
    """Path to a cached icon file (png, ico or icns), drawn on first use."""
    # Explorer caches icons by path: a new name whenever the artwork or format changes.
    path = icon_dir / f'spark-{color}-3.{ext}'
    if not path.exists():
        icon_dir.mkdir(parents=True, exist_ok=True)
        if ext == 'icns':
            data = icns_from_pngs({'ic07': draw_icon(color, 128), 'ic08': draw_icon(color, 256)})
        elif ext == 'ico':
            data = ico_from_pngs({s: draw_icon(color, s) for s in ICO_SIZES})
        else:
            data = draw_icon(color, 256)
        path.write_bytes(data)
    return path


# ---- UI surfaces -------------------------------------------------------------

def panel_png(width, height, rect, radius, fill=None, alpha=1.0, border=None, border_width=1.0, shadow=None) -> bytes:
    """A rounded rectangle inside a transparent image: optional soft shadow, fill and inner border.

    ``shadow`` is (opacity, blur, offset_y)."""
    ras = Raster(width, height)
    x0, y0, x1, y1 = rect
    if shadow:
        opacity, blur, dy = shadow
        ras.draw(rounded_rect(x0, y0 + dy, x1, y1 + dy, radius), (x0, y0 + dy, x1, y1 + dy),
                 (0, 0, 0), opacity, soft=blur)
    if fill:
        ras.draw(rounded_rect(x0, y0, x1, y1, radius), rect, hex_to_rgb(fill), alpha)
    if border:
        inset = border_width / 2
        inner = rounded_rect(x0 + inset, y0 + inset, x1 - inset, y1 - inset, max(0.0, radius - inset))
        ras.draw(lambda px, py: abs(inner(px, py)) - inset, rect, hex_to_rgb(border), alpha)
    return ras.png()


def swatch_png(size, color, selected, ring_width=2.0, gap=2.0) -> bytes:
    """A color dot; when selected, a white ring inside it marks the choice."""
    ras = Raster(size, size)
    ras.draw(rounded_rect(0, 0, size, size, size / 2), (0, 0, size, size), hex_to_rgb(color))
    if selected:
        c, r = size / 2, size / 2 - gap - ring_width / 2
        ras.draw(lambda px, py: abs(math.hypot(px - c, py - c) - r) - ring_width / 2,
                 (0, 0, size, size), (1, 1, 1))
    return ras.png()


def circle_png(size, color, border=None, border_width=0.0, box=None) -> bytes:
    box = box or size
    o = (box - size) / 2
    return panel_png(box, box, (o, o, o + size, o + size), size / 2, color, border=border, border_width=border_width)
