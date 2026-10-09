"use client";

import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import type { CrewRecord, PhaseResult, Project } from "@/lib/api";
import type { Persona } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import { HANDOFF_STATE, NODE_STATUS } from "@/components/agents/phaseState";
import { Icon } from "@/components/shell/icons";
import { clock, doneLine, took, type Station } from "./floor";

const MOTION_NOTE: Record<Persona["motion"], string> = {
  nod: "Considers, then commits",
  drift: "Thinks in space",
  thrum: "Steady machine rhythm",
  flicker: "Restless, never settles",
  scan: "Sweeps for defects",
  guard: "Braced and watchful",
  launch: "Coils, then goes",
  tally: "Counts, flips, counts",
};

/** 12400 → "12.4k". */
function compact(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function money(usd: number): string {
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

/** What it cost, never "$0.00" for "nobody priced this". */
function costText(usd: number, unpriced: number, calls: number): { text: string; title?: string } {
  if (!calls) return { text: "—" };
  if (unpriced) {
    return {
      text: usd ? `${money(usd)}+` : "Unpriced",
      title: `${unpriced} ${unpriced === 1 ? "call" : "calls"} on a model with no published price, not counted`,
    };
  }
  return { text: usd ? money(usd) : "Free" };
}

/** What a phase's row says about its work, in the build page's words. */
function summary(agent: Persona, station: Station, row: PhaseResult | undefined): string {
  switch (station.ns) {
    case "running":
      return station.bubble ? [station.bubble.head, station.bubble.path].filter(Boolean).join(" ") : agent.steps.doing;
    case "gate":
      return `${doneLine(agent, row)}. Waiting for your review.`;
    case "done":
      return doneLine(agent, row);
    case "redo":
      return "Sent back. Starts again from your note.";
    case "failed":
      return "Stopped partway through.";
    default:
      return "Hasn't started.";
  }
}

function Chip({ tone, children }: { tone: "ok" | "warn" | "bad" | ""; children: ReactNode }) {
  return <span className={"insp-chip" + (tone ? " " + tone : "")}>{children}</span>;
}

function Checks({ row }: { row: PhaseResult }) {
  const chips: ReactNode[] = [];
  if (row.schema_status === "valid") chips.push(<Chip key="s" tone="ok">Shape ok</Chip>);
  else if (row.schema_status === "repaired") chips.push(<Chip key="s" tone="warn">Shape repaired</Chip>);
  else if (row.schema_status === "invalid") chips.push(<Chip key="s" tone="bad">Shape off</Chip>);
  if (row.stack_status === "ok") chips.push(<Chip key="k" tone="ok">Stack ok</Chip>);
  else if (row.stack_status === "violated") chips.push(<Chip key="k" tone="bad">Off stack</Chip>);
  if (row.build_status === "ok") chips.push(<Chip key="b" tone="ok">Compiles</Chip>);
  else if (row.build_status === "failed") chips.push(<Chip key="b" tone="bad">Doesn&apos;t compile</Chip>);
  else if (row.build_status === "unchecked") chips.push(<Chip key="b" tone="">Not compiled</Chip>);
  return chips.length ? <span className="insp-chips">{chips}</span> : <span className="insp-none">—</span>;
}

/**
 * How much of the work before it this agent was shown (#80): one cell per earlier
 * phase, full when all of it fit, half when it was cut to fit, a sliver when only
 * the summary did.
 */
function Context({ row, given }: { row: PhaseResult | undefined; given: Project["given"] }) {
  const deps = row?.handoff?.deps ?? (given && row?.status === "running" && given.phase === row.phase ? given.deps : null);
  if (!deps) return <span className="insp-none">—</span>;
  if (!deps.length) return <span className="insp-none">Starts from your idea</span>;
  const whole = deps.filter((d) => d.full === "whole").length;
  const cutReplies = row?.handoff?.truncated_replies ?? 0;
  return (
    <span className="insp-gauge">
      <span className="insp-gauge-bar" aria-hidden="true">
        {deps.map((d) => (
          <i
            key={d.phase}
            className={"g-" + d.full}
            title={`${AGENT_BY_KEY[d.phase]?.codename ?? d.phase}: ${(HANDOFF_STATE[d.full] ?? HANDOFF_STATE.whole).label}`}
            style={{ ["--agent" as string]: AGENT_BY_KEY[d.phase]?.accent } as CSSProperties}
          />
        ))}
      </span>
      <span className="insp-gauge-text">
        {whole === deps.length ? "All of it fit" : `${whole} of ${deps.length} in full`}
        {cutReplies > 0 && `, ${cutReplies} ${cutReplies === 1 ? "reply" : "replies"} cut off`}
      </span>
    </span>
  );
}

function ThisBuild({ project, station }: { project: Project; station: Station }) {
  const { agent } = station;
  // The attempt on screen: in a replay, the one at that moment, not the last one.
  const row = station.row;
  const href = `/projects/${project.id}?phase=${agent.key}`;
  const t = took(row, project);
  return (
    <section className="insp-sec" aria-label={`${agent.codename} on this build`}>
      <h3 className="insp-h">This build</h3>
      <p className="insp-sum">{summary(agent, station, row)}</p>
      <dl className="rows">
        <div className="row">
          <dt>Status</dt>
          <dd>{station.ns ? NODE_STATUS[station.ns] : "—"}</dd>
        </div>
        {row?.model_used && (
          <div className="row">
            <dt>Model</dt>
            <dd className="mono insp-model">
              <span title={row.fallback_note ?? undefined}>
                {row.provider_used ? `${row.provider_used}/` : ""}
                {row.model_used}
              </span>
              {row.is_local !== null && (
                <span className={"insp-where" + (row.is_local ? " local" : "")}>{row.is_local ? "Local" : "Cloud"}</span>
              )}
            </dd>
          </div>
        )}
        {row && station.ns !== "running" && (
          <div className="row">
            <dt>Spent</dt>
            <dd className="mono">
              {row.total_tokens ? `${row.total_tokens.toLocaleString()} tok` : "—"}
              {t !== null ? ` · ${clock(t)}` : row.latency_ms ? ` · ${(row.latency_ms / 1000).toFixed(1)}s` : ""}
            </dd>
          </div>
        )}
        {row && station.ns !== "running" && station.ns !== "pending" && (
          <div className="row">
            <dt>Checks</dt>
            <dd>
              <Checks row={row} />
            </dd>
          </div>
        )}
        {row?.skills_used && row.skills_used.length > 0 && (
          <div className="row">
            <dt>Skills</dt>
            <dd className="insp-skills">{row.skills_used.join(", ")}</dd>
          </div>
        )}
        {row && (
          <div className="row">
            <dt>Given</dt>
            <dd>
              <Context row={row} given={project.given ?? null} />
            </dd>
          </div>
        )}
      </dl>
      {row?.fallback_note && <p className="insp-fallback">{row.fallback_note}</p>}
      {station.ns === "gate" ? (
        <Link className="btn btn-sm btn-primary insp-go" href={href}>
          Review on the build page {Icon.arrowRight}
        </Link>
      ) : row ? (
        <Link className="link insp-link" href={href}>
          See the work {Icon.arrowRight}
        </Link>
      ) : null}
    </section>
  );
}

function Career({ record }: { record: CrewRecord | null | undefined }) {
  const r = record;
  const none = !r || (r.builds === 0 && r.calls === 0);
  const cost = r ? costText(r.cost_usd, r.unpriced_calls, r.calls) : { text: "—" };
  const figures: { v: string; l: string; title?: string }[] = [
    { v: none ? "—" : String(r!.builds), l: r?.builds === 1 ? "Build" : "Builds" },
    { v: none || !r!.tokens ? "—" : compact(r!.tokens), l: "Tokens" },
    { v: none ? "—" : cost.text, l: "Cost", title: cost.title },
    { v: none || r!.local_share === null ? "—" : `${Math.round(r!.local_share * 100)}%`, l: "Local" },
    { v: none ? "—" : String(r!.rejected), l: "Sent back" },
    { v: none ? "—" : String(r!.build_failed), l: "Didn't compile" },
  ];
  return (
    <section className="insp-sec" aria-label="Across all your builds">
      <h3 className="insp-h">All your builds</h3>
      <dl className="insp-figs">
        {figures.map((f) => (
          <div key={f.l} className="insp-fig" title={f.title}>
            <dt>{f.l}</dt>
            <dd className={f.v === "—" ? "is-empty" : ""}>{f.v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export default function Inspector({
  station,
  project,
  record,
  asleep,
  poke,
  showRecord,
}: {
  station: Station;
  project: Project | null;
  record: CrewRecord | null | undefined;
  asleep: boolean;
  poke: number | undefined;
  /** Off on the tour, which has no build and no record to show. */
  showRecord: boolean;
}) {
  const { agent } = station;
  return (
    <aside className="inspect" style={{ ["--agent" as string]: agent.accent } as CSSProperties}>
      <div className="inspect-head">
        <span className="win-title">STATION {agent.n}</span>
        <span className={"plate-state s-" + station.tone}>{station.plate}</span>
      </div>

      <div className="inspect-portrait">
        <AgentSprite agent={agent} size={104} state={station.state} asleep={asleep} poke={poke} />
      </div>

      <h2 className="inspect-name">{agent.codename}</h2>
      <p className="inspect-role">{agent.role}</p>

      {showRecord && project && <ThisBuild project={project} station={station} />}
      {showRecord && <Career record={record} />}

      <dl className="rows insp-persona">
        <div className="row">
          <dt>Owns</dt>
          <dd>{agent.discipline}</dd>
        </div>
        <div className="row">
          <dt>Ships</dt>
          <dd className="mono">{agent.deliver}</dd>
        </div>
        <div className="row">
          <dt>Trait</dt>
          <dd style={{ color: "var(--agent)" }}>{agent.trait}</dd>
        </div>
        <div className="row">
          <dt>Spot by</dt>
          <dd>{agent.silhouette}</dd>
        </div>
        <div className="row">
          <dt>Moves</dt>
          <dd>{MOTION_NOTE[agent.motion]}</dd>
        </div>
      </dl>

      <p className="inspect-tagline">{agent.tagline}</p>
    </aside>
  );
}
