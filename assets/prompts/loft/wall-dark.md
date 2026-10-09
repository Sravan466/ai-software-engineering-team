# Startup loft · wall (after dark)

Kind: layer
Layer: wall
Variant: dark
Size: 1536 × 1024
Horizon: 54%
Save as: assets/floor/loft/wall-dark.png

The startup loft room on the crew floor is three images stacked: sky, wall and floor. This is the middle layer: the back wall, with see-through windows. Shown when the viewer's computer is set to dark mode; the daytime copy is `wall.md`.

## Attach

`assets/scope.png` (SCOPE's still), as the style reference.

## Prompt

Pixel art for a 2D game, in the style of the attached character sprite: hand-placed pixels, dark outlines 2 to 3 pixels thick, flat colour areas with hue-shifted shading (shadows lean cool, highlights lean warm), no airbrushed gradients, no blur, no photographic texture, no depth of field. Light comes from the upper left. Use the attached character only as a reference for line weight, pixel size and how colour is shaded. Do not draw the character. Camera: straight on, facing the back wall of the room from a slightly raised eye level, about 15 degrees down, the same eye level as the attached character. One vanishing point, at the centre of the horizon line. Canvas: exactly 1536 x 1024 pixels, landscape. The horizon (the line where the back wall meets the floor) is level, at 553 pixels from the top: 54% of the height. This image is only the back wall, on a transparent background: a PNG with a real alpha channel, not a grey-and-white checkerboard drawn in. Everything below the horizon at 553 pixels is fully transparent. The glass of every window is fully transparent too, so the sky behind it shows through; frames, sills and blinds are opaque. Keep the wall plain and uncluttered from 30% to 70% of the width and from 12% to 40% of the height: the app puts a live status board there. Anything standing against the wall sits on the horizon line and stays in the outer quarter on each side. Exactly the same wall at night, with the same layout and the windows in the same place: 4% to 24% and 76% to 96% of the width, 8% to 42% of the height. The hanging bulbs are lit a warm yellow, the neon wave glows a soft pink, and the brick is in shadow with a warm pool of light under each bulb. Keep the whole image dim and muted. Brightly coloured characters with dark outlines will stand in front of it, so the room has to be quieter than they are: low contrast, low saturation, nothing brighter than a soft mid-tone except small light sources. No people, no characters, no faces, no animals. No text, letters, numbers, logos or brand marks, no watermark and no signature anywhere in the image.

## Before you drop it in

- [ ] 1536 x 1024, landscape. (The script refuses any other shape: the horizon only lines up at this one.)
- [ ] The line where the wall meets the floor is level and about 54% of the way down (553 px).
- [ ] Real transparency where it should be: opened in Preview, the see-through part shows Preview's own background, not a grey-and-white checkerboard painted into the image. (A painted one is cleaned up by the script, but real alpha cuts cleaner.)
- [ ] No people, no text or numbers, no logos, no watermark.
- [ ] Darker and greyer than the character you attached, so the crew will read on top of it.
- [ ] Below the wall line is transparent, and so is the glass in every window.
- [ ] The middle of the wall (30% to 70% across, 12% to 40% down) is plain, for the board.

Save it as `assets/floor/loft/wall-dark.png`, then run `python3 frontend/scripts/floor_art.py loft` from the repo root. The room shows up in the picker once its sky, wall and floor are all cut.
