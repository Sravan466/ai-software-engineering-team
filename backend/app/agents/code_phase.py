"""The code phases: plan the files, then write them a batch at a time, as fenced code (#81).

The Backend and Frontend Engineers used to return a whole backend or frontend as
escaped strings in one JSON object, in one call capped at a few thousand output
tokens. That is the hardest request there is for any model: a reply long enough to
lose its thread partway, code mangled by JSON escaping, and one bad character that
sinks the whole phase. Now each phase:

  1. **plans** — one small JSON call, through the base agent's own repair loop: the
     summary and metadata it always returned, and the file list (`path`, `purpose`,
     `exports`, `imports`), with no code in it;
  2. **writes** — one call per batch, the batch sized from the model's *measured*
     prompt budget and reply ceiling (`model_profile.files_per_call`), never from its
     name. Each file comes back as `### path` and a fenced block. Each call sees the
     plan, the hand-off digests, the name registry and an index of what this phase has
     already written — bodies only where the budget allows;
  3. **checks as files land** — Python and JSON parse in-process per batch, and a
     batch that does not parse is repaired before the next one; imports wait for the
     whole-tree check once the last file is in, so a planned file is never "missing"
     just because it has not been written yet. That check is the phase's one `node` run.

The deliverable is the same `files: [{path, language, purpose, code}]` shape it always
was, so everything downstream reads it unchanged. If the plan comes back with no files
to write, the phase falls back to the one-reply path and says so (`generation.mode`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel

from app.agents import handoff
from app.agents.base import (
    _SKILLS_FRAME,
    _SKILLS_FRAME_COST,
    TASK_OVERRIDES,
    AgentContext,
    AgentResult,
    BaseAgent,
    _clip,
    _merge,
    _pack_skills,
)
from app.build import layout
from app.build.check import BuildCheck, Problem, syntax_problems
from app.core.config import settings
from app.core.constants import BuildStatus, SchemaStatus
from app.core.logging import get_logger
from app.core.reading import CodeBlock, code_blocks, json_object
from app.orchestration import activity, claim
from app.orchestration.charter import violations as charter_violations
from app.router import inflight
from app.router.model_profile import ModelProfile, files_per_call
from app.router.router import router
from app.schemas.agent_outputs import response_schema
from app.schemas.llm import ChatMessage, GenerationOptions, LLMResponse


log = get_logger(__name__)

#: Set by the eval harness (`--generation-mode`): `one` writes a file per call, `batch`
#: lets the budgets decide, `whole` is the old one-JSON reply. None in a running server,
#: where the person's per-role choice and the budgets decide.
MODE_OVERRIDE: Optional[str] = None
MODES = ("one", "batch", "whole")

#: How every write call is told to answer. In the system prompt, so it is identical on
#: every call of the phase and a runtime's prompt cache keeps it.
_WRITE_NOTE = (
    "Earlier phases are summarised as digests below; trust them. You are writing this "
    "phase's files from its plan, a few at a time.\n\n"
)
_WRITE_TAIL = (
    "Answer with the files you are asked for and nothing else. For each one, a line "
    "`### path`, then one fenced code block holding the complete file — runnable, no "
    "placeholders, no \"rest unchanged\". No JSON, no commentary. If a file itself "
    "contains ```, fence it with ```` instead."
)
#: Headings the write prompt is assembled from. `# Write now` is also what a scripted
#: test model looks for to know which files it was asked for.
_WRITE_NOW = "# Write now"
_FIX_ECHO = "# Your last version of {path} (fix it; keep what was right)\n{fence}{lang}\n{code}{nl}{fence}\n"


@dataclass
class _Planned:
    path: str
    purpose: str = ""
    exports: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    #: plan | split (a module a cut-off file was split into) | unplanned
    origin: str = "plan"
    #: Times the file was asked for and did not come back.
    misses: int = 0


@dataclass
class _Written:
    path: str
    code: str
    language: str
    purpose: str
    origin: str = "plan"
    complete: bool = True
    problems: list[Problem] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)

    def faults(self) -> list[str]:
        return [p.text() for p in self.problems] + list(self.stack)


class _Planner(BaseAgent):
    """The plan call: the owning phase's prompt and repair loop, with a plan for a shape.

    Stateless like every agent, so one instance per code phase serves every build.
    """

    def __init__(self, owner: "CodePhaseAgent") -> None:
        self.owner = owner
        self.key = owner.key
        self.title = owner.title
        self.complexity = owner.complexity
        self.role = owner.role
        self.depends_on = owner.depends_on
        self.output_model = owner.plan_model

    def response_schema(self) -> dict:  # the plan's, not the class default's
        return response_schema(self.output_model)

    def task_instruction(self) -> str:
        return self.owner.plan_instruction()


    def task_text(self) -> str:
        return TASK_OVERRIDES.get(f"{self.key}.plan") or self.task_instruction()

    def standing_brief(self, charter=None) -> str:
        return self.owner.standing_brief(charter)

    def own_checks(self, output: dict, ctx: AgentContext) -> tuple[dict, list[str]]:
        return output, self.owner.plan_problems(output, ctx)

    def _build_check(self, ctx: AgentContext, output: dict):
        return None  # a plan has no code to compile


class CodePhaseAgent(BaseAgent):
    """A phase that writes code: planned first, then written in batches (see module)."""

    #: The plan's shape: the deliverable's, with `files` as a list of planned files.
    plan_model: type[BaseModel] = BaseModel

    def __init__(self) -> None:
        self.planner = _Planner(self)

    # ── what each subclass says ──────────────────────────────────────────────
    def plan_instruction(self) -> str:
        raise NotImplementedError

    def write_instruction(self) -> str:
        """The phase's own requirements, repeated under every batch's file list."""
        return ""

    def plan_problems(self, output: dict, ctx: AgentContext) -> list[str]:
        """What is wrong with a plan beyond its shape, as repair-round lines."""
        files = output.get("files") if isinstance(output, dict) else None
        if not files:
            return ["`files` — the plan lists no files to write"]
        if len(files) > settings.code_max_files:
            return [
                f"`files` — {len(files)} files is more than this build may plan "
                f"({settings.code_max_files}); merge the small ones"
            ]
        return []

    # ── the run ──────────────────────────────────────────────────────────────
    def run(self, ctx: AgentContext) -> AgentResult:
        if not settings.code_by_file or MODE_OVERRIDE == "whole":
            result = BaseAgent.run(self, ctx)
            result.handoff = {**(result.handoff or {}), "generation": _whole_record(result, "configured")}
            return result
        return _Run(self, ctx).run()

    def write_system_prompt(self, charter=None, registry=None) -> str:
        return self._standing(charter, registry, _WRITE_NOTE) + _WRITE_TAIL

    def assemble(self, plan: dict, files: list[dict]) -> dict:
        """The deliverable: the plan's fields, with the written files as `files`."""
        out = {k: v for k, v in plan.items() if k != "files"}
        out["files"] = files
        return out


