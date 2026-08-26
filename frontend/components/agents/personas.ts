/**
 * The crew.
 *
 * Each pipeline phase is run by a named character rather than an anonymous job
 * title, because watching eight specialists hand work to each other is the thing
 * this product actually does. A persona carries: a codename, a colour that is
 * only ever theirs, a 16x16 sprite, a motion signature, and a voice — short
 * status lines written the way that character would write them.
 *
 * `key` matches the backend phase key in app/agents/*.
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
  /** Signature colour. Used for this agent and nothing else. */
  accent: string;
  accentDim: string;
  /** Voice: short status lines, in character, per state. */
  lines: { queued: string; working: string; done: string; rejected: string };
  debate?: boolean;
  /** 16x16 sprite. Keys index PALETTE below; "." is transparent. */
  sprite: string[];
};

// Shared sprite palette. Per-agent colours are substituted at render time:
//   A → accent, B → accentDim. Everything else is common to the crew.
export const PALETTE: Record<string, string> = {
  o: "#07080c", // outline
  V: "#141a2b", // visor glass
  E: "#eafcff", // eye light
  W: "#e9ecf2", // panel white
  X: "#5b6479", // panel detail
};

// Every sprite shares one silhouette — same helmet, same visor, same stance — so
// the crew reads as a crew. Identity lives in the colour, the chest device and
// the way each one moves.
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
    accent: "#ffb627",
    accentDim: "#8a5f10",
    lines: {
      queued: "Waiting for the brief",
      working: "Cutting scope",
      done: "Scope locked",
      rejected: "Rethinking scope",
    },
    sprite: [
      "........o.......",
      ".....ooooooo....",
      "....oAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBBWWWWWWBBo..",
      "..oBBWXXXXWBBo..",
      "..oBBWWWWWWBBo..",
      "..oBBBBBBBBBBo..",
      "...oo.oo.oo.....",
      "................",
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
    accent: "#4d9de0",
    accentDim: "#1d4a70",
    lines: {
      queued: "Waiting on scope",
      working: "Drawing the shape",
      done: "Architecture set",
      rejected: "Redrawing",
    },
    sprite: [
      "................",
      "...ooooooooo....",
      "...oAAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBBXoXoXoBBo..",
      "..oBBoXoXoXBBo..",
      "..oBBXoXoXoBBo..",
      "..oBBBBBBBBBBo..",
      "...oo.oo.oo.....",
      "................",
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
    accentDim: "#146b41",
    lines: {
      queued: "Waiting on the design",
      working: "Laying pipe",
      done: "Endpoints up",
      rejected: "Tearing it out",
    },
    sprite: [
      "................",
      "....oooooooo....",
      "...oAAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBWWWWWWWWBo..",
      "..oBWXoooooWBo..",
      "..oBWXoooooWBo..",
      "..oBBBBBBBBBBo..",
      "...oo.oo.oo.....",
      "................",
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
    accentDim: "#8d1f52",
    lines: {
      queued: "Waiting on the API",
      working: "Pushing pixels",
      done: "Interface built",
      rejected: "Starting the layout over",
    },
    sprite: [
      "................",
      "....oooooooo....",
      "...oAAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBWWWWWWWWBo..",
      "..oBWXXAAXXWBo..",
      "..oBWWWWWWWWBo..",
      "..oBBBBBBBBBBo..",
      "...oo.oo.oo.....",
      "................",
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
    accentDim: "#8f4210",
    lines: {
      queued: "Waiting for something to break",
      working: "Hunting edge cases",
      done: "Suite green",
      rejected: "Re-testing",
    },
    sprite: [
      "..o..........o..",
      "...ooooooooo....",
      "...oAAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBBWWWWWWBBo..",
      "..oBBWXooXWBBo..",
      "..oBBWWWWWWBBo..",
      "..oBBBBBBBBBBo..",
      "...oo.oo.oo.....",
      "................",
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
    accentDim: "#8d1a2f",
    lines: {
      queued: "Watching",
      working: "Modelling threats",
      done: "Surface hardened",
      rejected: "Re-auditing",
    },
    sprite: [
      "................",
      "....oooooooo....",
      "...oAAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBBWWWWWWBBo..",
      "..oBBWXXXXWBBo..",
      "..oBBBWXXWBBBo..",
      "..oBBBBWWBBBBo..",
      "...oo.oo.oo.....",
      "................",
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
    accentDim: "#0b6a7c",
    lines: {
      queued: "On the pad",
      working: "Wiring the pipeline",
      done: "Ready to ship",
      rejected: "Rebuilding the pipeline",
    },
    sprite: [
      "................",
      "....oooooooo....",
      "...oAAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBWoWoWoWoBo..",
      "..oBWWWWWWWWBo..",
      "..oBWoWoWoWoBo..",
      "..oBBBBBBBBBBo..",
      "...oo.oo.oo.....",
      "................",
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
    accentDim: "#5f7a10",
    lines: {
      queued: "Nothing to count yet",
      working: "Running the numbers",
      done: "Budget filed",
      rejected: "Recounting",
    },
    sprite: [
      "................",
      "....oooooooo....",
      "...oAAAAAAAAo...",
      "...oAAAAAAAAo...",
      "...oAVVVVVVAo...",
      "...oAVEVVEVAo...",
      "...oAVVVVVVAo...",
      "...oAAAAAAAAo...",
      "....oo.AA.oo....",
      "...oBBBBBBBBo...",
      "..oBBBWWWWBBBo..",
      "..oBBWXXXXWBBo..",
      "..oBBBWWWWBBBo..",
      "..oBBBBBBBBBBo..",
      "...oo.oo.oo.....",
      "................",
    ],
  },
];

export const AGENT_BY_KEY: Record<string, Persona> = Object.fromEntries(
  AGENTS.map((a) => [a.key, a]),
);
