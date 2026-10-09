# Image prompts for the crew floor

The crew floor's rooms are generated art, not CSS (#91). Every image starts as a prompt in this
folder. Nothing here is drawn by hand or in code.

1. Open the prompt's `.md` file, attach the image it names under **Attach** (a crew still, as the
   style reference), and paste the **Prompt** text into ChatGPT.
2. Check the result against **Before you drop it in**.
3. Save it where the prompt's `Save as:` line says, under `assets/floor/<room>/`.
4. From the repo root, run `python3 frontend/scripts/floor_art.py <room>`. It checks the image
   against the prompt (size, horizon, transparency, frame count, baselines), writes the WebPs
   to `frontend/public/floor/<room>/` and updates `frontend/public/floor/manifest.json`. If it
   refuses an image, it says why and which prompt to regenerate from.

A room appears in the floor's room picker once its `sky`, `wall` and `floor` are all cut. Until
then the night room is drawn in CSS and is the only one offered.

The lines at the top of each prompt (`Kind`, `Layer`, `Size`, `Horizon`, `Grid`, `Frame rate`,
`Anchor`, `Save as`) are read by the script, so change them only together with the image.

| Room | File | What it is |
|---|---|---|
| Night shift | `night/sky.md`, `night/wall.md`, `night/floor.md` | The control room after hours |
| | `night/rack.md` | A server rack with blinking lights, 10 frames |
| Paper company | `paper/sky.md`, `paper/wall.md`, `paper/floor.md` | A beige office, drop ceiling, carpet tiles |
| | `paper/light.md` | A flickering ceiling panel, 10 frames |
| Startup loft | `loft/sky.md`, `loft/wall.md`, `loft/floor.md` | A brick loft by day |
| | `loft/sky-dark.md`, `loft/wall-dark.md`, `loft/floor-dark.md` | The same loft at night, for dark mode |
| | `loft/rain.md` | Rain on the left window, 10 frames |
| Orbital station | `station/sky.md`, `station/wall.md`, `station/floor.md` | A ring module in low orbit |
| | `station/console.md` | A console with chasing lights, 10 frames |
| Every room | `shared/courier.md` | The drone that carries each hand-off, 12 frames |

The crew's own sprite sheets are cut by `frontend/scripts/agent_art.py` from the images in
`assets/`. It reads how many frames each row has from the sheet, so a crew sheet regenerated
with 8 to 12 frames a row plays with no code change. If the prompt for a regenerated sheet is
kept here as `crew/<codename>.md` with a `Grid:` line, the script checks the sheet against it.
