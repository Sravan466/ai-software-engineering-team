"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type SecurityFinding, type SecurityState, type WaiveKind } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { ReasonKinds, WAIVE_KINDS } from "./ReasonKinds";

/**
 * What was actually done about each thing Warden found.
 *
 * Findings used to be terminal. A documented high-severity issue with a written
 * remediation went into the archive with the code unchanged, because the report was
 * the end of the road — there was nowhere for a finding to *go*. This is the
 * somewhere: every critical and high finding leaves here fixed and re-audited, or
 * waived on the record with a reason, and the Ship button stays shut until each one
 * is one or the other.
 *
 * Sending a finding back reaches the agent that wrote the offending file, which is
 * the one route that can fix it — and because that rewinds everything built on top,
 * the security review runs again by itself. Nothing here marks a finding fixed on
 * the strength of having asked.
 *
 * Since #50 the list is split by who decides. Serious findings (critical/high, not
 * UI/UX) are the crew's: it fixes them itself, and they appear here as "Fixed
 * automatically", folded away with the round that fixed each. Small ones — medium,
 * low and polish — keep the Send back / Waive cards, exactly as before.
 */

/** Which findings a surface is about. */
type Scope = "small" | "serious" | "all";

const RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

const SEVERITY_CLASS: Record<string, string> = {
  critical: "badge-bad",
  high: "badge-bad",
  medium: "badge-warn",
  low: "badge",
};

const STATUS_COPY: Record<SecurityFinding["status"], { label: string; cls: string }> = {
  open: { label: "Open", cls: "badge-bad" },
  fix_requested: { label: "Being fixed", cls: "badge-run" },
  fixed: { label: "Fixed and re-audited", cls: "badge-ok" },
  gone: { label: "No longer reported", cls: "badge-ok" },
  waived: { label: "Waived", cls: "badge-warn" },
};

/**
 * The states that let a build ship. Anything else still needs a decision — and
 * still needs its buttons, which is the half that was missing: a finding whose fix
 * request died mid-flight sat at "Being fixed" with no controls and no way out,
 * while the server went on refusing every approve because of it.
 */
const SETTLED = new Set(["fixed", "gone", "waived"]);

