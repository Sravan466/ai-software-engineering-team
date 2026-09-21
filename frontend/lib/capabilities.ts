/**
 * Reading what a model can do — and saying it in words.
 *
 * The rule for "can this model run a build?" is decided by the backend, once, and
 * arrives as `cannot_build`. It used to be re-derived here as well, and the two
 * copies disagreed about what an empty capability list means: the backend read the
 * runtime saying "nothing" as a no, the page read it as "unknown". So the page no
 * longer has an opinion — it reads the list, and quotes the runtime's own words
 * (`model_capabilities`) when it has to explain.
 */

/** Whether a model can run a build — the backend's verdict, read rather than re-made. */
export function canRunABuild(model: string, cannotBuild?: readonly string[]): boolean {
  return !cannotBuild?.includes(model);
}

/** "a", "a and b", "a, b and c" — an English list, not a comma-joined array. */
export function listOf(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/**
 * What the runtime said, as the predicate of a sentence that begins "the runtime …".
 *
 * Quoted, never characterised: "it makes embeddings" is true of every case anyone
 * has today and is still a guess — all the rule knows is that `completion` is not
 * among what was reported. An empty list is the runtime answering, so it is said as
 * an answer rather than rendered as a blank.
 */
export function runtimeSays(capabilities: readonly string[], plural = false): string {
  if (capabilities.length === 0) return `reports nothing ${plural ? "they" : "it"} can do`;
  return `lists ${plural ? "them" : "it"} as ${listOf(capabilities)}`;
}
