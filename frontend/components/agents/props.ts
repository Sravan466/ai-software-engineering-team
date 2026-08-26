/**
 * Set dressing for the crew floor. Same string-grid material as the agents, so
 * the room and the people standing in it are made of the same thing.
 *
 * Props exist at three depths, and depth is the whole point: a room reads as a
 * room when things sit *behind* and *in front of* the people in it, not beside
 * them. So the set is split into what stands against the back wall (rack,
 * board, plant, monitors), what hovers over the floor (drone), and what sits
 * near the camera (crate, spool, mug) — the near tier is rendered larger and
 * darker, which is the cheapest honest depth cue there is.
 *
 * The wall's status board is deliberately NOT here — it carries live text, so it
 * is built from real elements rather than baked into a sprite.
 *
 * Every material is a ramp of at least three tones, hue-shifted rather than
 * merely darkened: shadows drift blue, highlights drift warm. A single hue
 * lightened and darkened reads as plastic, which is what the first pass looked
 * like.
 */

export const PROP_PALETTE: Record<string, string> = {
  o: "#05060c", // outline
  G: "#1d5439", // foliage, shadow
  g: "#39a066", // foliage, base
  h: "#74d69d", // foliage, highlight
  T: "#5f3a22", // terracotta, shadow
  t: "#96603a", // terracotta, base
  u: "#c2895a", // terracotta, highlight
  M: "#1a2030", // machine, base
  m: "#333d52", // machine, highlight
  D: "#0a1019", // recessed panel
  c: "#414d68", // coiled cable
  w: "#5c4028", // wood, shadow
  d: "#8a6238", // wood, base
  e: "#b58a55", // wood, highlight
  S: "#0d1424", // screen, off
  s: "#1f4160", // screen, lit
  L: "#22d3ee", // live indicator
  A: "#f5a524", // attention indicator
  W: "#e9ecf2", // label / board
  X: "#8b95ab", // mid grey
};

/* ── back tier: stands against the wall ─────────────────────────────────── */

/** A server rack — the thing FORGE keeps promising to tidy up. */
export const RACK: string[] = [
  "oooooooooo",
  "omMMMMMMmo",
  "oMDDDDDDMo",
  "oMDLWWWDMo",
  "oMDDDDDDMo",
  "oMDAWWWDMo",
  "oMDDDDDDMo",
  "oMDLWWWDMo",
  "oMDDDDDDMo",
  "omMMMMMMmo",
  "oMDDDDDDMo",
  "oMDLWWWDMo",
  "oMDDDDDDMo",
  "omMMMMMMmo",
  "oooooooooo",
  ".o......o.",
];

/** A potted plant. Every room has one; it is how you know it is a room. */
export const PLANT: string[] = [
  "............",
  "....ooo.....",
  "...oghgo....",
  "..oghhhgo...",
  ".oghhGhhgo..",
  "..oghGhgo...",
  "...oghgo....",
  "....oGo.....",
  "....oGo.....",
  "...ooooo....",
  "...outtuo...",
  "...oTttTo...",
  "...oTTTTo...",
  "...ooooo....",
];

/** The whiteboard ATLAS keeps redrawing. */
export const BOARD_PROP: string[] = [
  "oooooooooooooooooooo",
  "oWWWWWWWWWWWWWWWWWWo",
  "oWXXXXWWWWWWXXXXXXWo",
  "oWWWWWWWWWWWWWWWWWWo",
  "oWXXXXXXWWWWXXXXWWWo",
  "oWWWWWWWWWWWWWWWWWWo",
  "oWXXWWWXXXXWWWXXXXWo",
  "oWWWWWWWWWWWWWWWWWWo",
  "oWXXXXXXXXWWXXXXXXWo",
  "oWWWWWWWWWWWWWWWWWWo",
  "oooooooooooooooooooo",
  "...o............o...",
  "...o............o...",
  "...o............o...",
  "..oo............oo..",
  "....................",
];

/** A two-screen bench nobody has claimed. */
export const MONITORS: string[] = [
  "..oooooo..oooooo..",
  ".oSssssSooSssssSo.",
  ".oSssssSooSssssSo.",
  ".oSssssSooSssssSo.",
  ".oSSSSSSooSSSSSSo.",
  "..oooooo..oooooo..",
  "....ooo......ooo..",
  "....oMo......oMo..",
  "..ooooooooooooooo.",
  "..oXXXXXXXXXXXXXo.",
  "..ooooooooooooooo.",
  "...o...........o..",
  "...o...........o..",
  "...o...........o..",
];

/** The water cooler. Where the handoffs actually get argued about. */
export const COOLER: string[] = [
  "..oooooo..",
  ".oWLLLLWo.",
  ".oWLLLLWo.",
  ".oWLLLLWo.",
  ".oWLLLLWo.",
  "..oWWWWo..",
  "..oWWWWo..",
  ".oooooooo.",
  ".oMmmmmMo.",
  ".oMDDDDMo.",
  ".oMmmmmMo.",
  ".oMDDDDMo.",
  ".oMmmmmMo.",
  ".oooooooo.",
  "..o....o..",
  "..oo..oo..",
];

/* ── mid tier: on the floor between the wall and the crew, and in the air ── */

/** RELAY's courier drone, permanently mid-errand. */
export const DRONE: string[] = [
  ".oooo....oooo.",
  "oMmmMo..oMmmMo",
  "..oo......oo..",
  "...oooooooo...",
  "..oMMMMMMMMo..",
  "..oMLLWWLLMo..",
  "..oMMMMMMMMo..",
  "...oooooooo...",
];

/* ── near tier: sits between you and the crew ───────────────────────────── */

/** A shipping crate. Stencilled, scuffed, in the way. */
export const CRATE: string[] = [
  "..oooooooooo..",
  ".oeeddddddeeo.",
  ".odwddddddwdo.",
  ".oddwddddwddo.",
  ".oddwddddwddo.",
  ".odwddddddwdo.",
  ".oddddddddddo.",
  ".odWWWWWWWWdo.",
  ".oddddddddddo.",
  ".odwddddddwdo.",
  ".oeeddddddeeo.",
  "..oooooooooo..",
];

/** A cable spool. Half unwound, as they always are. */
export const SPOOL: string[] = [
  "....oooooooo....",
  "..ooMMMMMMMMoo..",
  ".oMmccccccccmMo.",
  ".oMmccccccccmMo.",
  ".oMmccccccccmMo.",
  ".oMmccccccccmMo.",
  ".oMmccccccccmMo.",
  ".oMmccccccccmMo.",
  "..ooMMMMMMMMoo..",
  "....oooooooo....",
  "...oo......oo...",
  "...oo......oo...",
];

/** Somebody's mug. Still warm. */
export const MUG: string[] = [
  "..oooo..",
  ".oWWWWo.",
  ".oWssWo.",
  ".oWWWWoo",
  ".oWWWWXo",
  ".oWWWWoo",
  "..oooo..",
  "........",
];
