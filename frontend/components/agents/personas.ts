/**
 * The crew.
 *
 * Each pipeline phase is run by a named character rather than an anonymous job
 * title, because watching eight specialists hand work to each other is the thing
 * this product actually does. A persona carries: a codename, a colour ramp that
 * is only ever theirs, a 24x24 sprite, a motion signature, and a voice — short
 * status lines written the way that character would write them.
 *
 * `key` matches the backend phase key in app/agents/*.
 *
 * ── How the sprites are drawn ──────────────────────────────────────────────
 * Two rules from pixel-art practice do the heavy lifting here, and both were
 * missing from the first pass:
 *
 *   1. SILHOUETTE FIRST. A sprite has to be identifiable with every colour
 *      knocked out. So no two crew members share an outline: SCOPE has a cap
 *      brim, ATLAS a survey spire, FORGE a raised welding shield, PRISM a pair
 *      of ear cups, SIEVE a loupe held up beside the head, WARDEN a crest and a
 *      slab shield, RELAY a pair of thruster fins, LEDGER a tally column. Colour
 *      is then confirmation, not the only signal — which is also what makes the
 *      roster survive colour blindness and a 30px render.
 *
 *   2. HUE-SHIFTED RAMPS, not brightness ramps. Every agent carries three tones:
 *      `accent` (base), `accentLit` (highlight, rotated warm) and `accentDim`
 *      (shadow, rotated cool). Darkening a single hue reads as flat plastic;
 *      rotating it as the value drops is what makes a 24px figure look lit.
 *
 * The grid grew 16 → 24 because eight distinguishable professions do not fit in
 * 16 rows once you spend nine of them on a head. 24 is still cheap: rows are
 * run-length merged into rects at render time.
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
  /** 24x24 sprite. Keys index PALETTE below; "." is transparent. */
  sprite: string[];
  /**
   * The thing on their desk. 14x12, drawn from DESK_PALETTE plus the agent's
   * own A/H/B ramp — a cabin with a generic monitor in it belongs to nobody.
   */
  deskProp: string[];
};

/**
 * Shared sprite palette — the materials every crew member is made of.
 * Per-agent colours are substituted at render time:
 *   A → accent, H → accentLit, B → accentDim.
 * Each material is a ramp, not a flat fill, and every ramp is hue-shifted:
 * shadows drift blue, highlights drift warm.
 */
