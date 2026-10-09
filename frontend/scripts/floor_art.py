"""
Turns the generated room art in `assets/floor/` into what the crew floor shows (#91).

    python3 frontend/scripts/floor_art.py              # every room, from the repo root
    python3 frontend/scripts/floor_art.py paper loft   # just these rooms
    python3 frontend/scripts/floor_art.py --check      # check the images, write nothing
    (needs Pillow; writes WebP through `cwebp`, like agent_art.py)

Every image starts as a prompt in `assets/prompts/<room>/<name>.md`. The prompt is
pasted into ChatGPT, and the image it makes is saved where the prompt's `Save as:`
line says, under `assets/floor/<room>/`. The raw images stay in `assets/` so the
WebPs can always be rebuilt. Outputs, in `frontend/public/floor/`:

    <room>/sky.webp, wall.webp, floor.webp    the room's three layers
    <room>/sky-dark.webp …                    the same room after dark, if drawn
    <room>/<piece>.webp                       an animated set piece, re-cut
    shared/courier.webp                       the hand-off courier, every room
    manifest.json                             what exists; the page reads it

A room is offered in the picker only once all three of its layers are cut.

Each prompt starts with a few `Key: value` lines the script reads:

    Kind: layer | sheet
    Layer: sky | wall | floor              (layers)
    Variant: dark                          (the after-dark copy of a layer)
    Size: 1536 × 1024
    Horizon: 54%                           (layers: from the top; matches --horizon)
    Grid: 10 columns × 1 row               (sheets)
    Frame rate: 10 fps                     (sheets)
    Anchor: bottom | none                  (sheets: re-anchor frames on their base,
                                            or keep each frame where it was drawn)
    Save as: assets/floor/paper/wall.png

What this fixes or refuses, the same way agent_art.py does for the crew:

  1. A checkerboard painted in instead of alpha is flood-filled away from the
     edges. A layer that must be see-through and still isn't stops the run.
  2. A layer the wrong shape stops the run: the horizon only lines up with the
     room when the aspect is the one the prompt asked for. A bigger image of the
     same shape is scaled down.
  3. The wall must be empty below the horizon and the floor empty above it, or
     they would paint over each other. The image model draws that line a few
     percent off (ChatGPT's walls stop at 51-53%), and a wall that stops short
     shows a band of sky under it. So a layer whose edge is within SEAT of the
     horizon is seated on it first: a wall is moved down (or up) until its foot
     is on the line; a floor that starts above the line is trimmed at it, and one
     that starts below is stretched up to it. Only an edge further off than
     that stops the run.
  4. A sheet's frames are found by their own pixels, not by slicing in equal
     parts, and the count must match the prompt's grid. Each frame is re-anchored
     on its base (unless the prompt says `Anchor: none`), and every frame's base
     is checked against the line before anything is written.

Every refusal names the prompt to regenerate from.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_art import components, cut_lines, ink_runs, strip_painted_checkerboard, webp  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
# Overridable so the script can be run against scratch images without touching the repo.
PROMPTS = Path(os.environ.get("FLOOR_ART_PROMPTS", ROOT / "assets" / "prompts"))
SRC_ROOT = Path(os.environ.get("FLOOR_ART_SRC", ROOT))
OUT = Path(os.environ.get("FLOOR_ART_OUT", ROOT / "frontend" / "public" / "floor"))

ROOMS = ("night", "paper", "loft", "station")
LAYERS = ("sky", "wall", "floor")
#: How far from the horizon a layer may spill: a skirting line, a shadow.
SPILL = 0.02
#: Share of a band's pixels that may be opaque on the wrong side of the horizon.
STRAY = 0.01
#: How far off the horizon a wall's foot or a floor's far edge may be drawn and
#: still be seated on it, as a share of the height.
SEAT = 0.06
#: Margin around a sheet's frames in each output cell, in output pixels.
PAD = 4


class Refused(SystemExit):
    pass


def refuse(prompt: Path, why: str) -> None:
    rel = prompt.relative_to(PROMPTS.parent) if PROMPTS.parent in prompt.parents else prompt
    raise Refused(f"{why}. Regenerate it from {rel}")


# ── prompts ──────────────────────────────────────────────────────────────────
HEADER = re.compile(r"^\s*([A-Za-z ]+):\s*(.+?)\s*$")


def read_prompt(path: Path) -> dict:
    """The `Key: value` lines at the top of a prompt, before its first heading."""
    meta: dict = {"path": path}
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            break
        m = HEADER.match(line)
        if m:
            meta[m.group(1).strip().lower()] = m.group(2).strip()
    return meta


def size_of(meta: dict) -> tuple:
    m = re.search(r"(\d+)\s*[x×]\s*(\d+)", meta.get("size", ""))
    if not m:
        raise Refused(f"{meta['path']}: no `Size:` line")
    return int(m.group(1)), int(m.group(2))


def grid_of(meta: dict) -> tuple:
    m = re.search(r"(\d+)\s*columns?\s*[x×]\s*(\d+)\s*rows?", meta.get("grid", ""), re.I)
    if not m:
        raise Refused(f"{meta['path']}: no `Grid:` line")
    return int(m.group(1)), int(m.group(2))


def horizon_of(meta: dict) -> float:
    m = re.search(r"([\d.]+)\s*%", meta.get("horizon", ""))
    return float(m.group(1)) / 100 if m else 0.54


def source_of(meta: dict) -> Path:
    save = meta.get("save as", "").strip("` ")
    if not save:
        raise Refused(f"{meta['path']}: no `Save as:` line")
    return SRC_ROOT / save


# ── layers ───────────────────────────────────────────────────────────────────
def opaque_share(alpha, box) -> float:
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return 0.0
    n = 0
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            if alpha[x, y] > 40:
                n += 1
    return n / (((x1 - x0 + 1) // 2) * ((y1 - y0 + 1) // 2))


def edge_row(alpha: Image.Image, foot: bool) -> Optional[int]:
    """Where a layer meets the horizon: across the columns with anything solid in
    them, the median of the first empty row under each one (a wall's foot) or of
    its first solid row (a floor's far edge). The median, so a cabinet or a bike
    standing against the wall doesn't move the line."""
    a = alpha.load()
    W, H = alpha.size
    span = range(H - 1, -1, -1) if foot else range(H)
    rows = []
    for x in range(0, W, 4):
        for y in span:
            if a[x, y] >= 128:
                rows.append(y + 1 if foot else y)
                break
    if not rows:
        return None
    rows.sort()
    return rows[len(rows) // 2]


def seat(im: Image.Image, layer: str, horizon: int) -> tuple:
    """(the layer seated on the horizon, what was done or ""). Within SEAT of the
    line a wall is moved, a floor drawn too high is trimmed and one drawn too low is
    stretched up; further off, it is left as drawn for the checks to refuse."""
    W, H = im.size
    edge = edge_row(im.getchannel("A"), foot=layer == "wall")
    if edge is None or edge == horizon or abs(edge - horizon) > round(H * SEAT):
        return im, ""
    off = horizon - edge
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    if layer == "wall":
        out.paste(im, (0, off))
        return out, f"moved {abs(off)}px {'down' if off > 0 else 'up'} onto the horizon"
    if off > 0:
        out = im.copy()
        out.paste((0, 0, 0, 0), (0, 0, W, horizon))
        return out, f"far edge trimmed {off}px to the horizon"
    out.paste(im.crop((0, edge, W, H)).resize((W, H - horizon), Image.LANCZOS), (0, horizon))
    return out, f"stretched {-off}px up to the horizon"


def cut_layer(meta: dict) -> tuple:
    """(the layer, a note on how it was seated on the horizon, or "")."""
    prompt = meta["path"]
    src = source_of(meta)
    layer = meta.get("layer", "")
    want_w, want_h = size_of(meta)
    im = Image.open(src)
    w, h = im.size
    if abs(w / h - want_w / want_h) > 0.01:
        refuse(prompt, f"{src.name} is {w}×{h}; the prompt asks for {want_w}×{want_h}, and the horizon only lines up at that shape")
    if (w, h) != (want_w, want_h):
        im = im.resize((want_w, want_h), Image.LANCZOS)

    if layer == "sky":
        # Opaque: the sky is the back of the room. Any alpha is flattened onto night.
        flat = Image.new("RGBA", im.size, (10, 13, 21, 255))
        flat.alpha_composite(im.convert("RGBA"))
        return flat.convert("RGB"), ""

    im = strip_painted_checkerboard(im)
    W, H = im.size
    horizon = round(H * horizon_of(meta))
    if im.getchannel("A").getextrema()[0] > 40:
        refuse(prompt, f"{src.name} has no transparent pixels; the {layer} layer has to be see-through")
    im, seated = seat(im, layer, horizon)
    a = im.getchannel("A").load()
    spill = round(H * SPILL)
    if layer == "wall":
        stray = opaque_share(a, (0, min(H, horizon + spill), W, H))
        if stray > STRAY:
            refuse(prompt, f"{src.name} paints {stray:.0%} of the floor below the horizon; the wall must stop at {horizon}px")
    elif layer == "floor":
        stray = opaque_share(a, (0, 0, W, max(0, horizon - spill)))
        if stray > STRAY:
            refuse(prompt, f"{src.name} paints {stray:.0%} of the wall above the horizon; the floor must start at {horizon}px")
    return im, seated


# ── sheets ───────────────────────────────────────────────────────────────────
def cut_sheet(meta: dict) -> tuple:
    """(sheet image, entry): frames found, checked, re-anchored onto an even grid."""
    prompt = meta["path"]
    src = source_of(meta)
    cols, rows = grid_of(meta)
    anchor = meta.get("anchor", "bottom").lower()
    sheet = strip_painted_checkerboard(Image.open(src))
    alpha = sheet.getchannel("A").load()
    W, H = sheet.size

    if anchor != "none":
        ink_y = [sum(1 for x in range(0, W, 2) if alpha[x, y] > 40) for y in range(H)]
        found_rows = len(ink_runs(ink_y))
        if found_rows != rows:
            refuse(prompt, f"{src.name} has {found_rows} rows of frames; the prompt asks for {rows}")
    ys = [0] + (cut_lines([sum(1 for x in range(0, W, 2) if alpha[x, y] > 40) for y in range(H)], rows, round(H / rows * 0.4)) if rows > 1 else []) + [H]

    frames = []
    for r in range(rows):
        y0, y1 = ys[r], ys[r + 1]
        ink_x = [sum(1 for y in range(y0, y1, 2) if alpha[x, y] > 40) for x in range(W)]
        if anchor != "none":
            found = len(ink_runs(ink_x))
            if found != cols:
                refuse(prompt, f"{src.name} row {r + 1} has {found} frames; the prompt asks for {cols} across")
        xs = [0] + (cut_lines(ink_x, cols, round(W / cols * 0.4)) if cols > 1 else []) + [W]
        for c in range(cols):
            slot = (xs[c], y0, xs[c + 1], y1)
            if anchor == "none":
                frames.append((r, c, sheet.crop(slot), None))
                continue
            mine = [k for k in components(alpha, slot) if k[0] > 12]
            # A neighbour's sliver sits on the slot's edge; this frame's own parts float free.
            keep = [k for k in mine if not (k[1][0] <= slot[0] and slot[0] > 0) and not (k[1][2] >= slot[2] and slot[2] < W)]
            if not keep:
                refuse(prompt, f"{src.name} row {r + 1} frame {c + 1} is empty or runs into its neighbour")
            box = (
                min(k[1][0] for k in keep),
                min(k[1][1] for k in keep),
                max(k[1][2] for k in keep),
                max(k[1][3] for k in keep),
            )
            mask = Image.new("L", sheet.size, 0)
            mp = mask.load()
            for k in keep:
                for x, y in k[2]:
                    mp[x, y] = 255
            frame = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
            frame.paste(sheet, (0, 0), mask)
            frames.append((r, c, frame.crop(box), box))

    if anchor == "none":
        # Each frame shows the same area (rain on one pane): crop every slot to the
        # box all of them share, measured from the slot's own corner, so the frames
        # stack exactly and the empty canvas around them is dropped.
        boxes = [f[2].getchannel("A").point(lambda v: 255 if v > 40 else 0).getbbox() for f in frames]
        if not all(boxes):
            refuse(prompt, f"{src.name} has an empty frame")
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        cw, ch = x1 - x0, y1 - y0
        out = Image.new("RGBA", (cw * cols, ch * rows), (0, 0, 0, 0))
        for r, c, img, _ in frames:
            part = img.crop((x0, y0, min(x1, img.size[0]), min(y1, img.size[1])))
            out.alpha_composite(part, (c * cw, r * ch))
        return out, {"cols": cols, "rows": rows, "w": cw, "h": ch}

    # Every frame of a row on one base, centred: a loop that bobs isn't a loop.
    cw = max(f[2].size[0] for f in frames) + PAD * 2
    ch = max(f[2].size[1] for f in frames) + PAD * 2
    out = Image.new("RGBA", (cw * cols, ch * rows), (0, 0, 0, 0))
    base = ch - PAD
    for r, c, img, _ in frames:
        w, h = img.size
        out.alpha_composite(img, (c * cw + (cw - w) // 2, r * ch + base - h))
    check_baselines(prompt, src, out, cols, rows, cw, ch, base)
    return out, {"cols": cols, "rows": rows, "w": cw, "h": ch}


def check_baselines(prompt: Path, src: Path, out: Image.Image, cols: int, rows: int, cw: int, ch: int, base: int):
    a = out.getchannel("A")
    bad = []
    for r in range(rows):
        for c in range(cols):
            box = a.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)).point(lambda v: 255 if v > 40 else 0).getbbox()
            if not box or abs(box[3] - base) > 1:
                bad.append(f"row {r + 1} frame {c + 1}")
    if bad:
        refuse(prompt, f"{src.name}: frames off the baseline ({', '.join(bad)})")


# ── the run ──────────────────────────────────────────────────────────────────
def save(img: Image.Image, dest: Path, check: bool) -> None:
    if check:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    webp(img, dest)


def fps_of(meta: dict) -> float:
    m = re.search(r"([\d.]+)\s*fps", meta.get("frame rate", ""), re.I)
    return float(m.group(1)) if m else 10.0


def run_room(room: str, check: bool, manifest: dict) -> list:
    folder = PROMPTS / room
    notes = []
    if not folder.is_dir():
        return notes
    layers: dict = {}
    dark: dict = {}
    pieces: dict = {}
    for prompt in sorted(folder.glob("*.md")):
        meta = read_prompt(prompt)
        kind = meta.get("kind", "").lower()
        src = source_of(meta) if "save as" in meta else None
        if src is None or not src.is_file():
            notes.append(f"  waiting: {prompt.relative_to(PROMPTS.parent)} (no image at {src.relative_to(SRC_ROOT) if src else '?'})")
            continue
        if kind == "layer":
            name = meta.get("layer", "")
            if name not in LAYERS:
                raise Refused(f"{prompt}: `Layer:` must be one of {', '.join(LAYERS)}")
            variant = meta.get("variant", "").lower()
            img, seated = cut_layer(meta)
            rel = f"{room}/{name}{'-dark' if variant == 'dark' else ''}.webp"
            save(img, OUT / rel, check)
            (dark if variant == "dark" else layers)[name] = rel
            notes.append(f"  cut {rel}" + (f" ({seated})" if seated else ""))
        elif kind == "sheet":
            img, entry = cut_sheet(meta)
            rel = f"{room}/{prompt.stem}.webp"
            save(img, OUT / rel, check)
            pieces[prompt.stem] = {"src": rel, **entry, "fps": fps_of(meta)}
            notes.append(f"  cut {rel} ({entry['cols']}×{entry['rows']})")
        else:
            raise Refused(f"{prompt}: `Kind:` must be layer or sheet")

    if room == "shared":
        if "courier" in pieces:
            manifest["courier"] = pieces["courier"]
        return notes
    if all(k in layers for k in LAYERS):
        art: dict = {"layers": layers}
        if all(k in dark for k in LAYERS):
            art["dark"] = dark
        if pieces:
            art["pieces"] = pieces
        manifest["themes"][room] = art
    elif layers:
        missing = [k for k in LAYERS if k not in layers]
        notes.append(f"  {room} is not offered yet: no {', '.join(missing)} layer")
    return notes


def main(argv: list) -> int:
    check = "--check" in argv
    only = [a for a in argv if not a.startswith("--")]
    rooms = only or [*ROOMS, "shared"]
    unknown = [r for r in rooms if r not in (*ROOMS, "shared")]
    if unknown:
        raise SystemExit(f"unknown room: {', '.join(unknown)}")

    path = OUT / "manifest.json"
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        manifest = {}
    manifest = {"version": 1, "themes": manifest.get("themes", {}), **({"courier": manifest["courier"]} if "courier" in manifest else {})}
    for room in rooms:
        if room != "shared":
            manifest["themes"].pop(room, None)
        print(room)
        for line in run_room(room, check, manifest):
            print(line)

    if check:
        print("checked, nothing written")
        return 0
    manifest["themes"] = dict(sorted(manifest["themes"].items()))
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
