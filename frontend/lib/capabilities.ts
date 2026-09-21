/**
 * Reading what a model can do — and saying it in words.
 *
 * Whether a model can run a build is decided by the backend, once, and arrives as
 * `cannot_build`. It used to be re-derived here as well, and the two copies came to
 * disagree about what an empty capability list means. So the page has no opinion of
 * its own: it reads the list, and quotes the runtime's own words
 * (`model_capabilities`) when it has to explain.
 */

import { listOf } from "@/lib/text";

/** Whether a model can run a build — the backend's verdict, read rather than re-made. */
export function canRunABuild(model: string, cannotBuild?: readonly string[]): boolean {
  return !cannotBuild?.includes(model);
}

/**
 * What the runtime said, as the predicate of a sentence that begins "the runtime …".
 *
 * Quoted rather than characterised, so the sentence says exactly what the runtime
 * reported and no more. The list a blocked model carries always has something in it
 * (the backend only says "cannot write" on a reported `embedding`), but a blank is
 * still said as words rather than rendered as a gap, should that ever change.
 */
export function runtimeSays(capabilities: readonly string[], plural = false): string {
  if (capabilities.length === 0) return `reports nothing ${plural ? "they" : "it"} can do`;
  return `lists ${plural ? "them" : "it"} as ${listOf(capabilities)}`;
}
