"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  type ApprovalMode,
  type LocalStatus,
  type RouterStatus,
  type Skill,
  type SkillOverrides,
  type SkillPreview,
} from "@/lib/api";
import {
  APPROVAL_MODES,
  ROUTING_MODES,
  type RoutingModeMeta,
} from "@/components/shell/phases";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";

/**
 * How a run is routed and how often it stops — and whether it can run at all.
 *
 * Three defects met here. Config was asked before intent, so this is a disclosure
 * rather than a gate. Manual routing sent `routing_mode: "manual"` with no model,
 * which the router silently resolved as Auto — so Manual now pins a real model or
 * it isn't Manual. And Start build was primary and enabled with nothing behind it
 * to run on, so the runtime is checked here instead of failing after the project
 * has already been created.
 */

export type RunConfig = {
  routing: RoutingModeMeta;
  /** `provider:model`, empty when nothing is pinned to the run. */
  model: string;
  approval: ApprovalMode;
  /** Projected monthly run cost above which the build interrupts itself. */
  costCap: number | null;
  /**
   * Skills this build forces on or off, over what keyword scoring would choose.
   * Scoring happens before the model call, so a miss is silent — this is how a
   * procedure reaches one build without being turned on for every other.
   */
  skills: SkillOverrides;
};

export const DEFAULT_RUN_CONFIG: RunConfig = {
  routing: ROUTING_MODES[0],
  model: "",
  approval: "checkpoints",
  costCap: null,
  skills: { pinned: [], excluded: [] },
};

export type ModelOption = {
  value: string;
  label: string;
  group: string;
  available: boolean;
};

// ── what can this machine actually run? ──────────────────────────────────────
/** Every model the run could be pinned to, local first. */
export function modelOptions(
  local: LocalStatus | null,
  models: RouterStatus | null,
): ModelOption[] {
  const options: ModelOption[] = [];

  // The configured default leads, and is what an auto-pick lands on. Ollama lists
  // everything that has been pulled — including the embedding model this app uses
  // for its own knowledge base — and alphabetical order would happily pin a build
  // to something that cannot hold a conversation.
  const preferred = local?.default_model;
  const names = [...(local?.models ?? [])].sort((a, b) =>
    a === preferred ? -1 : b === preferred ? 1 : a.localeCompare(b),
  );
  for (const name of names) {
    options.push({
      value: `ollama:${name}`,
      label: name === preferred ? `${name} · default` : name,
      group: "On this machine",
      available: Boolean(local?.reachable),
    });
  }

  for (const [provider, info] of Object.entries(models?.providers ?? {})) {
    if (info.is_local || !info.default_model) continue;
    options.push({
      value: `${provider}:${info.default_model}`,
      // Unavailable options stay listed and disabled: "why isn't Claude here?" is a
      // worse question than an option that says it needs a key.
      label: info.available
        ? `${provider} · ${info.default_model}`
        : `${provider} · ${info.default_model} — needs an API key`,
      group: "Cloud",
      available: info.available,
    });
  }

  return options;
}

function providerOf(spec: string): string {
  return spec.includes(":") ? spec.split(":", 1)[0] : "ollama";
}

function cloudAvailable(models: RouterStatus | null): boolean {
  return Object.values(models?.providers ?? {}).some((p) => p.available && !p.is_local);
}

export type RuntimeBlocker = {
  title: string;
  text: string;
  action: string;
  href: string;
};

/**
 * Why this run cannot start, or null when it can.
 *
 * Only ever returns a blocker on a *definite* negative: if the probe itself failed
 * we know nothing, and refusing to start on our own inability to ask is worse than
 * letting the run report the real error.
 */
