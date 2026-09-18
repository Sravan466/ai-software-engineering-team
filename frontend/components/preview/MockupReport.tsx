"use client";

import type { MockupReport as Report } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * What building this mockup found, in one line — and the evidence behind it.
 *
 * A mockup where every check held and one where the model's sections all fell back
 * to the platform's templates used to look identical. The line says which it was:
 * how many pages and sections, how big, how many checks passed, and how many
 * sections the platform had to draw itself because the model's version could not
 * be wired up. The details say which ones, and why.
 */
export default function MockupReport({ report }: { report: Report }) {
  const passed = report.checks.filter((c) => c.ok).length;
  const failed = report.checks.filter((c) => !c.ok);
  const fallback = report.sections.filter((s) => s.status === "fallback");
  const repaired = report.sections.filter((s) => s.status === "repaired");
  const kb = (report.bytes / 1024).toFixed(1);
  const render = report.render;

  return (
    <details className="mk-report">
      <summary>
        <span className="mk-facts">
          <span>
            <b>{report.routes.length}</b> page{report.routes.length === 1 ? "" : "s"}
          </span>
          <span>
            <b>{report.sections.length}</b> sections
          </span>
          <span className="mono">{kb} KB</span>
          <span className={"badge " + (failed.length ? "badge-bad" : "badge-ok")}>
            {failed.length ? Icon.alert : Icon.check}
            {passed}/{report.checks.length} checks
          </span>
          {render.ran && (
            <span className={"badge " + (render.console_errors ? "badge-bad" : "badge-ok")}>
              {render.console_errors ?? 0} console error{render.console_errors === 1 ? "" : "s"}
            </span>
          )}
          {fallback.length > 0 && (
            <span
              className="badge badge-warn"
              title="The model's version of these sections could not be wired up after a repair, so the platform drew them from its own templates."
            >
              {fallback.length} from template
            </span>
          )}
        </span>
        <span className="mk-report-more">
          Details
          <span className="mk-chev" aria-hidden="true">
            {Icon.chevron}
          </span>
        </span>
      </summary>

      <div className="mk-report-body">
        <div className="mk-col">
          <h4 className="label">Checks on the assembled site</h4>
          <ul className="mk-checks">
            {report.checks.map((c) => (
              <li key={c.name} data-ok={c.ok}>
                <span className="mk-check-mark" aria-hidden="true">
                  {c.ok ? Icon.check : Icon.alert}
                </span>
                <span>
                  {c.name}
                  <span className="sr-only">{c.ok ? " — passed" : " — failed"}</span>
                  {c.detail && <span className="mk-check-detail"> · {c.detail}</span>}
                </span>
              </li>
            ))}
            <li data-ok={render.ran ? !render.console_errors : undefined}>
              <span className="mk-check-mark" aria-hidden="true">
                {render.ran ? (render.console_errors ? Icon.alert : Icon.check) : Icon.info}
              </span>
              <span>
                {render.ran
                  ? `Rendered headless: ${render.console_errors ?? 0} console error(s)`
                  : `Not rendered headless — ${render.reason ?? "no browser available"}`}
              </span>
            </li>
          </ul>
        </div>

        <div className="mk-col">
          <h4 className="label">How it was built</h4>
          <dl className="mk-passes">
            <div>
              <dt>Design system</dt>
              <dd>{report.passes.design === "model" ? "Chosen by the model" : "Platform default"}</dd>
            </div>
            <div>
              <dt>Pages & data</dt>
              <dd>{report.passes.plan === "model" ? "Planned by the model" : "Built from the team's design"}</dd>
            </div>
            <div>
              <dt>Sample records</dt>
              <dd>
                {report.collections.map((c) => `${c.rows} ${c.label.toLowerCase()}`).join(", ")}
                {report.passes.seed !== "model" && " (some generated)"}
              </dd>
            </div>
            <div>
              <dt>Model</dt>
              <dd className="mono">
                {report.model} · {report.calls} calls · {report.tokens.toLocaleString()} tok
              </dd>
            </div>
          </dl>
          {(fallback.length > 0 || repaired.length > 0) && (
            <ul className="mk-sections">
              {[...fallback, ...repaired].map((s) => (
                <li key={s.id}>
                  <span className={"badge " + (s.status === "fallback" ? "badge-warn" : "")}>
                    {s.status === "fallback" ? "Template" : "Repaired"}
                  </span>
                  <span className="mk-section-name">{s.label}</span>
                  {s.problems[0] && <span className="mk-check-detail">{s.problems[0]}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </details>
  );
}
