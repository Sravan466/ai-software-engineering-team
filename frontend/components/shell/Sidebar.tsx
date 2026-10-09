"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api, type Account, type LocalStatus, type Project } from "@/lib/api";
import { canRunABuild } from "@/lib/capabilities";
import { databaseUnconnected } from "@/lib/database";
import { connectorsLabel, connectorsUnconnected } from "@/lib/connectors";
import { modelFor, modelName, sourceFor, triedText } from "@/lib/models";
import { Icon } from "./icons";
import { Skeleton } from "@/components/ui/Skeleton";
import { STATUS_DOT, STATUS_TEXT, crewHref, floorBuild, publishProjects, statusOf } from "@/lib/buildStatus";
import { timeAgo } from "@/lib/time";

const API_DOCS_URL =
  (process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000") + "/docs";

/**
 * The persistent left rail — and, under 900px, an overlay drawer.
 *
 * When collapsed, shell.css flips it to `visibility: hidden`, which also takes
 * it out of the tab order — otherwise keyboard focus disappears into an
 * off-screen drawer.
 */
export default function Sidebar({ onClose, account }: { onClose: () => void; account: Account }) {
  const pathname = usePathname();
  const router = useRouter();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [local, setLocal] = useState<LocalStatus | null>(null);
  const [localError, setLocalError] = useState(false);
  // Which row is asking "are you sure?". `DELETE /api/projects/{id}` has always
  // existed and nothing called it, so a build you no longer want was permanent.
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  // How many app connectors the account has connected — refreshed on every page
  // change, so connecting one in the tab shows here as soon as you move on.
  const [connectorCount, setConnectorCount] = useState<number | null>(null);
  useEffect(() => {
    let live = true;
    api
      .listConnectors()
      .then((c) => live && setConnectorCount(c.connected))
      .catch(() => live && setConnectorCount(null));
    return () => {
      live = false;
    };
  }, [pathname]);

  const signOut = useCallback(async () => {
    setSigningOut(true);
    try {
      await api.signOut();
    } catch {
      // Even if the backend didn't hear it, this browser is done with the session.
    }
    router.replace("/signin");
  }, [router]);

  const refresh = useCallback(async () => {
    try {
      const list = await api.listProjects();
      setProjects(list);
      // The home page's crew links read this list rather than fetching their own.
      publishProjects(list);
    } catch {
      // Keep the last good list if the backend blips.
      setProjects((p) => p ?? []);
    }
    try {
      setLocal(await api.getLocalModel());
      setLocalError(false);
    } catch {
      setLocalError(true);
    }
  }, []);

  // Cheap, and keeps the list fresh after a build is created or advances.
  useEffect(() => {
    refresh();
  }, [refresh, pathname]);

  const remove = useCallback(
    async (id: string) => {
      setDeletingId(id);
      try {
        await api.deleteProject(id);
        setProjects((list) => (list ?? []).filter((p) => p.id !== id));
        if (pathname === `/projects/${id}`) router.push("/");
      } catch {
        // Leave the row in place — the confirm closes and the list refreshes below.
      } finally {
        setDeletingId(null);
        setConfirmId(null);
        refresh();
      }
    },
    [pathname, refresh, router],
  );

  // "Ready" means a build can start on it, which is what the dot is read as. A
  // default that is pulled but cannot write used to show green here while the
  // composer refused to start on it — two answers about one model.
  const writes = canRunABuild(local?.default_model ?? "", local?.cannot_build);
  // A default its runtime sends to a hosted service is not a local model, and Local
  // builds refuse it — so it is not "ready" here either.
  const hosted = modelFor(local, local?.default_model)?.is_local === false;
  const ready = !localError && local?.reachable && local?.has_default && writes && !hosted;
  const home = sourceFor(local, local?.default_model);
  const name = modelName(local?.default_model);
  const runtimeLabel = localError
    ? "Backend unreachable"
    : !local?.reachable
      ? "No local runtime reachable"
      : !local.default_model
        ? "No local model can write"
        : home && !home.reachable
          ? `${home.label} not answering`
          : !local.has_default
            ? `${name} not available`
            : !writes
              ? `${name} can't write`
              : hosted
                ? `${name} runs hosted`
                : `${name} ready`;
  // The whole answer on hover: which source, and — when nothing answered — where
  // this looked, so "unreachable" is never a claim without its evidence.
  const runtimeTitle = !local
    ? undefined
    : !local.reachable
      ? `Tried ${triedText(local.tried)}`
      : home
        ? `${local.default_model} on ${home.label} at ${home.base_url}`
        : local.default_model ?? undefined;

  return (
    <aside className="sidebar">
      <nav aria-label="Builds">
        <div className="sb-top">
          <Link className="sb-brand" href="/">
            <span className="logo-mark" aria-hidden="true">
              AI
            </span>
            <span className="wordmark">
              SWE&nbsp;<span>Team</span>
            </span>
          </Link>
          <button className="icon-btn" onClick={onClose} aria-label="Close navigation">
            {Icon.menu}
          </button>
        </div>

        <button className="sb-new" onClick={() => router.push("/")}>
          {Icon.plus} New build
        </button>
      </nav>

      <div className="sb-scroll">
        <h2 className="label sb-label">Recent builds</h2>

        {projects === null ? (
          <div style={{ padding: "4px 20px", display: "flex", flexDirection: "column", gap: 12 }}>
            <Skeleton w="80%" />
            <Skeleton w="64%" />
            <Skeleton w="72%" />
          </div>
        ) : projects.length === 0 ? (
          <p className="sb-empty">
            No builds yet. Describe an idea on the home screen to start one.
          </p>
        ) : (
          projects.map((p) => {
            const active = pathname === `/projects/${p.id}`;
            const title = p.name || p.idea;
            const state = statusOf(p);

            if (confirmId === p.id) {
              return (
                <div
                  key={p.id}
                  className="sb-confirm"
                  role="group"
                  aria-label={`Delete ${title}?`}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setConfirmId(null);
                  }}
                >
                  <span className="sb-confirm-text">Delete this build?</span>
                  <button
                    className="btn btn-sm"
                    disabled={deletingId === p.id}
                    onClick={() => setConfirmId(null)}
                    autoFocus
                  >
                    Keep
                  </button>
                  <button
                    className="btn btn-sm btn-danger"
                    disabled={deletingId === p.id}
                    onClick={() => remove(p.id)}
                  >
                    {deletingId === p.id && <span className="btn-spinner" aria-hidden="true" />}
                    Delete
                  </button>
                </div>
              );
            }

            return (
              <div key={p.id} className="sb-row">
                <Link
                  href={`/projects/${p.id}`}
                  className="sb-item"
                  aria-current={active ? "page" : undefined}
                >
                  <span
                    className={"dot " + (STATUS_DOT[state] || "")}
                    title={STATUS_TEXT[state] || state}
                  />
                  <span className="sb-item-title">{title}</span>
                  {databaseUnconnected(p) && (
                      <span className="sb-db" role="img" aria-label="Database not connected" title="Database not connected">
                        {Icon.database}
                      </span>
                    )}
                  {connectorsUnconnected(p).length > 0 && (
                    <span
                      className="sb-db"
                      role="img"
                      aria-label={connectorsLabel(connectorsUnconnected(p).length)}
                      title={connectorsLabel(connectorsUnconnected(p).length)}
                    >
                      {Icon.plug}
                    </span>
                  )}
                  <span className="sb-item-time">{timeAgo(p.updated_at || p.created_at)}</span>
                </Link>
                <button
                  className="sb-del"
                  aria-label={`Delete build: ${title}`}
                  title="Delete this build"
                  onClick={() => setConfirmId(p.id)}
                >
                  {Icon.trash}
                </button>
              </div>
            );
          })
        )}
      </div>

      <div className="sb-foot">
        {/* Nothing can be built while no local runtime answers, so this is a
            link to the fix rather than a caption about the problem. */}
        <Link
          className={"sb-runtime" + (ready ? "" : " down")}
          href={ready ? "/settings" : "/setup"}
          title={runtimeTitle}
        >
          <span className={"dot " + (ready ? "dot-ok" : "dot-warn")} />
          <span className="sb-runtime-text">{runtimeLabel}</span>
          {!ready && <span className="sb-runtime-fix">Fix</span>}
        </Link>

        {/* Opens on the build in hand, or the latest one (#91). */}
        <Link
          className="sb-link"
          href={crewHref(floorBuild(projects))}
          aria-current={pathname === "/crew" ? "page" : undefined}
        >
          {Icon.sparkle} The crew
        </Link>
        <Link
          className="sb-link"
          href="/setup"
          aria-current={pathname === "/setup" ? "page" : undefined}
        >
          {Icon.laptop} Your computers
        </Link>
        <Link
          className="sb-link"
          href="/skills"
          aria-current={pathname === "/skills" ? "page" : undefined}
        >
          {Icon.book} Skills
        </Link>
        <Link
          className="sb-link"
          href="/connectors"
          aria-current={pathname?.startsWith("/connectors") ? "page" : undefined}
        >
          {Icon.plug} Connectors
          {connectorCount ? (
            <span className="sb-link-tag" aria-label={`${connectorCount} connected`}>
              {connectorCount}
            </span>
          ) : null}
        </Link>
        <Link
          className="sb-link"
          href="/settings"
          aria-current={pathname === "/settings" ? "page" : undefined}
        >
          {Icon.gear} Settings
        </Link>
        <a className="sb-link" href={API_DOCS_URL} target="_blank" rel="noreferrer">
          {Icon.api} API reference
          <span style={{ marginLeft: "auto", color: "var(--ink-4)" }}>{Icon.external}</span>
        </a>

        {/* Who these builds belong to — on a shared install, the question that
            matters before anything is started or deleted. */}
        <div className="sb-account">
          <span className="sb-avatar" aria-hidden="true">
            {(account.display_name || account.email || "?").trim().charAt(0).toUpperCase()}
          </span>
          <span className="sb-account-text">
            <span className="sb-account-name">
              {account.display_name || account.email}
              {account.is_owner && <span className="sb-link-tag">owner</span>}
            </span>
            {account.display_name && account.email && (
              <span className="sb-account-email">{account.email}</span>
            )}
          </span>
          <button
            className="icon-btn sb-signout"
            onClick={signOut}
            disabled={signingOut}
            aria-label="Sign out"
            title="Sign out"
          >
            {signingOut ? <span className="btn-spinner" aria-hidden="true" /> : Icon.signOut}
          </button>
        </div>
      </div>
    </aside>
  );
}