export function runtimeBlocker(
  config: RunConfig,
  local: LocalStatus | null,
  models: RouterStatus | null,
): RuntimeBlocker | null {
  const localReachable = local?.reachable ?? true;
  const localReady = localReachable && (local?.has_default ?? true);

  const notRunning: RuntimeBlocker = {
    title: "Ollama isn't running",
    text:
      `Nothing is answering at ${local?.base_url ?? "the local runtime"}, so there is no ` +
      "model to hand this idea to. Start Ollama — or add a cloud API key — and this build " +
      "can go.",
    action: "Set up the runtime",
    href: "/settings",
  };
  const notPulled: RuntimeBlocker = {
    title: `The ${local?.default_model ?? "local"} model isn't downloaded`,
    text:
      "Ollama is running but the model this build would use hasn't been pulled yet. " +
      "Pulling it is a one-time download.",
    action: "Pull the model",
    href: "/settings",
  };

  if (config.routing.backend === "local_only") {
    if (!localReachable) return notRunning;
    if (!localReady) return notPulled;
    return null;
  }

  if (config.routing.backend === "manual") {
    if (!config.model) {
      // Nothing pinned because nothing was *found* — but if neither probe answered,
      // "found nothing" and "could not ask" look identical from here, and only one of
      // them is a reason to refuse to start.
      if (!local && !models) return null;
      return {
        title: "No model to pin",
        text:
          "Manual routing runs every phase on one model you choose, and this machine " +
          "has none available — no local model pulled, no cloud key set.",
        action: "Add a model",
        href: "/settings",
      };
    }
    const provider = providerOf(config.model);
    const available =
      provider === "ollama" ? localReachable : models?.providers?.[provider]?.available ?? true;
    if (!available) {
      return {
        title: `${provider} isn't available`,
        text: `This run is pinned to ${config.model}, and ${provider} has no working credentials on this machine.`,
        action: "Fix it in Settings",
        href: "/settings",
      };
    }
    return null;
  }

  // Auto falls back to local, so it needs one or the other.
  if (!localReady && !cloudAvailable(models)) {
    return localReachable ? notPulled : notRunning;
  }
  return null;
}

// ── the one-line version, shown when the panel is closed ─────────────────────
export function settingsSummary(config: RunConfig): string {
  const parts = [
    config.routing.backend === "local_only" ? "Local models" : config.routing.label,
  ];
  if (config.routing.needsModel && config.model) parts.push(config.model);
  parts.push(APPROVAL_MODES.find((m) => m.id === config.approval)?.label ?? config.approval);
  if (config.costCap) parts.push(`cap $${config.costCap.toLocaleString()}/mo`);
  const { pinned, excluded } = config.skills;
  if (pinned.length) parts.push(`${pinned.length} skill${pinned.length > 1 ? "s" : ""} pinned`);
  if (excluded.length) parts.push(`${excluded.length} off`);
  return parts.join(" · ");
}

