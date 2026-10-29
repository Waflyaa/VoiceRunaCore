#!/usr/bin/env python3
"""Генератор иконок VoiceRunaCore на чистой стандартной библиотеке.

Рисует иконку «V» на тёмном фоне и складывает её в app/src-tauri/icons:

    32x32.png          32x32    — трей и окно
    128x128.png        128x128
    128x128@2x.png     256x256
    icon.png           256x256  — исходник для бандлера
    icon.ico           32/128/256 — контейнер PNG-кадров (Windows/NSIS)
    tray-muted.png     32x32    — иконка трея в состоянии «пауза»

PNG пишется вручную (zlib + struct), ICO — как контейнер готовых PNG-кадров.
Сглаживание аналитическое (по расстоянию до фигуры), внешних зависимостей нет,
результат воспроизводим побайтово.

Запуск:  python tools/make-icons.py
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

ICO_SIZES = (32, 128, 256)

BG_TOP = (22, 26, 36)
BG_BOTTOM = (12, 14, 20)
V_TOP = (79, 220, 255)
V_BOTTOM = (138, 123, 255)
MUTED_V = (138, 143, 156)
MUTED_SLASH = (255, 159, 67)


# --------------------------------------------------------------------------- #
# геометрия (в пикселях итогового изображения)
# --------------------------------------------------------------------------- #
def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[float, float, float]:
    t = _clamp01(t)
    return (_lerp(c1[0], c2[0], t), _lerp(c1[1], c2[1], t), _lerp(c1[2], c2[2], t))


def _sdf_round_rect(px: float, py: float, size: float, radius: float) -> float:
    """Знаковое расстояние до скруглённого квадрата: < 0 внутри."""
    half = size * 0.5
    qx = abs(px - half) - (half - radius)
    qy = abs(py - half) - (half - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _sdf_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length_sq = vx * vx + vy * vy
    t = 0.0 if length_sq <= 1e-9 else _clamp01((wx * vx + wy * vy) / length_sq)
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _coverage(distance: float) -> float:
    """Альфа из расстояния: 1 внутри, 0 снаружи, линейный переход в 1 px."""
    return _clamp01(0.5 - distance)


# --------------------------------------------------------------------------- #
# рендер RGBA (straight alpha)
# --------------------------------------------------------------------------- #
def render(size: int, muted: bool = False) -> bytes:
    radius = size * 0.22
    stroke = size * 0.115 * 0.5  # половина толщины штриха
    ax, ay = size * 0.295, size * 0.285
    bx, by = size * 0.500, size * 0.725
    cx, cy = size * 0.705, size * 0.285

    v_top = MUTED_V if muted else V_TOP
    v_bottom = MUTED_V if muted else V_BOTTOM

    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            bg_cov = _coverage(_sdf_round_rect(px, py, size, radius))
            if bg_cov <= 0.0:
                continue

            br, bg, bb = _mix(BG_TOP, BG_BOTTOM, y / max(1, size - 1))
            glow = _clamp01(1.0 - py / (size * 0.55)) * 0.06
            br = _lerp(br, 255.0, glow)
            bg = _lerp(bg, 255.0, glow)
            bb = _lerp(bb, 255.0, glow)

            d = min(
                _sdf_segment(px, py, ax, ay, bx, by),
                _sdf_segment(px, py, bx, by, cx, cy),
            )
            v_cov = _coverage(d - stroke)
            vr, vg, vb = _mix(v_top, v_bottom, (py - ay) / max(1.0, by - ay))

            if muted:
                slash = _sdf_segment(px, py, size * 0.24, size * 0.76, size * 0.76, size * 0.24)
                slash_cov = _coverage(slash - size * 0.045 * 0.5)
                if slash_cov > 0.0:
                    sr, sg, sb = (float(c) for c in MUTED_SLASH)
                    v_cov = max(v_cov, slash_cov)
                    vr = _lerp(vr, sr, 1.0 if v_cov <= 0.0 else slash_cov / v_cov)
                    vg = _lerp(vg, sg, 1.0 if v_cov <= 0.0 else slash_cov / v_cov)
                    vb = _lerp(vb, sb, 1.0 if v_cov <= 0.0 else slash_cov / v_cov)

            # «V» поверх фона, фон поверх прозрачности
            alpha = v_cov + bg_cov * (1.0 - v_cov)
            if alpha <= 1e-6:
                continue
            r = (vr * v_cov + br * bg_cov * (1.0 - v_cov)) / alpha
            g = (vg * v_cov + bg * bg_cov * (1.0 - v_cov)) / alpha
            b = (vb * v_cov + bb * bg_cov * (1.0 - v_cov)) / alpha

            o = (y * size + x) * 4
            out[o + 0] = int(round(_clamp01(r / 255.0) * 255.0))
            out[o + 1] = int(round(_clamp01(g / 255.0) * 255.0))
            out[o + 2] = int(round(_clamp01(b / 255.0) * 255.0))
            out[o + 3] = int(round(_clamp01(alpha) * 255.0))
    return bytes(out)


# --------------------------------------------------------------------------- #
# запись PNG / ICO
# --------------------------------------------------------------------------- #
def png_bytes(size: int, rgba: bytes) -> bytes:
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(frames: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(frames))
    entries = bytearray()
    blobs = bytearray()
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        dim = 0 if size >= 256 else size  # 0 в ICO означает 256
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    return bytes(header) + bytes(entries) + bytes(blobs)


def main() -> int:
    out_dir = Path(__file__).resolve().parent.parent / "app" / "src-tauri" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)

    cache: dict[tuple[int, bool], bytes] = {}

    def rgba(size: int, muted: bool = False) -> bytes:
        key = (size, muted)
        if key not in cache:
            cache[key] = render(size, muted=muted)
        return cache[key]

    targets = {
        "32x32.png": (32, False),
        "128x128.png": (128, False),
        "128x128@2x.png": (256, False),
        "icon.png": (256, False),
        "tray-muted.png": (32, True),
    }
    for name, (size, muted) in targets.items():
        data = png_bytes(size, rgba(size, muted))
        (out_dir / name).write_bytes(data)
        print(f"  {name:<18} {size:>3}x{size:<3} {len(data):>7} байт")

    frames = [(size, png_bytes(size, rgba(size, False))) for size in ICO_SIZES]
    ico = ico_bytes(frames)
    (out_dir / "icon.ico").write_bytes(ico)
    print(f"  {'icon.ico':<18} {'/'.join(str(s) for s in ICO_SIZES):<11} {len(ico):>7} байт")
    print(f"Готово: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
