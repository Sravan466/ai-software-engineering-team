/**
 * The crew.
 *
 * Each pipeline phase is run by a named character rather than an anonymous job
 * title, because watching eight specialists hand work to each other is the thing
 * this product actually does. A persona carries: a codename, a colour ramp that
 * is only ever theirs, an animated sprite sheet, a motion signature, and a voice — short
 * status lines written the way that character would write them.
 *
 * `key` matches the backend phase key in app/agents/*.
 *
 * ── How the characters are drawn ──────────────────────────────────────────
 * The art is raster: a sprite sheet per agent in public/agents/, cut from the
 * generated sheets in assets/ by scripts/agent_art.py (see `artFor`). Two
 * rules from the original 24x24 pixel pass still hold, and the art was
 * generated against them:
 *
 *   1. SILHOUETTE FIRST. A character has to be identifiable with every colour
 *      knocked out. So no two crew members share an outline: SCOPE has a hat
 *      brim, ATLAS a survey spire, FORGE a raised welding shield, PRISM a pair
 *      of ear cups, SIEVE a loupe held up beside the head, WARDEN a crest and a
 *      slab shield, RELAY a pair of thruster fins, LEDGER an eyeshade and a
 *      tally column. Colour is then confirmation, not the only signal — which
 *      is also what makes the roster survive colour blindness and an 18px render.
 *
 *   2. HUE-SHIFTED RAMPS, not brightness ramps. Every agent carries three tones:
 *      `accent` (base), `accentLit` (highlight, rotated warm) and `accentDim`
 *      (shadow, rotated cool). The art is painted in them, and the UI uses the
 *      same three for everything that belongs to that agent.
 */

export type AgentMotion =
  | "nod"      // considers, then commits
  | "drift"    // thinks in space
  | "thrum"    // steady machine rhythm
  | "flicker"  // restless, visual
  | "scan"     // sweeps for defects
  | "guard"    // braced, watchful
  | "launch"   // coiled, then goes
  | "tally";   // counts, flips, counts

export type Persona = {
  key: string;
  n: string;
  codename: string;
  role: string;
  discipline: string;
  deliver: string;
  trait: string;
  tagline: string;
  motion: AgentMotion;
  /** What you can pick this character out by with the colour turned off. */
  silhouette: string;
  /** Signature colour ramp. Used for this agent and nothing else. */
  accent: string;
  /** Highlight — the base hue rotated warm, not just lightened. */
  accentLit: string;
  /** Shadow — the base hue rotated cool, not just darkened. */
  accentDim: string;
  /** Voice: short status lines, in character, per state. */
  lines: { queued: string; working: string; done: string; rejected: string };
  debate?: boolean;
  /**
   * The thing on their desk. 14x12, drawn from DESK_PALETTE plus the agent's
   * own A/H/B ramp — a cabin with a generic monitor in it belongs to nobody.
   */
  deskProp: string[];
};

/**
 * Materials for the object on each agent's desk. The agent's own three tones
 * are mixed in at render time as A / H / B, so the prop is unmistakably theirs
 * without needing a second colour system.
 */
export const DESK_PALETTE: Record<string, string> = {
  o: "#05060c", // outline
  W: "#dfe4ee", // paper / casing
  X: "#8b95ab", // paper, shaded
  M: "#49536a", // metal
  m: "#7e8ca8", // metal, lit
  N: "#242b3c", // metal, shadow
  S: "#0d1424", // screen
  L: "#22d3ee", // exhaust / indicator
};

/**
 * Where an agent's art lives. `sheet` is 4 frames across and 6 rows down, one
 * row per state — queued, working, done, rejected, gate, then asleep
 * (agents.css positions them in that order); `still` is a small single frame
 * for renders too small for the sheet to read.
 */
export function artFor(a: Persona): { sheet: string; still: string } {
  const slug = a.codename.toLowerCase();
  return {
    sheet: `/agents/${slug}.webp?v=${ART_VERSION}`,
    still: `/agents/${slug}-still.webp?v=${ART_VERSION}`,
  };
}

/**
 * Bump whenever the sheet layout or the art changes. The CSS assumes the
 * layout (rows, order), so a cached sheet from before a change would be drawn
 * at the wrong offsets; a new query string makes every browser and CDN fetch
 * the new one. 2 = the 4x6 sheet with the sleep row.
 */
const ART_VERSION = 2;

