# Startup loft · rain on the window

Kind: sheet
Grid: 10 columns × 1 row
Frame rate: 10 fps
Anchor: none
Size: 1536 × 1024
Save as: assets/floor/loft/rain.png

An animated set piece. Laid over the left factory window's glass, from 4% to 24% across and 8% to 42% down.

## Attach

`assets/scope.png` (SCOPE's still), as the style reference.

## Prompt

A sprite sheet of one looping animation for a 2D pixel-art game, in the style of the attached character sprite: dark outlines 2 to 3 pixels thick, flat hue-shifted shading, no blur. Use the attached character only as a style reference; do not draw it. Layout: 10 frames in a single horizontal row, evenly spaced, every frame the same size, with at least 16 pixels of empty space between frames and around the edges, on a 1536 x 1024 canvas with the row centred vertically. The frames are consecutive steps of one movement: played left to right at 10 frames per second the loop is continuous, and frame 10 leads straight back into frame 1. Every frame shows exactly the same area, so the frames line up when stacked. Transparent background: a PNG with a real alpha channel, not a grey-and-white checkerboard drawn in. Only the rain on a window pane: drops and short trickles of water running down glass, drawn in pale blue-white pixels with a darker edge, on transparent glass. No window frame, no background. Each frame is about 1 wide to 1.13 tall (the window's shape). Drops slide down a few pixels per frame and new ones appear at the top, so the loop never looks like it restarts. No people, no characters, no faces, no animals. No text, letters, numbers, logos or brand marks, no watermark and no signature anywhere in the image.

## Before you drop it in

- [ ] Exactly 10 frames, in one row, with clear space between them. (The script counts them and refuses any other number.)
- [ ] Every frame covers the same area.
- [ ] Flicking from frame 10 back to frame 1 doesn't jump.
- [ ] Real transparency around the frames, not a painted checkerboard.
- [ ] No text, numbers, logos or watermark.

Save it as `assets/floor/loft/rain.png`, then run `python3 frontend/scripts/floor_art.py loft`.