export const PALETTE: Record<string, string> = {
  o: "#05060c", // outline — near-black, blue-cast so it sits in the room
  V: "#0d1428", // visor glass, deep
  v: "#1e2f57", // visor glass, lit
  E: "#dcfbff", // eye light
  M: "#49536a", // metal, base
  m: "#7e8ca8", // metal, highlight
  N: "#242b3c", // metal, shadow
  W: "#dfe4ee", // panel, lit
  X: "#8b95ab", // panel, mid
  K: "#141926", // undersuit
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

/** Build the render palette for one agent's desk prop. */
export function deskPaletteFor(a: Persona): Record<string, string> {
  return { ...DESK_PALETTE, A: a.accent, H: a.accentLit, B: a.accentDim };
}

// Rows 7–13 (head), 19–23 (stance) are common to the crew, so eight very
// different silhouettes still read as one team wearing one uniform.
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
    sprite: [
      "........................",
      "........................",
      "......oooooooooooo......",
      ".....oAHHHHHHHHHHAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "...oBBBBBBBBBBBBBBBBo...",
      "....oooooooooooooooo....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAVEEvvEEVAAo.....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "......oAAAAAAAAAAo......",
      ".......ooAAAAAAoo...oooo",
      "........oKKKKKKo....oWWo",
      "....oooBBBBBBBBBBoo.oXXo",
      "...oBBBWWWWWWWWWWBo.oWWo",
      "...oBBBWXooXXooXWBo.oXXo",
      "...oBBBWXXooooXXWBo.oWWo",
      "...oBBBWWWWWWWWWWBo.oooo",
      "....oooBBBBBBBBBBoo.....",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      ".....oMMMMo..oMMMMo.....",
      ".....oooooo..oooooo.....",
    ],
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
    sprite: [
      "...........oo...........",
      "..........oHHo..........",
      "..........oAAo..........",
      "......oooooAAooooo......",
      ".....oAHHHHHHHHHHAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "....oooooooooooooooo....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAVEEvvEEVAAo.....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "......oAAAAAAAAAAo......",
      ".......ooAAAAAAoo.......",
      "........oKKKKKKo........",
      "....oooBBBBBBBBBBooo....",
      "...oBBBWWWWWWWWWWBBBo...",
      ".ooBBBBWXoXoXoXWBBBBoo..",
      ".oMmoBBWXoXoXoXWBBomMo..",
      "..oooBBWWWWWWWWWWBooo...",
      "....oooBBBBBBBBBBooo....",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      ".....oMMMMo..oMMMMo.....",
      ".....oooooo..oooooo.....",
    ],
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
    sprite: [
      "........................",
      "....oooooooooooooooo....",
      "....oNmMMMMMMMMMMmNo....",
      "....oNMMMMMMMMMMMMNo....",
      "....oooooooooooooooo....",
      ".....oAHHHHHHHHHHAo.....",
      ".....oAAAAAAAAAAAAo.....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAVEEvvEEVAAo.....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "......oAAAAAAAAAAo......",
      ".......ooAAAAAAoo.......",
      "........oKKKKKKo........",
      "..ooooooBBBBBBBBBBoooooo",
      "..oBBBBBWWWWWWWWWWBBBBBo",
      "..oBBBBBWoooooooWBBBBBBo",
      "..oBBBBBWXXXXXXXWBBBBBBo",
      "..oooBBBWWWWWWWWWWBBBooo",
      "....oooBBBBBBBBBBooo....",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      "....ooMMMMo..oMMMMoo....",
      "....oooooo....oooooo....",
    ],
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
    sprite: [
      "........................",
      "........................",
      "....oooooooooooooooo....",
      "....oHHHHHHHHHHHHHHo....",
      ".....oAAAAAAAAAAAAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "..ooooAAAAAAAAAAAAoooo..",
      "..oHHoAAVVVVVVVVAAoHHo..",
      "..oHHoAAVEEvvEEVAAoHHo..",
      "..oAAoAAVVVVVVVVAAoAAo..",
      "..ooooAAAAAAAAAAAAoooo..",
      "......oAAAAAAAAAAo......",
      ".......ooAAAAAAoo.......",
      "........oKKKKKKo........",
      "....oooBBBBBBBBBBooo....",
      "...oBBBWWWWWWWWWWBBBo...",
      "...oBBBWHHAAAAHHWBBBo...",
      "...oBBBWXXWWWWXXWBBBo...",
      "...oBBBWWWWWWWWWWBBBo...",
      "....oooBBBBBBBBBBooo....",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      ".....oMMMMo..oMMMMo.....",
      ".....oooooo..oooooo.....",
    ],
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
    sprite: [
      "........................",
      "........................",
      ".........oooooo.........",
      ".......ooAAAAAAoo.......",
      "......oAHHHHHHHHAo......",
      ".....oAAAAAAAAAAAAo.....",
      "....oooooooooooooooo....",
      ".ooo.oAAVVVVVVVVAAo.....",
      "oAHAooAAVEEvvEEVAAo.....",
      "oHEHooAAVVVVVVVVAAo.....",
      "oAHAooAAAAAAAAAAAAo.....",
      ".oMo..oAAAAAAAAAAo......",
      "..oMo..ooAAAAAAoo.......",
      "...oMo..oKKKKKKo........",
      "....oooBBBBBBBBBBooo....",
      "...oBBBWWWWWWWWWWBBBo...",
      "...oBBBWXooXXooXWBBBo...",
      "...oBBBWXXooooXXWBBBo...",
      "...oBBBWWWWWWWWWWBBBo...",
      "....oooBBBBBBBBBBooo....",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      ".....oMMMMo..oMMMMo.....",
      ".....oooooo..oooooo.....",
    ],
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
    sprite: [
      "...........oo...........",
      "..........oHHo..........",
      ".........oHHHHo.........",
      "......oooHHHHHHooo......",
      ".....oAHHHHHHHHHHAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "....oooooooooooooooo....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAVEEvvEEVAAo.....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "......oAAAAAAAAAAo......",
      ".oooo..ooAAAAAAoo.......",
      "oMmmMo..oKKKKKKo........",
      "oMHHMooBBBBBBBBBBooo....",
      "oMHHMoBWWWWWWWWWWBBBo...",
      "oMmHmoBWXooXXooXWBBBo...",
      "oMmmmoBWXXooooXXWBBBo...",
      ".oMMo.BWWWWWWWWWWBBBo...",
      "..oo..oBBBBBBBBBBooo....",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      ".....oMMMMo..oMMMMo.....",
      ".....oooooo..oooooo.....",
    ],
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
    sprite: [
      ".................o......",
      "................oHo.....",
      "......ooooooooooooo.....",
      ".....oAHHHHHHHHHHAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "....oooooooooooooooo....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAVEEvvEEVAAo.....",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "......oAAAAAAAAAAo......",
      ".......ooAAAAAAoo.......",
      "........oKKKKKKo........",
      ".oM.oooBBBBBBBBBBooo.Mo.",
      "oMm.oBBBWWWWWWWWBBBo.mMo",
      "oMm.oBBBWXXooXXWBBBo.mMo",
      "oMm.oBBBWXoXXoXWBBBo.mMo",
      "oMm.oBBBWWWWWWWWBBBo.mMo",
      ".oM.oooBBBBBBBBBBooo.Mo.",
      "..o..ooBBBBBBBBBBoo..o..",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      ".....oMMMMo..oMMMMo.....",
      ".....oooooo..oooooo.....",
    ],
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
    sprite: [
      "........................",
      "........................",
      "........................",
      "......oooooooooooo......",
      ".....oAHHHHHHHHHHAo.....",
      ".....oAAAAAAAAAAAAo.....",
      "...ooHHHHHHHHHHHHHHoo...",
      ".....oAAVVVVVVVVAAo.....",
      ".....oAAVEEvvEEVAAo.ooo.",
      ".....oAAVVVVVVVVAAo.oWo.",
      ".....oAAAAAAAAAAAAo.oXo.",
      "......oAAAAAAAAAAo..oWo.",
      ".......ooAAAAAAoo...oXo.",
      "........oKKKKKKo....oWo.",
      "....oooBBBBBBBBBBoo.oXo.",
      "...oBBBWWWWWWWWWWBo.oWo.",
      "...oBBBWXXoXXoXXWBo.oXo.",
      "...oBBBWXXoXXoXXWBo.oWo.",
      "...oBBBWWWWWWWWWWBo.ooo.",
      "....oooBBBBBBBBBBoo.....",
      "......oBBBo..oBBBo......",
      "......oBBBo..oBBBo......",
      ".....oMMMMo..oMMMMo.....",
      ".....oooooo..oooooo.....",
    ],
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