/** Build the render palette for one agent's desk prop. */
export function deskPaletteFor(a: Persona): Record<string, string> {
  return { ...DESK_PALETTE, A: a.accent, H: a.accentLit, B: a.accentDim };
}

export const AGENTS: Persona[] = [
  {
    key: "product_manager",
    n: "01",
    codename: "SCOPE",
    role: "Product Manager",
    discipline: "Requirements",
    deliver: "product-spec.md",
    trait: "Ruthless",
    tagline: "Cuts the idea down to the part that ships.",
    motion: "nod",
    silhouette: "Flat cap brim, clipboard up",
    accent: "#ffb627",
    accentLit: "#ffdd8a",
    accentDim: "#8c520c",
    lines: {
      queued: "Waiting for the brief",
      working: "Cutting scope",
      done: "Scope locked",
      rejected: "Rethinking scope",
    },
    deskProp: [
      "...oooooooo...",
      "..oXXoooXXo...",
      "..oWWWWWWWWo..",
      "..oWoooooWWo..",
      "..oWWWWWWWWo..",
      "..oWoooooWWo..",
      "..oWWWWWWWWo..",
      "..oWAAAAoWWo..",
      "..oWWWWWWWWo..",
      "..oWAAAAAAWo..",
      "..oWWWWWWWWo..",
      "..oooooooooo..",
    ],
  },
  {
    key: "system_design",
    n: "02",
    codename: "ATLAS",
    role: "System Design",
    discipline: "Architecture",
    deliver: "architecture.md",
    trait: "Deliberate",
    tagline: "Draws the shape everything else has to fit.",
    motion: "drift",
    silhouette: "Survey spire, arms out to the rule",
    accent: "#4d9de0",
    accentLit: "#a5d8ff",
    accentDim: "#1e3f75",
    lines: {
      queued: "Waiting on scope",
      working: "Drawing the shape",
      done: "Architecture set",
      rejected: "Redrawing",
    },
    deskProp: [
      "..oooooooooo..",
      "..oSSSSSSSSo..",
      "..oSAoSoAoSo..",
      "..oSoSoSoSSo..",
      "..oSSAoAoSSo..",
      "..oSoSoSoSSo..",
      "..oSAoSoAoSo..",
      "..oSSSSSSSSo..",
      "..oSoAAoASSo..",
      "..oSSSSSSSSo..",
      "..oooooooooo..",
      "....o....o....",
    ],
  },
  {
    key: "backend_engineer",
    n: "03",
    codename: "FORGE",
    role: "Backend Engineer",
    discipline: "APIs & data",
    deliver: "backend/",
    trait: "Unhurried",
    tagline: "Builds the part that has to survive production.",
    motion: "thrum",
    debate: true,
    accent: "#3dd68c",
    accentLit: "#9bf5c4",
    accentDim: "#126044",
    silhouette: "Flat welding shield up, heaviest build",
    lines: {
      queued: "Waiting on the design",
      working: "Laying pipe",
      done: "Endpoints up",
      rejected: "Tearing it out",
    },
    deskProp: [
      "....oooo......",
      "...oAHHAo.....",
      "..oAHooHAo....",
      "..oAHooHAo....",
      "...oAHHAo.....",
      "....oAAo......",
      "..ooooAAoooo..",
      ".oMmmmAAmmmMo.",
      ".oMooooooooMo.",
      ".oMmmmmmmmmMo.",
      ".oooooooooooo.",
      "...o......o...",
    ],
  },
  {
    key: "frontend_engineer",
    n: "04",
    codename: "PRISM",
    role: "Frontend Engineer",
    discipline: "Interface",
    deliver: "frontend/",
    trait: "Restless",
    tagline: "Makes the thing people actually touch.",
    motion: "flicker",
    accent: "#ff5da2",
    accentLit: "#ffb3d1",
    accentDim: "#8a1c5c",
    silhouette: "Ear cups either side of the head",
    lines: {
      queued: "Waiting on the API",
      working: "Pushing pixels",
      done: "Interface built",
      rejected: "Starting the layout over",
    },
    deskProp: [
      "..............",
      "...oooooooo...",
      "..oHHHHHHHHo..",
      "..oAAAAAAAAo..",
      "..oBBBBBBBBo..",
      "..oWWWWWWWWo..",
      "..oXXXXXXXXo..",
      "..oooooooooo..",
      "....oo..oo....",
      "....oo..oo....",
      "..............",
      "..............",
    ],
  },
  {
    key: "qa_engineer",
    n: "05",
    codename: "SIEVE",
    role: "QA Engineer",
    discipline: "Tests",
    deliver: "tests/",
    trait: "Suspicious",
    tagline: "Assumes it is broken until it proves otherwise.",
    motion: "scan",
    accent: "#ff8a3d",
    accentLit: "#ffc294",
    accentDim: "#8a3a10",
    silhouette: "Loupe held up beside the head",
    lines: {
      queued: "Waiting for something to break",
      working: "Hunting edge cases",
      done: "Suite green",
      rejected: "Re-testing",
    },
    deskProp: [
      "....oooo......",
      "...oXXXXo.....",
      "..oooooooooo..",
      "..oWSSSSSSWo..",
      "..oWSoAAoSWo..",
      "..oWSAAAASWo..",
      "..oWSoAAoSWo..",
      "..oWSSSSSSWo..",
      "..oWSSSSSSWo..",
      "..oooooooooo..",
      "..............",
      "..............",
    ],
  },
  {
    key: "security_engineer",
    n: "06",
    codename: "WARDEN",
    role: "Security Engineer",
    discipline: "Threat model",
    deliver: "security-review.md",
    trait: "Unblinking",
    tagline: "Reads every feature as an attack surface.",
    motion: "guard",
    accent: "#ff4d6d",
    accentLit: "#ffa0b1",
    accentDim: "#7d1830",
    silhouette: "Crested helmet, slab shield on the left",
    lines: {
      queued: "Watching",
      working: "Modelling threats",
      done: "Surface hardened",
      rejected: "Re-auditing",
    },
    deskProp: [
      "..............",
      ".....oooo.....",
      "....oAAAAo....",
      "...oAAooAAo...",
      "...oAo..oAo...",
      "..oooooooooo..",
      "..oBBBBBBBBo..",
      "..oBBBooBBBo..",
      "..oBBBooBBBo..",
      "..oBBBBBBBBo..",
      "..oooooooooo..",
      "..............",
    ],
  },
  {
    key: "devops_engineer",
    n: "07",
    codename: "RELAY",
    role: "DevOps Engineer",
    discipline: "CI / CD",
    deliver: ".github/ + Dockerfile",
    trait: "Impatient",
    tagline: "Gets it off this machine and into the world.",
    motion: "launch",
    accent: "#22d3ee",
    accentLit: "#9df0fb",
    accentDim: "#0a5b73",
    silhouette: "Thruster fins flared behind the shoulders",
    lines: {
      queued: "On the pad",
      working: "Wiring the pipeline",
      done: "Ready to ship",
      rejected: "Rebuilding the pipeline",
    },
    deskProp: [
      "......oo......",
      ".....oHHo.....",
      ".....oAAo.....",
      "....oAAAAo....",
      "....oAAAAo....",
      "...oMAAAAMo...",
      "...oMAAAAMo...",
      "...ooAAAAoo...",
      "....oLLLLo....",
      ".....oLLo.....",
      "..oooooooooo..",
      "..............",
    ],
  },
  {
    key: "cost_estimation",
    n: "08",
    codename: "LEDGER",
    role: "Cost Estimation",
    discipline: "Budget",
    deliver: "cost-report.md",
    trait: "Literal",
    tagline: "Tells you what this will actually cost to run.",
    motion: "tally",
    accent: "#c6f135",
    accentLit: "#eafda0",
    accentDim: "#566f0e",
    silhouette: "Narrow eyeshade, tally column at the side",
    lines: {
      queued: "Nothing to count yet",
      working: "Running the numbers",
      done: "Budget filed",
      rejected: "Recounting",
    },
    deskProp: [
      "..oooooooooo..",
      "..oWWWWWWWWo..",
      "..oWAAoAAAWo..",
      "..oWWWWWWWWo..",
      "..oWAoAAAAWo..",
      "..oWWWWWWWWo..",
      "..oWAAAoAAWo..",
      "..oWWWWWWWWo..",
      "..oWoAAAAAWo..",
      "..oWWWWWWWWo..",
      "..oooooooooo..",
      "..............",
    ],
  },
];

export const AGENT_BY_KEY: Record<string, Persona> = Object.fromEntries(
  AGENTS.map((a) => [a.key, a]),
);
