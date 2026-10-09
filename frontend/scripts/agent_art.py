"""
Turns the generated crew art in `assets/` into what AgentSprite plays.

    python3 frontend/scripts/agent_art.py            # from the repo root
    python3 frontend/scripts/agent_art.py atlas      # just the named agents
    (needs Pillow; writes WebP through `cwebp`)

Inputs, per agent: a still (`scope.png`), a 5x4 sprite sheet
(`Scope Sprite Sheet.png`) and a 2x2 sleep sheet (`Scope Sleep Sheet.png`),
each straight out of an image model (1254x1254 squares, except ATLAS's
1024x1536 portrait sheet — any size works). Outputs, in
`frontend/public/agents/`:

    <codename>.webp        the sheet, re-cut onto an even grid of CELL cells: the
                           5 rows of the sprite sheet, then the sleep loop
    <codename>-still.webp  the still at ICON px, for the smallest renders

and `frontend/components/agents/sheets.json`, which says how many frames each
row of each sheet has. AgentSprite reads it, so a sheet regenerated with 10 or
12 frames a row plays with no code change (#91): run this script and rebuild.

How many frames a row has is read from the sheet, not assumed: the columns are
the runs of ink between clean vertical gaps (a stray mark too narrow to be a
figure joins the column beside it). The rows are the five states, always, in
order: queued, working, done, rejected, gate. When the prompt that made a sheet
is kept in `assets/prompts/crew/<codename>.md` (or `<codename>-sleep.md`) and
declares `Grid: 10 columns × 5 rows`, the frames found must match it, or the run
stops and names that prompt.

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

Sheets don't have to be square: five rows in a square left the image model too
little height, and ATLAS's bottom row ran off the canvas twice; its sheet is a
1024x1536 portrait. A sheet that can't be cut stops the run — regenerate it
(a temporary hand-made stand-in is in git history before #70, if one is needed).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "assets"
OUT = ROOT / "frontend" / "public" / "agents"

ROWS = 5  # the sprite sheet's rows, one per state; its columns are read from it
STATES = ["queued", "working", "done", "rejected", "gate"]
OUT_ROWS = ROWS + 1  # …plus the sleep loop as a sixth row
PROMPTS = ROOT / "assets" / "prompts" / "crew"
MANIFEST = ROOT / "frontend" / "components" / "agents" / "sheets.json"
CELL = 224  # 104px inspector portrait at 2x DPR, with headroom
ICON = 96  # stills are only used at <= 32px
FEET_Y = 0.95  # where the soles land in a cell (matches .sprite transform-origin)
MAX_H = 0.80  # tallest idle figure fills this much of the cell
MAX_W = 0.98

AGENTS = {
    # codename: (still, sheet); the sleep sheet is "<Name> Sleep Sheet.png"
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



def ink_runs(counts, min_share=0.5):
    """The runs of ink between clean gaps, as (start, end). A run much narrower than
    the others is a stray mark (an effect, a "!") and joins its nearer neighbour."""
    runs, i, n = [], 0, len(counts)
    while i < n:
        if counts[i] > 0:
            j = i
            while j < n and counts[j] > 0:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    if len(runs) < 2:
        return [tuple(r) for r in runs]
    widths = sorted(r[1] - r[0] for r in runs)
    median = widths[len(widths) // 2]
    merged = True
    while merged and len(runs) > 1:
        merged = False
        for k, r in enumerate(runs):
            if r[1] - r[0] < median * min_share:
                if k == 0:
                    other = 1
                elif k == len(runs) - 1:
                    other = k - 1
                else:
                    other = k - 1 if r[0] - runs[k - 1][1] <= runs[k + 1][0] - r[1] else k + 1
                lo, hi = min(k, other), max(k, other)
                runs[lo] = [runs[lo][0], runs[hi][1]]
                del runs[hi]
                merged = True
                break
    return [tuple(r) for r in runs]


def count_columns(sheet: Image.Image) -> int:
    """How many frames across the sheet has, from its own pixels."""
    alpha = sheet.getchannel("A").load()
    W, H = sheet.size
    ink_x = [sum(1 for y in range(0, H, 2) if alpha[x, y] > 40) for x in range(W)]
    return len(ink_runs(ink_x))


def count_rows(sheet: Image.Image) -> int:
    alpha = sheet.getchannel("A").load()
    W, H = sheet.size
    ink_y = [sum(1 for x in range(0, W, 2) if alpha[x, y] > 40) for y in range(H)]
    return len(ink_runs(ink_y))


GRID_LINE = re.compile(r"(\d+)\s*columns?\s*[x×]\s*(\d+)\s*rows?", re.I)


def declared_grid(prompt: Path) -> Optional[tuple]:
    """`Grid: 10 columns × 5 rows` from the prompt that made a sheet, if it was kept."""
    if not prompt.is_file():
        return None
    for line in prompt.read_text().splitlines():
        if line.lower().lstrip("-*# ").startswith("grid"):
            m = GRID_LINE.search(line)
            if m:
                return int(m.group(1)), int(m.group(2))
    return None


def check_grid(name: str, found: tuple, prompt: Path) -> None:
    want = declared_grid(prompt)
    if want and want != found:
        raise SystemExit(
            f"{name}: the prompt asked for {want[0]} columns × {want[1]} rows but the sheet has "
            f"{found[0]} × {found[1]} — regenerate it from {prompt.relative_to(ROOT)}"
        )


def frames(sheet: Image.Image, cols: int):
    """Yield (row, col, frame image, anchor, figure bbox) for each slot of the sheet.

    Slots are bounded by the empty gaps between figures, not by an even grid:
    the figures crowd the gaps, and their "!" and effect marks sit in them.
    """
    alpha = sheet.getchannel("A").load()
    W, H = sheet.size
    ink_x = [sum(1 for y in range(0, H, 2) if alpha[x, y] > 40) for x in range(W)]
    xs = [0] + cut_lines(ink_x, cols, round(W / cols * 0.35)) + [W]
    idle_h = {}
    bad = []
    for c in range(cols):
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
            elif bbox[3] >= H * 0.99 and (bbox[3] - bbox[1]) < 0.8 * idle_h[c]:
                # The last 1% of the sheet counts: a cut edge fades out over its
                # last pixels, so a cut figure can stop a few short of it.
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


def sleep_file(name: str) -> str:
    return f"{name.capitalize()} Sleep Sheet.png"


def sleep_frames(sheet: Image.Image, cols: int = 2, rows: int = 2):
    """Yield (frame image, anchor, figure bbox) for the sleep loop, in reading order.

    Cut at the real gaps like the main sheets; the Zs are separate shapes and
    stay with the figure in their quadrant. Sleep is drawn standing, so the
    feet anchor works unchanged.
    """
    alpha = sheet.getchannel("A").load()
    W, H = sheet.size
    ink_x = [sum(1 for y in range(0, H, 2) if alpha[x, y] > 40) for x in range(W)]
    ink_y = [sum(1 for x in range(0, W, 2) if alpha[x, y] > 40) for y in range(H)]
    xs = [0] + cut_lines(ink_x, cols, round(W / cols * 0.4)) + [W]
    ys = [0] + cut_lines(ink_y, rows, round(H / rows * 0.4)) + [H]
    for r in range(rows):
        for c in range(cols):
            slot = (xs[c], ys[r], xs[c + 1], ys[r + 1])
            n = r * cols + c + 1
            mine = [k for k in components(alpha, slot) if k[0] > 12]
            if not mine:
                raise SystemExit(f"sleep frame {n} is empty")
            figure = max(mine, key=lambda k: k[0])
            fb = figure[1]
            # Soles on the sheet's last pixel are fine, as in the main sheets;
            # any other edge contact means the figure was cut.
            on_floor = fb[3] >= slot[3] and slot[3] == H
            if fb[0] <= slot[0] or fb[1] <= slot[1] or fb[2] >= slot[2] or (fb[3] >= slot[3] and not on_floor):
                raise SystemExit(
                    f"sleep frame {n} touches the edge of its frame — regenerate it "
                    "with a margin on every edge"
                )
            # A neighbour's sliver sits on the frame's edge; this figure's own
            # Zs float free of it.
            keep = [
                k
                for k in mine
                if k is figure
                or not (k[1][0] <= slot[0] or k[1][1] <= slot[1] or k[1][2] >= slot[2] or k[1][3] >= slot[3])
            ]
            mask = Image.new("L", sheet.size, 0)
            mp = mask.load()
            for k in keep:
                for x, y in k[2]:
                    mp[x, y] = 255
            mask = mask.filter(ImageFilter.MaxFilter(3))
            frame = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
            frame.paste(sheet.crop(slot), slot[:2])
            frame = Image.composite(frame, Image.new("RGBA", sheet.size, (0, 0, 0, 0)), mask)
            yield frame, feet_anchor(figure[2], figure[1]), figure[1]


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


FPS_LINE = re.compile(r"(\d+(?:\.\d+)?)\s*fps", re.I)


def declared_fps(prompt: Path) -> Optional[float]:
    """`Frame rate: 12 fps` from the prompt, if it was kept and says one."""
    if not prompt.is_file():
        return None
    for line in prompt.read_text().splitlines():
        if line.lower().lstrip("-*# ").startswith("frame rate"):
            m = FPS_LINE.search(line)
            if m:
                return float(m.group(1))
    return None


def check_baselines(name: str, grid: Image.Image, per_row: list, tolerance: int = 2):
    """Every frame's feet on the same line, checked on the output rather than trusted.

    `place` anchors each frame on its feet, so a frame whose lowest pixels sit off
    the line was cut wrong (a tool or an effect taken for the feet). Played at 10–12
    fps that frame would bob, so the run stops on it.
    """
    alpha = grid.getchannel("A").load()
    want = round(CELL * FEET_Y)
    bad = []
    for r, n in enumerate(per_row):
        for c in range(n):
            shapes = components(alpha, (c * CELL, r * CELL, (c + 1) * CELL, (r + 1) * CELL))
            if not shapes:
                bad.append(f"row {r + 1} frame {c + 1} (empty)")
                continue
            # The figure, not a dropped page or a spark beside it.
            bottom = max(shapes, key=lambda k: k[0])[1][3] - r * CELL
            if abs(bottom - want) > tolerance and bottom < CELL:
                bad.append(f"row {r + 1} frame {c + 1} ({bottom - want:+d}px)")
    if bad:
        raise SystemExit(f"{name}: frames off the baseline: {', '.join(bad)}")


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
    cols = {}
    for name, (_, sheet_file) in AGENTS.items():
        sheet = strip_painted_checkerboard(Image.open(SRC / sheet_file))
        cols[name] = count_columns(sheet)
        if name in only:
            check_grid(sheet_file, (cols[name], ROWS), PROMPTS / f"{name}.md")
        cut[name] = []
        try:
            for f in frames(sheet, cols[name]):
                cut[name].append(f)
        except SystemExit as e:
            # Fatal only for a sheet being written; for the others the idle
            # row is all the shared scale needs, and it is always cut in full.
            if name in only or sum(1 for r, *_ in cut[name] if r == 0) < cols[name]:
                raise SystemExit(f"{sheet_file}: {e}") from None
            print(f"skip {name} (not being written): {e}")
            continue
        print(f"cut {name}: {cols[name]} frames a row, {len(cut[name])} frames")

    # The shared scale compares figures in source pixels, so a sheet drawn on a
    # different canvas than the rest can move everyone's size. Say so.
    sizes = {n: Image.open(SRC / f).size for n, (_, f) in AGENTS.items()}
    common = max(set(sizes.values()), key=list(sizes.values()).count)
    for n, sz in sizes.items():
        if sz != common:
            print(f"  note: {n}'s sheet is {sz[0]}x{sz[1]}, the rest {common[0]}x{common[1]} — "
                  "check its size against the crew")

    # One scale for the whole crew: the largest that fits every idle figure.
    scale = min(
        min(CELL * MAX_H / (b[3] - b[1]), CELL * MAX_W / (b[2] - b[0]))
        for fr in cut.values()
        for r, _c, _f, _a, b in fr
        if r == 0
    )

    try:
        manifest = json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        manifest = {}
    manifest = {"cell": CELL, "feet": FEET_Y, "sheets": manifest.get("sheets", {})}

    for name, (still_file, _) in AGENTS.items():
        if name not in only:
            continue
        # The sleep loop is drawn at its own size, so each agent's is scaled to
        # its idle figure — dozing off and waking up never change their size.
        try:
            raw = strip_painted_checkerboard(Image.open(SRC / sleep_file(name)))
            sc, sr = count_columns(raw), count_rows(raw)
            check_grid(sleep_file(name), (sc, sr), PROMPTS / f"{name}-sleep.md")
            sleep = list(sleep_frames(raw, sc, sr))
        except SystemExit as e:
            raise SystemExit(f"{sleep_file(name)}: {e}") from None

        width = max(cols[name], len(sleep))
        grid = Image.new("RGBA", (CELL * width, CELL * OUT_ROWS), (0, 0, 0, 0))
        idle_h = next((b[3] - b[1]) * scale for r, c, _f, _a, b in cut[name] if r == 0 and c == 0)
        for r, c, frame, anchor, bbox in cut[name]:
            grid.alpha_composite(place(frame, anchor, scale), (c * CELL, r * CELL))
        k = idle_h / (sleep[0][2][3] - sleep[0][2][1])
        for c, (frame, anchor, _b) in enumerate(sleep):
            # Scaled to the idle figure, the rising Zs could reach past the
            # cell; place() would clip them silently, so say so.
            fb = frame.getbbox()
            if (anchor[1] - fb[1]) * k > CELL * FEET_Y or (max(anchor[0] - fb[0], fb[2] - anchor[0])) * k > CELL / 2:
                print(f"  warning: {name} sleep frame {c + 1} is clipped by its cell")
            grid.alpha_composite(place(frame, anchor, k), (c * CELL, ROWS * CELL))
        check_baselines(name, grid, [cols[name]] * ROWS + [len(sleep)])
        webp(grid, OUT / f"{name}.webp")
        frames_per_row = {state: cols[name] for state in STATES}
        frames_per_row["asleep"] = len(sleep)
        entry = {"cols": width, "frames": frames_per_row}
        fps = declared_fps(PROMPTS / f"{name}.md")
        if fps:
            entry["fps"] = fps
        manifest["sheets"][name] = entry

        # The still is drawn at the same height as the idle frame, so swapping
        # one for the other never changes the figure's size.
        still = strip_painted_checkerboard(Image.open(SRC / still_file))
        a = still.getchannel("A").load()
        figure = max(components(a, (0, 0, *still.size)), key=lambda k: k[0])
        s = idle_h / (figure[1][3] - figure[1][1])
        big = place(still, feet_anchor(figure[2], figure[1]), s)
        webp(big.resize((ICON, ICON), Image.LANCZOS), OUT / f"{name}-still.webp")
        print(f"wrote {name}")

    manifest["sheets"] = dict(sorted(manifest["sheets"].items()))
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
