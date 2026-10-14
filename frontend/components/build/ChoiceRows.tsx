"use client";

import type { ReactNode } from "react";

/**
 * One question, every answer explained at once (#96).
 *
 * A segmented control showed one hint, the selected option's, so comparing three
 * answers meant clicking all three. These are native radios in a fieldset: the
 * legend names the group for a screen reader, the arrow keys move between options,
 * and each option's description is tied to it with `aria-describedby`.
 *
 * An option can carry something that opens under its own row while it is chosen
 * (Pick a model's select). It opens its own height and folds away again, and while
 * folded it is hidden from the keyboard and assistive tech, not merely clipped.
 */
export type Choice<T extends string> = {
  value: T;
  label: string;
  description: ReactNode;
  /** Opens under this row while it is the chosen one. */
  under?: ReactNode;
};

export default function ChoiceRows<T extends string>({
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
  choices: Choice<T>[];
  value: T;
  onChange: (next: T) => void;
  disabled?: boolean;
}) {
  return (
    <fieldset className="choices" disabled={disabled}>
      <legend className="label choices-legend">{legend}</legend>
      {choices.map((choice) => {
        const on = choice.value === value;
        const describedBy = `${name}-${choice.value}-what`;
        return (
          <div key={choice.value} className="choice" data-on={on || undefined}>
            <label className="choice-row">
              <input
                type="radio"
                name={name}
                value={choice.value}
                checked={on}
                onChange={() => onChange(choice.value)}
                aria-describedby={describedBy}
              />
              <span className="choice-dot" aria-hidden="true" />
              <span className="choice-text">
                <span className="choice-label">{choice.label}</span>
                <span className="choice-what" id={describedBy}>
                  {choice.description}
                </span>
              </span>
            </label>
            {choice.under !== undefined && (
              <div className="choice-under" data-open={on || undefined}>
                <div className="choice-under-inner">{choice.under}</div>
              </div>
            )}
          </div>
        );
      })}
    </fieldset>
  );
}
