"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Connector, type ConnectorCatalog } from "@/lib/api";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { ConnectorMark, StatusBadge } from "@/components/connectors/parts";

/**
 * The Connectors tab: the services a generated app uses at run time — Stripe for
 * payments, Resend for email — connected once for the account, and used by every
 * build that needs them.
 *
 * An Operate surface. Someone arrives here holding one question — "can the crew
 * build my checkout against my Stripe?" — so the page is a catalog to find one
 * thing in fast: search, a category row, and a "Connected" filter, over a grid
 * grouped by category. Not to be confused with "the connector" (Your computers),
 * the program that lends this app a computer's models; the copy keeps them apart.
 */

export default function ConnectorsPage() {
  useChrome({ sub: "Connectors" }, []);
  const [data, setData] = useState<ConnectorCatalog | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");
  const [onlyConnected, setOnlyConnected] = useState(false);

  const load = useCallback(() => {
    api
      .listConnectors()
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  const q = query.trim().toLowerCase();
  const matches = useCallback(
    (c: Connector) =>
      !q ||
      [c.label, c.blurb, c.category_label, c.capability_label, ...c.variables.map((v) => v.name)]
        .join(" ")
        .toLowerCase()
        .includes(q),
    [q],
  );

  const visible = useMemo(() => {
    const all = data?.connectors ?? [];
    return all.filter(
      (c) => matches(c) && (category === "all" || c.category === category) && (!onlyConnected || c.connected),
    );
  }, [data, matches, category, onlyConnected]);

  // Counts follow the search and the Connected filter, so a chip never promises
  // results the grid doesn't have.
  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const c of data?.connectors ?? []) {
      if (!matches(c) || (onlyConnected && !c.connected)) continue;
      out[c.category] = (out[c.category] ?? 0) + 1;
      out.all = (out.all ?? 0) + 1;
    }
    return out;
  }, [data, matches, onlyConnected]);

  const groups = useMemo(() => {
    const order = data?.categories ?? [];
    return order
      .map((cat) => ({
        ...cat,
        items: visible
          .filter((c) => c.category === cat.id)
          // Connected first, then what can be connected, then what's coming.
          .sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label)),
      }))
      .filter((g) => g.items.length);
  }, [data, visible]);

  const connected = data?.connected ?? 0;

  return (
    <div className="cx-wrap">
      <header className="cx-head">
        <h1>Connectors</h1>
        <p className="prose-lede">
          Connect a service once. Every build that needs it uses it automatically — the crew only ever
          sees the variable names, never your keys.
        </p>
      </header>

      {data && connected === 0 && (
        <p className="cx-hint">
          {Icon.info}
          <span>
            Connect Stripe and your next store build takes real test payments. Nothing here is required:
            a build that needs a service you haven&apos;t connected asks for it, or carries on without.
          </span>
        </p>
      )}

      <div className="cx-toolbar">
        <label className="cx-search">
          <span className="sr-only">Search connectors</span>
          {Icon.search}
          <input
            className="input"
            type="search"
            placeholder="Search Stripe, email, OPENAI_API_KEY…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button
          type="button"
          role="switch"
          aria-checked={onlyConnected}
          className="switch cx-switch"
          onClick={() => setOnlyConnected((v) => !v)}
        >
          <span className="switch-track" aria-hidden="true" />
          Connected{data ? ` (${connected})` : ""}
        </button>
      </div>

      <div className="cx-chips" role="group" aria-label="Category">
        <Chip active={category === "all"} onClick={() => setCategory("all")} label="All" count={counts.all ?? 0} />
        {(data?.categories ?? []).map((c) => (
          <Chip
            key={c.id}
            active={category === c.id}
            onClick={() => setCategory(c.id)}
            label={c.label}
            count={counts[c.id] ?? 0}
          />
        ))}
      </div>

      {error && (
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t load the connectors</span>
            <span className="notice-text">{error}</span>
            <div className="notice-actions">
              <button className="btn btn-sm" onClick={load}>
                {Icon.refresh} Try again
              </button>
            </div>
          </div>
        </div>
      )}

      {!data && !error ? (
        <div className="card">
          <SkeletonLines lines={6} />
        </div>
      ) : data ? (
        <div className="cx-groups" aria-live="polite">
          {groups.length === 0 ? (
            <div className="cx-empty">
              <p>
                {q ? (
                  <>
                    Nothing matches <b>{query.trim()}</b>
                    {onlyConnected ? " among your connected services" : ""}.
                  </>
                ) : (
                  "Nothing connected yet in this category."
                )}
              </p>
            </div>
          ) : (
            groups.map((g) => (
              <section key={g.id} className="cx-group" aria-labelledby={`cx-g-${g.id}`}>
                <div className="sec-head">
                  <h2 className="label" id={`cx-g-${g.id}`}>
                    {g.label}
                  </h2>
                  <span className="rule" />
                </div>
                <ul className="cx-grid">
                  {g.items.map((c) => (
                    <li key={c.id}>
                      <ConnectorCard c={c} />
                    </li>
                  ))}
                </ul>
              </section>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

function rank(c: Connector) {
  return c.connected ? 0 : c.connectable ? 1 : 2;
}

function Chip({ active, onClick, label, count }: { active: boolean; onClick: () => void; label: string; count: number }) {
  return (
    <button type="button" className="cx-chip" aria-pressed={active} onClick={onClick}>
      {label}
      <span className="cx-chip-count">{count}</span>
    </button>
  );
}

function ConnectorCard({ c }: { c: Connector }) {
  const body = (
    <>
      <span className="cx-card-top">
        <ConnectorMark id={c.id} label={c.label} />
        <StatusBadge c={c} />
      </span>
      <span className="cx-card-name">{c.label}</span>
      <span className="cx-card-blurb">{c.blurb}</span>
    </>
  );
  if (!c.connectable) {
    return (
      <div className="cx-card cx-card-soon" aria-label={`${c.label}, ${c.wave === 2 ? "coming next" : "coming soon"}`}>
        {body}
      </div>
    );
  }
  return (
    <Link href={`/connectors/${c.id}`} className={"cx-card" + (c.connected ? " cx-card-on" : "")}>
      {body}
      <span className="cx-card-go" aria-hidden="true">
        {c.connected ? "Manage" : `Connect ${c.label}`} {Icon.chevron}
      </span>
    </Link>
  );
}
