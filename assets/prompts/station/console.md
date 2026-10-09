# Orbital station · console

Kind: sheet
Grid: 10 columns × 1 row
Frame rate: 10 fps
Anchor: bottom
Size: 1536 × 1024
Save as: assets/floor/station/console.png

An animated set piece. Stands under the board, from 42% to 58% across, its base on the floor line. The wall prompt leaves that spot plain.

## Attach

`assets/scope.png` (SCOPE's still), as the style reference.

## Prompt

A sprite sheet of one looping animation for a 2D pixel-art game, in the style of the attached character sprite: dark outlines 2 to 3 pixels thick, flat hue-shifted shading, no blur. Use the attached character only as a style reference; do not draw it. Layout: 10 frames in a single horizontal row, evenly spaced, every frame the same size, with at least 16 pixels of empty space between frames and around the edges, on a 1536 x 1024 canvas with the row centred vertically. The frames are consecutive steps of one movement: played left to right at 10 frames per second the loop is continuous, and frame 10 leads straight back into frame 1. Every frame sits on the same baseline: the bottom of the object is at exactly the same height in every frame, and the object does not drift. Only the animated part changes. Transparent background: a PNG with a real alpha channel, not a grey-and-white checkerboard drawn in. A low control console seen straight on, wide (about 3 wide to 1 tall), off-white and grey, with a slanted top covered in small square buttons and two tiny screens. Lights chase from left to right across the buttons over the loop, and each screen shows a slowly scrolling bar graph a few pixels high. No people, no characters, no faces, no animals. No text, letters, numbers, logos or brand marks, no watermark and no signature anywhere in the image.

## Before you drop it in

- [ ] Exactly 10 frames, in one row, with clear space between them. (The script counts them and refuses any other number.)
- [ ] The bottom of the object is on the same line in every frame.
- [ ] Flicking from frame 10 back to frame 1 doesn't jump.
- [ ] Real transparency around the frames, not a painted checkerboard.
- [ ] No text, numbers, logos or watermark.

Save it as `assets/floor/station/console.png`, then run `python3 frontend/scripts/floor_art.py station`.
