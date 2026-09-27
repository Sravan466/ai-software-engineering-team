import type { RuntimeWarning } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * Runtime hygiene, where a runtime is listed: older than a known security fix, or
 * reachable from the network. A warning, never a block — the runtime still works,
 * and the person running it decides. Icon and title carry it, not colour alone.
 */
export default function RuntimeWarnings({ warnings, label }: { warnings?: RuntimeWarning[] | null; label: string }) {
  if (!warnings || warnings.length === 0) return null;
  return (
    <ul className="rt-warnings" aria-label={`Warnings about ${label}`}>
      {warnings.map((w) => (
        <li key={w.kind} className="notice notice-warn rt-warning" data-kind={w.kind}>
          {Icon.alert}
          <div className="notice-body">
            <p className="notice-title">{w.title}</p>
            <p className="notice-text">{w.detail}</p>
            {w.url && (
              <a className="link rt-warning-link" href={w.url} target="_blank" rel="noreferrer">
                Read the advisory{w.ids && w.ids.length === 1 ? ` (${w.ids[0]})` : ""} {Icon.external}
              </a>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}
