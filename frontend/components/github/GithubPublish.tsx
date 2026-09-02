"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type GithubPushResult, type GithubStatus } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";

// Light client-side mirror of the backend slug() so the prefilled repo name
// matches what GitHub will actually get.
function slug(text: string): string {
  const s = (text || "project")
    .slice(0, 48)
    .replace(/[^a-zA-Z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
  return s || "project";
}

export default function GithubPublish({
  id,
  defaultName,
  disabled,
}: {
  id: string;
  defaultName: string;
  disabled?: boolean;
}) {
  const [status, setStatus] = useState<GithubStatus | null>(null);
  const [name, setName] = useState("");
  const [priv, setPriv] = useState(true);
  const [pushing, setPushing] = useState(false);
  const [result, setResult] = useState<GithubPushResult | null>(null);
  // The failed action travels with its message: "that push didn't go through" is
  // the wrong sentence for a failed disconnect, and both used to share one string.
  const [error, setError] = useState<{ action: "push" | "disconnect"; text: string } | null>(null);
  // Kept apart from `error` again: this one is "we can't reach the feature at all",
  // which needs different words and a different way out.
  const [unreachable, setUnreachable] = useState("");
  const [checking, setChecking] = useState(false);
  const [notice, setNotice] = useState<"connected" | "error" | "">("");

  // A server without the GitHub router 404s here. That is not an error to shout
  // about — it is precisely the not-configured state, which this card already
  // knows how to explain, so say that instead of rendering a bare "Not Found".
  const refresh = useCallback(async () => {
    setChecking(true);
    try {
      setStatus(await api.githubStatus());
      setUnreachable("");
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 404) {
        setStatus({ configured: false, connected: false, login: null, name: null, avatar: null });
        setUnreachable("");
      } else {
        setUnreachable(e?.message || "The backend didn't answer.");
      }
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    setName(slug(defaultName));
  }, [refresh, defaultName]);

  // Surface the outcome of the OAuth round-trip, then clean the URL so a
  // refresh doesn't re-show it.
  useEffect(() => {
    const sp = new URLSearchParams(window.location.search);
    const g = sp.get("github");
    if (g === "connected" || g === "error") {
      setNotice(g);
      sp.delete("github");
      const qs = sp.toString();
      window.history.replaceState(
        {},
        "",
        window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash,
      );
    }
  }, []);

  function connect() {
    // Full-page redirect into GitHub; we come back to this exact URL.
    window.location.href = api.githubConnectUrl(window.location.href);
  }

  async function disconnect() {
    setError(null);
    try {
      await api.githubDisconnect();
      setResult(null);
      setNotice("");
      await refresh();
    } catch (e: any) {
      setError({ action: "disconnect", text: e.message });
    }
  }

  async function push() {
    setPushing(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.pushToGithub(id, { name: name.trim() || undefined, private: priv });
      setResult(res);
    } catch (e: any) {
      setError({ action: "push", text: e.message });
    } finally {
      setPushing(false);
    }
  }

  return (
    <section className="card">
      <div className="sec-head">
        <h2 className="label">Publish to GitHub</h2>
        <span className="rule" />
        {status?.connected && !unreachable && status.login && (
          <span className="badge badge-ok">
            <span className="dot dot-ok" aria-hidden="true" />@{status.login}
          </span>
        )}
      </div>

      {/* The feature itself is out of reach — the backend is down, or older than
          the router. Every other error surface in this app names the problem and
          offers a way forward; this one used to print a bare "Not Found".
          While it stands it replaces the card's body rather than sitting on top of
          it: the last known status is stale, and an enabled "Create repo and push"
          beneath a "not reachable" alert is a button that cannot work. */}
      {unreachable && (
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">GitHub publishing isn&apos;t reachable</span>
            <span className="notice-text">
              The backend didn&apos;t answer this card&apos;s status check, so we can&apos;t tell
              whether publishing is set up. Check that it is running on{" "}
              <code>:8000</code> and try again — everything else on this page still works, and the
              .zip above is unaffected.
            </span>
            <span className="notice-detail mono">{unreachable}</span>
            <div className="notice-actions">
              <button className="btn btn-sm" onClick={refresh} disabled={checking}>
                {checking && <span className="btn-spinner" aria-hidden="true" />}
                {Icon.refresh} Try again
              </button>
            </div>
          </div>
        </div>
      )}

      {!status && !unreachable && <SkeletonLines lines={2} />}

      {/* Not configured by the operator yet. */}
      {status && !unreachable && !status.configured && (
        <p className="muted" style={{ margin: 0, fontSize: "var(--t-base)", lineHeight: 1.6 }}>
          GitHub publishing isn&apos;t enabled on this server yet. The operator needs to add a free{" "}
          <a
            className="link"
            href="https://github.com/settings/developers"
            target="_blank"
            rel="noreferrer"
          >
            GitHub OAuth App
          </a>{" "}
          and set <code>GITHUB_CLIENT_ID</code> and <code>GITHUB_CLIENT_SECRET</code> in the
          backend&apos;s <code>.env</code>.
        </p>
      )}

      {/* Configured, not connected → Connect. */}
      {status && !unreachable && status.configured && !status.connected && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <p className="muted" style={{ margin: 0, fontSize: "var(--t-base)", lineHeight: 1.6, maxWidth: "58ch" }}>
            Sign in with your own GitHub account and we&apos;ll create a fresh repository on it, then
            push this entire project — source, docs and README — in a single commit.
          </p>
          <div>
            <button className="btn btn-primary" onClick={connect}>
              {Icon.github} Connect GitHub
            </button>
          </div>
          {notice === "error" && (
            <div className="notice notice-bad" role="alert">
              {Icon.alert}
              <div className="notice-body">
                <span className="notice-text">
                  The GitHub connection failed or was cancelled. Try connecting again.
                </span>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Connected → push form. */}
      {status && !unreachable && status.connected && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {result ? (
            <div className="notice">
              <span className="dot dot-ok" style={{ marginTop: 7 }} aria-hidden="true" />
              <div className="notice-body">
                <span className="notice-title">Pushed {result.files} files</span>
                <span className="notice-text">
                  <a className="link" href={result.html_url} target="_blank" rel="noreferrer">
                    {result.full_name}
                  </a>{" "}
                  · {result.private ? "private" : "public"} · branch{" "}
                  <code>{result.branch}</code>
                </span>
              </div>
            </div>
          ) : (
            <>
              <p className="muted" style={{ margin: 0, fontSize: "var(--t-base)", lineHeight: 1.6 }}>
                Pushing to <b>@{status.login}</b>. Pick a name for the new repository.
              </p>
              <div style={{ display: "flex", alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
                <div className="field" style={{ flex: 1, minWidth: 220 }}>
                  <label htmlFor="repo-name">Repository name</label>
                  <input
                    id="repo-name"
                    className="input input-mono"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="repository-name"
                    disabled={pushing}
                  />
                </div>
                <button
                  type="button"
                  className="switch"
                  role="switch"
                  aria-checked={priv}
                  onClick={() => setPriv((v) => !v)}
                  disabled={pushing}
                  style={{ minHeight: 38 }}
                >
                  <span className="switch-track" aria-hidden="true" />
                  Private
                </button>
                <button
                  className="btn btn-primary"
                  onClick={push}
                  disabled={pushing || disabled || !name.trim()}
                >
                  {pushing && <span className="btn-spinner" aria-hidden="true" />}
                  {pushing ? "Pushing…" : "Create repo and push"}
                </button>
              </div>
              {disabled && (
                <p className="field-hint" style={{ margin: 0 }}>
                  Nothing to push yet — finish the build first.
                </p>
              )}
            </>
          )}

          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button className="btn btn-sm" onClick={disconnect} disabled={pushing}>
              Disconnect
            </button>
            {result && (
              <button className="btn btn-sm" onClick={() => setResult(null)} disabled={pushing}>
                Push another
              </button>
            )}
          </div>
        </div>
      )}

      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 14 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">
              {error.action === "push"
                ? "That push didn’t go through"
                : "Couldn’t disconnect from GitHub"}
            </span>
            <span className="notice-text">{error.text}</span>
            {error.action === "push" && (
              <span className="notice-text">
                A repository of that name may already exist on @{status?.login} — try another
                name, or disconnect and sign in as a different account.
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
