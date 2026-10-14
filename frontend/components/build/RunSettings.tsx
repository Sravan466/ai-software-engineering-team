"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  type ApprovalMode,
  type AutoChoice,
  type LocalStatus,
  type RoleRow,
  type RouterStatus,
  type Skill,
  type SkillOverrides,
  type SkillPreview,
} from "@/lib/api";
import {
  APPROVAL_MODES,
  COST_CAP_COPY,
  ROUTING_BY_BACKEND,
  ROUTING_MODES,
  type RoutingModeMeta,
} from "@/components/shell/phases";
import { canRunABuild, runtimeSays } from "@/lib/capabilities";
import { hostOf, modelName, sourceFor, splitSpec, triedText } from "@/lib/models";
import { listOf } from "@/lib/text";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";
import PreviewRows from "@/components/skills/PreviewRows";
import { byTitle } from "@/components/skills/skills";
import ChoiceRows from "@/components/build/ChoiceRows";

/**
 * Which model builds it and when it checks with you, and whether it can run at all.
 *
 * Config is asked after intent, so this is a disclosure rather than a gate. Each
 * answer is said by what it runs, with the real model names, and an answer that
 * would run the same thing as another isn't offered (#96). Pick a model pins a real
 * model or it isn't Pick a model: `routing_mode: "manual"` with nothing pinned is
 * silently Auto. And Start build is checked against the runtime here instead of
 * failing after the project has already been created.
 */

export type RunConfig = {
  routing: RoutingModeMeta;
  /** `source:model`, empty when nothing is pinned to the run. */
  model: string;
  approval: ApprovalMode;
  /** Projected monthly hosting cost above which the build asks first. */
  costCap: number | null;
  /**
   * Skills this build forces on or off, over what keyword scoring would choose.
   * Scoring happens before the model call, so a miss is silent — this is how a
   * procedure reaches one build without being turned on for every other.
   */
  skills: SkillOverrides;
};

export const DEFAULT_RUN_CONFIG: RunConfig = {
  routing: ROUTING_BY_BACKEND.local_only,
  model: "",
  approval: "checkpoints",
  costCap: null,
  skills: { pinned: [], excluded: [] },
};

export type ModelOption = {
  value: string;
  /** The row as the select shows it: "claude-opus-4-8 · Anthropic". */
  label: string;
  /** The model's own name. */
  name: string;
  /** "On this computer · Ollama", "On another computer · Desk", "Cloud · uses your API key". */
  group: string;
  /** The runtime's label, or the provider's. */
  where: string;
  available: boolean;
  /** The local default — what picking Pick a model pins first. */
  isDefault?: boolean;
  /** Served from another computer; never pinned without the user choosing it. */
  remote?: boolean;
  /** Sent by its runtime to a hosted service to run; never pinned silently either. */
  hosted?: boolean;
  /** A cloud provider's model, billed to the user's key. */
  cloud?: boolean;
};

// ── what can this machine actually run? ──────────────────────────────────────
/** A model the picker is leaving out, and what the runtime said it does. */
export type OmittedModel = { name: string; does: string[] };

/**
 * Models this picker is leaving out, in the order they'd have appeared.
 *
 * Sorting the default to the front was the whole defence before this, and it only
 * ever moved the problem down one row: an embedding model sat in the same list as
 * the models that write code, and picking it produced a build that failed on its
 * first call. A name cannot settle it either — the next one will be called
 * something nobody here has heard of — so the backend asks the runtime, once.
 *
 * They are returned rather than silently dropped, because "why isn't it here?" is
 * the worse question — the same reason a cloud provider with no key stays listed
 * and disabled a few lines down. What the runtime *did* report travels with each
 * name so the panel can quote it instead of guessing at it.
 */
export function modelsThatCannotBuild(local: LocalStatus | null): OmittedModel[] {
  return (local?.sources ?? []).flatMap((source) =>
    source.models
      .filter((m) => !canRunABuild(m.spec, local?.cannot_build))
      .map((m) => ({ name: m.name, does: local?.model_capabilities?.[m.spec] ?? [] })),
  );
}

/**
 * Every model the run could be pinned to: each local source's, grouped by where it
 * runs and which runtime serves it, then the cloud. Provider names come from the
 * backend (`keyerrors.LABELS`), never a table of "pretty" model names kept here —
 * such a table is out of date within weeks.
 */
