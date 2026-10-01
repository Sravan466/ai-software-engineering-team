"use client";

import { useEffect, useRef, useState } from "react";
import { api, type Connector, type ConnectorPreview } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { ConnectForm, ConnectorMark } from "./parts";

export type ConnectorChoice = { use: string[]; skip: string[] };
export const NO_CHOICE: ConnectorChoice = { use: [], skip: [] };

/**
 * "Connectors this build will use", under the idea box — while the person types.
 *
 * The same answer the build will reach from the idea, with the reason for each
 * ("your idea mentions subscriptions"), so nothing is a surprise. A picked chip
 * switches off with one click; a connected one the matcher missed can be added;
 * one that isn't connected can be connected right here, in place — no modal — or
 * left for the build to ask about. None of it blocks starting the build.
 */
export default function BuildChips({
  idea,
  choice,
  onChange,
  disabled,
}: {
  idea: string;
  choice: ConnectorChoice;
  onChange: (c: ConnectorChoice) => void;
  disabled?: boolean;
}) {
  const [preview, setPreview] = useState<ConnectorPreview | null>(null);
  const [tick, setTick] = useState(0);
  const [adding, setAdding] = useState(false);
  const [sheet, setSheet] = useState<Connector | null>(null);
  const addRef = useRef<HTMLDivElement>(null);

  const text = idea.trim();
  useEffect(() => {
    if (text.length < 3) {
      setPreview(null);
      return;
    }
    let live = true;
    const t = window.setTimeout(() => {
      api
        .previewConnectors({ idea: text, use: choice.use, skip: choice.skip })
        .then((p) => live && setPreview(p))
        .catch(() => live && setPreview(null));
    }, 400);
    return () => {
      live = false;
      window.clearTimeout(t);
    };
  }, [text, choice.use, choice.skip, tick]);

  // The add menu closes on Escape or a click elsewhere.
  useEffect(() => {
    if (!adding) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !addRef.current?.contains(e.target as Node)) setAdding(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [adding]);

  if (!preview) return null;
  const picks = preview.connectors;
  if (!picks.length && !preview.skipped.length && !preview.addable.length) return null;

  const off = (id: string) =>
    onChange({ use: choice.use.filter((u) => u !== id), skip: [...new Set([...choice.skip, id])] });
  const on = (id: string) =>
    onChange({ use: choice.use, skip: choice.skip.filter((s) => s !== id) });
  const add = (id: string) => {
    setAdding(false);
    onChange({ use: [...new Set([...choice.use, id])], skip: choice.skip.filter((s) => s !== id) });
  };

  async function openSheet(id: string) {
    setSheet(await api.getConnector(id).catch(() => null));
  }

  return (
    <div className="cx-build" aria-live="polite">
      <span className="cx-build-title">
        {Icon.plug} Connectors this build will use
      </span>
      <div className="cx-build-row">
        {picks.map((p) =>
          p.connected ? (
            <button
              key={p.id}
              type="button"
              className="cx-pick"
              aria-pressed="true"
              onClick={() => off(p.id)}
              disabled={disabled}
              title={`${p.label} — ${p.reason}. Click to leave it out of this build.`}
            >
              <ConnectorMark id={p.id} label={p.label} size="sm" />
              <span className="cx-pick-check" aria-hidden="true">{Icon.check}</span>
              <span>{p.label}</span>
              <span className="cx-pick-why">{p.reason}</span>
              <span className="sr-only">— click to leave it out</span>
            </button>
          ) : (
            <span key={p.id} className="cx-need" title={p.reason}>
              <ConnectorMark id={p.id} label={p.label} size="sm" />
              <span className="cx-need-label">
                {p.label} <span>· not connected</span>
              </span>
              <button type="button" className="btn btn-sm btn-accent" onClick={() => openSheet(p.id)} disabled={disabled}>
                Connect
              </button>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                onClick={() => off(p.id)}
                disabled={disabled}
                aria-label={`Leave ${p.label} out of this build`}
              >
                Leave out
              </button>
            </span>
          ),
        )}
        {preview.skipped.map((s) => (
          <button
            key={s.id}
            type="button"
            className="cx-pick"
            aria-pressed="false"
            onClick={() => on(s.id)}
            disabled={disabled}
            title={`${s.label} is left out. Click to use it.`}
          >
            <ConnectorMark id={s.id} label={s.label} size="sm" />
            <span>{s.label}</span>
            <span className="sr-only">— left out, click to use it</span>
          </button>
        ))}
        {preview.addable.length > 0 && (
          <div className="cx-add" ref={addRef}>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              aria-expanded={adding}
              aria-haspopup="true"
              onClick={() => setAdding((a) => !a)}
              disabled={disabled}
            >
              {Icon.plus} Add connector
            </button>
            {adding && (
              <ul className="cx-add-menu">
                {preview.addable.map((a) => (
                  <li key={a.id}>
                    <button type="button" onClick={() => add(a.id)}>
                      <ConnectorMark id={a.id} label={a.label} size="sm" />
                      {a.label}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
      {picks.some((p) => !p.connected) && !sheet && (
        <p className="field-hint" style={{ margin: 0 }}>
          Not connected yet? The build asks for it after the architecture — or carries on without it.
        </p>
      )}

      {sheet && (
        <section className="cx-sheet" aria-labelledby="cx-sheet-title">
          <div className="cx-sheet-head">
            <ConnectorMark id={sheet.id} label={sheet.label} size="sm" />
            <h3 id="cx-sheet-title">Connect {sheet.label}</h3>
            <button type="button" className="icon-btn" aria-label="Close" onClick={() => setSheet(null)}>
              {Icon.close}
            </button>
          </div>
          <ConnectForm
            connector={sheet}
            onSave={(values, confirmLive) => api.connect(sheet.id, values, confirmLive)}
            onSaved={() => {
              setSheet(null);
              setTick((n) => n + 1);
            }}
          />
        </section>
      )}
    </div>
  );
}
