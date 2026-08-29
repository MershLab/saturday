"""Generate Saturday brand icons (.ico / .icns / .png sizes) from the slashed-S mark.

Run:  python packaging/gen_icons.py
Out:  packaging/icons/saturday.{ico,icns,png} + size PNGs.
Requires pillow (optional [desktop] extra). Safe to re-run; deterministic output.

Mark: the letter S broken into one chunky zigzag ribbon with parallel slash
cuts - a white monogram on a charcoal rounded tile, in the spirit of z.ai's
slashed-letter mark. The cyan accent lives in the UI, not in the logomark.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"

TILE = (13, 14, 18, 255)  # #0d0e12 near-black, slight blue
MARK = (244, 245, 247, 255)  # off-white

# S centerline on a 32-unit grid: top bar right->left, spine down-right,
# bottom bar right->left (sharp S = mirrored Z, z.ai construction).
S_PTS = [(22.3, 8.0), (9.7, 8.0), (22.2, 24.0), (9.7, 24.0)]
STROKE = 4.5


def _line_int(p1, p2, p3, p4):
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) < 1e-9:
        return p2
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def _miter_polygon(pts, w):
    """Polygon of a polyline stroked width w with miter joins."""
    ls, rs = [], []
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        d = (x2 - x1, y2 - y1)
        length = math.hypot(*d)
        nx, ny = -d[1] / length, d[0] / length
        ls.append(((x1 + nx * w / 2, y1 + ny * w / 2), (x2 + nx * w / 2, y2 + ny * w / 2)))
        rs.append(((x1 - nx * w / 2, y1 - ny * w / 2), (x2 - nx * w / 2, y2 - ny * w / 2)))
    left = [ls[0][0]]
    for i in range(len(pts) - 2):
        left.append(_line_int(ls[i][0], ls[i][1], ls[i + 1][0], ls[i + 1][1]))
    left.append(ls[-1][1])
    right = [rs[0][0]]
    for i in range(len(pts) - 2):
        right.append(_line_int(rs[i][0], rs[i][1], rs[i + 1][0], rs[i + 1][1]))
    right.append(rs[-1][1])
    return left + right[::-1]


def _band(p, q, w):
    """Thin parallelogram band around segment p->q (for the slash cuts)."""
    return _miter_polygon([p, q], w)


def _draw_mark(px: int) -> Image.Image:
    s = px / 32.0
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # charcoal rounded tile (full bleed, like a flat app icon)
    d.rounded_rectangle([0, 0, px - 1, px - 1], radius=6.9 * s, fill=TILE)

    # the S ribbon
    d.polygon([(x * s, y * s) for x, y in _miter_polygon(S_PTS, STROKE)], fill=MARK)

    # parallel slash cuts that detach thin slivers at both bends
    for p, q in [((9.8, 10.4), (14.2, 17.2)), ((17.8, 14.8), (22.2, 21.6))]:
        d.polygon([(x * s, y * s) for x, y in _band(p, q, 1.5)], fill=TILE)

    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    master = _draw_mark(2048)  # supersampled; downscale keeps edges smooth

    master.resize((512, 512), Image.LANCZOS).save(OUT / "saturday.png")
    for px in (16, 24, 32, 48, 64, 128, 256):
        master.resize((px, px), Image.LANCZOS).save(OUT / f"saturday-{px}.png")

    sizes = [(px, px) for px in (16, 24, 32, 48, 64, 128, 256)]
    master.resize((256, 256), Image.LANCZOS).save(
        OUT / "saturday.ico", sizes=sizes, append_images=[master.resize(sz, Image.LANCZOS) for sz in sizes[:-1]]
    )

    icns_sizes = [16, 32, 64, 128, 256, 512]
    imgs = [master.resize((px, px), Image.LANCZOS) for px in icns_sizes]
    imgs[-1].save(OUT / "saturday.icns", format="ICNS", append_images=imgs[:-1])

    print("wrote:", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