export function modelOptions(
  local: LocalStatus | null,
  models: RouterStatus | null,
): ModelOption[] {
  const options: ModelOption[] = [];

  // The configured default leads its group, and is what an auto-pick lands on.
  const preferred = local?.default_model;
  for (const source of local?.sources ?? []) {
    if (!source.reachable) continue;
    const group = source.remote
      ? `On another computer · ${source.label}`
      : `On this computer · ${source.label}`;
    const names = source.models
      .filter((m) => canRunABuild(m.spec, local?.cannot_build))
      .sort((a, b) =>
        a.spec === preferred ? -1 : b.spec === preferred ? 1 : a.name.localeCompare(b.name),
      );
    for (const m of names) {
      options.push({
        value: m.spec,
        label:
          (m.spec === preferred ? `${m.name} · default` : m.name) +
          (m.is_local ? "" : " · runs on a hosted service"),
        name: m.name,
        group,
        where: source.label,
        available: true,
        isDefault: m.spec === preferred,
        remote: source.remote,
        hosted: !m.is_local,
      });
    }
  }

  for (const [provider, info] of Object.entries(models?.providers ?? {})) {
    if (info.is_local || !info.default_model) continue;
    const label = info.label || provider;
    options.push({
      value: `${provider}:${info.default_model}`,
      // Unavailable options stay listed and disabled: "why isn't Claude here?" is a
      // worse question than an option that says it needs a key.
      label: info.available
        ? `${info.default_model} · ${label}`
        : `${info.default_model} · ${label} · add a key in Settings`,
      name: info.default_model,
      group: "Cloud · uses your API key",
      where: label,
      available: info.available,
      cloud: true,
    });
  }

  return options;
}

// ── which answers to offer ───────────────────────────────────────────────────
/**
 * What this account can build with, and so which answers are real ones.
 *
 * Two answers that would run the same thing are never both offered: Auto appears
 * only when it has two or more models to choose between, and with one usable model
 * there is no question at all, only a sentence naming it.
 */
export type ModelChoices = {
  /** Every model the account can use: it writes, its source answers, a key isn't refused. */
  usable: ModelOption[];
  /** What Auto can choose from. The backend's list once it has answered. */
  autoCount: number;
  /** The working cloud providers, by name. */
  cloudLabels: string[];
  /** The local default, when it can run a Local build. */
  localDefault: ModelOption | null;
  /** The answers offered, in order. Empty with one usable model or none. */
  modes: RoutingModeMeta[];
  /** The one model, when there is exactly one. */
  single: ModelOption | null;
  /** What a new build starts on, until someone chooses. */
  preferred: { routing: RoutingModeMeta; model: string };
};

export function modelChoices(
  options: ModelOption[],
  auto: AutoChoice[] | null | undefined,
): ModelChoices {
  const usable = options.filter((o) => o.available);
  // Auto never picks a model on another computer or a hosted one without being
  // asked, so they don't count towards its choice — they do count as usable.
  const autoCount = auto ? auto.length : usable.filter((o) => !o.remote && !o.hosted).length;
  // Which providers Auto could bill, and whether it has a free model too — from the
  // server's own list once it has answered, so the sentence and the count agree.
  const cloudLabels = auto
    ? Array.from(new Set(auto.filter((a) => !a.local).map((a) => a.where)))
    : Array.from(new Set(usable.filter((o) => o.cloud).map((o) => o.where)));
  const autoHasLocal = auto
    ? auto.some((a) => a.local)
    : usable.some((o) => !o.cloud && !o.remote && !o.hosted);
  const localDefault = usable.find((o) => o.isDefault && !o.hosted) ?? null;
  const single = usable.length === 1 ? usable[0] : null;

  const modes: RoutingModeMeta[] = [];
  if (usable.length > 1) {
    if (autoCount >= 2) modes.push(ROUTING_BY_BACKEND.auto);
    if (localDefault) modes.push(ROUTING_BY_BACKEND.local_only);
    modes.push(ROUTING_BY_BACKEND.manual);
  }

  // Auto first when every model it can pick is free; Local model when a key would
  // be billed, so no build spends on one unless someone chose that.
  let preferred: ModelChoices["preferred"];
  if (usable.length === 0) {
    // Nothing to build with: Local model, whose blocker says what to start or add.
    preferred = { routing: ROUTING_BY_BACKEND.local_only, model: "" };
  } else if (single) {
    preferred =
      single === localDefault
        ? { routing: ROUTING_BY_BACKEND.local_only, model: "" }
        : { routing: ROUTING_BY_BACKEND.manual, model: single.value };
  } else if (modes.includes(ROUTING_BY_BACKEND.auto) && (cloudLabels.length === 0 || !autoHasLocal)) {
    // Every model Auto can pick is free, or none is (only keys were added): then
    // Auto. A mix of free and billed: not Auto, so a key bills only when chosen.
    preferred = { routing: ROUTING_BY_BACKEND.auto, model: "" };
  } else if (localDefault) {
    preferred = { routing: ROUTING_BY_BACKEND.local_only, model: "" };
  } else {
    preferred = { routing: ROUTING_BY_BACKEND.manual, model: "" };
  }
  return { usable, autoCount, cloudLabels, localDefault, modes, single, preferred };
}

