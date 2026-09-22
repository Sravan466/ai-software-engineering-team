"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  api,
  type GenerationField,
  type GenerationValues,
  type ModelCheck,
  type ModelGeneration,
} from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";

/**
 * One model's generation settings, tuned in place under its row.
 *
 * Every field starts blank, and blank means "whatever the server or the model file
 * says" — which is printed under the field, so a person sees the number they are
 * overriding before they override it. A field this runtime has no way to send is
 * disabled and says so, rather than accepting a value that would never arrive.
 * Ranges come from the backend, which checks them again before saving.
 */

const GROUPS: { key: GenerationField["group"]; title: string; note: string }[] = [
  {
    key: "sampling",
    title: "Sampling",
    note: "Sent with every call to this model, in the runtime's own names.",
  },
  {
    key: "limits",
    title: "Ceilings",
    note: "Lower only — a model's own limit is never raised by a bigger number here.",
  },
  {
    key: "machine",
    title: "This machine",
    note: "About the computer the model runs on. Set on this backend, and sent only to a runtime on this same machine.",
  },
];

const THINKING_WORD: Record<string, string> = {
  off: "Off",
  on: "On",
  low: "Low",
  medium: "Medium",
  high: "High",
};

type Draft = Record<string, string>;

/** A stop sequence as one line of the textarea: newlines and tabs written as escapes. */
function escapeStop(stop: string): string {
  return stop.replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/\t/g, "\\t");
}
function unescapeStop(line: string): string {
  return line.replace(/\\(\\|n|t)/g, (_, c) => (c === "n" ? "\n" : c === "t" ? "\t" : "\\"));
}
function stopsOf(raw: string): string[] {
  return raw.split("\n").filter((l) => l !== "").map(unescapeStop);
}

function toDraft(values: GenerationValues, fields: GenerationField[]): Draft {
  const out: Draft = {};
  for (const f of fields) {
    const v = values[f.group]?.[f.key];
    if (v === undefined || v === null) out[f.key] = "";
    else if (Array.isArray(v)) out[f.key] = v.map((x) => escapeStop(String(x))).join("\n");
    else out[f.key] = String(v);
  }
  return out;
}

function shown(value: unknown): string {
  if (Array.isArray(value)) return value.map((s) => JSON.stringify(s)).join(" ");
  if (typeof value === "number") return Number.isInteger(value) ? value.toLocaleString() : String(value);
  return String(value);
}

/** What is wrong with one draft value, in words, or null — the backend checks again. */
function problem(field: GenerationField, raw: string): string | null {
  const text = raw.trim();
  if (!text) return null;
  if (field.kind === "float" || field.kind === "int") {
    const n = Number(text);
    if (!Number.isFinite(n)) return "Has to be a number.";
    if (field.kind === "int" && !Number.isInteger(n)) return "Has to be a whole number.";
    if ((field.minimum !== null && n < field.minimum) || (field.maximum !== null && n > field.maximum)) {
      return `Between ${shown(field.minimum)} and ${shown(field.maximum)}.`;
    }
  }
  if (field.kind === "duration" && !/^(-1|0|[1-9]\d{0,5}(ms|s|m|h)?)$/.test(text)) {
    return "A duration like 30s, 10m or 1h — or 0, or -1.";
  }
  if (field.kind === "stops") {
    const stops = stopsOf(raw);
    if (stops.length > 4) return "At most 4, one per line.";
    if (stops.some((l) => l.length > 32)) return "Each is at most 32 characters.";
  }
  return null;
}

function toValues(draft: Draft, fields: GenerationField[]): GenerationValues {
  const out: GenerationValues = {};
  for (const f of fields) {
    const raw = draft[f.key] ?? "";
    if (!raw.trim()) continue;
    let value: unknown = raw.trim();
    if (f.kind === "float" || f.kind === "int") value = Number(raw.trim());
    if (f.kind === "stops") value = stopsOf(raw);
    (out[f.group] ??= {})[f.key] = value;
  }
  return out;
}

