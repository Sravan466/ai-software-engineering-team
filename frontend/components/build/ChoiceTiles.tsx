"use client";

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";

/**
 * One question as its answers side by side, and what the chosen one needs under them.
 *
 * The composer's run settings used to be two columns of radio cards, every card
 * carrying its whole description. The columns came out hundreds of pixels apart in
 * height, the grid left a third track empty, and the hosting cap sat boxed inside a
 * tinted card. Here each answer is a tile with one line, enough to compare the three
 * without choosing any, and the rest — the longer explanation, Pick a model's select,
 * the cap — opens in one strip under the row, pointed at the tile it belongs to.
 *
 * Still native radios in a fieldset: the legend names the group, the arrow keys move
 * between answers, and Tab goes from the chosen one into its strip. The strip opens
 * and folds at its own height, keeps what it showed while it folds, and once folded
 * is hidden from the keyboard and assistive tech, not merely clipped. In a narrow
 * panel the tiles stack and the strip moves to sit directly under the chosen one.
 */
export type Tile<T extends string> = {
  value: T;
  label: string;
  /** One line on the tile: what this answer does, next to the others. */
  description: ReactNode;
  /** The rest of what it means, said in the strip once it is chosen. */
  more?: ReactNode;
  /** A field this answer needs, in the strip once it is chosen. */
  under?: ReactNode;
};

export default function ChoiceTiles<T extends string>({
  name,
  legend,
  choices,
  value,
  onChange,
  disabled,
}: {
  /** The radios' shared name, and the prefix of their ids. */
  name: string;
  legend: ReactNode;
  choices: Tile<T>[];
  value: T;
  onChange: (next: T) => void;
  disabled?: boolean;
}) {
  const on = Math.max(0, choices.findIndex((c) => c.value === value));
  const chosen = choices[on];
  const open = Boolean(chosen && (chosen.more || chosen.under));

  // While the strip folds it keeps what it showed, rather than folding up empty.
  const [shown, setShown] = useState<T | null>(open ? value : null);
  useEffect(() => {
    if (open) setShown(value);
  }, [open, value]);
  const at = open ? on : choices.findIndex((c) => c.value === shown);
  const detail = at >= 0 ? choices[at] : undefined;

  const inner = useRef<HTMLDivElement>(null);
  const height = useHeight(inner);
  const moreId = `${name}-more`;

  return (
    <fieldset className="tiles" disabled={disabled}>
      <legend className="label tiles-legend">{legend}</legend>
      <div
        className="tiles-tray"
        style={{ "--count": choices.length, "--at": Math.max(at, 0) } as CSSProperties}
      >
        {choices.map((choice, i) => {
          const isOn = i === on;
          const what = `${name}-${choice.value}-what`;
          return (
            <label
              key={choice.value}
              className="tile"
              data-on={isOn || undefined}
              style={{ "--i": i } as CSSProperties}
            >
              <input
                type="radio"
                name={name}
                value={choice.value}
                checked={isOn}
                onChange={() => onChange(choice.value)}
                aria-describedby={isOn && open && choice.more ? `${what} ${moreId}` : what}
              />
              <span className="tile-head">
                <span className="choice-dot" aria-hidden="true" />
                <span className="tile-label">{choice.label}</span>
              </span>
              <span className="tile-what" id={what}>
                {choice.description}
              </span>
            </label>
          );
        })}

        <div
          className="tiles-detail"
          data-open={open || undefined}
          // Its content's own height while open, so a swap between two answers that
          // both have something to say slides between their heights too.
          style={{ height: open ? (height ?? undefined) : 0 }}
        >
          <div ref={inner} className="tiles-detail-inner">
            {detail && (detail.more || detail.under) && (
              <div key={detail.value} className="tiles-detail-body">
                {detail.more && (
                  <p className="tiles-more" id={open ? moreId : undefined}>
                    {detail.more}
                  </p>
                )}
                {detail.under}
              </div>
            )}
          </div>
        </div>
      </div>
    </fieldset>
  );
}

/**
 * An element's laid-out height, kept current as its content changes. Null where it
 * can't be watched, which leaves the strip at its natural height, unanimated.
 */
function useHeight(ref: RefObject<HTMLElement>): number | null {
  const [height, setHeight] = useState<number | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    setHeight(el.offsetHeight);
    const ro = new ResizeObserver(() => setHeight(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return height;
}
