# Every room · the hand-off courier

Kind: sheet
Grid: 12 columns × 1 row
Frame rate: 16 fps
Anchor: bottom
Size: 1536 × 1024
Save as: assets/floor/shared/courier.png

An animated set piece. Carries each hand-off from the desk that finished to the desk that takes the work, in every room. Drawn about 38 px wide, so keep the shapes bold.

## Attach

`assets/scope.png` (SCOPE's still), as the style reference.

## Prompt

A sprite sheet of one looping animation for a 2D pixel-art game, in the style of the attached character sprite: dark outlines 2 to 3 pixels thick, flat hue-shifted shading, no blur. Use the attached character only as a style reference; do not draw it. Layout: 12 frames in a single horizontal row, evenly spaced, every frame the same size, with at least 16 pixels of empty space between frames and around the edges, on a 1536 x 1024 canvas with the row centred vertically. The frames are consecutive steps of one movement: played left to right at 16 frames per second the loop is continuous, and frame 12 leads straight back into frame 1. Every frame sits on the same baseline: the bottom of the object is at exactly the same height in every frame, and the object does not drift. Only the animated part changes. Transparent background: a PNG with a real alpha channel, not a grey-and-white checkerboard drawn in. A small hovering delivery drone seen from the side, facing right, with a plain kraft-brown parcel tied with a darker strap hanging under it on a short line. Dark grey body, one small cyan status light. Only the two rotors on top change from frame to frame: they spin, shown with two or three flat pixel streaks that turn a little further each frame, not real motion blur. The body, the line and the parcel are exactly the same pixels in every frame: no bob, no sway (the app moves the whole drone itself). The bottom of the parcel is the baseline. No people, no characters, no faces, no animals. No text, letters, numbers, logos or brand marks, no watermark and no signature anywhere in the image.

## Before you drop it in

- [ ] Exactly 12 frames, in one row, with clear space between them. (The script counts them and refuses any other number.)
- [ ] The bottom of the object is on the same line in every frame.
- [ ] Flicking from frame 12 back to frame 1 doesn't jump.
- [ ] Only the rotors move between frames; the body and the parcel stay put.
- [ ] Real transparency around the frames, not a painted checkerboard.
- [ ] No text, numbers, logos or watermark.

Save it as `assets/floor/shared/courier.png`, then run `python3 frontend/scripts/floor_art.py shared`.
