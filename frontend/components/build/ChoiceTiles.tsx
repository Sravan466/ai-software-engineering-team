"use client";

import { useLayoutEffect, useRef, type CSSProperties, type ReactNode, type RefObject } from "react";

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
  // A value that isn't among the answers (one stopped being offered, and the page
  // hasn't moved to another yet) checks nothing, as a radio group would: checking
  // the first would show an answer the run isn't using, and one you can't click.
  const on = choices.findIndex((c) => c.value === value);
  const chosen = on >= 0 ? choices[on] : undefined;
  const open = Boolean(chosen && (chosen.more || chosen.under));

  // While the strip folds it keeps what it showed, rather than folding up empty: the
  // last answer it was open for, recorded once that render has committed.
  const shown = useRef<T | null>(null);
  useLayoutEffect(() => {
    if (open) shown.current = value;
  }, [open, value]);
  const at = open ? on : choices.findIndex((c) => c.value === shown.current);
  const detail = at >= 0 ? choices[at] : undefined;

  const strip = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  useStripHeight(strip, inner, open, detail?.value ?? "");
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

        {/* Its height is written by `useStripHeight`, never by React. */}
        <div ref={strip} className="tiles-detail" data-open={open || undefined}>
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
 * The strip's height: its content's while open, so a swap between two answers that
 * both have something to say slides between their heights too; 0 while folded.
 *
 * Written to the element directly rather than through state. A layout effect sets
 * it before paint whenever the strip opens, folds or changes content, so the first
 * frame already heads for the new height; a ResizeObserver keeps it current on
 * reflows (the window narrows, a note appears under the select), and its callback
 * also runs before paint, where a state update would land a frame late and clip
 * the content in between. Unrounded: a whole-pixel height can shave the last pixel.
 */
function useStripHeight(
  strip: RefObject<HTMLElement>,
  inner: RefObject<HTMLElement>,
  open: boolean,
  content: string,
) {
  useLayoutEffect(() => {
    const outer = strip.current;
    const body = inner.current;
    if (!outer || !body) return;
    const fit = () => {
      outer.style.height = open ? `${body.getBoundingClientRect().height}px` : "0px";
    };
    fit();
    if (!open || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(fit);
    ro.observe(body);
    return () => ro.disconnect();
  }, [strip, inner, open, content]);
}
