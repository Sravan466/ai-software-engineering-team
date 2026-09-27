"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type Device, type DeviceModelInfo } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import RuntimeWarnings from "@/components/models/RuntimeWarnings";
import { ago, bytes, modelLine, useNow } from "./parts";

const STRUCTURED: Record<string, string> = {
  schema: "Held to a JSON schema",
  grammar: "Held to a grammar",
  json: "Valid JSON, any shape",
  none: "Free text only",
};

type Check = { key: string; label: string; ok: boolean | null; detail: string; fix?: string };

/**
 * Step 5: what the connector found, as a checklist that fills in live —
 * connected → runtimes → models — then the choice of chat and embedding model.
 */
export default function Detected({ devices, onChanged }: { devices: Device[]; onChanged: (d: Device) => void }) {
  const approved = devices.filter((d) => d.status === "approved");
  const [pick, setPick] = useState<string | null>(null);
  const device = approved.find((d) => d.id === pick) ?? approved.find((d) => d.online) ?? approved[0] ?? null;
  const now = useNow(15000);
  const [busy, setBusy] = useState<"" | "refresh" | "chat_model" | "embed_model">("");
  const [error, setError] = useState("");
  const [info, setInfo] = useState<DeviceModelInfo | null>(null);
  const [infoError, setInfoError] = useState("");

  const sources = device?.hello?.sources ?? [];
  const reachable = sources.filter((s) => s.reachable);
  const models = reachable.flatMap((s) => s.models.map((m) => ({ ...m, spec: `${s.id}:${m.name}`, runtime: s.label })));
  const chatModels = models.filter((m) => m.kind !== "embedding" && m.is_local);
  const embedModels = models.filter((m) => m.kind === "embedding");
  const hosted = models.filter((m) => !m.is_local);

  // The capability summary is the computer's own answer about the chosen model.
  const chatSpec = device?.chat_model ?? null;
  useEffect(() => {
    setInfo(null);
    setInfoError("");
    if (!device || !device.online || !chatSpec) return;
    let live = true;
    api
      .deviceModel(device.id, chatSpec)
      .then((i) => live && setInfo(i))
      .catch((e) => live && setInfoError(e?.message || "It didn't answer."));
    return () => {
      live = false;
    };
  }, [device?.id, device?.online, chatSpec]); // eslint-disable-line react-hooks/exhaustive-deps

  const checks: Check[] = useMemo(() => {
    if (!device) return [];
    return [
      {
        key: "connected",
        label: "Connected",
        ok: device.online,
        detail: device.online
          ? `Since ${ago(device.connected_since, now)} · connector ${device.connector_version ?? "?"}`
          : `Not connected · last seen ${ago(device.last_seen_at, now)}`,
        fix: device.online ? undefined : device.outdated ? "#t-outdated" : "#t-connector",
      },
      {
        key: "runtimes",
        label: "Runtimes",
        ok: device.hello ? reachable.length > 0 : null,
        detail: !device.hello
          ? "Waiting for the connector to describe this computer"
          : reachable.length
            ? reachable.map((s) => `${s.label}${s.version ? ` ${s.version}` : ""}`).join(", ")
            : device.hello.unknown.length
              ? "Something answered, but it wasn't recognised"
              : "No runtime is running",
        fix: device.hello && !reachable.length ? (device.hello.unknown.length ? "#t-unknown" : "#t-runtime") : undefined,
      },
      {
        key: "models",
        label: "Models",
        ok: device.hello ? chatModels.length > 0 : null,
        detail: !device.hello
          ? "—"
          : `${chatModels.length} can write${embedModels.length ? ` · ${embedModels.length} for embeddings` : ""}${
              hosted.length ? ` · ${hosted.length} run hosted, not here` : ""
            }`,
        fix: device.hello && reachable.length && !chatModels.length ? "#t-model" : undefined,
      },
    ];
  }, [device, now, reachable, chatModels.length, embedModels.length, hosted.length]);

  if (!device) {
    return (
      <p className="su-p dim">
        Nothing to show yet. Once a computer is approved in step 4, what its connector finds appears here.
      </p>
    );
  }

  async function refresh() {
    if (!device) return;
    setBusy("refresh");
    setError("");
    try {
      onChanged(await api.refreshDevice(device.id));
    } catch (e: any) {
      setError(e?.message || "It didn't answer.");
    } finally {
      setBusy("");
    }
  }

  async function choose(field: "chat_model" | "embed_model", spec: string) {
    if (!device) return;
    setBusy(field);
    setError("");
    try {
      onChanged(await api.updateDevice(device.id, { [field]: spec }));
    } catch (e: any) {
      setError(e?.message || "Couldn't save that.");
    } finally {
      setBusy("");
    }
  }

  const line = modelLine(device);
  const warned = sources.filter((s) => (device.warnings?.[s.id] ?? []).length > 0);
  const kinds = new Set(warned.flatMap((s) => (device.warnings?.[s.id] ?? []).map((w) => w.kind)));
  const allGood = checks.every((c) => c.ok);

  return (
    <div className="su-detected">
      {approved.length > 1 && (
        <label className="field su-which">
          <span className="label">Computer</span>
          <select className="select" value={device.id} onChange={(e) => setPick(e.target.value)}>
            {approved.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
                {d.online ? "" : " (offline)"}
              </option>
            ))}
          </select>
        </label>
      )}

      {allGood && line && (
        <p className="su-connected" role="status">
          <span className="su-connected-mark" aria-hidden="true">
            {Icon.check}
          </span>
          <span>
            Your computer is connected: <span className="mono">{line.model}</span> via {line.runtime}
          </span>
        </p>
      )}

      <ol className="su-checklist" aria-label="What the connector found">
        {checks.map((c) => (
          <li key={c.key} data-ok={c.ok === null ? "wait" : String(c.ok)}>
            <span className="su-check-mark" aria-hidden="true">
              {c.ok ? Icon.check : c.ok === false ? Icon.alert : <span className="btn-spinner" />}
            </span>
            <span className="su-check-label">{c.label}</span>
            <span className="su-check-detail">
              {c.detail}
              {c.fix && (
                <>
                  {" · "}
                  <a className="link" href={c.fix}>
                    How to fix
                  </a>
                </>
              )}
            </span>
          </li>
        ))}
      </ol>

      {warned.length > 0 && (
        <div className="su-hygiene">
          {warned.map((s) => (
            <RuntimeWarnings key={s.id} warnings={device.warnings?.[s.id]} label={s.label} />
          ))}
          <p className="field-hint">
            Builds still run. What to do:{" "}
            {kinds.has("outdated") && (
              <a className="link" href="#t-security">
                update the runtime
              </a>
            )}
            {kinds.size > 1 && " · "}
            {kinds.has("exposed") && (
              <a className="link" href="#t-exposed">
                keep it on this computer
              </a>
            )}
            . The connector warns in its terminal too.
          </p>
        </div>
      )}

      <div className="su-row">
        <button className="btn btn-sm" onClick={refresh} disabled={!device.online || busy !== ""}>
          {busy === "refresh" ? <span className="btn-spinner" aria-hidden="true" /> : Icon.refresh}
          {busy === "refresh" ? "Asking your computer…" : "Check again"}
        </button>
      </div>

      {models.length > 0 && (
        <div className="su-choose">
          <label className="field">
            <span className="label">Chat model — writes the crew’s work</span>
            <select
              className="select input-mono"
              value={device.chat_model ?? ""}
              disabled={busy !== ""}
              onChange={(e) => choose("chat_model", e.target.value)}
            >
              <option value="">Choose a model…</option>
              {chatModels.map((m) => (
                <option key={m.spec} value={m.spec}>
                  {m.name} · {m.runtime}
                  {m.size_bytes ? ` · ${bytes(m.size_bytes)}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="label">Embedding model — searches documents and memory</span>
            <select
              className="select input-mono"
              value={device.embed_model ?? ""}
              disabled={busy !== ""}
              onChange={(e) => choose("embed_model", e.target.value)}
            >
              <option value="">None — documents and memory aren’t searched</option>
              {embedModels.map((m) => (
                <option key={m.spec} value={m.spec}>
                  {m.name} · {m.runtime}
                </option>
              ))}
            </select>
            {!embedModels.length && (
              <span className="field-hint">No embedding model found. Step 2 says how to add one; builds work without it.</span>
            )}
          </label>
        </div>
      )}

      {device.chat_model && (
        <dl className="su-caps" aria-label="What the chosen model can do">
          <div>
            <dt>Context window</dt>
            <dd className="mono">
              {info?.context_window ? `${info.context_window.toLocaleString()} tokens` : infoError ? "—" : device.online ? "…" : "—"}
            </dd>
          </div>
          <div>
            <dt>Structured output</dt>
            <dd>{info?.structured_output ? STRUCTURED[info.structured_output] ?? info.structured_output : "—"}</dd>
          </div>
          <div>
            <dt>Size</dt>
            <dd className="mono">
              {[info?.parameter_label, info?.quantization].filter(Boolean).join(" · ") || "—"}
            </dd>
          </div>
          <div>
            <dt>Memory on this computer</dt>
            <dd className="mono">{bytes(device.hello?.ram_bytes)}</dd>
          </div>
        </dl>
      )}
      {infoError && <p className="field-hint">Couldn’t read the model’s details: {infoError}</p>}
      {error && (
        <p className="su-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
