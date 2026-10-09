# Paper company · sky

Kind: layer
Layer: sky
Size: 1536 × 1024
Horizon: 54%
Save as: assets/floor/paper/sky.png

The paper company room on the crew floor is three images stacked: sky, wall and floor. This is the farthest layer: what is outside the windows.

## Attach

`assets/scope.png` (SCOPE's still), as the style reference.

## Prompt

Pixel art for a 2D game, in the style of the attached character sprite: hand-placed pixels, dark outlines 2 to 3 pixels thick, flat colour areas with hue-shifted shading (shadows lean cool, highlights lean warm), no airbrushed gradients, no blur, no photographic texture, no depth of field. Light comes from the upper left. Use the attached character only as a reference for line weight, pixel size and how colour is shaded. Do not draw the character. Camera: straight on, facing the back wall of the room from a slightly raised eye level, about 15 degrees down, the same eye level as the attached character. One vanishing point, at the centre of the horizon line. Canvas: exactly 1536 x 1024 pixels, landscape. The horizon (the line where the back wall meets the floor) is level, at 553 pixels from the top: 54% of the height. This image is only what is seen through the windows: the outside. Fill the canvas edge to edge, fully opaque, with no window frame and no wall. Put the interesting part in the top 45% of the canvas, which is where the windows are. This is the farthest layer of three, so make it the dimmest and greyest of them. An overcast afternoon outside a suburban office park, seen from inside an office: a flat pale grey sky, a low line of bare trees, and the roof of a two-storey office building across a parking lot, all between 25% and 45% of the height, with a few parked cars as simple shapes. Muted and slightly green-grey. Keep the whole image dim and muted. Brightly coloured characters with dark outlines will stand in front of it, so the room has to be quieter than they are: low contrast, low saturation, nothing brighter than a soft mid-tone except small light sources. No people, no characters, no faces, no animals. No text, letters, numbers, logos or brand marks, no watermark and no signature anywhere in the image.

## Before you drop it in

- [ ] 1536 x 1024, landscape. (The script refuses any other shape: the horizon only lines up at this one.)
- [ ] The line where the wall meets the floor is level and about 54% of the way down (553 px).
- [ ] Real transparency where it should be: opened in Preview, the see-through part shows Preview's own background, not a grey-and-white checkerboard painted into the image. (A painted one is cleaned up by the script, but real alpha cuts cleaner.)
- [ ] No people, no text or numbers, no logos, no watermark.
- [ ] Darker and greyer than the character you attached, so the crew will read on top of it.
- [ ] Opaque everywhere: no transparent patches.
- [ ] The skyline or horizon sits in the top half, where the windows are.

Save it as `assets/floor/paper/sky.png`, then run `python3 frontend/scripts/floor_art.py paper` from the repo root. The room shows up in the picker once its sky, wall and floor are all cut.
