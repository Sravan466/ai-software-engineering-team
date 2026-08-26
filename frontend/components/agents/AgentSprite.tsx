import { PALETTE, type Persona } from "./personas";

export type SpriteState = "queued" | "working" | "done" | "rejected" | "gate";

/**
 * Renders a 16x16 persona sprite as SVG rects — one element per opaque pixel,
 * no image assets, sharp at any size.
 *
 * Motion is deliberately NOT inline: each persona carries a `motion` signature
 * that agents.css owns, so a character always moves the same way wherever it
 * appears, and every one of those keyframes has a reduced-motion path.
 */
export default function AgentSprite({
  agent,
  size = 48,
  state = "queued",
  className = "",
}: {
  agent: Persona;
  size?: number;
  state?: SpriteState;
  className?: string;
}) {
  const colorFor = (ch: string): string | null => {
    if (ch === ".") return null;
    if (ch === "A") return agent.accent;
    if (ch === "B") return agent.accentDim;
    return PALETTE[ch] ?? null;
  };

  const pixels: JSX.Element[] = [];
  agent.sprite.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const ch = row[x];
      const fill = colorFor(ch);
      if (!fill) {
        x++;
        continue;
      }
      // Merge horizontal runs of one colour into a single rect — a full crew of
      // sprites is ~8x fewer nodes this way, which matters when the roster and
      // the pipeline are both on screen.
      let run = 1;
      while (x + run < row.length && row[x + run] === ch) run++;
      pixels.push(
        <rect key={`${x}-${y}`} x={x} y={y} width={run} height={1} fill={fill} />,
      );
      x += run;
    }
  });

  return (
    <span
      className={`sprite motion-${agent.motion} is-${state} ${className}`}
      style={{ width: size, height: size, ["--agent" as string]: agent.accent }}
      data-agent={agent.codename}
    >
      <svg
        viewBox="0 0 16 16"
        width={size}
        height={size}
        shapeRendering="crispEdges"
        aria-hidden="true"
        focusable="false"
      >
        {pixels}
      </svg>
      {/* Working state only: a visor scanline that reads as "thinking". */}
      <span className="sprite-scan" aria-hidden="true" />
    </span>
  );
}