export default function SecurityFindings({
  id,
  busy,
  act,
  onChange,
  scope = "all",
  allowWaive = true,
  onCount,
}: {
  id: string;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
  /** Fired whenever a disposition changes, so the gate above can re-read itself. */
  onChange?: () => void;
  /** Small findings (the Security stop), serious ones (needs help), or both. */
  scope?: Scope;
  /** False hides Waive — on "needs help" it lives behind More. */
  allowWaive?: boolean;
  /** How many findings in scope still need something done. */
  onCount?: (n: number) => void;
}) {
  const [state, setState] = useState<SecurityState | null>(null);
  const [error, setError] = useState("");
  const [waiving, setWaiving] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [kind, setKind] = useState<WaiveKind | null>(null);
  const [working, setWorking] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError("");
    try {
      setState(await api.getSecurity(id));
    } catch (e: any) {
      setError(e.message);
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Severity first, then the ones still needing a decision — the reviewer's job here
  // is to empty the top of this list, so the top of the list is what needs them.
  const ordered = useMemo(() => {
    const all = state?.findings ?? [];
    // The crew's own fixes are not decisions: they fold away below, not in this list.
    const rows = all.filter((f) =>
      scope === "small"
        ? // A serious finding still open here is from a build parked before the crew
          // fixed its own; hiding it would leave Approve blocked with nothing to click.
          !f.serious || !SETTLED.has(f.status)
        : scope === "serious"
          ? f.serious && !SETTLED.has(f.status)
          : !(f.serious && f.status === "fixed"),
    );
    return [...rows].sort((a, b) => {
      const settled = (f: SecurityFinding) => (SETTLED.has(f.status) ? 1 : 0);
      return (
        settled(a) - settled(b) ||
        (RANK[a.severity] ?? 9) - (RANK[b.severity] ?? 9) ||
        a.title.localeCompare(b.title)
      );
    });
  }, [state, scope]);

  const fixedByCrew = useMemo(
    () =>
      scope === "serious"
        ? []
        : (state?.findings ?? [])
            .filter((f) => f.serious && f.status === "fixed")
            .sort((a, b) => (a.fixed_round ?? 99) - (b.fixed_round ?? 99)),
    [state, scope],
  );
  const open = ordered.filter((f) => !SETTLED.has(f.status)).length;
  // What actually blocks shipping — the same rule the server applies. Open medium and
  // low findings are worth reading, but they are not "needs a decision".
  const blocking = ordered.filter(
    (f) => !SETTLED.has(f.status) && (f.serious || f.severity === "critical" || f.severity === "high"),
  ).length;
  useEffect(() => {
    if (state) onCount?.(open);
  }, [state, open, onCount]);

  async function sendBack(finding: SecurityFinding) {
    setWorking(finding.key);
    const sent = await act(() => api.fixFinding(id, finding.key));
    setWorking(null);
    if (sent) onChange?.();
  }

  async function waive(finding: SecurityFinding) {
    const text = reason.trim();
    if (!text || (finding.serious && !kind)) return;
    setWorking(finding.key);
    setError("");
    try {
      await api.waiveFinding(id, finding.key, text, finding.serious ? (kind ?? undefined) : undefined);
      setWaiving(null);
      setReason("");
      setKind(null);
      await refresh();
      onChange?.();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setWorking(null);
    }
  }

  if (state === null && !error) {
    return (
      <div className="artifact-pad">
        <SkeletonLines lines={4} />
      </div>
    );
  }

  if (error && state === null) {
    // Before the empty state, not after. A failed fetch leaves `findings` empty,
    // and reporting that as "nothing to answer for" over a build nobody could read
    // the findings of is the most reassuring possible way to be wrong.
    return (
      <div className="artifact-pad">
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t load this build&apos;s findings</span>
            <span className="notice-text">
              {error} This doesn&apos;t mean there are no findings.
            </span>
            <div className="notice-actions">
              <button className="btn btn-sm btn-primary" onClick={refresh}>
                {Icon.refresh} Try again
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (ordered.length === 0) {
    return (
      <div className="artifact-pad">
        <div className="notice notice-ok">
          {Icon.check}
          <div className="notice-body">
            <span className="notice-title">Nothing outstanding</span>
            <span className="notice-text">
              {fixedByCrew.length
                ? "Every serious finding was fixed by the crew and confirmed by a re-audit."
                : "The security review found nothing that needs a decision."}
            </span>
          </div>
        </div>
        <FixedByCrew findings={fixedByCrew} />
      </div>
    );
  }

  const unresolved = blocking;

  return (
    <div className="findings">
      <div className="findings-head">
        <span className="findings-count">
          {unresolved > 0 ? (
            <>
              <span className="dot dot-bad dot-pulse" aria-hidden="true" />
              {scope === "serious"
                ? `${unresolved} still open`
                : `${unresolved} still need${unresolved === 1 ? "s" : ""} a decision`}
            </>
          ) : open > 0 ? (
            <>
              <span className="dot dot-warn" aria-hidden="true" />
              {open} open, none blocking
            </>
          ) : (
            <>
              <span className="dot dot-ok" aria-hidden="true" />
              {scope === "small" ? "Every finding for you has been settled" : "Every finding has been settled"}
            </>
          )}
        </span>
        <span className="rule" />
        {scope === "small" && (
          <span className="field-hint">Medium, low and UI/UX findings for you to decide</span>
        )}
      </div>

      <ul className="finding-list">
        {ordered.map((f) => {
          const agent = f.owner_phase ? AGENT_BY_KEY[f.owner_phase] : undefined;
          const status = STATUS_COPY[f.status];
          const needsDecision = !SETTLED.has(f.status);
          const mine = working === f.key;
          return (
            <li key={f.key} className="finding" data-severity={f.severity} data-status={f.status}>
              <div className="finding-top">
                <span className={`badge ${SEVERITY_CLASS[f.severity] ?? "badge"}`}>
                  {f.severity || "unrated"}
                </span>
                <b className="finding-title">{f.title}</b>
                <span className="rule" />
                <span className={`badge ${status.cls}`}>{status.label}</span>
              </div>

              <div className="finding-meta">
                {f.category && <span>{f.category}</span>}
                {f.location && <code className="mono">{f.location}</code>}
                {agent ? (
                  <span className="finding-owner" style={{ ["--agent" as string]: agent.accent }}>
                    <AgentSprite agent={agent} size={18} state="queued" />
                    {agent.codename} wrote it
                  </span>
                ) : (
                  <span className="finding-owner finding-owner-none">
                    {Icon.info} no file owns this
                  </span>
                )}
              </div>

              {f.recommendation && <p className="finding-fix">{f.recommendation}</p>}
              {f.note && (
                <p className="finding-note">
                  <b>
                    Waived
                    {f.waive_kind
                      ? ` · ${WAIVE_KINDS.find((k) => k.key === f.waive_kind)?.label ?? f.waive_kind}`
                      : ""}
                    :
                  </b>{" "}
                  {f.note}
                </p>
              )}

              {needsDecision && waiving !== f.key && (!f.serious || allowWaive) && (
                <div className="finding-acts">
                  {/* On "needs help" the crew already sent these back, round after round. */}
                  {scope !== "serious" && (
                  <button
                    className="btn btn-sm btn-primary"
                    disabled={busy || mine || !f.owner_phase}
                    onClick={() => sendBack(f)}
                    title={
                      f.owner_phase
                        ? `Send this back to ${agent?.codename ?? f.owner_phase} and re-run the review`
                        : "No agent in this build owns this file, so it can't be sent back."
                    }
                  >
                    {mine && <span className="btn-spinner" aria-hidden="true" />}
                    {Icon.undo}
                    {f.status === "fix_requested" ? "Send back again" : "Send back to fix"}
                  </button>
                  )}
                  <button className="btn btn-sm" disabled={busy || mine} onClick={() => setWaiving(f.key)}>
                    Waive it
                  </button>
                </div>
              )}

              {waiving === f.key && (
                <div className="finding-waive">
                  {f.serious && <ReasonKinds name={`kind-${f.key}`} kind={kind} setKind={setKind} />}
                  <div className="field">
                    <label htmlFor={`waive-${f.key}`}>Why is this acceptable for this build?</label>
                    <input
                      id={`waive-${f.key}`}
                      className="input"
                      autoFocus
                      placeholder="e.g. internal tool behind SSO, no untrusted browser reaches it"
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") waive(f);
                        if (e.key === "Escape") {
                          setWaiving(null);
                          setReason("");
                        }
                      }}
                    />
                  </div>
                  <button
                    className="btn btn-sm btn-primary"
                    disabled={!reason.trim() || mine || (f.serious && !kind)}
                    onClick={() => waive(f)}
                  >
                    {mine && <span className="btn-spinner" aria-hidden="true" />}
                    Record the waiver
                  </button>
                  <button
                    className="btn btn-sm"
                    onClick={() => {
                      setWaiving(null);
                      setReason("");
                      setKind(null);
                    }}
                  >
                    Cancel
                  </button>
                  <p className="field-hint" style={{ flexBasis: "100%" }}>
                    This is the only record of why a known issue shipped. Write it for whoever
                    reads the build later.
                  </p>
                </div>
              )}
            </li>
          );
        })}
      </ul>

      <FixedByCrew findings={fixedByCrew} />

      {error && (
        <div className="notice notice-bad" role="alert" style={{ margin: "14px 16px" }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-text">{error}</span>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * What the crew fixed by itself, folded away. Proof, not a decision: each one names
 * the round whose re-audit stopped reporting it.
 */
function FixedByCrew({ findings }: { findings: SecurityFinding[] }) {
  if (findings.length === 0) return null;
  return (
    <details className="fixed-crew">
      <summary>
        <span className="fixed-crew-chev" aria-hidden="true">{Icon.chevron}</span>
        <span className="dot dot-ok" aria-hidden="true" />
        Fixed automatically
        <span className="seg-count">{findings.length}</span>
      </summary>
      <ul className="fixed-crew-list">
        {findings.map((f) => (
          <li key={f.key} className="fixed-crew-row">
            <span className={`badge ${SEVERITY_CLASS[f.severity] ?? "badge"}`}>{f.severity}</span>
            <span className="fixed-crew-title">{f.title}</span>
            {f.location && <code className="mono">{f.location}</code>}
            <span className="fixed-crew-proof">
              {f.fixed_round ? `Round ${f.fixed_round}` : "Fixed"} · the re-audit no longer reports it
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}
