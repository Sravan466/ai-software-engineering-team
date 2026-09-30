"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type GithubStatus, type VercelConnection } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { VercelConnect } from "@/components/deploy/ShipCard";

/** Settings → Deploy: the account's Vercel token and GitHub connection. */
export default function DeploySettings() {
  const [state, setState] = useState<{ github: GithubStatus; vercel: VercelConnection } | null>(null);
  const [error, setError] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [github, vercel] = await Promise.all([api.githubStatus(), api.deployConnections()]);
      setState({ github, vercel: vercel.vercel });
      setError("");
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-labelledby="deploy-settings-title">
      <div className="sec-head">
        <h2 className="label" id="deploy-settings-title">
          Deploy
        </h2>
        <span className="rule" />
      </div>
      <p className="ship-copy" style={{ marginBottom: 14 }}>
        Where finished builds go: your own Vercel for a frontend, your own GitHub (and from there Render) for
        anything with a backend. Tokens are encrypted on this backend and never sent to the browser.
      </p>
      {!state && !error && <SkeletonLines lines={2} />}
      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginBottom: 12 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t load the deploy connections</span>
            <span className="notice-text">{error}</span>
          </div>
        </div>
      )}
      {state && (
        <div className="ship-stack">
          {state.vercel.connected && !replacing ? (
            <div className="ship-conn">
              <span className="ship-conn-text">
                Vercel · connected as <b>{state.vercel.username}</b> ·{" "}
                <span className="mono">{state.vercel.hint}</span>
              </span>
              <span className="ship-conn-actions">
                <button type="button" className="btn btn-sm btn-ghost" onClick={() => setReplacing(true)} disabled={busy}>
                  Replace
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={() => run(() => api.removeVercelToken())}
                  disabled={busy}
                >
                  Remove
                </button>
              </span>
            </div>
          ) : (
            <div className="ship-panel">
              <VercelConnect
                onSaved={async () => {
                  setReplacing(false);
                  await load();
                }}
              />
            </div>
          )}
          <div className="ship-conn">
            <span className="ship-conn-mark">{Icon.github}</span>
            <span className="ship-conn-text">
              {!state.github.configured
                ? "GitHub isn't enabled on this server (GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET)."
                : state.github.connected
                  ? (
                      <>
                        GitHub · <b>@{state.github.login}</b>
                      </>
                    )
                  : state.github.reason === "revoked"
                    ? "GitHub access was removed — reconnect from a build's Deliver tab."
                    : "GitHub isn't connected. Connect it from a build's Deliver tab."}
            </span>
            {state.github.connected && (
              <span className="ship-conn-actions">
                <button type="button" className="btn btn-sm btn-ghost" onClick={() => run(() => api.githubDisconnect())} disabled={busy}>
                  Disconnect
                </button>
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
