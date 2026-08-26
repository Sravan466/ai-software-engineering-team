/**
 * Set dressing for the crew floor. Same string-grid material as the agents, so
 * the room and the people standing in it are made of the same thing.
 *
 * The wall's status board is deliberately NOT here — it carries live text, so it
 * is built from real elements rather than baked into a sprite.
 */

export const PROP_PALETTE: Record<string, string> = {
  o: "#05060a", // outline
  G: "#2c7049", // foliage shadow
  g: "#3ba169", // foliage light
  T: "#7a4a2b", // terracotta
  t: "#96603a", // terracotta highlight
  M: "#1a2030", // machine body
  D: "#0e1420", // recessed panel
  L: "#22d3ee", // live indicator
  W: "#e9ecf2", // label
};

/** A potted plant. Every room has one; it is how you know it is a room. */
export const PLANT: string[] = [
  "............",
  "....ooo.....",
  "...ogGgo....",
  "..ogGGGgo...",
  ".ogGGoGGgo..",
  "..ogGGGgo...",
  "...ogGgo....",
  "....oGo.....",
  "....oGo.....",
  "...ooooo....",
  "...otttoo...",
  "...oTTTo....",
  "...ooooo....",
  "............",
];

/** A server rack — the thing FORGE keeps promising to tidy up. */
export const RACK: string[] = [
  "oooooooooo",
  "oMMMMMMMMo",
  "oMDDDDDDMo",
  "oMDLWWWDMo",
  "oMDDDDDDMo",
  "oMDLWWWDMo",
  "oMDDDDDDMo",
  "oMDLWWWDMo",
  "oMDDDDDDMo",
  "oMMMMMMMMo",
  "oMDDDDDDMo",
  "oMDLWWWDMo",
  "oMDDDDDDMo",
  "oMMMMMMMMo",
  "oooooooooo",
  ".o......o.",
];
