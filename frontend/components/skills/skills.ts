"use client";

import { useEffect, useState } from "react";
import { api, type Skill, type SkillLibrary } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";

/** Library order for people: by the title they read, not the slug on disk. */
export function byTitle(a: Skill, b: Skill): number {
  return a.title.localeCompare(b.title, undefined, { sensitivity: "base" });
}

/**
 * Keywords that match every build a skill's agents run, because they are words in
 * the phase's own name — `frontend` on Prism ("frontend_engineer Frontend Code"),
 * `design` on Atlas ("system_design Architecture Design").
 *
 * Such a keyword carries no signal about the build. It makes the skill arrive on a
 * CLI tool with no interface at all, and it outscores skills whose keywords matched
 * something real. Matched against `match_text` from the server — the exact words
 * the selector reads — with the selector's own whole-word rule.
 */
export function alwaysMatching(
  keywords: string[],
  agents: string[],
  phases: SkillLibrary["phases"],
): { keyword: string; who: string[] }[] {
  const serving = agents.length ? phases.filter((p) => agents.includes(p.key)) : phases;
  const out: { keyword: string; who: string[] }[] = [];
  for (const raw of keywords) {
    const keyword = raw.trim().toLowerCase();
    if (!keyword) continue;
    const re = new RegExp(`(?<![\\p{L}\\p{N}_])${escape(keyword)}(?![\\p{L}\\p{N}_])`, "u");
    const who = serving
      .filter((p) => re.test(p.match_text ?? ""))
      .map((p) => AGENT_BY_KEY[p.key]?.codename ?? p.label);
    if (who.length) out.push({ keyword, who });
  }
  return out;
}

function escape(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// One request for the whole page, however many phases ask. A build page draws a
// "Worked from" line per phase and re-renders on every poll; each asking the
// library for itself would be eight fetches every two and a half seconds.
let titles: Promise<Record<string, string>> | null = null;

/** The library changed: the next "Worked from" asks again. */
export function forgetSkillTitles(): void {
  titles = null;
}

/**
 * Skill titles by name, for printing what a phase worked from in the words the
 * Skills page uses. Until it loads — or for a skill since deleted — the name
 * itself is what shows.
 */
export function useSkillTitles(): Record<string, string> {
  const [map, setMap] = useState<Record<string, string>>({});
  useEffect(() => {
    let live = true;
    if (!titles) {
      const mine: Promise<Record<string, string>> = api
        .listSkills()
        .then((lib) => Object.fromEntries(lib.skills.map((s) => [s.name, s.title])))
        .catch(() => {
          // Try again next time rather than remembering a failure — but only if
          // nothing newer has been asked for since; that one is not ours to drop.
          if (titles === mine) titles = null;
          return {};
        });
      titles = mine;
    }
    titles.then((m) => live && setMap(m));
    return () => {
      live = false;
    };
  }, []);
  return map;
}
