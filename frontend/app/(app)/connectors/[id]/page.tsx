"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, type Connector } from "@/lib/api";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { ConnectForm, ConnectorMark, LockLine, StatusBadge, VarChips } from "@/components/connectors/parts";

/**
 * One connector: what the crew builds with it, the names its code reads, where the
 * key lives, and — once connected — which builds use it. Connecting here is for
 * the account, so every build that needs it from now on is linked to it.
 */
export default function ConnectorPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [c, setC] = useState<Connector | null>(null);
  const [error, setError] = useState("");
  useChrome({ sub: c ? `Connectors · ${c.label}` : "Connectors" }, [c?.label]);

  const load = useCallback(() => {
    api
      .getConnector(id)
      .then((d) => {
        setC(d);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, [id]);
  useEffect(load, [load]);

  const used = c?.used_by ?? [];

  return (
    <div className="cx-wrap">
      <Link href="/connectors" className="cx-back">
        {Icon.chevron} All connectors
      </Link>

      {error ? (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 16 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t load this connector</span>
            <span className="notice-text">{error}</span>
            <div className="notice-actions">
              <button className="btn btn-sm" onClick={load}>
                {Icon.refresh} Try again
              </button>
            </div>
          </div>
        </div>
      ) : !c ? (
        <div className="card" style={{ marginTop: 16 }}>
          <SkeletonLines lines={6} />
        </div>
      ) : (
        <>
          <header className="cx-detail-head">
            <ConnectorMark id={c.id} label={c.label} size="lg" />
            <div>
              <h1>
                {c.label}
                <StatusBadge c={c} />
              </h1>
              <p>
                {c.blurb}. {c.category_label}
                {c.has_test_mode ? " · has a test mode" : ""}.
              </p>
            </div>
          </header>

          <div className="cx-detail-grid">
            <section className="card card-flush" aria-labelledby="cx-connect-title">
              <div className="card-head" style={{ padding: "14px 18px" }}>
                <h2 className="label" id="cx-connect-title">
                  {c.connected ? "Your connection" : `Connect ${c.label}`}
                </h2>
              </div>
              {c.connectable ? (
                <>
                  <ConnectForm
                    connector={c}
                    onSave={async (values, confirmLive) => {
                      const r = await api.connect(c.id, values, confirmLive);
                      if (r.connector) setC(r.connector);
                      return r;
                    }}
                    onRetest={async () => {
                      const r = await api.retestConnector(c.id);
                      if (r.connector) setC(r.connector);
                      return r;
                    }}
                    onRemove={async () => {
                      const r = await api.disconnect(c.id);
                      setC(r.connector);
                    }}
                    removeConfirm={
                      used.length
                        ? `${used.length} ${used.length === 1 ? "build uses" : "builds use"} this. ${
                            used.length === 1 ? "It" : "They"
                          }'ll show ${c.label} as not connected until you connect it again.`
                        : "No build uses it yet. You can connect it again any time."
                    }
                  />
                  <LockLine names={c.variables.filter((v) => v.secret).map((v) => v.name)} />
                </>
              ) : (
                <div className="db-pad">
                  <p className="muted" style={{ margin: 0, lineHeight: 1.6 }}>
                    {c.label} is {c.wave === 2 ? "next in line" : "on the roadmap"}. It can&apos;t be connected yet — a
                    build that needs it today writes code against the names below and leaves the keys to you.
                  </p>
                </div>
              )}
            </section>

            <div className="cx-side-col">
              {c.builds.length > 0 && (
                <section className="card" aria-labelledby="cx-builds-title">
                  <div className="sec-head">
                    <h2 className="label" id="cx-builds-title">What the crew builds with it</h2>
                  </div>
                  <ul className="cx-builds">
                    {c.builds.map((b) => (
                      <li key={b}>
                        {Icon.check}
                        <span>{b}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              <section className="card" aria-labelledby="cx-vars-title">
                <div className="sec-head">
                  <h2 className="label" id="cx-vars-title">Variables</h2>
                </div>
                <VarChips variables={c.variables} />
                <p className="field-hint" style={{ marginTop: 10 }}>
                  Public ones go in the browser bundle; server ones never leave the server.
                </p>
              </section>
              {c.connectable && (
                <section className="card" aria-labelledby="cx-used-title">
                  <div className="sec-head">
                    <h2 className="label" id="cx-used-title">Used by</h2>
                  </div>
                  {used.length ? (
                    <ul className="cx-usedby">
                      {used.map((u) => (
                        <li key={u.id}>
                          <Link href={`/projects/${u.id}`}>
                            <span className="cx-usedby-name">{u.name}</span>
                            {Icon.chevron}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="dim" style={{ margin: 0, fontSize: "var(--t-sm)", lineHeight: 1.55 }}>
                      No build yet. One whose idea needs {c.capability_label} picks it up automatically.
                    </p>
                  )}
                </section>
              )}
              {c.docs_url && (
                <a className="btn btn-sm" href={c.docs_url} target="_blank" rel="noopener noreferrer" style={{ alignSelf: "flex-start" }}>
                  {c.label} docs {Icon.external}
                </a>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
