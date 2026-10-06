"use client";

import type { SkillPick, SkillPreview } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";

/**
 * What each phase would get for one idea — shared by the Skills page's dry run and
 * the composer's "What would this idea get?".
 *
 * Why a skill was chosen is printed, not tucked into a tooltip: the matched keywords
 * are the whole answer to "is this skill reaching my build for the right reason?",
 * and a hover-only answer is no answer on a keyboard or a phone. Skills that matched
 * but lost their place to the per-phase cap are shown too, because nothing in a
 * finished build says they were left out.
 */
export default function PreviewRows({
  preview,
  showLabels = false,
}: {
  preview: SkillPreview;
  /** Print each phase's name beside its agent — the Skills page has the room. */
  showLabels?: boolean;
}) {
  return (
    <ol className="dryrun-rows">
      {preview.phases.map((p) => {
        const agent = AGENT_BY_KEY[p.phase];
        const over = p.over_cap ?? [];
        const pinnedFull = p.skills.length > 0 && p.skills.every((s) => s.pinned);
        return (
          <li
            key={p.phase}
            className="dryrun-row"
            style={{ ["--agent" as string]: agent?.accent }}
          >
            <span className="dryrun-who">
              <b className="agent-line-name">{agent?.codename ?? p.phase}</b>
              {showLabels && <span className="dryrun-what">{p.label}</span>}
            </span>
            <span className="dryrun-result">
              {p.skills.length === 0 ? (
                <span className="dryrun-none">nothing matched</span>
              ) : (
                <span className="dryrun-picks">
                  {p.skills.map((s) => (
                    <Pick key={s.name} pick={s} />
                  ))}
                </span>
              )}
              {over.length > 0 && (
                <span className="dryrun-over">
                  <span className="dryrun-over-label">
                    {preview.max_per_phase === 0
                      ? "Matched, but this backend gives each phase no skills (SKILLS_MAX_PER_PHASE is 0):"
                      : pinnedFull
                        ? `Also matched but left out. Pinned skills took all ${preview.max_per_phase} places:`
                        : `Also matched but left out. The phase takes ${preview.max_per_phase} and these ranked lower:`}
                  </span>
                  <span className="dryrun-picks">
                    {over.map((s) => (
                      <Pick key={s.name} pick={s} dropped />
                    ))}
                  </span>
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function Pick({ pick, dropped }: { pick: SkillPick; dropped?: boolean }) {
  // A skill here with nothing matched and no pin has no keywords at all — it is
  // relevant to every build it serves, which is its whole reason for being here.
  const why = pick.matched.length
    ? pick.matched.join(" · ")
    : pick.pinned
      ? ""
      : "no keywords";
  return (
    <span
      className={"pick" + (pick.pinned ? " pick-pinned" : "") + (dropped ? " pick-dropped" : "")}
    >
      <span className="pick-title">{pick.title}</span>
      {why && (
        <span className="pick-why">
          {pick.matched.length > 0 && <span className="sr-only">matched </span>}
          {why}
        </span>
      )}
    </span>
  );
}
