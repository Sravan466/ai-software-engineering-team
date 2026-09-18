"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type SecurityFinding, type SecurityState } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";

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
 */

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
}: {
  id: string;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
  /** Fired whenever a disposition changes, so the gate above can re-read itself. */
  onChange?: () => void;
}) {
  const [state, setState] = useState<SecurityState | null>(null);
  const [error, setError] = useState("");
  const [waiving, setWaiving] = useState<string | null>(null);
  const [reason, setReason] = useState("");
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
    const rows = state?.findings ?? [];
    return [...rows].sort((a, b) => {
      const settled = (f: SecurityFinding) => (SETTLED.has(f.status) ? 1 : 0);
      return (
        settled(a) - settled(b) ||
        (RANK[a.severity] ?? 9) - (RANK[b.severity] ?? 9) ||
        a.title.localeCompare(b.title)
      );
    });
  }, [state]);

  async function sendBack(finding: SecurityFinding) {
    setWorking(finding.key);
    const sent = await act(() => api.fixFinding(id, finding.key));
    setWorking(null);
    if (sent) onChange?.();
  }

  async function waive(finding: SecurityFinding) {
    const text = reason.trim();
    if (!text) return;
    setWorking(finding.key);
    setError("");
    try {
      await api.waiveFinding(id, finding.key, text);
      setWaiving(null);
      setReason("");
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

  if (ordered.length === 0) {
    return (
      <div className="artifact-pad">
        <div className="notice notice-ok">
          {Icon.check}
          <div className="notice-body">
            <span className="notice-title">Nothing outstanding</span>
            <span className="notice-text">
              The security review reported no findings this build has to answer for.
            </span>
          </div>
        </div>
      </div>
    );
  }

  const unresolved = state?.unresolved ?? 0;

  return (
    <div className="findings">
      <div className="findings-head">
        <span className="findings-count">
          {unresolved > 0 ? (
            <>
              <span className="dot dot-bad dot-pulse" aria-hidden="true" />
              {unresolved} still need{unresolved === 1 ? "s" : ""} a decision
            </>
          ) : (
            <>
              <span className="dot dot-ok" aria-hidden="true" />
              Every serious finding has been settled
            </>
          )}
        </span>
        <span className="rule" />
        {state && state.rounds_used > 0 && (
          <span
            className="field-hint"
            title="Each round sends the findings back to the agents that own the files, then re-runs the review."
          >
            {state.rounds_used} of {state.rounds_allowed} automatic fix round
            {state.rounds_allowed === 1 ? "" : "s"} used
          </span>
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
                  <b>Waived:</b> {f.note}
                </p>
              )}

              {needsDecision && waiving !== f.key && (
                <div className="finding-acts">
                  <button
                    className="btn btn-sm btn-primary"
                    disabled={busy || mine || !f.owner_phase}
                    onClick={() => sendBack(f)}
                    title={
                      f.owner_phase
                        ? `Send this back to ${agent?.codename ?? f.owner_phase} and re-run the review`
                        : "Nothing in this build owns the file this points at, so there is no agent to send it to."
                    }
                  >
                    {mine && <span className="btn-spinner" aria-hidden="true" />}
                    {Icon.undo}
                    {f.status === "fix_requested" ? "Send back again" : "Send back to fix"}
                  </button>
                  <button className="btn btn-sm" disabled={busy || mine} onClick={() => setWaiving(f.key)}>
                    Waive it
                  </button>
                </div>
              )}

              {waiving === f.key && (
                <div className="finding-waive">
                  <div className="field">
                    <label htmlFor={`waive-${f.key}`}>Why is this acceptable for this build?</label>
                    <input
                      id={`waive-${f.key}`}
                      className="input"
                      autoFocus
                      placeholder="e.g. internal tool behind SSO — no untrusted browser reaches it"
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
                    disabled={!reason.trim() || mine}
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
                    }}
                  >
                    Cancel
                  </button>
                  <p className="field-hint" style={{ flexBasis: "100%" }}>
                    This is the only record anyone reading the build later has of why a known issue
                    shipped, so write it for them.
                  </p>
                </div>
              )}
            </li>
          );
        })}
      </ul>

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
