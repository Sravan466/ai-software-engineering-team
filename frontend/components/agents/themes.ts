"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * The crew floor's rooms (#91).
 *
 * A theme is the room around the crew: its background layers, the colours of the
 * cabins, desks and nameplates standing in it, whether the screen effects
 * (scanlines, vignette) apply, and which props stand on the floor. The eight
 * agents' own colours never change with it.
 *
 * Backgrounds are generated art, not CSS. Each room's `sky`, `wall` and `floor` are
 * images in `public/floor/<theme>/`, cut by `scripts/floor_art.py` from the images
 * in `assets/floor/<theme>/`, which were made from the prompts in
 * `assets/prompts/<theme>/`. The script writes `public/floor/manifest.json`, and a
 * room is only offered once its layers are listed there. The night room is the
 * exception: it is also drawn in CSS, which is what shows until its art exists.
 */

export type ThemeId = "night" | "paper" | "loft" | "station";

export type Layers = { sky: string; wall: string; floor: string };

/** An animated set piece: a sheet of frames on an even grid, one loop per row. */
export type PieceSheet = {
  src: string;
  cols: number;
  rows: number;
  /** One frame's size, in the sheet's own pixels. */
  w: number;
  h: number;
  fps: number;
};

export type ThemeArt = {
  layers: Layers;
  /** The same room after dark, for a viewer whose system is set to dark. */
  dark?: Layers;
  pieces?: Record<string, PieceSheet>;
};

export type FloorManifest = {
  version: number;
  themes: Partial<Record<ThemeId, ThemeArt>>;
  /** Shared across rooms: the hand-off courier. */
  courier?: PieceSheet;
};

/** The props drawn in code (components/agents/props.ts) a room can stand up. */
export type PropId =
  | "board"
  | "rack"
  | "monitors"
  | "rack-2"
  | "crates"
  | "plant"
  | "cooler"
  | "plant-2"
  | "spool"
  | "crate"
  | "mug";

/**
 * Where a set piece stands, in the art's own coordinates: percentages of the
 * 1536×1024 layers, so it stays on its window or its spot on the wall at every
 * room shape. Measured on the cut art (the walls are seated on the horizon by
 * floor_art.py), so a regenerated wall may need these moved.
 */
export type PiecePlace = {
  /** Left edge, top edge and width, as % of the art. */
  x: number;
  y: number;
  w: number;
  /** Behind the wall, seen only through its glass; in front of the wall; between
   *  the wall and the crew; or nearest you. */
  tier: "out" | "back" | "mid" | "near";
};

/** The size every room layer is generated at (assets/prompts), and the horizon in it. */
export const ART_W = 1536;
export const ART_H = 1024;

export type FloorTheme = {
  id: ThemeId;
  name: string;
  /** One line in the picker. */
  blurb: string;
  /** Three colours for the picker's swatch: wall, floor, light. */
  swatch: [string, string, string];
  /** Drawn in CSS when it has no art: only the night room. */
  drawn: boolean;
  scanlines: boolean;
  vignette: boolean;
  props: PropId[];
  /** Where each of the room's animated pieces goes, once its sheet exists. */
  pieces: Record<string, PiecePlace>;
  /**
   * Overrides for the room's colour tokens (agents.css, `.floor`). Kept dim and
   * low in saturation in every room: the sprites are shared, and their dark
   * outlines only read against a room quieter than they are.
   */
  tokens: Record<string, string>;
};

const ALL_PROPS: PropId[] = ["board", "rack", "monitors", "rack-2", "crates", "plant", "cooler", "plant-2", "spool", "crate", "mug"];

