/**
 * Placeholders that occupy the space the real content will take, so a build
 * view doesn't jump around while the first fetch lands.
 */
export function Skeleton({ h = 14, w = "100%", r }: { h?: number; w?: number | string; r?: number }) {
  return (
    <span
      className="skeleton"
      style={{ display: "block", height: h, width: w, borderRadius: r }}
      aria-hidden="true"
    />
  );
}

export function SkeletonLines({ lines = 3 }: { lines?: number }) {
  const widths = ["92%", "78%", "85%", "64%", "88%"];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 9 }} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} w={widths[i % widths.length]} />
      ))}
    </div>
  );
}
