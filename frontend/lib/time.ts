/**
 * Server times. The API sends them with a UTC offset; an older row without one is
 * UTC too, and is read that way rather than as local time.
 */
export function serverTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(t) ? null : t;
}

/** Compact relative time ("now", "3m", "3h", "2d"), for lists of builds. */
export function timeAgo(iso: string): string {
  const then = serverTime(iso);
  if (then === null) return "";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 60) return "now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d`;
  return new Date(then).toLocaleDateString();
}