// ── the panel ────────────────────────────────────────────────────────────────
export default function RunSettings({
  config,
  onChange,
  options,
  disabled,
  idea = "",
}: {
  config: RunConfig;
  onChange: (next: RunConfig) => void;
  options: ModelOption[];
  disabled?: boolean;
  /** What has been typed so far, so the skill picker can show what it would get. */
  idea?: string;
}) {
  const { routing, model, approval, costCap } = config;

  // The field holds text, not the parsed number. Round-tripping through `Number`
  // swallowed the decimal point as you typed it ("1." became "1"), so no fractional
  // cap could ever be entered — and a value that parsed to NaN silently reached the
  // API as `null`, creating a run with no cap at all while the UI showed one.
  //
  // No resync from the prop: this panel is the only thing that ever writes the cap
  // it is shown, and an effect keyed on the parsed number would wipe the "." the
  // moment it was typed.
  const [capText, setCapText] = useState(costCap === null ? "" : String(costCap));

  // Manual with nothing pinned is the dead control this fixes, so picking Manual
  // pins something the moment it is picked — the first model that actually works.
  useEffect(() => {
    if (!routing.needsModel || model) return;
    const first = options.find((o) => o.available);
    if (first) onChange({ ...config, model: first.value });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routing.needsModel, model, options]);

  const groups = Array.from(new Set(options.map((o) => o.group)));

  return (
    <div className="settings-grid">
      <div className="setting">
        <span className="label" id="routing-label">
          Which models run it
        </span>
        <div className="seg" role="group" aria-labelledby="routing-label">
          {ROUTING_MODES.map((m) => (
            <button
              key={m.id}
              type="button"
              className="seg-btn"
              aria-pressed={routing.id === m.id}
              onClick={() => onChange({ ...config, routing: m, model: m.needsModel ? model : "" })}
              disabled={disabled}
            >
              {m.label}
            </button>
          ))}
        </div>
        <p className="field-hint">{routing.hint}</p>

        {routing.needsModel && (
          <div className="field" style={{ marginTop: 4 }}>
            <label htmlFor="pinned-model">Model for every phase</label>
            {options.length > 0 ? (
              <select
                id="pinned-model"
                className="select"
                value={model}
                disabled={disabled}
                onChange={(e) => onChange({ ...config, model: e.target.value })}
              >
                {groups.map((group) => (
                  <optgroup key={group} label={group}>
                    {options
                      .filter((o) => o.group === group)
                      .map((o) => (
                        <option key={o.value} value={o.value} disabled={!o.available}>
                          {o.label}
                        </option>
                      ))}
                  </optgroup>
                ))}
              </select>
            ) : (
              <p className="field-hint">
                No models are available yet.{" "}
                <Link className="link" href="/settings">
                  Pull a local model or add a key
                </Link>
                .
              </p>
            )}
          </div>
        )}
      </div>

      <div className="setting">
        <span className="label" id="gates-label">
          When it stops for you
        </span>
        <div className="seg" role="group" aria-labelledby="gates-label">
          {APPROVAL_MODES.map((m) => (
            <button
              key={m.id}
              type="button"
              className="seg-btn"
              aria-pressed={approval === m.id}
              onClick={() => onChange({ ...config, approval: m.id })}
              disabled={disabled}
            >
              {m.label}
            </button>
          ))}
        </div>
        <p className="field-hint">
          {APPROVAL_MODES.find((m) => m.id === approval)?.hint}
        </p>

        {approval === "checkpoints" && (
          <div className="field" style={{ marginTop: 4 }}>
            <label htmlFor="cost-cap">Interrupt if projected running cost exceeds</label>
            <div className="prefixed">
              <span className="prefixed-mark" aria-hidden="true">
                $
              </span>
              <input
                id="cost-cap"
                className="input input-mono"
                inputMode="decimal"
                placeholder="no cap"
                value={capText}
                disabled={disabled}
                onChange={(e) => {
                  const raw = e.target.value.replace(/[^0-9.]/g, "");
                  setCapText(raw);
                  const value = Number(raw);
                  onChange({
                    ...config,
                    // Anything not yet a usable number means "no cap" until it is one.
                    costCap: raw !== "" && Number.isFinite(value) && value > 0 ? value : null,
                  });
                }}
              />
              <span className="prefixed-suffix">/month</span>
            </div>
            <p className="field-hint">
              Ledger estimates what the finished product costs to run. Over this, the
              build stops and tells you instead of finishing quietly.
            </p>
          </div>
        )}
      </div>

      <SkillPicker
        overrides={config.skills}
        onChange={(skills) => onChange({ ...config, skills })}
        idea={idea}
        disabled={disabled}
      />
    </div>
  );
}

// ── which procedures this build works from ───────────────────────────────────
/**
 * Skills are matched on keywords *before* the model is called, so a skill that
 * does not match simply never arrives and nothing in the finished build says so.
 * Two controls answer that here: a per-skill Auto / Always / Never, and a check
 * against the idea already typed — the only way to see a miss without paying for
 * a whole run to find it.
 */