export default function ModelTune({
  spec,
  name,
  onSaved,
  onClose,
  id,
}: {
  spec: string;
  name: string;
  onSaved: (check: ModelCheck) => void;
  onClose: () => void;
  id?: string;
}) {
  const [data, setData] = useState<ModelGeneration | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState<"save" | "reset" | null>(null);
  const [status, setStatus] = useState("");
  const [invalidCount, setInvalidCount] = useState(0);
  const headingId = useId();
  const title = useRef<HTMLHeadingElement>(null);
  const form = useRef<HTMLFormElement>(null);

  useEffect(() => {
    let live = true;
    api
      .getModelGeneration(spec)
      .then((next) => {
        if (!live) return;
        setData(next);
        setDraft(toDraft(next.values, next.fields));
      })
      .catch((e: any) => live && setLoadError(e.message));
    return () => {
      live = false;
    };
  }, [spec]);

  const fields = data?.fields ?? [];
  const byKey = useMemo(() => Object.fromEntries(fields.map((f) => [f.key, f])), [fields]);
  const saved = useMemo(() => (data ? toDraft(data.values, data.fields) : {}), [data]);
  const dirty = fields.some((f) => (draft[f.key] ?? "") !== (saved[f.key] ?? ""));
  const errors = Object.fromEntries(
    fields.map((f) => [f.key, problem(f, draft[f.key] ?? "")]).filter(([, p]) => p),
  ) as Record<string, string>;
  const hasErrors = Object.keys(errors).length > 0;
  const anythingSaved = data ? Object.values(data.values).some((g) => g && Object.keys(g).length) : false;

  function set(key: string, value: string) {
    setDraft((d) => ({ ...d, [key]: value }));
    setStatus("");
    setSaveError("");
    setInvalidCount(0);
  }

  async function save() {
    if (!data || saving) return;
    if (hasErrors) {
      // Every problem is shown at once, one summary is announced, and focus goes to
      // the first field that needs fixing — not a burst of alerts from every field.
      setTouched(Object.fromEntries(fields.map((f) => [f.key, true])));
      setInvalidCount(Object.keys(errors).length);
      requestAnimationFrame(() => form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus());
      return;
    }
    setSaving("save");
    setSaveError("");
    try {
      const next = await api.setModelGeneration(data.spec, toValues(draft, fields));
      setData(next);
      setDraft(toDraft(next.values, next.fields));
      setTouched({});
      setStatus(`Saved. The next call to ${name} uses these — no other model is touched.`);
      onSaved(next.check);
      title.current?.focus();
    } catch (e: any) {
      setSaveError(e.message);
    } finally {
      setSaving(null);
    }
  }

  async function reset() {
    if (!data) return;
    setSaving("reset");
    setSaveError("");
    try {
      const next = await api.resetModelGeneration(data.spec);
      setData(next);
      setDraft(toDraft(next.values, next.fields));
      setTouched({});
      setStatus(`Back on the server's defaults for ${name}.`);
      onSaved(next.check);
      title.current?.focus();
    } catch (e: any) {
      setSaveError(e.message);
    } finally {
      setSaving(null);
    }
  }

  /** The line under a field: what applies if it is left blank, or why it can't be set. */
  function hint(f: GenerationField): string {
    if (!data) return "";
    if (data.supported[f.key] === false) {
      return `${data.source_label} can't take this, so it isn't sent.`;
    }
    const base = data.untuned;
    if (f.key === "context_window") return `Without one: ${base.context_window.toLocaleString()} tokens.`;
    if (f.key === "max_output_tokens") return `Without one: ${base.max_output_tokens.toLocaleString()} tokens.`;
    if (f.key === "reasoning_tokens") {
      return `Kept only while it thinks — ${data.fallbacks.reasoning_tokens.toLocaleString()} by default.`;
    }
    if (f.group === "machine" && !data.machine_applies) {
      return "Not sent: this model runs on another computer, whose own settings apply.";
    }
    if (f.key === "kv_cache_type") return `Assumed ${data.fallbacks.kv_cache_type} unless set.`;
    if (f.group === "machine") return "The runtime decides unless set.";
    if (f.key in data.defaults) return `Server default ${shown(data.defaults[f.key])}.`;
    const sent = data.sent_when_unset[f.key];
    if (sent !== undefined && sent !== null) return `Not reported — ${shown(sent)} is sent.`;
    return "Not reported — the runtime's own default applies.";
  }

  if (loadError) {
    return (
      <div className="tune" id={id} role="region" aria-label={`Generation settings for ${name}`}>
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-text">Couldn&apos;t load its settings: {loadError}</span>
          </div>
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="tune" id={id} aria-busy="true">
        <SkeletonLines lines={3} />
      </div>
    );
  }

  const thinkingField = byKey["thinking"];
  const thinkingValue = draft["thinking"] ?? "";
  const defaultThinking = data.untuned.thinking_level ?? data.fallbacks.thinking;

  return (
    <section className="tune" id={id} aria-labelledby={headingId}>
      <header className="tune-head">
        <h4 className="tune-title" id={headingId} ref={title} tabIndex={-1}>
          Tune <span className="mono">{name}</span>
        </h4>
        <p className="field-hint">
          Blank uses the default printed under each field. Saved settings apply from the next call,
          with no restart.
        </p>
      </header>

      <form
        ref={form}
        className="tune-form"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
        noValidate
      >
        {/* Thinking leads: it decides how much of each reply is spent before the answer. */}
        {thinkingField && (
          <fieldset className="tune-group">
            <legend className="tune-legend">Thinking</legend>
            {data.thinking === "none" ? (
              <p className="field-hint">This model answers directly — there is nothing to set.</p>
            ) : data.thinking_options.length === 0 ? (
              <p className="field-hint">{data.source_label} can&apos;t be told how hard this model thinks.</p>
            ) : (
              <>
                <div className="seg" role="group" aria-label="How hard it thinks">
                  <button
                    type="button"
                    className="seg-btn"
                    aria-pressed={thinkingValue === ""}
                    onClick={() => set("thinking", "")}
                  >
                    Default · {THINKING_WORD[defaultThinking] ?? defaultThinking}
                  </button>
                  {data.thinking_options.map((opt) => (
                    <button
                      key={opt}
                      type="button"
                      className="seg-btn"
                      aria-pressed={thinkingValue === opt}
                      onClick={() => set("thinking", opt)}
                    >
                      {THINKING_WORD[opt] ?? opt}
                    </button>
                  ))}
                </div>
                <p className="field-hint">
                  {data.thinking === "levels"
                    ? "This model thinks at a level and can't be switched off, so its default is the lowest."
                    : data.thinking === "always"
                      ? "This model always reasons first, so a budget is kept for it."
                      : thinkingField.help}
                </p>
              </>
            )}
          </fieldset>
        )}

        {GROUPS.map((group) => {
          const own = fields.filter((f) => f.group === group.key && f.key !== "thinking");
          if (own.length === 0) return null;
          return (
            <fieldset key={group.key} className="tune-group">
              <legend className="tune-legend">{group.title}</legend>
              <p className="field-hint tune-note">{group.note}</p>
              <div className="tune-grid">
                {own.map((f) => {
                  const inputId = `${headingId}-${f.key}`;
                  const hintId = `${inputId}-hint`;
                  const helpId = `${inputId}-help`;
                  const errorId = `${inputId}-error`;
                  const unsupported =
                    data.supported[f.key] === false || (f.group === "machine" && !data.machine_applies);
                  const error = touched[f.key] ? errors[f.key] : null;
                  const describedBy = [error ? errorId : null, hintId, helpId].filter(Boolean).join(" ");
                  const common = {
                    id: inputId,
                    disabled: unsupported,
                    readOnly: saving !== null,
                    "aria-describedby": describedBy,
                    "aria-invalid": error ? true : undefined,
                    onBlur: () => setTouched((t) => ({ ...t, [f.key]: true })),
                  };
                  const placeholder =
                    f.key in data.defaults && !unsupported ? shown(data.defaults[f.key]) : undefined;
                  return (
                    <div
                      key={f.key}
                      className="field tune-field"
                      data-wide={f.kind === "stops" || undefined}
                      data-unsupported={unsupported || undefined}
                      title={f.help}
                    >
                      <label htmlFor={inputId}>{f.label}</label>
                      {f.kind === "choice" ? (
                        <select
                          {...common}
                          className="select"
                          value={draft[f.key] ?? ""}
                          onChange={(e) => set(f.key, e.target.value)}
                        >
                          <option value="">Default</option>
                          {f.choices.map((c) => (
                            <option key={c} value={c}>
                              {c}
                            </option>
                          ))}
                        </select>
                      ) : f.kind === "stops" ? (
                        <textarea
                          {...common}
                          className="textarea input-mono"
                          rows={2}
                          placeholder={placeholder ?? "One per line — write a newline as \\n"}
                          value={draft[f.key] ?? ""}
                          onChange={(e) => set(f.key, e.target.value)}
                        />
                      ) : (
                        <input
                          {...common}
                          className="input input-mono"
                          type="text"
                          inputMode={f.kind === "int" ? "numeric" : f.kind === "float" ? "decimal" : undefined}
                          spellCheck={false}
                          placeholder={placeholder ?? (f.kind === "duration" ? "e.g. 10m" : undefined)}
                          autoComplete="off"
                          value={draft[f.key] ?? ""}
                          onChange={(e) => set(f.key, e.target.value)}
                        />
                      )}
                      <span className="field-hint" id={hintId}>
                        {hint(f)}
                      </span>
                      {/* The field's explanation, for everyone the hover title never reaches. */}
                      <span className="sr-only" id={helpId}>
                        {f.help}
                      </span>
                      {error && (
                        <span className="tune-error" id={errorId}>
                          {error}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </fieldset>
          );
        })}

        {invalidCount > 0 && hasErrors && (
          <p className="tune-error" role="alert">
            {invalidCount === 1 ? "One setting needs fixing" : `${invalidCount} settings need fixing`} before
            it can be saved.
          </p>
        )}

        {saveError && (
          <div className="notice notice-bad" role="alert">
            {Icon.alert}
            <div className="notice-body">
              <span className="notice-title">Not saved</span>
              <span className="notice-text">{saveError}</span>
            </div>
          </div>
        )}

        <div className="tune-actions">
          <button
            className="btn btn-sm btn-primary"
            type="submit"
            disabled={!dirty && saving === null}
            aria-disabled={saving !== null || undefined}
          >
            {saving === "save" && <span className="btn-spinner" aria-hidden="true" />}
            {saving === "save" ? "Saving…" : "Save settings"}
          </button>
          {anythingSaved && (
            <button className="btn btn-sm" type="button" onClick={() => saving === null && reset()}>
              {saving === "reset" && <span className="btn-spinner" aria-hidden="true" />}
              Reset to defaults
            </button>
          )}
          <button className="btn btn-sm btn-ghost" type="button" onClick={onClose}>
            Close
          </button>
          <span className="tune-status" role="status">
            {status && (
              <>
                {Icon.check}
                {status}
              </>
            )}
          </span>
        </div>
      </form>
    </section>
  );
}