export const THEMES: FloorTheme[] = [
  {
    id: "night",
    name: "Night shift",
    blurb: "The control room after hours, the city lit up outside.",
    swatch: ["#0f141f", "#0b1119", "#6082ba"],
    drawn: true,
    scanlines: true,
    vignette: true,
    props: ALL_PROPS,
    pieces: {
      // In the plain gap between the left window and the pillar, on the skirting.
      rack: { x: 22.6, y: 34.1, w: 4.6, tier: "back" },
    },
    tokens: {},
  },
  {
    id: "paper",
    name: "Paper company",
    blurb: "Beige walls, a drop ceiling, carpet tiles.",
    swatch: ["#6f6656", "#4d4a42", "#e9dcb5"],
    drawn: false,
    scanlines: false,
    vignette: true,
    props: ["plant", "cooler", "plant-2", "mug", "crates"],
    pieces: {
      // One unlit slot of the drop ceiling, above the left window.
      light: { x: 11, y: 12.5, w: 7, tier: "back" },
    },
    tokens: {
      "--cabin-back-a": "#6d685c",
      "--cabin-back-b": "#57534a",
      "--cabin-back-c": "#433f38",
      "--cabin-cap-b": "#8c8676",
      "--cabin-cap-c": "#615c51",
      "--cabin-side-l": "#77715f",
      "--cabin-side-l2": "#5f5a4d",
      "--cabin-side-r": "#3d3a33",
      "--cabin-side-r2": "#4a463e",
      "--cabin-weave": "rgba(255, 240, 210, 0.05)",
      "--desk-top-a": "#8a7a5c",
      "--desk-top-b": "#6b5d45",
      "--desk-face-a": "#5c4f3a",
      "--desk-face-b": "#3f3627",
      "--room-outline": "#17140f",
      "--room-glow": "rgba(255, 226, 160, 0.16)",
      "--plate-bg": "rgba(18, 16, 12, 0.92)",
      "--hint-ink": "#e9e1cc",
      "--prop-dim": "0.78",
    },
  },
  {
    id: "loft",
    name: "Startup loft",
    blurb: "Brick and factory windows. At night when your system is dark.",
    swatch: ["#5b4a44", "#3e3833", "#f2c98a"],
    drawn: false,
    scanlines: false,
    vignette: false,
    props: ["plant", "plant-2", "cooler", "mug", "monitors"],
    pieces: {
      // Behind the left window, so the wall crops it to the glass (day and dark walls).
      rain: { x: 4.8, y: 25, w: 20.5, tier: "out" },
    },
    tokens: {
      "--cabin-back-a": "#4f5550",
      "--cabin-back-b": "#3f4440",
      "--cabin-back-c": "#2f3330",
      "--cabin-cap-b": "#6f6a5f",
      "--cabin-cap-c": "#4c4840",
      "--cabin-side-l": "#5d635d",
      "--cabin-side-l2": "#474c47",
      "--cabin-side-r": "#2a2e2b",
      "--cabin-side-r2": "#353a36",
      "--cabin-weave": "rgba(240, 255, 240, 0.04)",
      "--desk-top-a": "#9a7550",
      "--desk-top-b": "#76583b",
      "--desk-face-a": "#5e4630",
      "--desk-face-b": "#3e2e20",
      "--room-outline": "#14110e",
      "--room-glow": "rgba(255, 210, 150, 0.14)",
      "--plate-bg": "rgba(18, 15, 12, 0.92)",
      "--hint-ink": "#efe4d6",
      "--prop-dim": "0.8",
    },
  },
  {
    id: "station",
    name: "Orbital station",
    blurb: "A ring module in low orbit, Earth in the ports.",
    swatch: ["#1a2129", "#11171d", "#7fd6e0"],
    drawn: false,
    scanlines: true,
    vignette: true,
    props: ["rack", "rack-2", "monitors", "crates", "spool", "mug"],
    pieces: {
      // Under the board, its base on the horizon.
      console: { x: 42, y: 39, w: 16, tier: "back" },
    },
    tokens: {
      "--cabin-back-a": "#26303b",
      "--cabin-back-b": "#1c242d",
      "--cabin-back-c": "#131a21",
      "--cabin-cap-b": "#56677a",
      "--cabin-cap-c": "#334050",
      "--cabin-side-l": "#36424f",
      "--cabin-side-l2": "#26303b",
      "--cabin-side-r": "#11171d",
      "--cabin-side-r2": "#19212a",
      "--room-glow": "rgba(120, 214, 224, 0.16)",
      "--plate-bg": "rgba(5, 9, 13, 0.8)",
      "--prop-dim": "0.7",
    },
  },
];

export const THEME_BY_ID: Record<ThemeId, FloorTheme> = Object.fromEntries(THEMES.map((t) => [t.id, t])) as Record<
  ThemeId,
  FloorTheme
>;

const STORAGE_KEY = "aiteam.crew-floor.theme";
const MANIFEST_URL = "/floor/manifest.json";

let manifestOnce: Promise<FloorManifest> | null = null;

/** The art that has been cut so far. A missing or broken manifest is no art, not an error. */
function loadManifest(): Promise<FloorManifest> {
  if (!manifestOnce) {
    manifestOnce = fetch(MANIFEST_URL, { cache: "no-cache" })
      .then((r) => (r.ok ? r.json() : null))
      .then((m) => (m && typeof m === "object" && m.themes ? (m as FloorManifest) : { version: 1, themes: {} }))
      .catch(() => ({ version: 1, themes: {} }));
  }
  return manifestOnce;
}

function readChoice(): ThemeId | null {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v && v in THEME_BY_ID ? (v as ThemeId) : null;
  } catch {
    // Storage blocked (a private window, site data off): the night room, quietly.
    return null;
  }
}

function writeChoice(id: ThemeId): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Not kept past this visit; the room still changes.
  }
}

function prefersDark(): boolean {
  return typeof window === "undefined" || !window.matchMedia || window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export type FloorThemeState = {
  theme: FloorTheme;
  /** This room's layers for the viewer's light or dark setting, if it has art. */
  layers: Layers | null;
  pieces: Record<string, PieceSheet>;
  courier: PieceSheet | null;
  /** Rooms that can be shown: drawn, or with their art cut. */
  available: FloorTheme[];
  choose: (id: ThemeId) => void;
};

/**
 * The viewer's room. Per viewer, in this browser only: no account setting and
 * nothing in the database. The night room until a choice is made, and whenever
 * storage is blocked or the chosen room's art is missing.
 */
export function useFloorTheme(): FloorThemeState {
  const [manifest, setManifest] = useState<FloorManifest>({ version: 1, themes: {} });
  const [choice, setChoice] = useState<ThemeId>("night");
  const [dark, setDark] = useState(true);

  useEffect(() => {
    let live = true;
    loadManifest().then((m) => live && setManifest(m));
    const saved = readChoice();
    if (saved) setChoice(saved);
    setDark(prefersDark());
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    const onChange = () => setDark(prefersDark());
    mq?.addEventListener?.("change", onChange);
    return () => {
      live = false;
      mq?.removeEventListener?.("change", onChange);
    };
  }, []);

  const available = THEMES.filter((t) => t.drawn || manifest.themes[t.id]?.layers);
  const theme = available.find((t) => t.id === choice) ?? THEME_BY_ID.night;
  const art = manifest.themes[theme.id];
  const layers = art ? (dark && art.dark ? art.dark : art.layers) : null;

  const choose = useCallback((id: ThemeId) => {
    setChoice(id);
    writeChoice(id);
  }, []);

  return { theme, layers, pieces: art?.pieces ?? {}, courier: manifest.courier ?? null, available, choose };
}