function SkillPicker({
  overrides,
  onChange,
  idea,
  disabled,
}: {
  overrides: SkillOverrides;
  onChange: (next: SkillOverrides) => void;
  idea: string;
  disabled?: boolean;
}) {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [checking, setChecking] = useState(false);

  // Loaded when this panel is, not when the page is: the composer's whole shape
  // is "ask nothing until it is asked for", and a library fetch behind a closed
  // disclosure is a request nobody wanted.
  useEffect(() => {
    let live = true;
    api
      .listSkills()
      .then((lib) => live && setSkills(lib.skills.filter((s) => s.usable)))
      .catch(() => live && setSkills([]));
    return () => {
      live = false;
    };
  }, []);

  const set = useCallback(
    (name: string, state: "auto" | "pin" | "off") => {
      onChange({
        pinned: state === "pin"
          ? [...overrides.pinned.filter((n) => n !== name), name]
          : overrides.pinned.filter((n) => n !== name),
        excluded: state === "off"
          ? [...overrides.excluded.filter((n) => n !== name), name]
          : overrides.excluded.filter((n) => n !== name),
      });
      setPreview(null);
    },
    [onChange, overrides],
  );

  async function check() {
    if (checking) return;
    setChecking(true);
    try {
      setPreview(
        await api.previewSkills({
          idea: idea.trim(),
          pinned: overrides.pinned,
          excluded: overrides.excluded,
        }),
      );
    } catch {
      setPreview(null);
    } finally {
      setChecking(false);
    }
  }

  if (skills !== null && skills.length === 0) return null;

  const touched = overrides.pinned.length + overrides.excluded.length;

  return (
    <div className="setting skill-picker">
      <button
        type="button"
        className="run-settings-toggle skill-picker-toggle"
        aria-expanded={open}
        aria-controls="skill-picker"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
      >
        <span className={"phase-chev" + (open ? " open" : "")} aria-hidden="true">
          {Icon.chevron}
        </span>
        <span className="label">What the crew works from</span>
        <span className="run-settings-summary">
          {skills === null
            ? "reading the library…"
            : touched === 0
              ? `${skills.length} skills, matched to this idea automatically`
              : `${skills.length} skills · ${touched} decided by you`}
        </span>
      </button>

      {open && skills && (
        <div id="skill-picker" className="skill-picker-body">
          <ul className="pick-rows">
            {skills.map((skill) => {
              const state = overrides.pinned.includes(skill.name)
                ? "pin"
                : overrides.excluded.includes(skill.name)
                  ? "off"
                  : "auto";
              return (
                <li key={skill.name} className="pick-row">
                  <span className="pick-id">
                    <span className="pick-name">{skill.title}</span>
                    <span className="pick-serves">
                      {skill.agents.length === 0
                        ? "every agent"
                        : skill.agents
                            .map((a) => AGENT_BY_KEY[a]?.codename ?? a)
                            .join(" · ")}
                      {/* Auto on a skill switched off in the library means never, and
                          a row that does not say so is a control that looks live and
                          is not. Always still reaches it — a pin is the more specific
                          instruction. */}
                      {!skill.enabled && <span className="pick-off">off in the library</span>}
                    </span>
                  </span>
                  <span className="seg" role="group" aria-label={skill.title}>
                    {(["auto", "pin", "off"] as const).map((option) => (
                      <button
                        key={option}
                        type="button"
                        className="seg-btn"
                        aria-pressed={state === option}
                        disabled={disabled}
                        onClick={() => set(skill.name, option)}
                      >
                        {option === "auto" ? "Auto" : option === "pin" ? "Always" : "Never"}
                      </button>
                    ))}
                  </span>
                </li>
              );
            })}
          </ul>

          <div className="skill-picker-check">
            <button
              type="button"
              className="btn btn-sm"
              disabled={!idea.trim() || checking || disabled}
              onClick={check}
            >
              {checking && <span className="btn-spinner" aria-hidden="true" />}
              {checking ? "Checking…" : "What would this idea get?"}
            </button>
            {!idea.trim() && (
              <span className="field-hint">Write the idea first — it is what they match against.</span>
            )}
          </div>

          {preview && (
            <ol className="dryrun-rows">
              {preview.phases.map((phase) => {
                const agent = AGENT_BY_KEY[phase.phase];
                return (
                  <li
                    key={phase.phase}
                    className="dryrun-row"
                    style={{ ["--agent" as string]: agent?.accent }}
                  >
                    <span className="dryrun-who">
                      <b className="agent-line-name">{agent?.codename ?? phase.phase}</b>
                    </span>
                    {phase.skills.length === 0 ? (
                      <span className="dryrun-none">nothing matched</span>
                    ) : (
                      <span className="dryrun-picks">
                        {phase.skills.map((s) => (
                          <span
                            key={s.name}
                            className={"pick" + (s.pinned ? " pick-pinned" : "")}
                            title={s.reason}
                          >
                            {s.title}
                          </span>
                        ))}
                      </span>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
