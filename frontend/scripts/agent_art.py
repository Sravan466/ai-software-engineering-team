"""
Turns the generated crew art in `assets/` into what AgentSprite plays.

    python3 frontend/scripts/agent_art.py            # from the repo root
    python3 frontend/scripts/agent_art.py atlas      # just the named agents
    (needs Pillow; writes WebP through `cwebp`)

Inputs, per agent: a still (`scope.png`) and a 5x4 sprite sheet
(`Scope Sprite Sheet.png`), both 1254x1254 out of an image model. Outputs, in
`frontend/public/agents/`:

    <codename>.webp        the sheet, re-cut onto an even 4x5 grid of CELL cells
    <codename>-still.webp  the still at ICON px, for the smallest renders

The model output can't be used as-is, for three reasons this script exists to fix:

  1. Some sheets came back with a checkerboard *painted in* instead of alpha.
     It is flood-filled away from the edges; the figures all have a dark
     outline, so the fill stops at the character.
  2. The frames aren't on an even grid — the "!" and the effect marks spill
     into the row above. Each frame is found by its own pixels (the largest
     connected shape near the centre of its slot is the character; smaller
     shapes in the slot are its effects) rather than by slicing in fifths.
  3. Frames drift a few pixels. Every frame is re-anchored on its feet: the
     bottom of the character and the centre of its lowest rows. Arms, tools and
     heads move between frames; feet don't, so that is what stays put.

All eight are scaled by one factor so the crew keeps its relative builds
(FORGE is meant to be the broadest).

The source art lives in `assets/` at the repo root (raw model output, kept so
the WebPs in public/agents/ can always be rebuilt). To change a character,
replace its still and sheet in `assets/` under the names in AGENTS and rerun
for that agent.

ATLAS: its first sheet runs off the canvas in the last row, which this script
refuses, so `public/agents/atlas.webp` is a hand-made stand-in (its idle
frames reused for the waiting row, with its own "!" on frames 1–2). Running
the script for every agent stops at ATLAS until a regenerated sheet is in
place — name the other seven to rebuild them meanwhile.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "assets"
OUT = ROOT / "frontend" / "public" / "agents"

COLS, ROWS = 4, 5
CELL = 224  # 104px inspector portrait at 2x DPR, with headroom
ICON = 96  # stills are only used at <= 32px
FEET_Y = 0.95  # where the soles land in a cell (matches .sprite transform-origin)
MAX_H = 0.80  # tallest idle figure fills this much of the cell
MAX_W = 0.98

AGENTS = {
    # codename: (still, sheet)
    "scope": ("scope.png", "Scope Sprite Sheet.png"),
    "atlas": ("Atlas.png", "ATLAS Sprite Sheet.png"),
    "forge": ("FORGE.png", "ForgeSprite Sheet.png"),
    "prism": ("PRISM.png", "Prism Sprite Sheet.png"),
    "sieve": ("SIEVE .png", "Sieve Sprite Sheet.png"),
    "warden": ("WARDEN.png", "Warden Sprite Sheet.png"),
    "relay": ("RELAY.png", "Relay Sprite Sheet.png"),
    "ledger": ("LEDGER.png", "Ledger Sprite Sheet.png"),
}


def strip_painted_checkerboard(im: Image.Image) -> Image.Image:
    """Clear a light-grey checkerboard that was drawn instead of alpha."""
    im = im.convert("RGBA")
    W, H = im.size
    px = im.load()
    if px[2, 2][3] == 0:
        return im  # real transparency already

    def bg(p):
        r, g, b, _ = p
        return max(r, g, b) - min(r, g, b) < 14 and min(r, g, b) >= 185

    seen = bytearray(W * H)
    q = deque([(x, 0) for x in range(W)] + [(x, H - 1) for x in range(W)])
    q.extend([(0, y) for y in range(H)] + [(W - 1, y) for y in range(H)])
    while q:
        x, y = q.popleft()
        i = y * W + x
        if seen[i] or not bg(px[x, y]):
            continue
        seen[i] = 1
        px[x, y] = (0, 0, 0, 0)
        if x > 0:
            q.append((x - 1, y))
        if x < W - 1:
            q.append((x + 1, y))
        if y > 0:
            q.append((x, y - 1))
        if y < H - 1:
            q.append((x, y + 1))

    # Checkerboard trapped inside a closed shape (between an arm and the body):
    # a large light region that alternates between the two board tones.
    seen = bytearray(W * H)
    for sy in range(H):
        for sx in range(W):
            i = sy * W + sx
            if seen[i] or not px[sx, sy][3] or not bg(px[sx, sy]):
                continue
            comp, q = [], deque([(sx, sy)])
            seen[i] = 1
            while q:
                x, y = q.popleft()
                comp.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < W and 0 <= ny < H:
                        j = ny * W + nx
                        if not seen[j] and px[nx, ny][3] and bg(px[nx, ny]):
                            seen[j] = 1
                            q.append((nx, ny))
            tones = {px[x, y][0] // 16 for x, y in comp}
            if len(comp) > 150 and len(tones) >= 2 and min(px[x, y][0] for x, y in comp) < 235:
                for x, y in comp:
                    px[x, y] = (0, 0, 0, 0)

    # One pixel of light fringe left against the cleared area.
    alpha = im.getchannel("A").load()
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            if alpha[x, y] and bg(px[x, y]) and not (
                alpha[x - 1, y] and alpha[x + 1, y] and alpha[x, y - 1] and alpha[x, y + 1]
            ):
                px[x, y] = (0, 0, 0, 0)
    return im


def components(alpha, box, threshold=40):
    """Connected opaque shapes inside box: list of (pixel count, bbox, pixels)."""
    x0, y0, x1, y1 = box
    W = x1 - x0
    seen = bytearray(W * (y1 - y0))
    out = []
    for sy in range(y0, y1):
        for sx in range(x0, x1):
            i = (sy - y0) * W + (sx - x0)
            if seen[i] or alpha[sx, sy] <= threshold:
                continue
            seen[i] = 1
            q, pts = deque([(sx, sy)]), []
            while q:
                x, y = q.popleft()
                pts.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if x0 <= nx < x1 and y0 <= ny < y1:
                        j = (ny - y0) * W + (nx - x0)
                        if not seen[j] and alpha[nx, ny] > threshold:
                            seen[j] = 1
                            q.append((nx, ny))
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append((len(pts), (min(xs), min(ys), max(xs) + 1, max(ys) + 1), pts))
    return out


def feet_anchor(pts, bbox):
    """(centre x of the lowest 12% of the figure, bottom y)."""
    bottom = bbox[3]
    cut = bottom - max(4, int((bbox[3] - bbox[1]) * 0.12))
    xs = [x for x, y in pts if y >= cut]
    return (min(xs) + max(xs)) / 2, bottom


def cut_lines(counts, n, reach):
    """n-1 cut positions: the gap nearest each nominal grid line.

    A gap is a run of lines with no ink. Image models don't keep an even pitch
    (one sheet's rows sit ~60px low), so the search reaches well past the
    nominal line; if no clean gap is in reach, the emptiest line is used.
    """
    size = len(counts)
    gaps, i = [], 0
    while i < size:
        if counts[i] == 0:
            j = i
            while j < size and counts[j] == 0:
                j += 1
            if 0 < i and j < size:  # interior gaps only, not the margins
                gaps.append((i + j) // 2)
            i = j
        else:
            i += 1
    out = []
    for k in range(1, n):
        near = round(k * size / n)
        inside = [g for g in gaps if abs(g - near) <= reach]
        if inside:
            out.append(min(inside, key=lambda g: abs(g - near)))
        else:
            lo, hi = max(0, near - reach), min(size - 1, near + reach)
            out.append(min(range(lo, hi + 1), key=lambda g: (counts[g], abs(g - near))))
    return out


def frames(sheet: Image.Image):
    """Yield (row, col, frame image, anchor, figure bbox) for each slot of the sheet.

    Slots are bounded by the empty gaps between figures, not by an even grid:
    the figures crowd the gaps, and their "!" and effect marks sit in them.
    """
    alpha = sheet.getchannel("A").load()
    W, H = sheet.size
    ink_x = [sum(1 for y in range(0, H, 2) if alpha[x, y] > 40) for x in range(W)]
    xs = [0] + cut_lines(ink_x, COLS, round(W / COLS * 0.35)) + [W]
    idle_h = {}
    bad = []
    for c in range(COLS):
        x0, x1 = xs[c], xs[c + 1]
        ink_y = [sum(1 for x in range(x0, x1) if alpha[x, y] > 40) for y in range(H)]
        ys = [0] + cut_lines(ink_y, ROWS, round(H / ROWS * 0.4)) + [H]
        for r in range(ROWS):
            slot = (x0, ys[r], x1, ys[r + 1])
            mine = [k for k in components(alpha, slot) if k[0] > 12]
            if not mine:
                raise SystemExit(f"empty frame r{r} c{c}")
            figure = max(mine, key=lambda k: k[0])
            # A neighbour's sliver sits on the slot's side edge; the figure's
            # own marks float free of it.
            keep = [k for k in mine if k is figure or not (k[1][0] <= x0 or k[1][2] >= x1)]
            mask = Image.new("L", sheet.size, 0)
            mp = mask.load()
            for k in keep:
                for x, y in k[2]:
                    mp[x, y] = 255
            # Grow the mask by a pixel so soft edges around each shape survive;
            # only the slot is pasted, so it can't reach a neighbour.
            mask = mask.filter(ImageFilter.MaxFilter(3))
            frame = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
            frame.paste(sheet.crop(slot), slot[:2])
            frame = Image.composite(frame, Image.new("RGBA", sheet.size, (0, 0, 0, 0)), mask)
            bbox = figure[1]
            anchor = feet_anchor(figure[2], bbox)

            if r == 0:
                idle_h[c] = bbox[3] - bbox[1]
            elif bbox[3] >= H - 2 and (bbox[3] - bbox[1]) < 0.8 * idle_h[c]:
                # Touching the edge alone isn't enough — most sheets put the soles
                # on the last pixel. A figure clearly shorter than its idle self
                # has lost its feet, and that can't be repaired here. Reported
                # once the whole sheet is cut, so the idle row is always complete.
                bad.append(f"row {r + 1}, frame {c + 1}")
            yield r, c, frame, anchor, bbox
    if bad:
        raise SystemExit(
            f"{', '.join(bad)} run off the bottom of the sheet — "
            "regenerate it with a margin on every edge"
        )


def place(frame, anchor, scale, cell=CELL):
    """Scale the frame about its feet and drop it on a cell at FEET_Y."""
    ax, ay = anchor
    bbox = frame.getbbox()
    crop = frame.crop(bbox)
    w, h = crop.size
    crop = crop.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    ox = cell / 2 - (ax - bbox[0]) * scale
    oy = cell * FEET_Y - (ay - bbox[1]) * scale
    out = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
    # The scale is fitted to the idle row, so a tool swung wide or a jump can
    # reach past the cell. A plain paste onto the empty cell copies the pixels
    # exactly and clips at the edge, where alpha_composite would refuse a
    # negative offset.
    out.paste(crop, (round(ox), round(oy)))
    return out


def webp(img: Image.Image, dest: Path):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.save(tmp.name)
    try:
        subprocess.run(
            ["cwebp", "-quiet", "-q", "82", "-alpha_q", "100", "-exact", "-m", "6", tmp.name, "-o", str(dest)],
            check=True,
        )
    finally:
        os.unlink(tmp.name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:]) or set(AGENTS)
    if only - set(AGENTS):
        raise SystemExit(f"unknown agent: {', '.join(sorted(only - set(AGENTS)))}")
    # Every sheet is cut even when only some are written: the shared scale
    # depends on all eight, so a rebuilt agent stays the same size as the rest.
    cut = {}
    for name, (_, sheet_file) in AGENTS.items():
        sheet = strip_painted_checkerboard(Image.open(SRC / sheet_file))
        cut[name] = []
        try:
            for f in frames(sheet):
                cut[name].append(f)
        except SystemExit as e:
            # Fatal only for a sheet being written; for the others the idle
            # row is all the shared scale needs, and it is always cut in full.
            if name in only or sum(1 for r, *_ in cut[name] if r == 0) < COLS:
                raise SystemExit(f"{sheet_file}: {e}") from None
            print(f"skip {name}: {e}")
            continue
        print(f"cut {name}: {len(cut[name])} frames")

    # One scale for the whole crew: the largest that fits every idle figure.
    scale = min(
        min(CELL * MAX_H / (b[3] - b[1]), CELL * MAX_W / (b[2] - b[0]))
        for fr in cut.values()
        for r, _c, _f, _a, b in fr
        if r == 0
    )

    for name, (still_file, _) in AGENTS.items():
        if name not in only:
            continue
        grid = Image.new("RGBA", (CELL * COLS, CELL * ROWS), (0, 0, 0, 0))
        idle_h = None
        for r, c, frame, anchor, bbox in cut[name]:
            grid.alpha_composite(place(frame, anchor, scale), (c * CELL, r * CELL))
            if r == 0 and c == 0:
                idle_h = (bbox[3] - bbox[1]) * scale
        webp(grid, OUT / f"{name}.webp")

        # The still is drawn at the same height as the idle frame, so swapping
        # one for the other never changes the figure's size.
        still = strip_painted_checkerboard(Image.open(SRC / still_file))
        a = still.getchannel("A").load()
        figure = max(components(a, (0, 0, *still.size)), key=lambda k: k[0])
        s = idle_h / (figure[1][3] - figure[1][1])
        big = place(still, feet_anchor(figure[2], figure[1]), s)
        webp(big.resize((ICON, ICON), Image.LANCZOS), OUT / f"{name}-still.webp")
        print(f"wrote {name}")


if __name__ == "__main__":
    sys.exit(main())