/** "on this computer", or the runtime on another computer it runs on. */
function whereItRuns(option: ModelOption): string {
  if (option.cloud) return `through your ${option.where} key`;
  if (option.hosted) return `on a hosted service, through ${option.where}`;
  return option.remote ? `on ${option.where}` : "on this computer";
}

/**
 * Agents pointed at a local model of their own in Settings, by that model: what a
 * Local build runs besides the default. A cloud choice isn't listed — a Local build
 * doesn't use it.
 */
function localOverrides(roles: RoleRow[] | null, local: LocalStatus | null): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const row of roles ?? []) {
    const agent = AGENT_BY_KEY[row.role];
    if (!agent || !row.assigned || row.assigned === local?.default_model) continue;
    if (!sourceFor(local, row.assigned)) continue;
    const name = modelName(row.assigned);
    out.set(name, [...(out.get(name) ?? []), agent.codename]);
  }
  return out;
}

function describe(
  mode: RoutingModeMeta,
  choices: ModelChoices,
  roles: RoleRow[] | null,
  local: LocalStatus | null,
) {
  if (mode.backend === "auto") {
    const billed = choices.cloudLabels;
    return (
      <>
        Picks each agent&apos;s model from the {choices.autoCount} you have, by how each one did in
        your past builds.
        {billed.length > 0 &&
          ` ${listOf(billed)} ${billed.length === 1 ? "bills" : "bill"} your key for the agents ${
            billed.length === 1 ? "it runs" : "they run"
          }.`}
      </>
    );
  }
  if (mode.backend === "local_only" && choices.localDefault) {
    const home = choices.localDefault;
    const overrides = Array.from(localOverrides(roles, local));
    return (
      <>
        Every agent uses <span className="mono">{home.name}</span> {whereItRuns(home)}.{" "}
        {home.remote ? "Free, and nothing goes to a cloud service." : "Free, and nothing leaves your machine."}
        {overrides.map(([name, agents]) => (
          <span key={name}>
            {" "}
            {listOf(agents)} {agents.length === 1 ? "uses" : "use"} <span className="mono">{name}</span>{" "}
            instead (set in Settings).
          </span>
        ))}
      </>
    );
  }
  return mode.hint;
}

export type RuntimeBlocker = {
  title: string;
  text: string;
  action: string;
  href: string;
};

/**
 * Why the local half of a run cannot start, or null when it can — decided from the
 * sources, never from one runtime's name. Null also when there is nothing definite
 * to say: the server's preflight answers everything subtler.
 */
function localBlocker(local: LocalStatus | null): RuntimeBlocker | null {
  if (!local) return null;
  if (!local.reachable) {
    const tried = triedText(local.tried);
    return {
      title: "No local runtime reachable",
      text:
        (tried ? `Nothing answered at ${tried}. ` : "Nothing is answering. ") +
        "Start a local runtime, or add a cloud API key in Settings.",
      action: "Set up your computer",
      href: "/setup",
    };
  }
  const home = sourceFor(local, local.default_model);
  if (local.default_model && home && !home.reachable) {
    return {
      title: `${home.label} isn't answering`,
      text:
        `The default model is on ${home.label} at ${hostOf(home.base_url)}, which isn't ` +
        "answering. Start it, or choose a model from a runtime that is running.",
      action: "Choose in Settings",
      href: "/settings",
    };
  }
  if (local.default_model && !local.has_default) {
    const where = home?.label ?? "its runtime";
    return {
      title: `${modelName(local.default_model)} isn't on ${where}`,
      text: home?.can_download
        ? `${where} is running but doesn't have this build's model. You only need to download it once.`
        : `${where} is running but doesn't serve this build's model. ${home?.add_model ?? ""}`,
      action: home?.can_download ? "Download the model" : "Choose in Settings",
      href: "/settings",
    };
  }
  return null;
}

