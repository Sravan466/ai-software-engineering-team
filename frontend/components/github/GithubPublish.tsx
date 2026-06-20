"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type GithubPushResult, type GithubStatus } from "@/lib/api";

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
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<"connected" | "error" | "">("");

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.githubStatus());
    } catch (e: any) {
      setError(e.message);
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
    setError("");
    try {
      await api.githubDisconnect();
      setResult(null);
      setNotice("");
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function push() {
    setPushing(true);
    setError("");
    setResult(null);
    try {
      const res = await api.pushToGithub(id, { name: name.trim() || undefined, private: priv });
      setResult(res);
    } catch (e: any) {
      // Strip the leading "400: " status that the API client prepends.
      setError(String(e.message || e).replace(/^\d+:\s*/, ""));
    } finally {
      setPushing(false);
    }
  }

  return (
    <div className="fd-card">
      <div className="sec-head">
        <span className="fd-kicker">publish to github</span>
        <span className="fd-rule" />
        {status?.connected && status.login && (
          <span className="fd-badge fd-badge-ok">@{status.login}</span>
        )}
      </div>

      {/* Not configured by the operator yet. */}
      {status && !status.configured && (
        <p className="fd-muted" style={{ fontSize: 14, lineHeight: 1.55, margin: 0 }}>
          GitHub publishing isn’t enabled on this server yet. The operator needs to add a free{" "}
          <a
            className="fd-link"
            href="https://github.com/settings/developers"
            target="_blank"
            rel="noreferrer"
          >
            GitHub OAuth App
          </a>{" "}
          and set <span className="fd-mono">GITHUB_CLIENT_ID</span> /{" "}
          <span className="fd-mono">GITHUB_CLIENT_SECRET</span> in the backend{" "}
          <span className="fd-mono">.env</span>.
        </p>
      )}

      {/* Configured, not connected → Connect button. */}
      {status && status.configured && !status.connected && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p className="fd-muted" style={{ fontSize: 14, lineHeight: 1.55, margin: 0, maxWidth: "54ch" }}>
            Log in with your own GitHub account, and we’ll create a fresh repository on it and push
            this entire project — code, docs and README — in one commit.
          </p>
          <div>
            <button className="fd-btn fd-btn-primary" onClick={connect}>
              Connect GitHub →
            </button>
          </div>
          {notice === "error" && (
            <p style={{ color: "var(--accent)", fontSize: 13.5, margin: 0 }}>
              GitHub connection failed or was cancelled. Please try again.
            </p>
          )}
        </div>
      )}

      {/* Connected → push form. */}
      {status && status.connected && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {result ? (
            <div className="setup-step" style={{ alignItems: "flex-start" }}>
              <span className="n" style={{ color: "var(--a2)" }}>
                ✓
              </span>
              <span style={{ fontSize: 14, lineHeight: 1.6 }}>
                Pushed {result.files} files to{" "}
                <a className="fd-link" href={result.html_url} target="_blank" rel="noreferrer">
                  {result.full_name} →
                </a>{" "}
                <span className="fd-dim">
                  ({result.private ? "private" : "public"} · {result.branch})
                </span>
              </span>
            </div>
          ) : (
            <>
              <p className="fd-muted" style={{ fontSize: 14, lineHeight: 1.55, margin: 0 }}>
                Pushing to <b>@{status.login}</b>’s account. Pick a name for the new repository.
              </p>
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
                <input
                  className="fd-input"
                  style={{ minWidth: 220, flex: 1 }}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="repository-name"
                  disabled={pushing}
                />
                <label
                  className="fd-mono"
                  style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}
                >
                  <input
                    type="checkbox"
                    checked={priv}
                    onChange={(e) => setPriv(e.target.checked)}
                    disabled={pushing}
                  />
                  private
                </label>
                <button
                  className="fd-btn fd-btn-primary"
                  onClick={push}
                  disabled={pushing || disabled || !name.trim()}
                >
                  {pushing ? "Pushing…" : "Create repo & push →"}
                </button>
              </div>
              {disabled && (
                <p className="fd-dim" style={{ fontSize: 13, margin: 0 }}>
                  Nothing to push yet — finish the build first.
                </p>
              )}
            </>
          )}

          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <button className="fd-btn fd-btn-sm" onClick={disconnect} disabled={pushing}>
              Disconnect
            </button>
            {result && (
              <button
                className="fd-btn fd-btn-sm"
                onClick={() => setResult(null)}
                disabled={pushing}
              >
                Push another
              </button>
            )}
          </div>
        </div>
      )}

      {error && (
        <p style={{ color: "var(--accent)", fontSize: 13.5, marginTop: 12, marginBottom: 0 }}>
          {error}
        </p>
      )}
    </div>
  );
}
