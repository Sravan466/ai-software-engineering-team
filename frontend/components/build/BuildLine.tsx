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
  runs,
}: {
  state: string | null;
  problems: number;
  commands: string[];
  /** Each built phase's real build (#75). */
  runs?: Record<string, { status: string; summary: string }>;
}) {
  if (!state) return null;
  const run = commands.slice(0, 2).join(" && ");
  const outcomes = Object.values(runs ?? {});
  // It installed and built in a sandbox, not only parsed.
  const built = outcomes.length > 0 && outcomes.every((r) => r.status === "ok");
  if (state === "failed") {
    return (
      <p className="build-line-strip" data-state="failed">
        {Icon.alert}
        <span>
          {problems} {outcomes.some((r) => r.status === "failed") ? "build" : "compile"} problem
          {problems === 1 ? "" : "s"} left after the crew&apos;s fix rounds. They&apos;re marked in the tree below.
        </span>
      </p>
    );
  }
  if (state === "unchecked") {
    return (
      <p className="build-line-strip" data-state="unchecked">
        {Icon.info}
        <span>Some files couldn&apos;t be compiled here, so they&apos;re unchecked.</span>
      </p>
    );
  }
  return (
    <p className="build-line-strip" data-state="ok">
      {Icon.check}
      <span>
        {built
          ? "It installs and builds in a clean sandbox, and every import resolves."
          : "Every file parses and every import resolves."}
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
