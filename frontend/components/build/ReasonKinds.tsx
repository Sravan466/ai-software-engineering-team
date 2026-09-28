"use client";

import type { WaiveKind } from "@/lib/api";

/**
 * Why a serious problem is being let through. GitHub's own dismissal reasons, minus
 * the ones that don't apply to generated code: a waiver of something serious is a
 * risk decision, and the record says which kind.
 */
export const WAIVE_KINDS: { key: WaiveKind; label: string; hint: string }[] = [
  { key: "false_positive", label: "False positive", hint: "It isn't actually a problem in this code." },
  { key: "mitigated", label: "Mitigated elsewhere", hint: "Something outside this code already handles it." },
  { key: "accepted_risk", label: "Accepted risk", hint: "It's real, and you're shipping it knowingly." },
];

/** The reason-kind choice, shared by a code acceptance and a serious waiver. */
export function ReasonKinds({
  name,
  kind,
  setKind,
}: {
  name: string;
  kind: WaiveKind | null;
  setKind: (k: WaiveKind) => void;
}) {
  return (
    <fieldset className="reason-kinds">
      <legend>Why</legend>
      {WAIVE_KINDS.map((k) => (
        <label key={k.key} className="reason-kind" data-on={kind === k.key || undefined}>
          <input
            type="radio"
            name={name}
            value={k.key}
            checked={kind === k.key}
            onChange={() => setKind(k.key)}
          />
          <span className="reason-kind-label">{k.label}</span>
          <span className="reason-kind-hint">{k.hint}</span>
        </label>
      ))}
    </fieldset>
  );
}