/**
 * Why this run cannot start, or null when it can — for the cases this page can
 * explain best from what it already knows: no runtime answers, the default's runtime
 * is down, or it does not have the model. Anything subtler (a default or an agent's
 * model that cannot write, a run Auto would send elsewhere) is the server's to
 * answer, through `preflight`.
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
  if (config.routing.backend === "local_only") return localBlocker(local);

  if (config.routing.backend === "manual") {
    if (!config.model) {
      // Nothing pinned because nothing was *found* — but if neither probe answered,
      // "found nothing" and "could not ask" look identical from here, and only one of
      // them is a reason to refuse to start.
      if (!local && !models) return null;
      // Something is there, but only models that send prompts off this machine, which
      // are never pinned for anyone. The select says "Choose a model…"; so does this.
      if (modelOptions(local, models).some((o) => o.available)) {
        return {
          title: "Choose a model",
          text:
            "Pick a model runs every agent on the one you choose. The models available run " +
            "on another computer or a hosted service, so choose one under “Model”.",
          action: "Review models in Settings",
          href: "/settings",
        };
      }
      const all = (local?.sources ?? []).flatMap((s) => (s.reachable ? s.models : []));
      // Every model found was left out as unable to write — not merely some of them.
      const onlyNonWriting =
        all.length > 0 && !all.some((m) => canRunABuild(m.spec, local?.cannot_build));
      return {
        title: "No model to build with",
        text: onlyNonWriting
          ? "The local models found can't write, and no cloud key is set."
          : "No local runtime serves a model, and no cloud key is set.",
        action: "Add a model",
        href: "/settings",
      };
    }
    const { source } = splitSpec(config.model);
    const local_source = local?.sources.find((s) => s.id === source);
    if (local_source) {
      if (!local_source.reachable) {
        return {
          title: `${local_source.label} isn't answering`,
          text: `This build runs on ${modelName(config.model)} on ${local_source.label} at ${hostOf(local_source.base_url)}, which isn't answering.`,
          action: "Fix it in Settings",
          href: "/settings",
        };
      }
      return null;
    }
    const provider = models?.providers?.[source];
    if (provider && !provider.available) {
      const label = provider.label || source;
      return {
        title: `${label} isn't available`,
        text: `This build runs on ${modelName(config.model)}, and ${label} has no working key on this machine.`,
        action: "Fix it in Settings",
        href: "/settings",
      };
    }
    return null;
  }

  // Auto falls back to local, so it needs one or the other.
  const cloud = Object.values(models?.providers ?? {}).some((p) => p.available && !p.is_local);
  if (!cloud) return localBlocker(local);
  return null;
}

// ── the one-line version, shown when the panel is closed ─────────────────────
export function settingsSummary(config: RunConfig, choices: ModelChoices): string {
  const parts: string[] = [];
  if (config.routing.backend === "auto") {
    parts.push(`Auto · ${choices.autoCount} model${choices.autoCount === 1 ? "" : "s"}`);
  } else if (config.routing.backend === "manual") {
    parts.push(config.model ? modelName(config.model) : "Pick a model");
  } else if (choices.localDefault) {
    parts.push(`${choices.localDefault.name}, ${whereItRuns(choices.localDefault)}`);
  } else {
    parts.push(ROUTING_BY_BACKEND.local_only.label);
  }
  parts.push(APPROVAL_MODES.find((m) => m.id === config.approval)?.summary ?? config.approval);
  if (config.costCap) parts.push(`hosting cap $${config.costCap.toLocaleString()}/mo`);
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
  choices,
  roles = null,
  local = null,
  omitted = [],
  disabled,
  idea = "",
}: {
  config: RunConfig;
  /** `byUser` is false for a change the panel made itself (pinning a model). */
  onChange: (next: RunConfig, byUser?: boolean) => void;
  options: ModelOption[];
  /** Which answers are real ones on this account (`modelChoices`). */
  choices: ModelChoices;
  /** Each agent's own model from Settings, for what Local model runs besides the default. */
  roles?: RoleRow[] | null;
  local?: LocalStatus | null;
  /**
   * Local models deliberately left out of `options` because they cannot
   * complete text. Named under the control rather than dropped in silence — a
   * model you can see in Settings and not here is a discrepancy someone will go
   * looking for.
   */
  omitted?: OmittedModel[];
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

  // Pick a model with nothing pinned is a dead control, so choosing it pins
  // something the moment it is chosen: the default, else the first model that
  // works on this machine. Never, silently, a source on another computer, a hosted
  // model or a cloud key: prompts sent there leave this one, and a key bills
  // someone, so those are picked by hand from the select.
  useEffect(() => {
    if (!routing.needsModel || model) return;
    const first =
      options.find((o) => o.available && o.isDefault) ??
      options.find((o) => o.available && !o.remote && !o.hosted && !o.cloud);
    if (first) onChange({ ...config, model: first.value }, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routing.needsModel, model, options]);

  const groups = Array.from(new Set(options.map((o) => o.group)));

  const picker = (
    <div className="field choice-field">
      <label htmlFor="pinned-model">Model</label>
      {options.length > 0 ? (
        <select
          id="pinned-model"
          className="select"
          value={model}
          disabled={disabled}
          onChange={(e) => onChange({ ...config, model: e.target.value })}
        >
          {/* Without this, a select with nothing pinned shows its first option as
              if it were chosen — and choosing that option changes nothing. */}
          {!model && (
            <option value="" disabled>
              Choose a model…
            </option>
          )}
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
          {omitted.length > 0 ? "No local model found can write. " : "No models are available yet. "}
          <Link className="link" href="/settings">
            Add a local runtime or a key
          </Link>
          .
        </p>
      )}
      <OmittedModels models={omitted} />
    </div>
  );

  return (
    <div className="settings-grid">
      <div className="setting">
        {choices.modes.length === 1 && choices.modes[0].needsModel ? (
          // Only Pick a model is a real answer here (models elsewhere, nothing Auto
          // or Local model could run): no one-option radio, the select itself.
          <>
            <span className="label">Which model builds it</span>
            {picker}
          </>
        ) : choices.modes.length > 0 ? (
          <ChoiceRows
            name="routing"
            legend="Which model builds it"
            value={routing.id}
            disabled={disabled}
            onChange={(id) => {
              const next = ROUTING_MODES.find((m) => m.id === id);
              if (next) onChange({ ...config, routing: next, model: next.needsModel ? model : "" });
            }}
            choices={choices.modes.map((m) => ({
              value: m.id,
              label: m.label,
              description: describe(m, choices, roles, local),
              under: m.needsModel ? picker : undefined,
            }))}
          />
        ) : (
          <>
            <span className="label">Which model builds it</span>
            {choices.single ? (
              <p className="setting-line">
                Builds with <span className="mono">{choices.single.name}</span>{" "}
                {whereItRuns(choices.single)}.{" "}
                <Link className="link" href="/settings">
                  Add a model or a cloud key in Settings
                </Link>{" "}
                to let Auto choose.
              </p>
            ) : (
              <p className="field-hint">
                No model can run a build yet.{" "}
                <Link className="link" href="/settings">
                  Add a local runtime or a key
                </Link>
                .
              </p>
            )}
            <OmittedModels models={omitted} />
          </>
        )}
      </div>

      <div className="setting">
        <ChoiceRows
          name="checks"
          legend="When it checks with you"
          value={approval}
          disabled={disabled}
          onChange={(id) => onChange({ ...config, approval: id })}
          choices={APPROVAL_MODES.map((m) => ({
            value: m.id,
            label: m.label,
            description: m.hint,
            under:
              m.id === "checkpoints" ? (
                <div className="field choice-field">
                  <label htmlFor="cost-cap">{COST_CAP_COPY.label}</label>
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
                      aria-describedby="cost-cap-hint"
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
                    <span className="prefixed-suffix">{COST_CAP_COPY.suffix}</span>
                  </div>
                  <p className="field-hint" id="cost-cap-hint">
                    {COST_CAP_COPY.hint}
                  </p>
                </div>
              ) : undefined,
          }))}
        />
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

/**
 * The models this list is not offering, and why — in one line, under the control.
 *
 * Nothing here is actionable, so it is a hint rather than a notice: the answer to
 * "where did `nomic-embed-text` go?" costs a sentence, and asking someone to
 * dismiss a banner for it would cost more than the question is worth.
 *
 * The sentence quotes the runtime rather than naming a kind of model. "It makes
 * embeddings" is true of every case anyone has today and is still a guess — the
 * rule upstream is `completion` is absent, and the copy says exactly that much.
 */
function OmittedModels({ models }: { models: OmittedModel[] }) {
  if (models.length === 0) return null;
  const one = models.length === 1;
  // The union of what they do, so two embedding models read as one reason.
  const does = Array.from(new Set(models.flatMap((m) => m.does)));
  return (
    <p className="field-hint omitted-models">
      {models.map((m, i) => (
        <span key={m.name}>
          {i > 0 && (i === models.length - 1 ? " and " : ", ")}
          <span className="mono">{m.name}</span>
        </span>
      ))}
      {one ? " isn't here" : " aren't here"}. The runtime {runtimeSays(does, !one)}, so{" "}
      {one ? "it can't" : "they can't"} run a build.
    </p>
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
      .then((lib) => live && setSkills(lib.skills.filter((s) => s.usable).sort(byTitle)))
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
              ? `${skills.length} skills matched to this idea`
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
              <span className="field-hint">Write the idea first. Skills are matched against it.</span>
            )}
          </div>

          {preview && <PreviewRows preview={preview} />}
        </div>
      )}
    </div>
  );
}