def _whole_record(result: AgentResult, reason: str) -> dict:
    files = [f for f in (result.output or {}).get("files") or [] if isinstance(f, dict)]
    return {
        "mode": "whole",
        "reason": reason,
        "calls": len(result.calls or [result.response]),
        "files_per_call": len(files),
        "files_planned": len(files),
        "files_written": len(files),
        "truncated_replies": result.truncated_replies,
    }


class _Run:
    """One code phase's run for one build. Holds what the shared agent cannot."""

    def __init__(self, agent: CodePhaseAgent, ctx: AgentContext) -> None:
        self.agent = agent
        self.ctx = ctx
        self.calls: list[LLMResponse] = []
        self.truncated = 0
        self.repairs = 0
        self.planned: list[_Planned] = []
        self.written: dict[str, _Written] = {}
        self.unwritten: list[str] = []
        self.left_to_platform: list[str] = []
        self.cap: Optional[int] = None
        self.largest = 0
        self.lengths: list[int] = []

    # ── driving ──────────────────────────────────────────────────────────────
    def run(self) -> AgentResult:
        agent, ctx = self.agent, self.ctx
        agent._pin(ctx)
        activity.begin(agent.key)
        try:
            return self._run()
        finally:
            activity.end()

    def _run(self) -> AgentResult:
        agent, ctx = self.agent, self.ctx
        activity.stage("planning")
        with inflight.agent(f"{agent.title} — planning the files"):
            plan = agent.planner.run(ctx)
        self.plan = plan
        self.calls += plan.calls or [plan.response]
        self.truncated += plan.truncated_replies
        self.planned = self._normalise(plan.output)
        if not self.planned:
            return self._whole("The plan listed no files to write, so the phase wrote its code in one reply.")

        self.profile: ModelProfile = router.profile_for(
            ctx.routing_mode,
            ctx.preferred_model,
            complexity=agent._complexity(ctx),
            role=agent.key,
            pin=ctx.pin_model,
        )
        self.choice = _choice(router.files_per_call_choice(agent.key))
        self.system = agent.write_system_prompt(ctx.charter, agent._registry(ctx))
        self.digests = {
            dep: handoff.digest(dep, ctx.prior_outputs[dep])
            for dep in agent.depends_on
            if isinstance(ctx.prior_outputs.get(dep), dict)
        }
        queue = list(self.planned)
        activity.plan([p.path for p in queue], self._batch_size(len(queue)))

        while queue:
            claim.between_calls()
            n = self._batch_size(len(queue))
            batch, queue = queue[:n], queue[n:]
            queue = self._write(batch) + queue
            # A file the model wrote ahead of its turn is not asked for again.
            queue = [p for p in queue if p.path not in self.written]


        return self._finish()

    def _batch_size(self, remaining: int) -> int:
        n = files_per_call(self.profile, remaining, avg_file_tokens=self._avg(), override=self.choice)
        if self.cap:
            n = min(n, self.cap)
        n = max(1, min(n, remaining))
        activity.per_call(n)
        return n

    def _avg(self) -> Optional[float]:
        """The average file's tokens: the configured guess, refined by every file landed."""
        if not self.lengths:
            return None
        chars = max(settings.approx_chars_per_token, 1.0)
        prior = settings.code_avg_file_tokens
        return (prior + sum(n / chars for n in self.lengths)) / (1 + len(self.lengths))

    # ── one batch ────────────────────────────────────────────────────────────
    def _write(self, batch: list[_Planned]) -> list[_Planned]:
        """Write one batch. Returns the files to ask for again, ahead of the rest."""
        for p in batch:
            activity.file(p.path, "writing")
        self.largest = max(self.largest, len(batch))
        resp = self._call(self._messages(batch), batch, "writing")
        cut = _cut(resp)
        self.truncated += 1 if cut else 0
        landed, partial = self._take(resp, batch)

        again: list[_Planned] = []
        missing = [p for p in batch if p.path not in landed]
        if cut and missing:
            if len(batch) > 1:
                # Half as many next time — never the same request under the same cap.
                self.cap = max(1, len(batch) // 2)
                log.info("%s: a reply was cut off at the output limit; batches of %d now.", self.agent.title, self.cap)
                again = missing
            else:
                landed += self._split(batch[0], partial.get(batch[0].path))
        else:
            for p in missing:
                p.misses += 1
                if p.misses < 2:
                    again.append(p)  # asked for and not returned: once more
                else:
                    self._unwritten(p)
        self._check(landed)
        return again

    def _split(self, planned: _Planned, partial: Optional[CodeBlock]) -> list[str]:
        """One file was too long for one reply: ask once for it as two smaller modules."""
        claim.between_calls()
        resp = self._call(self._messages([planned], split=True), [planned], "writing")
        cut = _cut(resp)
        self.truncated += 1 if cut else 0
        landed, still = self._take(resp, [planned], split_of=planned)
        if planned.path in landed:
            return landed
        cut_short = still.get(planned.path) or partial
        if cut_short is not None and cut_short.code.strip():
            # What arrived is kept, and the compile check says it does not parse — a
            # file the fix loop can finish beats one nothing else knows is missing.
            self._land(planned, cut_short.code, cut_short.language, complete=False)
            return landed + [planned.path]
        self._unwritten(planned)
        return landed

    def _check(self, paths: list[str]) -> None:
        """Parse what landed; send back what does not parse, before the next batch."""
        if not paths:
            return
        for path in paths:
            self._judge(self.written[path])
        failing = [self.written[p] for p in paths if self.written[p].faults()]
        allowed = max(settings.schema_repair_rounds, 0) + (1 if self.ctx.escalate else 0)
        rounds = 0
        while failing and rounds < allowed:
            rounds += 1
            failing = self._repair(failing)
        for path in paths:
            activity.file(path, "failed" if self.written[path].faults() else "ok")

    def _repair(self, failing: list[_Written]) -> list[_Written]:
        """One repair call for `failing`, problems named. Returns what still fails."""
        claim.between_calls()
        self.repairs += 1
        for w in failing:
            activity.file(w.path, "fixing")
        planned = [self._plan_entry(w.path) for w in failing]
        resp = self._call(self._messages(planned, fixing=failing), planned, "fixing")
        self.truncated += 1 if _cut(resp) else 0
        blocks = {path: block for path, block in self._match(resp, planned) if block.complete}
        still: list[_Written] = []
        for w in failing:
            block = blocks.get(w.path)
            if block is not None:
                trial = _Written(w.path, block.code, block.language or w.language, w.purpose, w.origin)
                self._judge(trial)
                # Fewer things wrong wins; a repair that came back worse is not kept.
                if len(trial.faults()) <= len(w.faults()):
                    self.written[w.path] = trial
                    w = trial
            if w.faults():
                still.append(w)
        return still

    # ── reading a reply ──────────────────────────────────────────────────────
    def _take(
        self, resp: LLMResponse, batch: list[_Planned], split_of: Optional[_Planned] = None
    ) -> tuple[list[str], dict[str, CodeBlock]]:
        """Land every complete file in a reply. Returns (paths landed, blocks cut short)."""
        landed: list[str] = []
        partial: dict[str, CodeBlock] = {}
        for path, block in self._match(resp, batch):
            if not block.complete:
                partial[path] = block
                continue
            planned = self._entry(path)
            if planned is None:
                origin = "split" if split_of is not None else "unplanned"
                planned = _Planned(path=path, purpose=f"Split from {split_of.path}" if split_of else "", origin=origin)
                self.planned.append(planned)
                activity.file(path, "writing")
            elif path in self.written and planned not in batch:
                continue  # written already; a second copy is not asked for
            self._land(planned, block.code, block.language)
            landed.append(path)
        return landed, partial

    def _match(self, resp: LLMResponse, batch: list[_Planned]) -> list[tuple[str, CodeBlock]]:
        """Each block in a reply, with the planned path it is for."""
        blocks = code_blocks(resp.text)
        if not blocks:
            blocks = _salvage_json(resp.text)
        unnamed = [b for b in blocks if not b.path]
        asked = [p.path for p in batch]
        out: list[tuple[str, CodeBlock]] = []
        for block in blocks:
            if not block.path:
                continue
            out.append((self._resolve(block.path, asked), block))
        named = {path for path, _ in out}
        # A file asked for and answered without its heading: matched in order.
        for path, block in zip([p for p in asked if p not in named], unnamed):
            out.append((path, block))
        return out

    def _resolve(self, path: str, asked: list[str]) -> str:
        placed = layout.place(self.agent.key, path)
        known = [p.path for p in self.planned]
        if placed in known:
            return placed
        clean = layout.clean(path)
        for pool in (asked, known):
            ends = [p for p in pool if p.endswith("/" + clean) or clean.endswith("/" + layout.relative_to_side(p))]
            if len(ends) == 1:
                return ends[0]
        base = clean.rsplit("/", 1)[-1]
        named = [p for p in asked if p.rsplit("/", 1)[-1] == base]
        return named[0] if len(named) == 1 else placed

    def _land(self, planned: _Planned, code: str, language: str = "", complete: bool = True) -> None:
        from app.core.artifacts import language_of

        # The extension's name for it first, so a file reads `typescript` whether the
        # fence said `tsx` or `ts`; the fence only for an extension nobody maps.
        given = language if language and language not in ("text", "txt", "plaintext") else ""
        self.written[planned.path] = _Written(
            path=planned.path,
            code=code,
            language=language_of(planned.path) or given,

            purpose=planned.purpose,
            origin=planned.origin,
            complete=complete,
        )
        self.lengths.append(len(code))

    def _judge(self, w: _Written) -> None:
        w.problems = syntax_problems(w.path, w.code)
        if not w.complete and not w.problems:
            w.problems = [Problem(w.path, "was cut off at the model's output limit before it ended", "syntax")]
        w.stack = (
            charter_violations(self.ctx.charter, self.agent.key, {"files": [{"path": w.path, "code": w.code}]})
            if settings.enforce_stack_charter
            else []
        )

    def _entry(self, path: str) -> Optional[_Planned]:
        return next((p for p in self.planned if p.path == path), None)

    def _plan_entry(self, path: str) -> _Planned:
        return self._entry(path) or _Planned(path=path)

    def _unwritten(self, planned: _Planned) -> None:
        if planned.path not in self.unwritten:
            self.unwritten.append(planned.path)
        activity.file(planned.path, "missing")

    # ── the call ─────────────────────────────────────────────────────────────
    def _call(self, messages: list[ChatMessage], batch: list[_Planned], doing: str) -> LLMResponse:
        agent, ctx = self.agent, self.ctx
        first = batch[0].path
        number = len([p for p in self.planned if p.path in self.written]) + 1
        total = len(self.planned)
        more = f" and {len(batch) - 1} more" if len(batch) > 1 else ""
        label = f"{agent.title} — {doing} {first}{more} ({min(number, total)} of {total})"
        activity.stage(doing, detail=first, total=total)
        with inflight.agent(label):
            resp = router.complete(
                messages,
                mode=ctx.routing_mode,
                preferred_model=ctx.preferred_model,
                options=GenerationOptions(),
                complexity=agent._complexity(ctx),
                role=agent.key,
                pin=ctx.pin_model,
            )
        self.calls.append(resp)
        return resp

    # ── the prompt ───────────────────────────────────────────────────────────
    def _messages(
        self, batch: list[_Planned], *, split: bool = False, fixing: Optional[list[_Written]] = None
    ) -> list[ChatMessage]:
        """The ask for one batch, held to the prompt budget section by section.

        The system prompt is the same on every call of the phase. The user turn carries,
        most important first: the files to write (always), the last version of a file
        being fixed, the plan, the idea, reviewer feedback, the hand-off digests, the
        index of what is already written, the code of the files this batch imports, and
        the skills — each measured, so the whole never passes the window.
        """
        ctx, budget = self.ctx, self.profile.prompt_char_budget
        sections: dict[str, str] = {}
        left = budget - len(self.system)

        def put(name: str, text: str, force: bool = False) -> bool:
            nonlocal left
            cost = len(text) + 1  # the "\n" each section is joined with
            if not text or (cost > left and not force):
                return False
            sections[name] = text
            left -= cost
            return True

        if not put("write", self._write_now(batch, split, fixing), force=True):
            log.error("%s: the file list alone does not fit this model's window.", self.agent.title)
        for w in fixing or []:
            room = max(int(left * 0.4) // max(len(fixing), 1) - 200, 0)
            put(f"echo:{w.path}", _echo(w, room))

        for compact in (0, 1, 2):
            if put("plan", self._plan_text(batch, compact)):
                break
        if ctx.idea:
            put("idea", f"# Product idea\n{_clip(ctx.idea, min(len(ctx.idea), max(budget // 8, 200), max(left - 200, 0)))}\n")
        if ctx.feedback:
            share = min(len(ctx.feedback), budget // 7, max(left - 200, 0))
            put(
                "feedback",
                "# Reviewer feedback on your previous attempt — address it directly\n"
                f"{_clip(ctx.feedback, share)}\n",
            )
        if self.digests:
            each = max(min(int(budget * settings.handoff_digest_share), left // 2) // len(self.digests), 300)
            for dep, d in self.digests.items():
                for limit in (each, each // 2, 300):
                    if put(f"dep:{dep}", f"# From {dep} (digest)\n```json\n{handoff.fit(d, limit)}\n```\n"):
                        break
        put("written", self._written_index(batch))
        bodies = self._bodies(batch, int(left * 0.4))
        put("bodies", bodies)
        if ctx.skills:
            body, _ = _pack_skills(ctx.skills, left - _SKILLS_FRAME_COST - 1)
            if body:
                put("skills", _SKILLS_FRAME.format(body=body))

        order = ["idea", *[k for k in sections if k.startswith("dep:")], "skills", "feedback", "plan", "written", "bodies"]
        parts = [sections[k] for k in order if k in sections]
        parts.append(sections.get("write", ""))
        parts += [sections[k] for k in sections if k.startswith("echo:")]
        return [
            ChatMessage(role="system", content=self.system),
            ChatMessage(role="user", content="\n".join(p for p in parts if p)),
        ]

    def _write_now(self, batch: list[_Planned], split: bool, fixing: Optional[list[_Written]]) -> str:
        total = len(self.planned)
        done = len([p for p in self.planned if p.path in self.written])
        if fixing:
            head = f"{_WRITE_NOW} — fix {len(fixing)} file{'s' if len(fixing) > 1 else ''}"
        elif len(batch) == 1:
            head = f"{_WRITE_NOW} — file {min(done + 1, total)} of {total}"
        else:
            head = f"{_WRITE_NOW} — files {done + 1}–{min(done + len(batch), total)} of {total}"
        lines = [head]
        broken = {w.path: w for w in fixing or []}
        for p in batch:
            line = f"- `{p.path}`"
            if p.purpose:
                line += f" — {p.purpose}"
            if p.exports:
                line += f". Exports: {', '.join(p.exports[:12])}"
            if p.imports:
                line += f". Imports: {', '.join(p.imports[:8])}"
            lines.append(line)
            for fault in (broken[p.path].faults() if p.path in broken else [])[:6]:
                lines.append(f"  - problem: {fault}")
        if split:
            lines.append(
                f"Your last reply for `{batch[0].path}` was cut off at this model's output limit. "
                "Write it shorter as two files: this one, and a new module beside it that takes "
                "part of its code (this one imports from it). Return both."
            )
        if fixing:
            lines.append("Return each file above in full, with every problem fixed.")
        instruction = TASK_OVERRIDES.get(f"{self.agent.key}.write") or self.agent.write_instruction()
        if instruction:
            lines.append(instruction)
        lines.append("Answer with `### path` and one fenced block per file — nothing else.")
        return "\n".join(lines) + "\n"

    def _plan_text(self, batch: list[_Planned], compact: int) -> str:
        """The plan: whole (0), purposes only outside this batch (1), or paths only (2)."""
        mine = {p.path for p in batch}
        meta = {k: v for k, v in (self.plan.output or {}).items() if k != "files" and k in self.agent.plan_model.model_fields}
        files = []
        for p in self.planned:
            if compact == 2 and p.path not in mine:
                files.append(p.path)
                continue
            entry: dict = {"path": p.path, "purpose": p.purpose}
            if compact == 0 or p.path in mine:
                if p.exports:
                    entry["exports"] = p.exports
                if p.imports:
                    entry["imports"] = p.imports
            files.append(entry)
        if compact:
            meta = {k: meta[k] for k in ("framework", "summary") if k in meta}
        body = json.dumps({**meta, "files": files}, ensure_ascii=False)
        return f"# The plan\n```json\n{body}\n```\n"

    def _written_index(self, batch: list[_Planned]) -> str:
        done = [w for w in self.written.values()]
        if not done:
            return ""
        lines = ["# Written so far — import from these by their real paths; do not write them again"]
        for w in done:
            planned = self._entry(w.path)
            line = f"- `{w.path}`"
            if w.purpose:
                line += f" — {w.purpose}"
            if planned is not None and planned.exports:
                line += f". Exports: {', '.join(planned.exports[:12])}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def _bodies(self, batch: list[_Planned], room: int) -> str:
        """The code of written files this batch imports, whole, while they fit."""
        wanted = [i for p in batch for i in p.imports if i in self.written]
        out: list[str] = []
        spent = 0
        for path in dict.fromkeys(wanted):
            w = self.written[path]
            block = _block(w.path, w.code, w.language)
            if spent + len(block) + 1 > room:
                continue
            out.append(block)
            spent += len(block) + 1
        if not out:
            return ""
        return "# Code this batch imports\n" + "\n".join(out)

    # ── the plan ─────────────────────────────────────────────────────────────
    def _normalise(self, output: dict) -> list[_Planned]:
        """The plan's files at the paths the build will place them, in a writable order.

        Placed as the archive will place them (`backend/…`, `frontend/…`), so the file
        each call is asked for is the file that ships. Files the platform writes itself
        are left to it, and each file comes after the plan files it imports where the
        plan allows that order.
        """
        from app.build.scaffold import platform_owned

        files = [f for f in (output or {}).get("files") or [] if isinstance(f, dict)]
        files = [f for f in files if isinstance(f.get("path"), str) and layout.clean(f["path"])]
        charter = self.ctx.charter
        language = charter.get("language").token if charter is not None and charter.get("language") else None
        placed = layout.Placer(language).place_all(self.agent.key, [(f["path"], "") for f in files])
        where = {layout.clean(f["path"]): p for (p, *_), f in zip(placed, files)}
        out: list[_Planned] = []
        seen: set[str] = set()
        for (path, *_), f in zip(placed, files):
            if not path or path in seen:
                continue
            seen.add(path)
            if platform_owned(path):
                self.left_to_platform.append(path)
                continue
            imports = []
            for item in _strings(f.get("imports")):
                target = where.get(layout.clean(item)) or layout.place(self.agent.key, item)
                if target and target != path and target not in imports:
                    imports.append(target)
            out.append(
                _Planned(
                    path=path,
                    purpose=str(f.get("purpose") or "").strip(),
                    exports=_strings(f.get("exports"))[:20],
                    imports=imports,
                )
            )
        out = out[: max(settings.code_max_files, 1)]
        known = {p.path for p in out}
        for p in out:
            p.imports = [i for i in p.imports if i in known]
        return _ordered(out)

    # ── the deliverable ──────────────────────────────────────────────────────
    def _finish(self) -> AgentResult:
        agent, ctx = self.agent, self.ctx
        activity.stage("checking", detail="", total=len(self.planned))
        output, errors = self._assemble()
        stack = charter_violations(ctx.charter, agent.key, output) if settings.enforce_stack_charter else []
        build = agent._build_check(ctx, output)
        if build is not None and build.status == BuildStatus.FAILED.value and settings.schema_repair_rounds > 0:
            build, output, errors, stack = self._fix_build(build, output, errors, stack)

        notes = [
            Problem(p.path, "was not in the plan; kept", "plan")
            for p in self.planned
            if p.origin == "unplanned"
        ]
        plan = self.plan
        # "Repaired" is about the shape — the plan needing a second try. A file fixed
        # because it did not parse is the compile gate's business, counted in
        # `generation.repairs`, and the deliverable's shape was never wrong.
        repaired = bool(plan.repair_rounds)

        shape = [e for e in errors if e not in stack]
        status = (
            SchemaStatus.INVALID.value
            if shape
            else (SchemaStatus.REPAIRED.value if repaired else SchemaStatus.VALID.value)
        )
        written = [p.path for p in self.planned if p.origin == "plan" and p.path in self.written]
        mode = "batch" if self.largest > 1 else "one"
        record = {
            **(plan.handoff or {}),
            "truncated_replies": self.truncated,
            "generation": {
                "mode": mode,
                "files_per_call": self.largest,
                "calls": len(self.calls),
                "plan_calls": len(plan.calls or [plan.response]),
                "files_planned": len([p for p in self.planned if p.origin == "plan"]),
                "files_written": len(written),
                "truncated_replies": self.truncated,
                "repairs": self.repairs,
                "plan": [
                    {"path": p.path, "purpose": p.purpose, "exports": p.exports, "imports": p.imports, "origin": p.origin}
                    for p in self.planned
                ],
                "unwritten": list(self.unwritten),
                "left_to_platform": list(self.left_to_platform),
                "files": {
                    path: ("failed" if w.faults() else "ok") for path, w in self.written.items()
                },
            },
        }
        return AgentResult(
            output=output,
            content_md=agent.to_markdown(output),
            response=_merge(self.calls),
            schema_status=status,
            schema_note="; ".join(e.replace("`", "") for e in shape[:3]) or None,
            repair_rounds=plan.repair_rounds + self.repairs,
            calls=list(self.calls),
            stack_violations=stack,
            build_status=build.status if build else None,
            build_problems=(build.as_list() if build else []) + [n.as_dict() for n in notes],
            skills_used=list(plan.skills_used),
            handoff=record,
            truncated_replies=self.truncated,
        )

    def _assemble(self) -> tuple[dict, list[str]]:
        files = [
            {"path": w.path, "language": w.language, "purpose": w.purpose or (self._plan_entry(w.path).purpose), "code": w.code}
            for p in self.planned
            for w in [self.written.get(p.path)]
            if w is not None
        ]
        output = self.agent.assemble(self.plan.output or {}, files)
        output, errors = self.agent._validate(output)
        if not errors:
            output, own = self.agent.own_checks(output, self.ctx)
            errors = errors + own
        return output, errors

    def _fix_build(
        self, build: BuildCheck, output: dict, errors: list[str], stack: list[str]
    ) -> tuple[BuildCheck, dict, list[str], list[str]]:
        """The whole-tree check failed: one round on the files it names, then check again.

        Only for what the per-file check could not see — an import, a name, a JavaScript
        parse. A file that already failed its own repair round has had its round.
        """
        named: dict[str, list[Problem]] = {}
        for p in build.problems:
            w = self.written.get(p.path)
            if w is not None and not w.faults():
                named.setdefault(p.path, []).append(p)

        if not named:
            return build, output, errors, stack
        failing: list[_Written] = []
        for path, problems in named.items():
            w = self.written[path]
            failing.append(_Written(w.path, w.code, w.language, w.purpose, w.origin, w.complete, problems, w.stack))
        before = dict(self.written)
        claim.between_calls()
        n = self._batch_size(len(failing))
        for i in range(0, len(failing), n):

            group = failing[i : i + n]
            for w in group:
                activity.file(w.path, "fixing")
            self.repairs += 1
            planned = [self._plan_entry(w.path) for w in group]
            resp = self._call(self._messages(planned, fixing=group), planned, "fixing")
            self.truncated += 1 if _cut(resp) else 0
            for path, block in self._match(resp, planned):
                if block.complete and path in named:
                    trial = _Written(path, block.code, block.language or self.written[path].language,
                                     self.written[path].purpose, self.written[path].origin)
                    self._judge(trial)
                    if not trial.problems:
                        self.written[path] = trial
            claim.between_calls()
        output2, errors2 = self._assemble()
        stack2 = charter_violations(self.ctx.charter, self.agent.key, output2) if settings.enforce_stack_charter else []
        build2 = self.agent._build_check(self.ctx, output2)
        for path in named:
            activity.file(path, "ok" if not any(p.path == path for p in (build2.problems if build2 else [])) else "failed")
        # The second check is kept only when it is no worse than the first.
        if build2 is not None and len(build2.problems) <= len(build.problems):
            return build2, output2, errors2, stack2
        self.written = before
        for path in named:
            activity.file(path, "failed")
        return build, output, errors, stack


    def _whole(self, reason: str) -> AgentResult:
        """No plan to write from: the one-reply path, with the plan's calls counted."""
        log.warning("%s: %s", self.agent.title, reason)
        result = BaseAgent.run(self.agent, self.ctx)
        calls = self.calls + list(result.calls or [result.response])
        result.calls = calls
        result.response = _merge(calls)
        result.truncated_replies += self.truncated
        record = _whole_record(result, reason)
        record["calls"] = len(calls)
        result.handoff = {**(result.handoff or {}), "generation": record}
        return result


# ── helpers ─────────────────────────────────────────────────────────────────
def _choice(value: object) -> object:
    """The eval harness's mode wins over the person's choice; `batch` is automatic."""
    if MODE_OVERRIDE == "one":
        return 1
    if MODE_OVERRIDE == "batch":
        return None
    return value


def _cut(resp: LLMResponse) -> bool:
    return (getattr(resp, "finish_reason", None) or "") == "length"


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _ordered(files: list[_Planned]) -> list[_Planned]:
    """Each file after the plan files it imports, where a cycle allows; else plan order."""
    out: list[_Planned] = []
    done: set[str] = set()
    pending = list(files)
    while pending:
        ready = next((p for p in pending if all(i in done for i in p.imports)), None)
        nxt = ready or pending[0]
        pending.remove(nxt)
        out.append(nxt)
        done.add(nxt.path)
    return out


def _fence(code: str) -> str:
    longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=0)
    return "`" * max(3, longest + 1)


def _block(path: str, code: str, language: str = "") -> str:
    fence = _fence(code)
    nl = "" if code.endswith("\n") else "\n"
    return f"### {path}\n{fence}{language}\n{code}{nl}{fence}\n"


def _echo(w: _Written, room: int) -> str:
    """The broken version of a file, clipped to `room`, for the repair call to correct."""
    if room <= 200:
        return ""
    code = w.code if len(w.code) <= room else w.code[:room] + "\n… [cut here to fit this model's context window]\n"
    fence = _fence(code)
    nl = "" if code.endswith("\n") else "\n"
    return _FIX_ECHO.format(path=w.path, fence=fence, lang=w.language, code=code, nl=nl)


def _salvage_json(text: str) -> list[CodeBlock]:
    """A model that answered with JSON anyway: its files, if it named them with code."""
    found = json_object(text)
    if not isinstance(found, dict):
        return []
    items = found.get("files") if isinstance(found.get("files"), list) else [found]
    out = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            code = item.get("code") if isinstance(item.get("code"), str) else item.get("content")
            if isinstance(code, str) and code.strip():
                out.append(CodeBlock(path=item["path"].strip().lstrip("/"), code=code, language=str(item.get("language") or "")))
    return out
