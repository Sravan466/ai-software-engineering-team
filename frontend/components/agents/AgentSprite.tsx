import { PALETTE, type Persona } from "./personas";

export type SpriteState = "queued" | "working" | "done" | "rejected" | "gate";

/**
 * Renders a persona sprite as SVG rects — one element per run of opaque pixels,
 * no image assets, sharp at any size.
 *
 * Three things are deliberately not inline here:
 *
 *   • Motion. Each persona carries a `motion` signature that agents.css owns, so
 *     a character always moves the same way wherever it appears, and every one
 *     of those keyframes has a reduced-motion path.
 *   • Grid size. It is read off the sprite rather than hardcoded, so a character
 *     can be redrawn at a different resolution without touching this file.
 *   • The contact shadow. A figure with nothing under it floats; `ground` opts
 *     into an ellipse tinted with the agent's own colour, which is what makes
 *     the crew read as standing on the floor rather than pasted over it.
 */
export default function AgentSprite({
  agent,
  size = 48,
  state = "queued",
  ground = false,
  className = "",
}: {
  agent: Persona;
  size?: number;
  state?: SpriteState;
  /** Draw a contact shadow beneath the figure. On for anyone standing in a room. */
  ground?: boolean;
  className?: string;
}) {
  const cols = Math.max(...agent.sprite.map((r) => r.length));
  const rows = agent.sprite.length;

  const colorFor = (ch: string): string | null => {
    if (ch === ".") return null;
    if (ch === "A") return agent.accent;
    if (ch === "H") return agent.accentLit;
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
      className={`sprite motion-${agent.motion} is-${state}${ground ? " grounded" : ""} ${className}`}
      style={{
        ["--sprite-size" as string]: `${size}px`,
        ["--agent" as string]: agent.accent,
      }}
      data-agent={agent.codename}
    >
      {ground && <span className="sprite-ground" aria-hidden="true" />}
      <svg
        viewBox={`0 0 ${cols} ${rows}`}
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
