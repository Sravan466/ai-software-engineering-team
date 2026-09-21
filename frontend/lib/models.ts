/**
 * Reading a model reference: `source:model`.
 *
 * Every model the backend hands the page is named with the source that serves it —
 * a local runtime's id, or a cloud provider — so the page never has to guess which
 * runtime a bare name meant. Only the first colon separates the two: a model's own
 * name keeps its tag (`qwen2.5:7b`).
 */
import type { LocalSource, LocalStatus, SourceModel } from "@/lib/api";

export function splitSpec(spec: string): { source: string; model: string } {
  const at = spec.indexOf(":");
  return at < 0 ? { source: "", model: spec } : { source: spec.slice(0, at), model: spec.slice(at + 1) };
}

/** The model half, for places where the source is already said nearby. */
export function modelName(spec: string | null | undefined): string {
  return spec ? splitSpec(spec).model : "";
}

/** The listed model a spec names, with what its runtime said about it. */
export function modelFor(local: LocalStatus | null, spec: string | null | undefined): SourceModel | null {
  const source = sourceFor(local, spec);
  return source?.models.find((m) => m.spec === spec) ?? null;
}

export function sourceFor(local: LocalStatus | null, spec: string | null | undefined): LocalSource | null {
  if (!local || !spec) return null;
  const { source } = splitSpec(spec);
  return local.sources.find((s) => s.id === source) ?? null;
}

/** `127.0.0.1:1234` — an address the way a person reads it. */
export function hostOf(url: string): string {
  return url.replace(/^https?:\/\//, "");
}

/** "tried 127.0.0.1:11434, :1234 and :8080" — what was looked at, compactly. */
export function triedText(tried: readonly string[]): string {
  // `localhost` and `127.0.0.1` are one address; saying it twice reads as two.
  const hosts = Array.from(
    new Set(tried.map((t) => hostOf(t).replace(/^localhost(?=:|$)/, "127.0.0.1"))),
  );
  if (hosts.length === 0) return "";
  const shared = hosts.every((h) => h.startsWith("127.0.0.1:"));
  const parts = shared ? [hosts[0], ...hosts.slice(1).map((h) => h.slice("127.0.0.1".length))] : hosts;
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}
