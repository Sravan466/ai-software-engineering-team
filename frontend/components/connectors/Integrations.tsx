"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useState } from "react";
import { api, type IntegrationRow, type IntegrationsState, type Project } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { ConnectForm, ConnectorMark, LockLine, VarChips } from "./parts";

/**
 * A build's app connectors: the "Connect your services" card at the question
 * after the architecture, and the Connectors section of the project page.
 *
 * Only connectors that aren't connected yet are asked about. One already in the
 * account's Connectors is linked — shown as "From your Connectors" — and the
 * build never stops for it. Keys given here are saved to the account by default,
 * so the next build that needs the service doesn't ask; unticked, they're this
 * build's alone, which is also how "a different key for this build" is kept.
 */

type Act = (fn: () => Promise<unknown>) => Promise<boolean>;

function useIntegrations(id: string, refreshKey?: unknown) {
  const [state, setState] = useState<IntegrationsState | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api
      .getIntegrations(id)
      .then((s) => {
        setState(s);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, [id]);
  useEffect(load, [load, refreshKey]);
  return { state, setState, error, reload: load };
}

function names(list: string[]) {
  if (list.length <= 1) return list[0] ?? "";
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

function Strong({ list }: { list: string[] }) {
  return (
    <>
      {list.map((l, i) => (
        <span key={l}>
          {i > 0 && (i === list.length - 1 ? " and " : ", ")}
          <b className="db-strong">{l}</b>
        </span>
      ))}
    </>
  );
}

// ── one connector's row ──────────────────────────────────────────────────────
function RowBadge({ row }: { row: IntegrationRow }) {
  if (row.status === "connected" || row.status === "unchecked") {
    const where = row.source === "account" ? "From your Connectors" : "This build's key";
    const mode = row.mode === "test" ? " · Test mode" : row.mode === "live" ? " · Live" : "";
    const hint = row.saved?.find((s) => s.hint.startsWith("…"))?.hint;
    const cls = row.status === "unchecked" ? "badge-warn" : row.mode === "live" ? "badge-bad" : "badge-ok";
    return (
      <span className={`badge ${cls}`}>
        <span className={`dot ${cls === "badge-ok" ? "dot-ok" : cls === "badge-bad" ? "dot-bad" : "dot-warn"}`} aria-hidden="true" />
        {row.status === "unchecked" ? `${where} · saved, not tested` : `${where}${mode}${hint ? ` · ${hint}` : ""}`}
      </span>
    );
  }
  if (row.status === "failed") {
    return (
      <span className="badge badge-bad">
        <span className="dot dot-bad" aria-hidden="true" />
        Key failed its last test
      </span>
    );
  }
  if (row.status === "later") {
    return <span className="badge badge-warn">Later · not connected</span>;
  }
  return (
    <span className="badge badge-warn">
      <span className="dot dot-warn" aria-hidden="true" />
      Needs an answer
    </span>
  );
}

function Row({
  id,
  row,
  busy,
  onState,
  canRemove,
  onRemoveFromBuild,
}: {
  id: string;
  row: IntegrationRow;
  busy: boolean;
  onState: (s: IntegrationsState) => void;
  canRemove: boolean;
  onRemoveFromBuild: () => void;
}) {
  // A failed key is answered but not connected: offer a new one, like an unanswered row.
  const answered = row.status === "connected" || row.status === "unchecked";
  // "different" = a key for this build only, over the account's.
  const [open, setOpen] = useState<null | "connect" | "different">(null);
  const [toAccount, setToAccount] = useState(true);
  const [later, setLater] = useState(false);
  const panelId = useId();
  const checkId = useId();

  const different = open === "different";
  const fromAccount = row.source === "account";
  // A project key, or a fresh one, is shown in the form; an account one is managed
  // in the Connectors tab, so the form here starts empty for an override.
  const connector = {
    ...row,
    connected: row.source === "project" && !different,
    saved: row.source === "project" && !different ? row.saved : [],
  };
  const showForm = open !== null || row.source === "project";

  async function markLater() {
    setLater(true);
    try {
      onState(await api.integrationLater(id, row.id));
    } finally {
      setLater(false);
    }
  }

  return (
    <li className="cx-row">
      <div className="cx-row-main">
        <ConnectorMark id={row.id} label={row.label} />
        <div className="cx-row-text">
          <span className="cx-row-name">
            {row.label}
            <RowBadge row={row} />
          </span>
          {row.reason && <span className="cx-row-why">Used because {row.reason}.</span>}
          <VarChips variables={row.variables} />
        </div>
        <div className="cx-row-acts">
          {fromAccount && answered ? (
            <>
              <Link className="btn btn-sm btn-ghost" href={`/connectors/${row.id}`}>
                Manage in Connectors
              </Link>
              <button
                type="button"
                className="btn btn-sm"
                aria-expanded={different}
                aria-controls={panelId}
                onClick={() => setOpen((o) => (o === "different" ? null : "different"))}
                disabled={busy}
              >
                Use a different key for this build
              </button>
            </>
          ) : row.source !== "project" ? (
            <>
              <button
                type="button"
                className="btn btn-sm btn-accent"
                aria-expanded={open === "connect"}
                aria-controls={panelId}
                onClick={() => setOpen((o) => (o === "connect" ? null : "connect"))}
                disabled={busy}
              >
                {Icon.plug} Connect {row.label}
              </button>
              {row.status !== "later" && (
                <button type="button" className="btn btn-sm" onClick={markLater} disabled={busy || later}>
                  {later && <span className="btn-spinner" aria-hidden="true" />}
                  Later
                </button>
              )}
            </>
          ) : null}
          {canRemove && (
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={onRemoveFromBuild}
              disabled={busy}
              aria-label={`Leave ${row.label} out of this build`}
            >
              {Icon.close} Leave out
            </button>
          )}
        </div>
      </div>
      {showForm && (
        <div className="cx-row-form" id={panelId}>
          <ConnectForm
            connector={connector}
            busy={busy}
            onSave={async (values, confirmLive) => {
              const r = await api.saveIntegration(id, row.id, values, {
                confirm_live: confirmLive,
                // An override, or a project key being replaced, stays with this build.
                save_to_account: different || row.source === "project" ? false : toAccount,
              });
              onState(r.state);
              return r;
            }}
            onSaved={() => setOpen(null)}
            onRemove={
              row.source === "project"
                ? async () => {
                    onState(await api.removeIntegrationKey(id, row.id));
                  }
                : undefined
            }
            removeLabel="Remove this build's key"
            removeConfirm={
              row.account_connected
                ? "The build goes back to the key in your Connectors."
                : "The build shows it as not connected until you add a key again."
            }
            extra={
              !different && row.source !== "project" ? (
                <label className="db-check cx-save-account" htmlFor={checkId}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
                    <input id={checkId} type="checkbox" checked={toAccount} onChange={(e) => setToAccount(e.target.checked)} />
                    Save to my Connectors for future builds
                  </span>
                </label>
              ) : null
            }
          />
        </div>
      )}
    </li>
  );
}

// ── the gate ─────────────────────────────────────────────────────────────────
export function IntegrationsGate({
  project,
  id,
  busy,
  act,
}: {
  project: Project;
  id: string;
  busy: boolean;
  act: Act;
}) {
  const { state, setState, error, reload } = useIntegrations(id);
  const [going, setGoing] = useState<"later" | "continue" | null>(null);

  const rows = state?.connectors ?? [];
  const linked = rows.filter((r) => r.source === "account" && (r.status === "connected" || r.status === "unchecked"));
  const waiting = rows.filter((r) => !linked.includes(r));
  const ready = Boolean(state) && state!.unanswered.length === 0;

  async function allLater() {
    setGoing("later");
    await act(() => api.integrationsAllLater(id));
    setGoing(null);
  }
  async function carryOn() {
    setGoing("continue");
    await act(() => api.integrationsContinue(id));
    setGoing(null);
  }
  async function leaveOut(iid: string) {
    try {
      setState(await api.changeIntegrations(id, { remove: iid }));
    } catch {
      reload();
    }
  }

  return (
    <section className="decision decision-database decision-integrations" aria-labelledby="decision-title">
      <header className="decision-head">
        <span className="decision-mark" aria-hidden="true">{Icon.plug}</span>
        <div className="decision-headings">
          <h2 id="decision-title">Connect your services</h2>
          <p>
            {rows.length ? (
              <>
                Atlas&apos;s design uses <Strong list={rows.map((r) => r.label)} />.{" "}
                {linked.length > 0 && (
                  <>
                    {names(linked.map((r) => r.label))} {linked.length === 1 ? "is" : "are"} already connected from your
                    Connectors.{" "}
                  </>
                )}
                {waiting.length > 0 && (
                  <>
                    Connect {names(waiting.map((r) => r.label))} now so the crew builds against{" "}
                    {waiting.length === 1 ? "it" : "them"}, or keep going and add {waiting.length === 1 ? "it" : "them"}{" "}
                    any time.
                  </>
                )}
              </>
            ) : (
              project.gate_note
            )}
          </p>
        </div>
        <span className="badge badge-warn">
          <span className="dot dot-warn dot-pulse" aria-hidden="true" />
          Waiting on you
        </span>
      </header>

      {error ? (
        <div className="db-pad">
          <div className="notice notice-bad" role="alert">
            {Icon.alert}
            <div className="notice-body">
              <span className="notice-title">Couldn&apos;t load this build&apos;s services</span>
              <span className="notice-text">{error}</span>
              <div className="notice-actions">
                <button className="btn btn-sm btn-primary" onClick={reload}>
                  {Icon.refresh} Try again
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : !state ? (
        <div className="db-pad">
          <SkeletonLines lines={4} />
        </div>
      ) : (
        <ul className="cx-rows">
          {rows.map((r) => (
            <Row
              key={r.id}
              id={id}
              row={r}
              busy={busy}
              onState={setState}
              canRemove={state.can_change}
              onRemoveFromBuild={() => leaveOut(r.id)}
            />
          ))}
        </ul>
      )}

      <div className="cx-gate-foot">
        <button type="button" className="btn btn-lg btn-accent" onClick={carryOn} disabled={busy || !ready}>
          {going === "continue" ? <span className="btn-spinner" aria-hidden="true" /> : Icon.play} Continue build
        </button>
        <button type="button" className="btn btn-lg" onClick={allLater} disabled={busy}>
          {going === "later" && <span className="btn-spinner" aria-hidden="true" />}
          Add all later
        </button>
        {!ready && state && (
          <span className="dim" style={{ fontSize: "var(--t-sm)" }}>
            Connect or choose Later for each service to continue.
          </span>
        )}
      </div>

      <LockLine names={rows.flatMap((r) => r.variables.filter((v) => v.secret).map((v) => v.name))} />
    </section>
  );
}

// ── the project page's Connectors section ────────────────────────────────────
export function IntegrationsPanel({ id, refreshKey }: { id: string; refreshKey?: unknown }) {
  const { state, setState, error, reload } = useIntegrations(id, refreshKey);
  const [adding, setAdding] = useState("");
  const [changeError, setChangeError] = useState("");

  if (error) {
    return (
      <div className="card">
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t load this build&apos;s connectors</span>
            <span className="notice-text">{error}</span>
            <div className="notice-actions">
              <button className="btn btn-sm" onClick={reload}>
                {Icon.refresh} Try again
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }
  if (!state || (!state.used.length && !state.can_change)) return null;
  const missing = state.not_connected.length;

  async function change(body: { add?: string; remove?: string }) {
    setChangeError("");
    try {
      setState(await api.changeIntegrations(id, body));
      setAdding("");
    } catch (e: any) {
      setChangeError(e?.message || "Couldn't change the build's connectors.");
    }
  }

  return (
    <section className="card cx-panel" aria-labelledby="cx-panel-title">
      <div className="sec-head">
        <h2 className="label" id="cx-panel-title">Connectors</h2>
        <span className="rule" />
        {state.used.length > 0 && (
          <span className={`badge ${missing ? "badge-warn" : "badge-ok"}`}>
            <span className={`dot ${missing ? "dot-warn" : "dot-ok"}`} aria-hidden="true" />
            {missing ? `${missing} not connected` : "All connected"}
          </span>
        )}
      </div>
      {state.used.length ? (
        <p className="muted db-card-lede">
          The services this build&apos;s code reads keys for. A key from your Connectors is linked, not copied — replace it
          there and every build that uses it gets the new one.
        </p>
      ) : (
        <p className="muted db-card-lede">This build uses no app connectors.</p>
      )}
      <ul className="cx-rows">
        {state.connectors.map((r) => (
          <Row
            key={r.id}
            id={id}
            row={r}
            busy={false}
            onState={setState}
            canRemove={state.can_change}
            onRemoveFromBuild={() => change({ remove: r.id })}
          />
        ))}
      </ul>
      {state.can_change && state.addable.length > 0 && (
        <div className="cx-panel-add">
          <label className="sr-only" htmlFor="cx-panel-add">
            Add a connector to this build
          </label>
          <select id="cx-panel-add" className="select" value={adding} onChange={(e) => setAdding(e.target.value)}>
            <option value="">Add a connector to this build…</option>
            {state.addable.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </select>
          <button type="button" className="btn btn-sm" disabled={!adding} onClick={() => change({ add: adding })}>
            {Icon.plus} Add
          </button>
        </div>
      )}
      {changeError && (
        <p className="db-msg db-msg-bad" role="alert" style={{ marginTop: 10 }}>
          {Icon.alert}
          <span>{changeError}</span>
        </p>
      )}
    </section>
  );
}
