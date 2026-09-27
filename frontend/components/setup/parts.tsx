"use client";

import { useEffect, useState, type ReactNode } from "react";
import type { Device, OS } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/** Text from the backend's tables, with `backticks` as inline code. Nothing else is
 *  interpreted: the strings are data, not markup. */
export function Rich({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith("`") && part.endsWith("`") && part.length > 2 ? (
          <code key={i} className="su-code">
            {part.slice(1, -1)}
          </code>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

/** A command in a well, with a copy button that says when it worked. */
export function CopyLine({ command, label }: { command: string; label: string }) {
  const [copied, setCopied] = useState<"idle" | "ok" | "failed">("idle");
  useEffect(() => {
    if (copied === "idle") return;
    const t = setTimeout(() => setCopied("idle"), 2200);
    return () => clearTimeout(t);
  }, [copied]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied("ok");
    } catch {
      setCopied("failed");
    }
  }
  return (
    <div className="su-cmd">
      <code className="su-cmd-text" aria-label={label}>
        {command}
      </code>
      <button className="btn btn-sm su-cmd-copy" onClick={copy} aria-label={`Copy: ${label}`}>
        {copied === "ok" ? Icon.check : Icon.copy}
        {copied === "ok" ? "Copied" : copied === "failed" ? "Select and copy" : "Copy"}
      </button>
      <span className="sr-only" aria-live="polite">
        {copied === "ok" ? "Copied to the clipboard." : copied === "failed" ? "Couldn't copy; select the text instead." : ""}
      </span>
    </div>
  );
}

export const OS_LABEL: Record<OS, string> = { macos: "macOS", windows: "Windows", linux: "Linux" };

export function guessOS(): OS {
  if (typeof navigator === "undefined") return "macos";
  const ua = navigator.userAgent;
  if (/Windows/i.test(ua)) return "windows";
  if (/Mac/i.test(ua)) return "macos";
  return "linux";
}

export function OSPicker({ value, onChange, available }: { value: OS; onChange: (os: OS) => void; available?: OS[] }) {
  const all: OS[] = ["macos", "windows", "linux"];
  return (
    <div className="seg" role="radiogroup" aria-label="Your computer's operating system">
      {all.map((os) => {
        const off = available ? !available.includes(os) : false;
        return (
          <button
            key={os}
            role="radio"
            aria-checked={value === os}
            className="seg-btn"
            onClick={() => onChange(os)}
            title={off ? `Not supported on ${OS_LABEL[os]}` : undefined}
          >
            {OS_LABEL[os]}
          </button>
        );
      })}
    </div>
  );
}

/** Ticks every `ms` while mounted, so countdowns and "3m ago" stay true. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

export function ago(iso: string | null, now: number): string {
  if (!iso) return "never";
  const secs = Math.max(0, (now - +new Date(iso)) / 1000);
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  return new Date(iso).toLocaleDateString();
}

export function bytes(n: number | null | undefined): string {
  if (!n) return "—";
  const gib = n / 2 ** 30;
  return gib >= 1 ? `${gib.toFixed(gib >= 10 ? 0 : 1)} GB` : `${Math.round(n / 2 ** 20)} MB`;
}

/** "qwen2.5:7b via Ollama" for a device's chosen model, or its first usable one. */
export function modelLine(d: Device): { model: string; runtime: string } | null {
  const sources = d.hello?.sources ?? [];
  const pick = (spec: string | null) => {
    if (!spec) return null;
    const i = spec.indexOf(":");
    const source = sources.find((s) => s.id === spec.slice(0, i));
    return source ? { model: spec.slice(i + 1), runtime: source.label } : null;
  };
  const chosen = pick(d.chat_model);
  if (chosen) return chosen;
  for (const s of sources) {
    const m = s.models.find((m) => m.kind !== "embedding" && m.is_local);
    if (m) return { model: m.name, runtime: s.label };
  }
  return null;
}

export function StatusDot({ device }: { device: Device }) {
  const cls = device.status === "pending" ? "dot-warn dot-pulse" : device.online ? "dot-ok" : "";
  return <span className={"dot " + cls} aria-hidden="true" />;
}

export function statusText(d: Device, now: number): string {
  if (d.status === "pending") return "Waiting for your approval";
  if (d.online) return "Connected";
  return `Offline · last seen ${ago(d.last_seen_at, now)}`;
}

export function Step({
  n,
  title,
  summary,
  state,
  open,
  onToggle,
  children,
}: {
  n: number;
  title: string;
  summary?: ReactNode;
  state: "done" | "current" | "todo";
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  const id = `step-${n}`;
  return (
    <li className="su-step" data-state={state} data-open={open || undefined}>
      <span className="su-marker" aria-hidden="true">
        {state === "done" ? Icon.check : n}
      </span>
      <div className="su-step-body">
        <h2 className="su-step-h">
          <button className="su-step-toggle" aria-expanded={open} aria-controls={id} onClick={onToggle}>
            <span className="sr-only">Step {n}{state === "done" ? ", done" : ""}: </span>
            <span className="su-step-title">{title}</span>
            {summary && !open && <span className="su-step-summary">{summary}</span>}
            <span className="su-chev" aria-hidden="true">
              {Icon.chevron}
            </span>
          </button>
        </h2>
        <div id={id} className="su-step-panel" hidden={!open}>
          {children}
        </div>
      </div>
    </li>
  );
}
