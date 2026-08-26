/**
 * Renders any string-grid as crisp pixel art. Shared by the agent sprites and
 * the props that dress the crew floor, so everything on screen is built from
 * the same 1px-per-cell material.
 */
export default function PixelArt({
  grid,
  palette,
  width,
  className = "",
  style,
}: {
  grid: string[];
  palette: Record<string, string>;
  width: number;
  className?: string;
  style?: React.CSSProperties;
}) {
  const cols = Math.max(...grid.map((r) => r.length));
  const rows = grid.length;
  const cells: JSX.Element[] = [];

  grid.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const ch = row[x];
      const fill = ch === "." ? null : palette[ch];
      if (!fill) {
        x++;
        continue;
      }
      // Merge horizontal runs of one colour into a single rect.
      let run = 1;
      while (x + run < row.length && row[x + run] === ch) run++;
      cells.push(<rect key={`${x}-${y}`} x={x} y={y} width={run} height={1} fill={fill} />);
      x += run;
    }
  });

  return (
    <svg
      className={className}
      viewBox={`0 0 ${cols} ${rows}`}
      width={width}
      height={(width / cols) * rows}
      shapeRendering="crispEdges"
      style={{
        imageRendering: "pixelated",
        display: "block",
        // Must be inline styles, not attributes: the global `svg { width: 16px }`
        // icon floor in globals.css would otherwise override them.
        width,
        height: (width / cols) * rows,
        ...style,
      }}
      aria-hidden="true"
      focusable="false"
    >
      {cells}
    </svg>
  );
}
