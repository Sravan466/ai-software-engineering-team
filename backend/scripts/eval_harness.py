"""Run fixed ideas through the whole pipeline and score what comes out.

    cd backend
    .venv/bin/python -m scripts.eval_harness                 # the default ideas, local model
    .venv/bin/python -m scripts.eval_harness --limit 2       # the first two only
    .venv/bin/python -m scripts.eval_harness --ideas my.json # a JSON list of idea strings
    .venv/bin/python -m scripts.eval_harness --score <project-id> [<project-id> ...]

Every run is unattended — no gate stops for a judgement call — and waits for the
mockup the Frontend phase draws. Results go to `data/evals/<timestamp>.json` and a
table on stdout. Run it nightly (cron, CI) against the model you ship with, and a
prompt change stops being guesswork: the numbers from the previous night are the
baseline.

Local runs take minutes per idea. That is the cost of measuring what a real model
does, rather than what a stub does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.constants import ApprovalMode, PipelineStatus  # noqa: E402
from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import PreviewRevision, Project  # noqa: E402
from app.evals.scorecard import score  # noqa: E402
from app.orchestration.runner import runner  # noqa: E402
from app.preview.jobs import jobs  # noqa: E402

#: Five to ten fixed ideas, as the issue asks: different shapes of product, so a
#: change that helps dashboards and breaks marketplaces shows up as exactly that.
DEFAULT_IDEAS = [
    "A tiny link shortener with click analytics",
    "A team standup bot that collects async updates",
    "A recipe box where families share and rate recipes",
    "A small-clinic appointment booking system with reminders",
    "A marketplace for renting camera gear between neighbours",
    "An expense tracker for freelancers with monthly reports",
    "A volunteer shift scheduler for a food bank",
]


def _wait_for_mockup(project_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db = SessionLocal()
        try:
            if db.query(PreviewRevision).filter(PreviewRevision.project_id == project_id).count():
                return
        finally:
            db.close()
        job = jobs.get(project_id)
        if job is not None and not job.running and job.error:
            return
        time.sleep(3)


def run_idea(idea: str, mode: str, model: str | None, mockup_timeout: float) -> str:
    db = SessionLocal()
    try:
        project = Project(
            idea=idea,
            routing_mode=mode,
            preferred_model=model,
            approval_mode=ApprovalMode.UNATTENDED.value,
            require_approval=False,
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        started = time.monotonic()
        runner.continue_run(db, project)
        # A build that stopped at a gate is still worth scoring — the gate is data.
        while project.status == PipelineStatus.AWAITING_APPROVAL.value:
            runner.approve_current(db, project)
            runner.continue_run(db, project)
        print(f"  pipeline {project.status} in {time.monotonic() - started:.0f}s", flush=True)
        _wait_for_mockup(project.id, mockup_timeout)
        return project.id
    finally:
        db.close()


def _row(result: dict) -> str:
    m = result.get("mockup") or {}
    return (
        f"{result['idea'][:34]:34}  schema {result['schema_conformance'] or 0:>4}  "
        f"charter {result['charter_violations']}  files {result['files']:>3}  "
        f"bytes {result['bytes']:>7,}  compiles {result['compiles'] or '-':9}  "
        f"mockup {m.get('routes', '-')}p/{m.get('sections', '-')}s "
        f"{m.get('bytes') or 0:>7,}B checks {m.get('checks_passed', '-')}/{m.get('checks_total', '-')} "
        f"console {m.get('console_errors', '-')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ideas", help="JSON file holding a list of idea strings")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N ideas")
    parser.add_argument("--mode", default="local_only", help="routing mode (local_only | auto | manual)")
    parser.add_argument("--model", default=None, help="preferred provider:model for manual mode")
    parser.add_argument("--mockup-timeout", type=float, default=3600.0, help="seconds to wait for each mockup")
    parser.add_argument("--score", nargs="*", help="score these existing project ids instead of running")
    parser.add_argument("--out", default="./data/evals", help="where results are written")
    args = parser.parse_args()

    init_db()
    if args.score:
        ids = args.score
    else:
        ideas = DEFAULT_IDEAS
        if args.ideas:
            with open(args.ideas, encoding="utf-8") as fh:
                ideas = [str(i) for i in json.load(fh)]
        if args.limit:
            ideas = ideas[: args.limit]
        ids = []
        for n, idea in enumerate(ideas, 1):
            print(f"[{n}/{len(ideas)}] {idea}", flush=True)
            ids.append(run_idea(idea, args.mode, args.model, args.mockup_timeout))

    results = []
    db = SessionLocal()
    try:
        for pid in ids:
            project = db.get(Project, pid)
            if project is not None:
                results.append(score(db, project))
    finally:
        db.close()

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(args.out, f"{stamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"at": stamp, "mode": args.mode, "model": args.model, "results": results}, fh, indent=2)
    for result in results:
        print(_row(result))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
