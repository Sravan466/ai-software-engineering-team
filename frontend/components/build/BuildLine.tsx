"use client";

import { Icon } from "@/components/shell/icons";

/**
 * One line above the file tree saying whether this build compiles, and how to run it.
 *
 * Silent about builds from before the check existed (`null`): "not checked" must not
 * read as "checked and fine", and it must not read as a failure either.
 */
export default function BuildLine({
  state,
  problems,
  commands,
}: {
  state: string | null;
  problems: number;
  commands: string[];
}) {
  if (!state) return null;
  const run = commands.slice(0, 2).join(" && ");
  if (state === "failed") {
    return (
      <p className="build-line-strip" data-state="failed">
        {Icon.alert}
        <span>
          {problems} compile problem{problems === 1 ? "" : "s"} left after the repair round — marked in the tree
          below.
        </span>
      </p>
    );
  }
  if (state === "unchecked") {
    return (
      <p className="build-line-strip" data-state="unchecked">
        {Icon.info}
        <span>Some files could not be compiled here, so they are unchecked — not passed.</span>
      </p>
    );
  }
  return (
    <p className="build-line-strip" data-state="ok">
      {Icon.check}
      <span>
        Every file parses and every import resolves.
        {run && (
          <>
            {" "}
            Run it with <code className="mono">{run}</code>.
          </>
        )}
      </span>
    </p>
  );
}
