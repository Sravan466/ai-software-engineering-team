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

Each later call is told what every written file *really* exports (#101), read from its
code as it lands, and the plan entry is rewritten to match: a file planned with a
default export and written with a named one is imported as named from then on.

The deliverable is the same `files: [{path, language, purpose, code}]` shape it always
was, so everything downstream reads it unchanged. If the plan comes back with no files
to write, the phase falls back to the one-reply path and says so (`generation.mode`).
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from pydantic import BaseModel

from app.agents import handoff
from app.agents.base import (
    RevisionRefused,
    feedback_frame,
    feedback_text,
    window_too_small,
    _MEMORY_FRAME,
    _RAG_FRAME,
    _SKILLS_FRAME,
    _SKILLS_FRAME_COST,
    _TRUNCATED,
    TASK_OVERRIDES,
    AgentContext,
    AgentResult,
    BaseAgent,
    _clip,
    _cut_off as _cut,
    _merge,
    _pack_skills,
)
from app.build import exports as module_exports
from app.build import fixscope, layout, placeholders, wiring
from app.build.check import BuildCheck, Problem, file_problems, resolve
from app.core.artifacts import NOTE_KIND
from app.core.config import settings
from app.core.constants import BuildStatus, Phase, SchemaStatus
from app.core.logging import get_logger
from app.core.reading import CodeBlock, code_blocks, json_object
from app.orchestration import activity, claim, crew_chat
from app.orchestration.charter import violations as charter_violations
from app.router import inflight
from app.router.base import WindowTooSmall
from app.router.fitting import send_fitting
from app.router.model_profile import BATCH_REPLY_SHARE, ModelProfile, files_per_call, reply_floor, reply_gives_way
from app.router.router import router
from app.schemas.agent_outputs import EditPlan, response_schema
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
#: A change asked for on the app preview (#78): the file as it is, to change in place.
_EDIT_ECHO = "# The current version of {path} (change only what is asked)\n{fence}{lang}\n{code}{nl}{fence}\n"
#: The share of a fix call's free prompt the failing files' echoes may take (#101).
_FIX_ECHO_SHARE = 0.4
#: ...in a fix round (#103), where the files being fixed are the call: the plan, the
#: digests and the idea get what they leave.
_FIX_ROUND_ECHO_SHARE = 0.6


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
    #: The exports the plan promised, before the code said what it really exports (#101).
    promised: list[str] = field(default_factory=list)
    #: Imports the plan listed as files of its own that no plan file is at (#119).
    dropped: list[str] = field(default_factory=list)


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
    #: What the code really exports (#101): read as it lands, then the checker's own
    #: answer once the tree is checked. None until read; a read that failed stays None.
    exports: Optional[module_exports.Exports] = None
    read: bool = False

    def real_exports(self) -> Optional[module_exports.Exports]:
        """What the code exports — None for a file cut off before it ended, whose
        exports are only the ones that made it in before the cut."""
        if not self.complete:
            return None
        if not self.read:
            self.exports, self.read = module_exports.read(self.path, self.code), True
        return self.exports

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

    def _build_check(self, ctx: AgentContext, output: dict, previous=None, cap: bool = True):
        return None  # a plan has no code to compile

    def merge_edit(self, base: dict, raw: object) -> dict:
        # The plan is a new object, not an edit of the deliverable it is shown.
        return raw if isinstance(raw, dict) else {}


#: How a code phase plans a change (#79): only the files it touches.
_EDIT_PLAN_TASK = (
    "Plan this change to your part of the app. List ONLY the files you will add or "
    "change: an existing path to change that file, a new path to add one, each with what "
    "changes in it. Put files to remove in `deleted`. Files you don't list stay exactly "
    "as they are. Keep the change as small as the request allows."
)


class _EditPlanner(_Planner):
    """The plan call for a change (#79): the owner's prompt, an edit plan for a shape."""

    def __init__(self, owner: "CodePhaseAgent") -> None:
        super().__init__(owner)
        self.output_model = EditPlan

    def task_instruction(self) -> str:
        return _EDIT_PLAN_TASK

    def task_text(self) -> str:
        return TASK_OVERRIDES.get(f"{self.key}.edit") or _EDIT_PLAN_TASK

    def edit_task_text(self) -> str:
        return self.task_text()

    def own_checks(self, output: dict, ctx: AgentContext) -> tuple[dict, list[str]]:
        return output, []  # changing nothing is an answer


class CodePhaseAgent(BaseAgent):
    """A phase that writes code: planned first, then written in batches (see module)."""

    #: The plan's shape: the deliverable's, with `files` as a list of planned files.
    plan_model: type[BaseModel] = BaseModel
    #: A fix round edits this phase's files in place (#103) rather than re-running it.
    fixes_in_place = True

    def __init__(self) -> None:
        self.planner = _Planner(self)
        self.edit_planner = _EditPlanner(self)

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
        from app.build.scaffold import platform_owned

        # Read exactly as `_normalise` reads it (#119): a plan the gate passes reaches the
        # writer with this wiring. A package wires nothing, and nor does a file the
        # platform writes, which the writer never sees.
        listed = [w for w in _plan_wiring(self.key, files, _language(getattr(ctx, "charter", None)))
                  if w.path.endswith(_SOURCE_EXT)]
        if len(listed) >= 3 and not any(
            t.endswith(_SOURCE_EXT) for w in listed if not platform_owned(w.path) for t in w.imports
        ):
            # The plan never says which file uses which (#103): the entry file is then
            # written first and blind, guessing names the files after it never export.
            entry = next((str(w.plan["path"]) for w in listed if _layer(w.path) == _ENTRY), None)
            return [
                "`files` — no file lists its `imports`. Give each file the plan paths it imports"
                + (f", `{entry}` most of all: it uses the others" if entry else "")
            ]
        return []

    # ── the run ──────────────────────────────────────────────────────────────
    def run(self, ctx: AgentContext) -> AgentResult:
        if ctx.fixing and isinstance(ctx.base_output, dict) and ctx.base_output.get("files"):
            # A fix round (#103): the files with problems, edited; nothing planned again.
            # File by file whatever the setting, as a change is.
            return _Run(self, ctx).fix()
        if ctx.editing:
            # A change request (#79): the files as they are, changed where asked. File
            # by file whatever the setting, because that is the only way to send back
            # three files without writing the other twenty again.
            return _Run(self, ctx).change()
        if not settings.code_by_file or MODE_OVERRIDE == "whole":
            result = BaseAgent.run(self, ctx)
            result.handoff = {**(result.handoff or {}), "generation": _whole_record(result, "configured")}
            return result
        return _Run(self, ctx).run()

    def write_system_prompt(self, charter=None, registry=None) -> str:
        return self._standing(charter, registry, _WRITE_NOTE) + _WRITE_TAIL

    def _ask(self, ctx: AgentContext, output: dict, ask: dict) -> tuple[dict, list[LLMResponse]]:
        """"Change this element" from the app preview (#78): the per-file write call,
        for the one file that renders it, with the file as it is and the change asked."""
        return _Run(self, ctx).edit(output, ask)

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
        #: The notes to the crew the build held when they were last taken (#102): what the
        #: graph node saw at the phase's start, so a note sent while it planned is caught.
        self.notes_seen = ctx.asked_seen
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
        #: A change request (#79): every file as it was, the ones this run changes, and
        #: every path a reply landed. Empty on a first build.
        self.existing: dict[str, str] = {}
        self.changing: set[str] = set()
        self.landed: set[str] = set()
        self.edits: list[_Planned] = []
        self.edit_summary = ""
        #: path -> what the change asks of that file.
        self.asks: dict[str, str] = {}
        self.change_mode = False
        #: Files a change left as they were: too long to show, or the reply cut off.
        self.too_long: list[str] = []
        self.cut_off: list[str] = []
        #: ...or every rewrite of it dropped much of the file (#101).
        self.lost: list[str] = []
        #: What the change asks, in the person's words: a request to remove code may
        #: shrink a file as much as it likes.
        self.asked = ""
        #: The reply ceiling the last prompt was built against when its instructions
        #: needed some of the reply's room (#101); None: the model's own.
        self.reply_cap: Optional[int] = None
        #: A fix round (#103): the files it edits are in `edits`, every file as it was in
        #: `existing`. Stored path -> placed path, for the checker's problems.
        self.fix_mode = False
        self.place_of: dict[str, str] = {}
        #: A new file the round writes -> the problems of the files that import it.
        self.needed_by: dict[str, list[Problem]] = {}
        #: ...and what each of those files' import lines takes from it (#119).
        self.wants: dict[str, list[wiring.Wanted]] = {}
        #: Every path the build has, placed — what a new file's own imports must reach.
        self.tree: set[str] = set()
        #: Imports pointed at the file they meant, with no model (#119).
        self.repointed: list[dict] = []
        #: Edited files put back as they were: they came back with more problems.
        self.reverted: list[str] = []
        #: Files asked for in a fix call and not sent back.
        self.unanswered: list[str] = []
        #: Whether the last prompt built carried the person's note (#103), and how many of
        #: the round's calls did and didn't.
        self._person_put = False
        self.person_calls = {"sent": 0, "dropped": 0}
        #: Each edited file -> every file its fix call wrote: reverted together (#103).
        self.called_with: dict[str, frozenset] = {}
        #: The attempt being fixed, checked without the prompt's caps: what "worse" is
        #: measured against. Placed path -> problems; and the paths that didn't parse.
        self.before: dict[str, int] = {}
        self.unparsed: set[str] = set()
        #: Whether the attempt before could be checked: without that, nothing is "worse".
        self.compared = False
        #: What the platform mended itself, without a model (#103): `wiring.Fix.as_dict()`.
        self.mended: list[dict] = []
        #: Every file as the round's own calls left it, before the platform's mends.
        self.edited: dict[str, str] = {}

    # ── driving ──────────────────────────────────────────────────────────────
    def run(self) -> AgentResult:
        agent, ctx = self.agent, self.ctx
        agent._pin(ctx)
        board = activity.begin(agent.key)
        try:
            result = self._run()
        except BaseException:
            activity.end(board, finished=False)
            raise
        activity.end(board)
        return result

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
        router.note_write_overhead(agent.key, len(self.system))
        self.digests = {
            dep: handoff.digest(dep, ctx.prior_outputs[dep])
            for dep in agent.depends_on
            if isinstance(ctx.prior_outputs.get(dep), dict)
        }
        queue = list(self.planned)
        # The plan's own summary is the build page's "here's what I'll write" (#86).
        summary = plan.output.get("summary") if isinstance(plan.output, dict) else None
        activity.plan([p.path for p in queue], self._batch_size(len(queue)), note=summary if isinstance(summary, str) else "")

        while queue:
            self._between_files()
            n = self._batch_size(len(queue))
            batch, queue = queue[:n], queue[n:]
            queue = self._write(batch) + queue
            # A file the model wrote ahead of its turn is not asked for again.
            queue = [p for p in queue if p.path not in self.written]


        return self._finish()

    def edit(self, output: dict, ask: dict) -> tuple[dict, list[LLMResponse]]:
        """One file changed as the person asked on the app preview (#78).

        Every other file stays as it is: the crew is shown the file, the lines the
        element sits on, and the change, and writes that one file back whole. It is
        parsed (and repaired once if it doesn't) like any file this phase writes.
        """
        agent, ctx = self.agent, self.ctx
        path = str(ask.get("path") or "")
        files = [f for f in output.get("files") or [] if isinstance(f, dict) and isinstance(f.get("path"), str)]
        if not any(f["path"] == path for f in files):
            raise RevisionRefused(f"{path} isn't one of the files {agent.title} wrote.")
        from types import SimpleNamespace

        for f in files:
            code = f.get("code") if isinstance(f.get("code"), str) else str(f.get("content") or "")
            self.planned.append(_Planned(path=f["path"], purpose=str(f.get("purpose") or "")))
            self.written[f["path"]] = _Written(f["path"], code, str(f.get("language") or ""), str(f.get("purpose") or ""))
        self.plan = SimpleNamespace(output={k: v for k, v in output.items() if k != "files"}, repair_rounds=0,
                                    calls=[], response=None, handoff={}, skills_used=[])
        agent._pin(ctx)
        self._one_file_calls()
        lines = ask.get("lines") or []
        where = f"lines {lines[0]}–{lines[1]}" if len(lines) == 2 and lines[0] != lines[1] else (
            f"line {lines[0]}" if lines else "this file"
        )
        what = str(ask.get("what") or "the selected element")
        request = f"In {where} ({what}): {str(ask.get('instruction') or '').strip()}"
        if isinstance(ask.get("content"), str) and ask["content"].strip():
            # The file as the app preview was built from it — the lines it names are
            # lines of this text (the platform may have added a directive at the top).
            self.written[path].code = ask["content"]
        # The file as it is, to hold the rewrite to (#101): one element changed is not
        # half the file gone.
        self.existing = {path: self.written[path].code}
        self.changing = {path}
        self.asked = request
        # Named again in any repair round, so a fix of the rewrite still makes the change.
        self.asks[path] = request
        planned = self._entry(path)
        current = self.written[path]
        asked = _Written(path, current.code, current.language, current.purpose, problems=[Problem(path, request, "edit")])
        board = activity.begin(agent.key)
        try:
            activity.plan([path], 1, note=request)
            claim.between_calls()
            resp = self._call(lambda: self._messages([planned], editing=asked), [planned], "changing")
            self.truncated += 1 if _cut(resp) else 0
            landed, _partial = self._take(resp, [planned])
            if path not in landed:
                raise RevisionRefused(
                    "The crew didn't send the file back in full, so nothing was changed. Try again, or say it differently."
                )
            self._check([path])
            if self.written[path].code == self.existing[path]:
                raise RevisionRefused(
                    "The crew sent the file back unchanged, so nothing was changed. Try again, or say it differently."
                )
            if self.written[path].faults():
                parses = not any(p.kind == "syntax" for p in self.written[path].problems)
                raise RevisionRefused(
                    ("The crew's version of the file isn't finished, so nothing was changed: " if parses
                     else "The crew's version of the file doesn't parse, so nothing was changed: ")
                    + "; ".join(self.written[path].faults()[:2])
                )
        except BaseException:
            activity.end(board, finished=False)
            raise
        activity.end(board)
        out_files = []
        for f in files:
            entry = dict(f)
            if f["path"] == path:
                entry["code"] = self.written[path].code
                entry.pop("content", None)
            out_files.append(entry)
        return {**output, "files": out_files}, list(self.calls)

    # ── a change request (#79) ───────────────────────────────────────────────
    def change(self) -> AgentResult:
        """Change this phase's files as a change request asks — and only those.

        One small plan call names the files to add, change or delete; each is then
        written by the per-file call, a file being changed shown to the model whole
        first. Every file the plan doesn't name is kept byte for byte. What comes out is
        checked as any attempt is: parsed, compiled as a tree, built for real.
        """
        agent, ctx = self.agent, self.ctx
        agent._pin(ctx)
        board = activity.begin(agent.key)
        try:
            result = self._change()
        except BaseException:
            activity.end(board, finished=False)
            raise
        activity.end(board)
        return result

    def _change(self) -> AgentResult:
        from types import SimpleNamespace

        from app.build.scaffold import platform_owned

        agent, ctx = self.agent, self.ctx
        self.change_mode = True
        self.asked = ctx.change
        base = ctx.base_output or {}
        self._load(base)

        activity.stage("planning")
        with inflight.agent(f"{agent.title} — planning the change"):
            plan = agent.edit_planner.run(ctx)
        self.calls += plan.calls or [plan.response]
        self.truncated += plan.truncated_replies
        out = plan.output if isinstance(plan.output, dict) else {}
        self.edit_summary = str(out.get("summary") or "").strip()[:300]
        # The deliverable keeps its own fields — its summary, endpoints, pages; the
        # change is to its files.
        self.plan = SimpleNamespace(
            output={k: v for k, v in base.items() if k != "files"},
            repair_rounds=plan.repair_rounds,
            calls=plan.calls or [plan.response],
            response=plan.response,
            handoff=plan.handoff or {},
            skills_used=plan.skills_used,
        )
        self.profile = router.profile_for(
            ctx.routing_mode, ctx.preferred_model, complexity=agent._complexity(ctx), role=agent.key, pin=ctx.pin_model,
        )
        known = {layout.place(agent.key, p): p for p in self.existing}

        deleted: list[str] = []
        for item in _strings(out.get("deleted")):
            found = self._existing(item, known)
            if found and found not in deleted:
                deleted.append(found)
        for path in deleted:
            self.planned = [p for p in self.planned if p.path != path]
            self.written.pop(path, None)

        too_long = self.too_long
        room = int(self.profile.prompt_char_budget * 0.6)
        asked_imports: dict[str, list[str]] = {}
        for item in out.get("files") or []:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not layout.clean(item["path"]):
                continue
            found = self._existing(item["path"], known)
            path = found or layout.place(agent.key, item["path"])
            if not path or path in deleted or any(e.path == path for e in self.edits):
                continue
            if not found and platform_owned(path):
                self.left_to_platform.append(path)
                continue
            what = str(item.get("purpose") or "").strip()
            exports = _strings(item.get("exports"))[:20]
            if found:
                if len(self.existing[found]) + 400 > room:
                    # Whole or not at all: a file clipped to fit would be written back
                    # without its end. It stays as it is, and the record says why.
                    too_long.append(found)
                    continue
                entry = self._entry(found)
                entry.exports = exports or entry.exports
                self.asks[found] = what
            else:
                entry = _Planned(path=path, purpose=what, exports=exports)
                self.planned.append(entry)
            asked_imports[entry.path] = _strings(item.get("imports"))
            self.edits.append(entry)
        known_paths = {p.path for p in self.planned}
        for e in self.edits:
            # Read by the plan's own rule (#119), once every file the change adds is known.
            imports: list[str] = []
            for name in asked_imports.get(e.path, []):
                target = _plan_import(e.path, name, known_paths, lambda p: layout.place(agent.key, p)) or self._existing(name, known)
                if target and target != e.path and target in known_paths and target not in imports:
                    imports.append(target)
            e.imports = imports or [i for i in e.imports if i in known_paths]
        self.changing = {e.path for e in self.edits if e.path in self.existing}

        self._one_file_calls(profile=False)
        activity.plan([e.path for e in self.edits], 1, note=self.edit_summary or ctx.change.splitlines()[0][:200])
        queue = list(self.edits)
        while queue:
            self._between_files()
            batch, queue = queue[:1], queue[1:]
            try:
                queue = self._write(batch) + queue
            except _NoRoom as e:
                too_long.append(e.path)
                activity.file(e.path, "missing")
            queue = [p for p in queue if p.path not in self.landed and p.path not in self.cut_off]
        for path in self.lost:
            activity.file(path, "missing")

        result = self._finish()
        changed = sorted(
            p for p in self.changing if p in self.written and self.written[p].code != self.existing.get(p)
        )
        added = sorted(p.path for p in self.planned if p.path not in self.existing and p.path in self.written)
        generation = dict((result.handoff or {}).get("generation") or {})
        generation.update({"mode": "edit", "files_planned": len(self.edits)})
        result.handoff = {
            **(result.handoff or {}),
            "generation": generation,
            "change": {
                "summary": self.edit_summary,
                "changed": changed,
                "added": added,
                "deleted": deleted,
                # Asked for and not changed: the model sent it back as it was, or never.
                "unchanged": sorted(
                    p for p in self.changing
                    if p not in changed and p not in too_long and p not in self.cut_off and p not in self.lost
                ),
                "too_long": too_long,
                "cut_off": list(self.cut_off),
                "lost_code": list(self.lost),
                "kept": len([p for p in self.existing if p not in changed and p not in deleted]),
            },
        }
        return result

    def _one_file_calls(self, profile: bool = True) -> None:
        """What an edit's calls are built from (#78, #79, #103): the model's budgets, one
        file a call, the system prompt every call shares, the hand-off digests."""
        agent, ctx = self.agent, self.ctx
        if profile:
            self.profile = router.profile_for(
                ctx.routing_mode, ctx.preferred_model, complexity=agent._complexity(ctx), role=agent.key,
                pin=ctx.pin_model,
            )
        self.choice = 1
        self.system = agent.write_system_prompt(ctx.charter, agent._registry(ctx))
        router.note_write_overhead(agent.key, len(self.system))
        self.digests = {
            dep: handoff.digest(dep, ctx.prior_outputs[dep])
            for dep in agent.depends_on
            if isinstance(ctx.prior_outputs.get(dep), dict)
        }

    def _load(self, base: dict) -> None:
        """The deliverable an edit starts from (#79, #103): every file kept as it is,
        until an edit says otherwise."""
        for f in base.get("files") or []:
            if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not f["path"].strip():
                continue
            code = f.get("code") if isinstance(f.get("code"), str) else str(f.get("content") or "")
            purpose = str(f.get("purpose") or "")
            self.planned.append(_Planned(path=f["path"], purpose=purpose, origin="kept"))
            self.written[f["path"]] = _Written(f["path"], code, str(f.get("language") or ""), purpose, origin="kept")
        self.existing = {p: w.code for p, w in self.written.items()}

    # ── a fix round (#103) ───────────────────────────────────────────────────
    def fix(self) -> AgentResult:
        """Fix what the checker found, in the files it names, and only those.

        Nothing is planned again: the file list, the purposes and the deliverable's
        other fields stay as they are. Each file with a problem is shown whole with its
        problems, and so is the file a problem names, so an importer and the module it
        disagrees with are decided in one call where both fit. Each is written back
        whole, then parsed and repaired like any file this phase writes. Every other file
        is kept byte for byte. A file too long to show whole stays as it is and its
        problem is carried to the next round; an edit that leaves its file with more
        problems than it had is put back.
        """
        agent, ctx = self.agent, self.ctx
        agent._pin(ctx)
        board = activity.begin(agent.key)
        try:
            result = self._fix()
        except BaseException:
            activity.end(board, finished=False)
            raise
        activity.end(board)
        return result

    def _fix(self) -> AgentResult:
        from types import SimpleNamespace

        from app.build import fixscope
        from app.build.check import phase_tree

        agent, ctx = self.agent, self.ctx
        self.fix_mode = True
        base = ctx.base_output or {}
        self._load(base)
        # Every rewrite is judged as an edit of the file it replaces: code it loses is a
        # problem, as it is for a change (#101).
        self.changing = set(self.existing)

        # The checker reports placed paths; the deliverable may store them otherwise.
        language = _language(ctx.charter)
        placed = layout.Placer(language).place_all(agent.key, [(p.strip().lstrip("/"), "") for p in self.written])
        self.place_of = {stored: where for (where, *_), stored in zip(placed, self.written)}
        stored_at = {where: stored for stored, where in self.place_of.items()}
        own = {self.place_of[p]: w.code for p, w in self.written.items()}
        tree, _mine = phase_tree(ctx.prior_outputs, agent.key, base, ctx.charter)
        self.tree = set(tree) | set(own)
        # A stack note names a file as the phase stored it (`server/index.js`); the
        # checker's problems name where it's placed (`backend/index.js`).
        placed_as = {layout.clean(k): v for k, v in self.place_of.items()}

        def placed(p: dict) -> dict:
            path = p.get("path") or (fixscope.stack_path(str(p.get("message") or "")) if p.get("kind") == "stack" else None)
            return {**p, "path": placed_as.get(layout.clean(str(path or "")), path)}

        problems = [placed(p) for p in ctx.fixing]
        found = fixscope.scope(problems, own, tree)
        # The attempt as it was, every problem counted: the prompt's caps (five a file,
        # twenty-four in all) would make a file past the cut look clean before the edit.
        before = agent._build_check(ctx, base, cap=False)
        self.compared = before is not None and before.status != BuildStatus.UNCHECKED.value
        self.before, self.unparsed = _counts(before)

        self.plan = SimpleNamespace(
            output={k: v for k, v in base.items() if k != "files"},
            repair_rounds=0,
            calls=[],
            response=None,
            handoff={},
            skills_used=[],
        )
        self._one_file_calls()
        # An import whose path repeats a folder (#119), pointed at the file it meant with
        # no model; that file is written below when it doesn't exist yet.
        for where, spec, target in found.repoint:
            self._repoint(stored_at.get(where), where, spec, target, own)

        touch: dict[str, list[Problem]] = {}
        for where in found.touch:
            if where in stored_at:
                touch[stored_at[where]] = list(found.touch[where])
        unlocated = list(found.unlocated)
        located = 0
        if unlocated:
            located = len(self.calls)
            for path, problems in self._locate(unlocated, base, stored_at).items():
                touch.setdefault(path, []).extend(problems)
            located = len(self.calls) - located

        too_long = self.too_long
        for path, problems in touch.items():
            w = self.written[path]
            w.problems = problems
            entry = self._entry(path)
            entry.imports = [
                stored_at[i] for i in fixscope.imports_of(self.place_of[path], w.code, own) if i in stored_at
            ]
            if not self._fix_fits([w]):
                # Whole or not at all: a file clipped to fit would come back without its
                # end. It stays as it is, and its problem is carried.
                too_long.append(path)
                continue
            self.edits.append(entry)
        fixing = [self.written[e.path] for e in self.edits]
        news: list[_Planned] = []
        for where, problems in found.new.items():
            if where in stored_at or self._entry(where) is not None or fixscope.repeats(where):
                continue  # a path that repeats a folder sequence is never written (#119)
            importers = ", ".join(dict.fromkeys(f"`{p.path}`" for p in problems))
            # What every file that imports it takes from it, read off its import line
            # (#119): the names it must export, so the plan and the index list them too.
            wants = wiring.wanted_by(where, own)
            entry = _Planned(
                path=where, purpose=f"Imported by {importers}; it doesn't exist yet", origin="plan",
                exports=list(dict.fromkeys(label for w in wants for label in w.labels()))[:20],
            )
            self.planned.append(entry)
            self.edits.append(entry)
            self.needed_by[where] = problems
            self.wants[where] = wants
            news.append(entry)

        sent = len(ctx.fixing)
        activity.plan(
            [e.path for e in self.edits], 1,
            note=f"Fixing {sent} problem{'' if sent == 1 else 's'} in {len(self.edits)} file{'' if len(self.edits) == 1 else 's'}",
        )
        # A missing module beside the file that imports it, in one call where that file
        # fits whole (#119): the module written with the names it must export — or, if it
        # shouldn't exist, the import changed instead.
        paired: dict[str, list[_Planned]] = {}
        for importer, new in found.new_pairs:
            stored, entry = stored_at.get(importer), self._entry(new)
            if stored is None or entry is None or entry not in news or stored in self.too_long:
                continue
            w = self.written[stored]
            missing = [p for p in found.new[new] if p.path == importer]
            trial = _Written(w.path, w.code, w.language, w.purpose, w.origin,
                             problems=(list(touch.get(stored, [])) if stored not in paired else w.problems) + missing)
            if not self._fix_fits([trial]):
                continue
            w.problems = trial.problems
            paired.setdefault(stored, []).append(entry)
            if self._entry(stored) not in self.edits:
                self.edits.append(self._entry(stored))
            touch.setdefault(stored, [])
        # Modules nobody wrote first: the files that import them are then edited
        # against their real exports, rather than told the module doesn't exist.
        queue = [e for e in news if not any(e in entries for entries in paired.values())]
        while queue:
            claim.between_calls()
            batch, queue = queue[:1], queue[1:]
            queue = self._write(batch) + queue
            queue = [p for p in queue if p.path not in self.written]
        for stored, entries in paired.items():
            claim.between_calls()
            self._fix_pair(self.written[stored], entries)
        fixing = [w for w in fixing if w.path not in paired]
        pairs = [(stored_at.get(a), stored_at.get(b)) for a, b in found.pairs]
        for group in self._fix_groups(fixing, pairs):
            claim.between_calls()
            self._fix_group(group)

        result = self._finish()
        changed = sorted(p for p in self.existing if p in self.edited and self.edited[p] != self.existing[p])
        added = sorted(p.path for p in self.planned if p.path not in self.existing and p.path in self.written)
        generation = dict((result.handoff or {}).get("generation") or {})
        generation.update(
            {"mode": "fix", "files_planned": len(self.planned), "files_written": len([p for p in self.planned if p.path in self.written])}
        )
        result.handoff = {
            **(result.handoff or {}),
            "deps": [{"phase": d, "digest": True, "full": "digest_only", "omitted": ["*"]} for d in self.digests],
            "generation": generation,
            "fix": {
                "problems": sent,
                "files": [e.path for e in self.edits],
                "changed": changed,
                "added": added,
                # Asked for and sent back as it was, or not sent back at all.
                "unchanged": sorted(
                    p for p in touch
                    if p not in changed and p not in too_long and p not in self.cut_off
                    and p not in self.lost and p not in self.reverted
                ),
                "too_long": list(too_long),
                "cut_off": list(self.cut_off),
                "lost_code": list(self.lost),
                "reverted": list(self.reverted),
                "unanswered": list(self.unanswered),
                # Imports pointed at the file they meant, with no model (#119).
                "repointed": list(self.repointed),
                # Each new file (#119): the names its importers asked for, what it really
                # exports, and what it still doesn't — carried to the checker, not hidden.
                "new_exports": {
                    path: {
                        "asked": (self._entry(path).promised or self._entry(path).exports) if self._entry(path) else [],
                        "exported": (real.labels() if real is not None else None),
                        "missing": wiring.unexported(wants, real, self.written[path].code),
                    }
                    for path, wants in self.wants.items() if wants and path in self.written
                    for real in [self.written[path].real_exports()]
                },
                # The person's note (#103): "unsent" when the round made no call at all,
                # "dropped" when no call had room for it, "partly" when only some did.
                "person": (
                    "unsent" if not sum(self.person_calls.values())
                    else "dropped" if not self.person_calls["sent"]
                    else "partly" if self.person_calls["dropped"]
                    else "sent"
                ) if self.ctx.person_note else None,
                "kept": len([p for p in self.existing if p not in changed]),
                # Fix and write calls, the planner's aside: 0 when every file was too long
                # to show whole, and the round tried nothing.
                "calls": len(self.calls) - located,
            },
        }
        return result

    def _locate(self, problems: list[dict], base: dict, stored_at: dict[str, str]) -> dict[str, list[Problem]]:
        """The files to fix for problems no path or message placed: one small call
        through the change planner, which names files and writes no code."""
        from dataclasses import replace

        agent = self.agent
        text = (
            "Fix these problems the checker found. It couldn't tell which file each one is in, "
            "so list the files that need to change:\n"
            + "\n".join(f"- {str(p.get('message') or '').strip()[:600]}" for p in problems)
        )
        child = replace(self.ctx, change=text, base_output=base, likely_files=(), fixing=())
        activity.stage("planning")
        with inflight.agent(f"{agent.title} — finding the files to fix"):
            plan = agent.edit_planner.run(child)
        self.calls += plan.calls or [plan.response]
        self.truncated += plan.truncated_replies
        self.plan.calls = list(plan.calls or [plan.response])
        self.plan.repair_rounds = plan.repair_rounds
        out = plan.output if isinstance(plan.output, dict) else {}
        # Placed as the round placed them, renamed roots and all.
        known = {**{layout.place(agent.key, p): p for p in self.existing}, **stored_at}
        found: dict[str, list[Problem]] = {}
        for item in out.get("files") or []:
            path = self._existing(item.get("path") or "", known) if isinstance(item, dict) else None
            if path and path not in found:
                found[path] = [
                    Problem(path, str(p.get("message") or "has a problem"), str(p.get("kind") or "build"))
                    for p in problems
                ]
        return found

    def _fix_groups(self, fixing: list[_Written], pairs: list[tuple]) -> list[list[_Written]]:
        """The fix calls: each importer with the module it disagrees with, in one call
        where they fit together; every other file on its own."""
        parent = {w.path: w.path for w in fixing}

        def root(path: str) -> str:
            while parent[path] != path:
                parent[path] = parent[parent[path]]
                path = parent[path]
            return path

        for a, b in pairs:
            if a in parent and b in parent:
                parent[root(a)] = root(b)
        # Each module beside the files that import it, so the calls a large group is cut
        # into still keep a pair together where they can.
        order: list[str] = []
        for a, b in pairs:
            for path in (b, a):
                if path in parent and path not in order:
                    order.append(path)
        order += [w.path for w in fixing if w.path not in order]
        by_path = {w.path: w for w in fixing}
        together: dict[str, list[_Written]] = {}
        for path in order:
            together.setdefault(root(path), []).append(by_path[path])
        return [group for files in together.values() for group in self._repair_groups(files)]

    def _fix_group(self, group: list[_Written]) -> None:
        """One fix call: the files shown whole with their problems, written back whole.
        A group the prompt it is sent in can't hold whole — rebuilt for a model with a
        smaller window — goes one file at a time; a file that can't is left as it is."""
        for w in group:
            activity.file(w.path, "fixing")
        planned = [self._plan_entry(w.path) for w in group]
        try:
            resp = self._call(lambda: self._messages(planned, fixing=group), planned, "fixing")
        except _NoRoom:
            if len(group) > 1:
                for w in group:
                    self._fix_group([w])
                return
            self.too_long.append(group[0].path)
            activity.file(group[0].path, "missing")
            return
        cut = _cut(resp)
        self.truncated += 1 if cut else 0
        blocks = {path: block for path, block in self._match(resp, planned) if block.complete}
        landed: list[str] = []
        for w in group:
            block = blocks.get(w.path)
            if block is None:
                # Not sent back whole: it stays as it was — a cut-off copy would replace
                # code other files import — and its problems are carried.
                (self.cut_off if cut else self.unanswered).append(w.path)
                activity.file(w.path, "missing")
                continue
            self._land(self._plan_entry(w.path), block.code, block.language)
            landed.append(w.path)
        for path in landed:
            self.called_with[path] = frozenset(landed)
        self._check(landed)

    def _fix_pair(self, w: _Written, news: list[_Planned]) -> None:
        """One call for a file and the modules it imports that don't exist (#119): the
        modules written with the names its import lines take — or, if one shouldn't
        exist, the import changed instead. The file is judged as any edit is."""
        for path in [w.path] + [n.path for n in news]:
            activity.file(path, "fixing" if path == w.path else "writing")
        planned = [self._plan_entry(w.path)] + news
        try:
            resp = self._call(lambda: self._messages(planned, fixing=[w]), planned, "fixing")
        except _NoRoom:
            self.too_long.append(w.path)
            activity.file(w.path, "missing")
            for n in news:
                self._unwritten(n)
            return
        cut = _cut(resp)
        self.truncated += 1 if cut else 0
        blocks = {path: block for path, block in self._match(resp, planned) if block.complete}
        landed: list[str] = []
        for entry in planned:
            block = blocks.get(entry.path)
            if block is not None:
                self._land(entry, block.code, block.language)
                landed.append(entry.path)
        if not landed:
            (self.cut_off if cut else self.unanswered).append(w.path)
            activity.file(w.path, "missing")
        for n in news:
            if n.path not in self.written:
                activity.file(n.path, "missing")  # the import was changed instead, or not
        for path in landed:
            self.called_with[path] = frozenset(landed)
        self._check(landed)

    def _repoint(self, stored: Optional[str], where: str, spec: str, target: str, own: dict[str, str]) -> None:
        """`stored`'s import of `spec` pointed at `target` instead, with no model (#119)."""
        w = self.written.get(stored) if stored else None
        if w is None:
            return
        python = where.endswith(".py")
        if python:
            new = wiring._python_relative(where, target)  # noqa: SLF001 - one rule
        else:
            ext = posixpath.splitext(spec)[1]
            new = wiring._relative(where, target, ext if ext in _JS else "")  # noqa: SLF001
        changed = wiring._repoint(w.code, spec, new, python) if new and new != spec else None  # noqa: SLF001
        if changed is None:
            return
        self.written[stored] = _Written(stored, changed, w.language, w.purpose, w.origin)
        own[where] = changed
        self.repointed.append({"path": stored, "from": spec, "to": new})
        log.info("%s: pointed %s's import `%s` at `%s` (the path repeated a folder).", self.agent.title, stored, spec, new)

    def _keep_no_worse(
        self, build: Optional[BuildCheck], output: dict, errors: list[str], stack: list[str]
    ) -> tuple[Optional[BuildCheck], dict, list[str], list[str]]:
        """Put back every edit that left the build worse (#103), as SWE-agent's edit
        command does: a file with more problems than it was sent with, or a module
        whose edit broke a file nobody touched. Every file its call wrote goes back with
        it — half of an importer and its module would disagree again — and the tree is
        checked again until nothing kept is worse. Counted without the prompt's caps.

        A file that didn't parse and now does is never worse: a parse error hides the
        rest of a file's problems, and fixing it shows them."""
        from app.build import fixscope

        if build is None or build.status == BuildStatus.UNCHECKED.value or not self.compared:
            return build, output, errors, stack
        added = [p for p in self.written if p not in self.existing]
        if not added and all(self.written.get(p) is None or self.written[p].code == code for p, code in self.existing.items()):
            return build, output, errors, stack  # nothing edited: nothing to compare
        reverted_any = False
        for _ in range(len(self.called_with) + 1):
            check = self.agent._build_check(self.ctx, output, cap=False)
            if check is None or check.status == BuildStatus.UNCHECKED.value:
                break
            after, unparsed = _counts(check)
            edited = {
                self.place_of.get(path, path): path for path in self.existing
                if path in self.written and self.written[path].code != self.existing[path]
            }
            own = {self.place_of.get(p, p): w.code for p, w in self.written.items()}
            new = {p for p in self.written if p not in self.existing}
            # A module the round wrote that doesn't parse goes: the import it answered is
            # missing again, which is what it was before.
            dropped = [p for p in new if p in unparsed]
            for path in dropped:
                self.written.pop(path)
                self.planned = [p for p in self.planned if p.path != path]
                self.reverted.append(path)
                activity.file(path, "failed")
            worse: list[str] = []
            # A file that parsed and doesn't now is worse whatever the count says: a parse
            # error hides the rest of a file's problems.
            broke_parse = [place for place in unparsed if place not in self.unparsed]
            for place in dict.fromkeys([*after, *broke_parse]):
                if place in new:
                    continue  # a module the round wrote has no "before"; one that doesn't parse went above
                n = after.get(place, 0)
                if place not in broke_parse and (
                    n <= self.before.get(place, 0) or (place in self.unparsed and place not in unparsed)
                ):
                    continue
                if place in edited:
                    worse.append(edited[place])
                    continue
                # A file nobody edited, worse now: the edited module its problems name broke
                # it — or, where no message names one, the edited files it imports; or, if
                # it imports none of them, every edit, since one of them did.
                causes = {
                    fixscope.named(p.path, p.message, self.place_of.values())
                    for p in check.problems if p.path == place
                } & set(edited)
                if not causes and place in own:
                    causes = set(fixscope.imports_of(place, own[place], own)) & set(edited)
                worse += [edited[c] for c in (causes or edited)]
            undo = [path for w in dict.fromkeys(worse) for path in sorted(self.called_with.get(w, {w}))]
            undo = [path for path in dict.fromkeys(undo) if path in self.existing and self.written[path].code != self.existing[path]]
            if not undo and not dropped:
                break
            for path in undo:
                w = self.written[path]
                self.written[path] = _Written(path, self.existing[path], w.language, w.purpose, w.origin)
                if path not in self.reverted:
                    self.reverted.append(path)
                activity.file(path, "failed")
            if undo or dropped:
                log.info("%s: put back %d edited file(s) and dropped %d new one(s) that left the build worse: %s",
                         self.agent.title, len(undo), len(dropped), ", ".join(undo + dropped))
            output, errors = self._assemble()
            reverted_any = True
        if not reverted_any:
            return build, output, errors, stack
        stack = charter_violations(self.ctx.charter, self.agent.key, output) if settings.enforce_stack_charter else []
        build = self.agent._build_check(self.ctx, output)
        self._take_exports(build)
        return build, output, errors, stack

    def _tally(self, output: dict) -> tuple[Optional[int], set[str]]:
        """(problems in the tree, counted uncapped; the files that don't parse) — or None
        when the check couldn't run, which says nothing either way."""
        check = self.agent._build_check(self.ctx, output, cap=False)
        if check is None or check.status == BuildStatus.UNCHECKED.value:
            return None, set()
        counts, unparsed = _counts(check)
        return sum(counts.values()), unparsed

    def _mend(
        self, build: Optional[BuildCheck], output: dict, errors: list[str], stack: list[str]
    ) -> tuple[Optional[BuildCheck], dict, list[str], list[str]]:
        """The platform's own wiring fixes (#103), before any model round: an import path
        one folder off, a default import of a named export or the other way round, a
        misspelt import kept under its old name, and TypeScript's own not-exported fix
        (`app.build.wiring`). Up to two passes, since a corrected path can show a name the
        module doesn't export. A fix is kept only when the tree then has fewer problems —
        counted uncapped — and every file that parsed still parses; the record says what
        was changed."""
        from app.build import wiring
        from app.build.check import CODEFIX_CODES, code_fixes, phase_tree

        agent, ctx = self.agent, self.ctx
        if not settings.platform_wiring_fixes or build is None or build.status != BuildStatus.FAILED.value:
            return build, output, errors, stack
        current: Optional[int] = None
        broken: set[str] = set()
        for _ in range(2):
            stored_at = {self.place_of.get(p, p): p for p in self.written}
            tree, mine = phase_tree(ctx.prior_outputs, agent.key, output, ctx.charter)
            own = {p: tree[p] for p in mine if p in stored_at}
            mended, made = wiring.mend(build.problems, own, tree.keys(), build.exports or {})
            if any(p.code in CODEFIX_CODES for p in build.problems):
                fixes = code_fixes({**tree, **mended}, [p.path for p in build.problems if p.code in CODEFIX_CODES])
                mended, more = wiring.apply_code_fixes(mended, fixes)
                made += more
            if not made:
                break
            if current is None:
                current, broken = self._tally(output)
            if current is None:
                break  # nothing to compare against: nothing is kept
            changed = [p for p, code in mended.items() if code != own.get(p)]
            # Everything at once first; file by file only when that didn't leave fewer
            # problems — so one wrong answer can't sink the right ones, and the usual
            # case costs one check.
            kept: set[str] = set()
            for trial in [changed] + ([[p] for p in changed] if len(changed) > 1 else []):
                was = {stored_at[p]: self.written[stored_at[p]] for p in trial}
                for place in trial:
                    w = self.written[stored_at[place]]
                    self.written[stored_at[place]] = replace(w, code=mended[place], exports=None, read=False)
                trial_output, trial_errors = self._assemble()
                total, now_broken = self._tally(trial_output)
                if total is not None and total < current and not (now_broken - broken):
                    current, output, errors, broken = total, trial_output, trial_errors, now_broken
                    kept.update(trial)
                    if trial is changed:
                        break
                else:
                    self.written.update(was)
            for place in kept:
                self._judge(self.written[stored_at[place]])  # its own record, on its new code
            mine_made = [f for f in made if f.path in kept]
            if len(mine_made) < len(made):
                log.info("%s: %d of the platform's wiring fixes didn't leave fewer problems; not kept.",
                         agent.title, len(made) - len(mine_made))
            if not kept:
                break
            stack = charter_violations(ctx.charter, agent.key, output) if settings.enforce_stack_charter else []
            build = agent._build_check(ctx, output)
            self._take_exports(build)
            self.mended += [f.as_dict() for f in mine_made]
            still = {p.path for p in (build.problems if build is not None else [])}
            for f in mine_made:
                activity.file(stored_at.get(f.path, f.path), "failed" if f.path in still else "ok")
            log.info("%s: the platform mended %d import(s) itself, without a model: %s", agent.title, len(mine_made),
                     "; ".join(f"{f.path}: {f.note}" for f in mine_made))
            if build is None or build.status != BuildStatus.FAILED.value:
                break
        return build, output, errors, stack

    def _existing(self, path: str, known: dict[str, str]) -> Optional[str]:
        """The file a change names, as it is stored, or None for a new one."""
        if path in self.existing:
            return path
        placed = layout.place(self.agent.key, path)
        if placed in known:
            return known[placed]
        clean = layout.clean(path)
        ends = [p for p in self.existing if p.endswith("/" + clean)]
        return ends[0] if len(ends) == 1 else None

    def _batch_size(self, remaining: int) -> int:
        n = files_per_call(
            self.profile, remaining, avg_file_tokens=self._avg(), override=self.choice, overhead=len(self.system)
        )
        if self.cap:
            n = min(n, self.cap)
        n = max(1, min(n, remaining))
        activity.per_call(n)
        return n

    def _avg(self) -> Optional[float]:
        """The average file's tokens: the configured guess, refined by every file landed."""
        if not self.lengths:
            return None
        chars = self.profile.ratio if getattr(self, "profile", None) is not None else max(settings.approx_chars_per_token, 1.0)
        prior = settings.code_avg_file_tokens
        return (prior + sum(n / chars for n in self.lengths)) / (1 + len(self.lengths))

    # ── one batch ────────────────────────────────────────────────────────────
    def _write(self, batch: list[_Planned]) -> list[_Planned]:
        """Write one batch. Returns the files to ask for again, ahead of the rest."""
        for p in batch:
            activity.file(p.path, "writing")
        self.largest = max(self.largest, len(batch))
        resp = self._call(lambda: self._messages(batch), batch, "writing")
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
            elif self.change_mode and batch[0].path in self.changing:
                # A change (#79) to a file that exists: splitting it would write it anew
                # from nothing, and a cut-off copy would replace working code. It stays
                # as it was, and the change's record says why.
                self.cut_off.append(batch[0].path)
                activity.file(batch[0].path, "missing")
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
        resp = self._call(lambda: self._messages([planned], split=True), [planned], "writing")
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
        # A file cut off at the output limit is not asked for again whole: under the
        # same limit that can only be cut again. The compile gate names it instead.
        failing = [self.written[p] for p in paths if self.written[p].faults() and self.written[p].complete]
        allowed = max(settings.schema_repair_rounds, 0) + (1 if self.ctx.escalate else 0)
        rounds = 0
        while failing and rounds < allowed:
            rounds += 1
            failing = [w for group in self._repair_groups(failing) for w in self._repair(group)]
        for path in paths:
            w = self.written[path]
            if (self.change_mode or self.fix_mode) and any(placeholders.is_lost_code(p) for p in w.problems):
                # Every rewrite dropped much of a file that works (#101): it stays as it
                # was, as a cut-off one does, and the change's record says why.
                self.written[path] = _Written(path, self.existing[path], w.language, w.purpose, w.origin)
                self._judge(self.written[path])
                self._agree(self._entry(path), self.written[path].real_exports())
                if path not in self.lost:
                    self.lost.append(path)
                continue
            activity.file(path, "failed" if w.faults() else "ok")

    def _repair_groups(self, failing: list[_Written]) -> list[list[_Written]]:
        """`failing` in groups one repair call can each show whole (#101), in order:
        their echoes within the pool `_messages` gives them beside the real ask, and
        their files within the reply's share. A batch's failing files sent together
        would each be clipped to a slice of that room, and the model would rebuild
        their missing ends from nothing."""
        groups: list[list[_Written]] = [[]]
        for w in failing:
            trial = groups[-1] + [w]
            if groups[-1] and not self._fix_fits(trial):
                groups.append([w])
            else:
                groups[-1] = trial
        return groups

    def _fix_fits(self, group: list[_Written]) -> bool:
        ask = self._write_now([self._plan_entry(w.path) for w in group], False, group)
        pool = int(max(self.profile.prompt_char_budget - len(self.system) - len(ask) - 1, 0) * self._echo_share())
        reply = int(self.profile.max_output_tokens * BATCH_REPLY_SHARE * self.profile.ratio)
        sources = [len(self._echo_source(w)) for w in group]
        return sum(n + 200 for n in sources) <= pool and sum(sources) <= reply

    def _echo_share(self) -> float:
        return _FIX_ROUND_ECHO_SHARE if self.fix_mode else _FIX_ECHO_SHARE

    def _echo_source(self, w: _Written) -> str:
        """What a fix call shows of `w`: the file as it was when a rewrite lost much of
        it (#101), else the failing version."""
        if w.path in self.existing and any(placeholders.is_lost_code(p) for p in w.problems):
            return self.existing[w.path]
        return w.code

    def _repair(self, failing: list[_Written]) -> list[_Written]:
        """One repair call for `failing`, problems named. Returns what still fails."""
        claim.between_calls()
        self.repairs += 1
        for w in failing:
            activity.file(w.path, "fixing")
        planned = [self._plan_entry(w.path) for w in failing]
        try:
            resp = self._call(lambda: self._messages(planned, fixing=failing), planned, "fixing")
        except _NoRoom:
            if not self.fix_mode:
                raise
            return list(failing)  # a fix round shows a file whole or not at all (#103)
        self.truncated += 1 if _cut(resp) else 0
        blocks = {path: block for path, block in self._match(resp, planned) if block.complete}
        still: list[_Written] = []
        for w in failing:
            block = blocks.get(w.path)
            if block is not None:
                trial = _Written(w.path, block.code, w.language, w.purpose, w.origin)
                self._judge(trial)
                # Fewer things wrong wins; a repair that came back worse is not kept.
                if len(trial.faults()) <= len(w.faults()):
                    self.written[w.path] = trial
                    self._agree(self._entry(w.path), trial.real_exports())
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
            if planned is None and self.fix_mode and split_of is None:
                continue  # a fix round writes the files it asks for, never one unasked (#119)
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
        blocks: list[CodeBlock] = []
        for block in code_blocks(resp.text) or _salvage_json(resp.text):
            # A model still answering in JSON, fenced as ```json for a file that is not
            # JSON: the files are inside it. A dict literal is valid Python, so taking
            # it as the file's code would sail through the per-file check.
            if block.complete and not block.path.endswith(".json") and (
                block.language == "json" or block.code.lstrip().startswith("{")
            ):
                inside = _salvage_json(block.code)
                if inside:
                    blocks += inside
                    continue
            blocks.append(block)
        asked = [p.path for p in batch]
        out = [(self._resolve(b.path, asked), b) for b in blocks if b.path]
        named = {path for path, _ in out}
        # A file asked for and answered without its heading takes the next nameless
        # block its fence could be — a ```bash install line is never `main.py`.
        unnamed = [b for b in blocks if not b.path]
        for path in [p for p in asked if p not in named]:
            pick = next((b for b in unnamed if _suits(b.language, path)), None)
            if pick is not None:
                unnamed.remove(pick)
                out.append((path, pick))
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
        if planned.path in self.unwritten:  # it came in late, unasked: it is written now
            self.unwritten.remove(planned.path)
        self.landed.add(planned.path)
        self.lengths.append(len(code))
        self._agree(planned, self.written[planned.path].real_exports())

    def _agree(self, planned: Optional[_Planned], real: Optional[module_exports.Exports]) -> None:
        """The plan says what the code exports, once the code exists (#101): later calls,
        and the plan they are shown, describe the file as it was written. A name the plan
        promised and the code doesn't export is kept apart (`missing`), so the next call
        is told not to import it rather than never hearing of it."""
        if planned is not None and real is not None and not real.empty():
            if planned.origin == "plan" and not planned.promised:
                planned.promised = list(planned.exports)
            planned.exports = real.labels()

    def _unexported(self, planned: Optional[_Planned], real: module_exports.Exports) -> list[str]:
        """Names the plan promised that the code doesn't export."""
        if planned is None or not planned.promised:
            return []
        have = {real.default or "", *real.named, *real.types}
        wanted = []
        for entry in planned.promised:
            name = next(iter(re.findall(r"[A-Za-z_$][\w$]*", entry.replace("(default)", "").replace("(type)", ""))), "")
            if name and name not in have and name.lower() not in ("default", "type", "export", "exports") and name not in wanted:
                wanted.append(name)
        return wanted

    def _take_exports(self, build: Optional[BuildCheck]) -> None:
        """The tree check's exact answer for each written module, re-exports followed."""
        for path, real in ((build.exports if build is not None else None) or {}).items():
            w = self.written.get(path)
            if w is not None and isinstance(real, module_exports.Exports):
                w.exports, w.read = real, True
                self._agree(self._entry(path), real)

    def _judge(self, w: _Written) -> None:
        w.problems = file_problems(w.path, w.code)
        before = self.existing.get(w.path) if w.path in self.changing else None
        if before is not None and settings.check_placeholders and not any(p.kind == "syntax" for p in w.problems):
            # An edit answers for what it wrote, not for a TODO the file already had —
            # and only the person's words excuse lost code, never the plan's (#101).
            w.problems = placeholders.edited(w.path, before, w.code, self.asked)
        if not w.complete and not any(p.kind == "syntax" for p in w.problems):
            w.problems = [Problem(w.path, "was cut off at the model's output limit before it ended", "syntax")] + w.problems
        wants = self.wants.get(w.path) if self.fix_mode else None
        if wants and w.complete and not any(p.kind == "syntax" for p in w.problems):
            # A new file is held to what its importers take (#119): a name missing is
            # asked for once more, by name, as a file that doesn't parse is.
            missing = wiring.unexported(wants, w.real_exports(), w.code)
            if missing:
                w.problems = w.problems + [Problem(w.path, _missing_text(missing, wants), "import")]
            # ...and the names it exports must exist in it: told to export `User`, a small
            # model writes `module.exports = { User }` beside a `UserSchema`.
            ghosts = module_exports.undefined(w.path, w.code)
            if ghosts:
                names = ", ".join(f"`{n}`" for n in ghosts)
                w.problems = w.problems + [Problem(
                    w.path, f"exports {names}, but never defines {'it' if len(ghosts) == 1 else 'them'}. "
                    f"Define {names} in this file.", "reference",
                )]
        if self.fix_mode and w.path in self.needed_by and w.complete and not any(p.kind == "syntax" for p in w.problems):
            # A new file's own imports answered in the round that wrote it (#119): one that
            # points at nothing is asked about once more, rather than written as the next
            # round's new file — the path that grew `routes/routes/api/users/…`.
            w.problems = w.problems + self._dangling(w)
        w.stack = (
            charter_violations(self.ctx.charter, self.agent.key, {"files": [{"path": w.path, "code": w.code}]})
            if settings.enforce_stack_charter
            else []
        )

    def _dangling(self, w: _Written) -> list[Problem]:
        """The relative imports of a file this round wrote new that reach no file."""
        from app.build.scaffold import js_imports, py_imports

        pool = self.tree | set(self.needed_by)
        here = posixpath.dirname(w.path)
        out: list[Problem] = []
        if w.path.endswith(".py"):
            specs = []
            for module, level in py_imports(w.code):
                if level and module:
                    base = here
                    for _ in range(level - 1):
                        base = posixpath.dirname(base)
                    specs.append(("." * level + module, layout.join(base, *module.split("."))))
        else:
            specs = [(spec, layout.join(here, spec)) for spec in dict.fromkeys(js_imports(w.code)) if spec.startswith(".")]
        for spec, target in specs:
            if fixscope.find(target, pool) is None:
                out.append(Problem(
                    w.path,
                    f"imports `{spec}`, and no file in this build is at {target}. Import a file that exists, "
                    "or write what you need in this file.",
                    "import",
                ))
        return out

    def _entry(self, path: str) -> Optional[_Planned]:
        return next((p for p in self.planned if p.path == path), None)

    def _plan_entry(self, path: str) -> _Planned:
        return self._entry(path) or _Planned(path=path)

    def _unwritten(self, planned: _Planned) -> None:
        if planned.path not in self.unwritten:
            self.unwritten.append(planned.path)
        activity.file(planned.path, "missing")

    # ── the call ─────────────────────────────────────────────────────────────
    def _between_files(self) -> None:
        """The safe point between two files (#81): stop here for Stop or a lost claim —
        and pick up a note the person sent meanwhile (#102), so the files still to write
        carry it. The count comes back in the read this makes anyway."""
        held = claim.between_calls(notes=True)
        if held == self.notes_seen or (self.notes_seen is None and not held[0]):
            return
        build = inflight.current()
        if build is None:
            self.notes_seen = held  # no build to read them for: don't ask again every file
            return
        found = crew_chat.take_notes(build["id"], self.agent.key)
        if found is not None:
            # A read that failed keeps what the phase had, and tries again next file.
            self.ctx.asked, self.notes_seen = found

    def _call(self, build: Callable[[], list[ChatMessage]], batch: list[_Planned], doing: str) -> LLMResponse:
        """One write call. `build` makes its prompt from `self.profile`; a prompt the
        model refuses as too long (#101) is rebuilt to fit and sent once more, held to
        the model it was rebuilt for, and the phase's later calls are sized the same."""
        agent, ctx = self.agent, self.ctx
        first = batch[0].path
        if (self.change_mode or self.fix_mode) and self.edits:
            # A change (#79) or a fix round (#103) counts its own files, not the ones it keeps.
            number = len([e for e in self.edits if e.path in self.landed]) + 1
            total = len(self.edits)
        else:
            number = len([p for p in self.planned if p.path in self.written]) + 1
            total = len(self.planned)
        more = f" and {len(batch) - 1} more" if len(batch) > 1 else ""
        label = f"{agent.title} — {doing} {first}{more} ({min(number, total)} of {total})"
        activity.stage(doing, detail=first, total=total)

        def built(profile: ModelProfile) -> list[ChatMessage]:
            self.profile = profile
            return build()

        def send(messages: list[ChatMessage], pin: Optional[str]) -> LLMResponse:
            with inflight.agent(label):
                return router.complete(
                    messages,
                    mode=ctx.routing_mode,
                    preferred_model=ctx.preferred_model,
                    # Code is exact output too (#101), held to the reply ceiling the prompt
                    # was built against when its instructions needed some of the room.
                    options=GenerationOptions(exact=True, max_tokens=self.reply_cap),
                    complexity=agent._complexity(ctx),
                    role=agent.key,
                    pin=pin or ctx.pin_model,
                )

        resp, _, self.profile = send_fitting(built, self.profile, send, who=agent.title)
        self.calls.append(resp)
        if self.fix_mode and ctx.person_note:
            self.person_calls["sent" if self._person_put else "dropped"] += 1
        return resp

    # ── the prompt ───────────────────────────────────────────────────────────
    def _messages(
        self,
        batch: list[_Planned],
        *,
        split: bool = False,
        fixing: Optional[list[_Written]] = None,
        editing: Optional[_Written] = None,
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
        ask = self._write_now(batch, split, fixing, editing)
        self.reply_cap = None
        if len(self.system) + len(ask) + 1 > budget:
            # The standing instructions and the list of files to write are the call. The
            # reply gives way to its floor before the window is called too small (#101),
            # so the files' real exports and any problems to fix still have room; sent
            # anyway, a runtime would cut the instructions from the head.
            if reply_gives_way(self.profile, len(self.system) + len(ask) + 1) is None:
                raise WindowTooSmall(window_too_small(self.agent.title, self.profile, len(self.system) + len(ask)))
            floor = reply_floor(self.profile)
            budget, self.reply_cap = floor.prompt_char_budget, floor.max_output_tokens
            log.info(
                "%s: the call for %s doesn't fit %s beside a full reply, so the reply is capped at %s tokens.",
                self.agent.title, batch[0].path, self.profile.model, f"{floor.max_output_tokens:,}",
            )
        left = budget - len(self.system)

        def put(name: str, text: str, force: bool = False) -> bool:
            nonlocal left
            cost = len(text) + 1  # the "\n" each section is joined with
            if not text or (cost > left and not force):
                return False
            sections[name] = text
            left -= cost
            return True

        put("write", ask, force=True)
        # The echoes share one pool, each by its size (#101): a group `_repair_groups`
        # made fits whole; a single file larger than the pool is clipped to it.
        pool = int(left * self._echo_share())
        needs = {w.path: len(self._echo_source(w)) + 200 for w in fixing or []}
        total = sum(needs.values())
        if self.fix_mode and fixing and total > pool:
            # A fix round (#103) shows each file whole or not at all — checked against
            # the prompt as it is really built, which a smaller fallback model shrinks.
            raise _NoRoom(max(fixing, key=lambda w: needs[w.path]).path)
        for w in fixing or []:
            # A file that fits gets its whole need, frame included: a short file is shown
            # too, never dropped as too small to echo.
            room = needs[w.path] if total <= pool else max(pool * needs[w.path] // max(total, 1) - 200, 0)
            if w.path in self.existing and any(placeholders.is_lost_code(p) for p in w.problems):
                # The rewrite lost much of the file (#101): what to start again from is
                # the file as it was, whole, not the half that came back.
                was = _Written(w.path, self.existing[w.path], w.language, w.purpose)
                if put(f"echo:{w.path}", _echo(was, max(len(was.code) + 200, 400), _EDIT_ECHO)):
                    continue
            put(f"echo:{w.path}", _echo(w, room))
        if editing is not None:
            # The whole file or nothing: a clipped file written back would lose its end.
            echo = _echo(editing, max(len(editing.code) + 200, 400), _EDIT_ECHO)
            if not put(f"echo:{editing.path}", echo):
                raise RevisionRefused(
                    f"{editing.path} is too long for this model to rewrite in one reply, so nothing was changed. "
                    "Pick a model with a larger window for the frontend, or ask for a smaller change."
                )
        if self.change_mode and not fixing and not split:
            # A change request (#79): each file being changed, as it is now, whole —
            # sized to fit when the change was planned.
            for p in batch:
                if p.path in self.changing:
                    was = _Written(p.path, self.existing[p.path], self.written[p.path].language if p.path in self.written else "", p.purpose)
                    if not put(f"echo:{p.path}", _echo(was, max(len(was.code) + 200, 400), _EDIT_ECHO)):
                        # Never asked for blind: a file the model can't see would be
                        # written from its purpose alone, and everything else in it lost.
                        raise _NoRoom(p.path)

        if self.fix_mode:
            # What the person asked the crew to try (#103): whole, right after the files
            # and before anything that can be cut. Without room for it, the round's record
            # says it wasn't sent rather than the card claiming it was.
            self._person_put = bool(ctx.person_note) and put(
                "person", f"# What the person asks you to try\n{ctx.person_note.strip()}\n"
            )
            # What a fix needs next after the files themselves (#103): what the files
            # around them really export, and the code of what they import. The plan,
            # the idea and the digests get what is left.
            put("written", self._written_index(batch))
            put("bodies", self._bodies(batch, int(left * 0.5)))
        for compact in (0, 1, 2):
            if put("plan", self._plan_text(batch, compact)):
                break
        if ctx.idea:
            put("idea", f"# Product idea\n{_clip(ctx.idea, min(len(ctx.idea), max(budget // 8, 200), max(left - 200, 0)))}\n")
        if self.change_mode and ctx.change:
            share = min(len(ctx.change), budget // 6, max(left - 200, 0))
            put("change", f"# The change to make\n{_clip(ctx.change, share)}\n")
        if self.fix_mode:
            # A fix round (#103): each file's problems are under it in the ask. The round's
            # note says them again, so only what it adds is shown — a fix to keep.
            if ctx.fix_note:
                share = min(len(ctx.fix_note), budget // 7, max(left - 200, 0))
                put("feedback", f"# Keep this while you fix\n{_clip(ctx.fix_note, share)}\n")
        elif ctx.feedback or ctx.asked:
            # The reviewer's note, and what the person asked for during the build (#102) —
            # one slot, one share.
            said = feedback_text(ctx)
            share = min(len(said), budget // 7, max(left - 200, 0))
            put("feedback", f"{feedback_frame(ctx)}{_clip(said, share)}\n")
        if self.digests:
            each = max(min(int(budget * settings.handoff_digest_share), left // 2) // len(self.digests), 300)
            for dep, d in self.digests.items():
                for limit in (each, each // 2, 300):
                    if put(f"dep:{dep}", f"# From {dep} (digest)\n```json\n{handoff.fit(d, limit)}\n```\n"):
                        break
        if not self.fix_mode:
            put("written", self._written_index(batch))
            put("bodies", self._bodies(batch, int(left * 0.4)))
        if ctx.extra_context:
            room = min(len(ctx.extra_context), budget // 10, max(left - 200, 0))
            # A heading with nothing under it is worse than no heading at all.
            if room >= min(len(ctx.extra_context), 200):
                put("extra", f"# Team decision to honour\n{_clip(ctx.extra_context, room)}\n")
        if ctx.skills:
            body, _ = _pack_skills(ctx.skills, left - _SKILLS_FRAME_COST - 1)
            if body:
                put("skills", _SKILLS_FRAME.format(body=body))
        # What the old one-reply prompt also carried, after everything above: the
        # knowledge base, then lessons from past projects, in what is left.
        for name, text, frame, share in (
            ("rag", ctx.rag_context, _RAG_FRAME, 0.6),
            ("memory", ctx.memory_context, _MEMORY_FRAME, 1.0),
        ):
            room = int(left * share) - len(frame.format(body="")) - len(_TRUNCATED) - 2
            if text and room > 200:
                put(name, frame.format(body=_clip(text, min(len(text), room))))

        order = [
            "idea", *[k for k in sections if k.startswith("dep:")], "skills", "rag", "memory",
            "extra", "change", "feedback", "plan", "written", "bodies",
            # Next to the ask it is about (#103).
            "person",
        ]
        parts = [sections[k] for k in order if k in sections]
        parts.append(sections.get("write", ""))
        parts += [sections[k] for k in sections if k.startswith("echo:")]
        return [
            ChatMessage(role="system", content=self.system),
            ChatMessage(role="user", content="\n".join(p for p in parts if p)),
        ]

    def _write_now(
        self, batch: list[_Planned], split: bool, fixing: Optional[list[_Written]], editing: Optional[_Written] = None
    ) -> str:
        total = len(self.planned)
        done = len([p for p in self.planned if p.path in self.written])
        if editing is not None:
            p = batch[0]
            return (
                f"{_WRITE_NOW} — change 1 file\n"
                f"- `{p.path}`" + (f" — {p.purpose}" if p.purpose else "") + "\n"
                + "".join(f"  - change: {fault}\n" for fault in editing.faults()[:3])
                + "Make that change and only that change. Return the whole file, with everything "
                "else exactly as it is: the other files import from it.\n"
                "Answer with `### path` and one fenced block — nothing else.\n"
            )
        if self.change_mode and not fixing and not split:
            return self._change_now(batch)
        if self.fix_mode and not fixing and not split:
            return self._fix_new_now(batch)
        if fixing:
            head = f"{_WRITE_NOW} — fix {len(fixing)} file{'s' if len(fixing) > 1 else ''}"
        elif len(batch) == 1:
            head = f"{_WRITE_NOW} — file {min(done + 1, total)} of {total}"
        else:
            head = f"{_WRITE_NOW} — files {done + 1}–{min(done + len(batch), total)} of {total}"
        lines = [head]
        broken = {w.path: w for w in fixing or []}
        adding = [p for p in batch if fixing and p.path in self.needed_by and p.path not in broken]
        if adding:
            head = f"{_WRITE_NOW} — fix {len(fixing)} file{'s' if len(fixing) > 1 else ''}, add {len(adding)}"
            lines = [head]
        for p in batch:
            if p in adding:
                lines += self._new_lines(p)
                continue
            line = f"- `{p.path}`"
            if p.purpose:
                line += f" — {p.purpose}"
            # A new file's repair lists what it was asked to export (#119), not the names
            # it wrote instead.
            shown = p.promised if p.path in self.wants and p.promised else p.exports
            if shown:
                line += f". Exports: {', '.join(shown[:12])}"
            if p.imports:
                line += f". Imports: {', '.join(p.imports[:8])}"
            lines.append(line)
            for fault in (broken[p.path].faults() if p.path in broken else [])[:6]:
                lines.append(f"  - problem: {fault}")
            if p.path in broken and p.path in self.changing and self.asks.get(p.path):
                # A repair of an edit still makes the edit (#101): without this, the file
                # as it was is a "fix" that drops the change.
                lines.append(f"  - change: {self.asks[p.path]}")
        if split:
            lines.append(
                f"Your last reply for `{batch[0].path}` was cut off at this model's output limit. "
                "Write it shorter as two files: this one, and a new module beside it that takes "
                "part of its code (this one imports from it). Return both."
            )
        if adding:
            lines.append(
                "Write each new file with exactly those exports, and return the file that imports it with "
                "its problems fixed. Or, if a new file shouldn't exist, change that import instead — the import "
                "and the lines that use what it brought in, nothing else — and don't write the new file."
            )
        elif fixing:
            lines.append("Return each file above in full, with every problem fixed.")
        if fixing:
            if self.fix_mode:
                lines.append(
                    "Change only what the problems need. Keep everything else in each file exactly as "
                    "it is: the other files import from it."
                )
        lines += self._instructions()
        lines.append("Answer with `### path` and one fenced block per file — nothing else.")
        return "\n".join(lines) + "\n"

    def _change_now(self, batch: list[_Planned]) -> str:
        """The ask for a change request's files (#79): change these, add those."""
        at = next((i for i, e in enumerate(self.edits) if e.path == batch[0].path), 0) + 1
        lines = [f"{_WRITE_NOW} — change {at} of {len(self.edits)}"]
        for p in batch:
            if p.path in self.changing:
                lines.append(f"- `{p.path}`" + (f" — {p.purpose}" if p.purpose else ""))
                if self.asks.get(p.path):
                    lines.append(f"  - change: {self.asks[p.path]}")
                lines.append(
                    "  Its current version is below. Make that change and only that change: return "
                    "the whole file, with everything else in it exactly as it is."
                )
            else:
                line = f"- `{p.path}` (new file)" + (f" — {p.purpose}" if p.purpose else "")
                if p.exports:
                    line += f". Exports: {', '.join(p.exports[:12])}"
                if p.imports:
                    line += f". Imports: {', '.join(p.imports[:8])}"
                lines.append(line)
        lines += self._instructions()
        lines.append("Answer with `### path` and one fenced block per file — nothing else.")
        return "\n".join(lines) + "\n"

    def _instructions(self) -> list[str]:
        """The phase's own write instruction, and the module format the stack fixed (#103)."""
        out = []
        instruction = TASK_OVERRIDES.get(f"{self.agent.key}.write") or self.agent.write_instruction()
        if instruction:
            out.append(instruction)
        from app.orchestration.charter import format_instruction

        fmt = getattr(self.ctx.charter, "module_format", None)
        if fmt and self.agent.key == Phase.BACKEND_ENGINEER.value:
            out.append(format_instruction(fmt))
        return out

    def _fix_new_now(self, batch: list[_Planned]) -> str:
        """The ask for a file a fix round writes new (#103): what imports it, how — each
        importer's own import line — and the names it must export (#119)."""
        lines = [f"{_WRITE_NOW} — add {len(batch)} file{'s' if len(batch) > 1 else ''}"]
        for p in batch:
            lines += self._new_lines(p)
        lines += self._instructions()
        lines.append("Answer with `### path` and one fenced block per file — nothing else.")
        return "\n".join(lines) + "\n"

    def _new_lines(self, p: _Planned) -> list[str]:
        """A new file in an ask (#103, #119): who imports it, by their own lines, and the
        names it must export."""
        lines = [f"- `{p.path}` (new file)" + (f" — {p.purpose}" if p.purpose else "")]
        wants = self.wants.get(p.path) or []
        for want in wants[:4]:
            lines.append(f"  - `{want.importer}` imports it: `{want.line}`")
        for problem in self.needed_by.get(p.path, [])[:4]:
            lines.append(f"  - needed by: {problem.text()}")
        ask = _export_ask(wants)
        lines.append(f"  - {ask}" if ask else "  - Write it with exactly what those files import from it.")
        return lines

    def _plan_text(self, batch: list[_Planned], compact: int) -> str:
        """The plan: whole (0), purposes only outside this batch (1), or paths only (2)."""
        if self.change_mode:
            # The change's plan, not the app's: what is being added and changed.
            body = json.dumps(
                {
                    "change": self.edit_summary,
                    "files": [
                        {"path": e.path, "action": "change" if e.path in self.changing else "add",
                         "what": self.asks.get(e.path) or e.purpose}
                        for e in self.edits
                    ],
                },
                ensure_ascii=False,
            )
            return f"# The change plan\n```json\n{body}\n```\n"
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
        mine = {p.path for p in batch}
        editing = self.change_mode or self.fix_mode
        done = [w for w in self.written.values() if not (editing and w.path in mine)]
        if not done:
            return ""
        lines = [
            "# The app's other files — import from these by their real paths; don't write them"
            if editing
            else "# Written so far — import from these by their real paths; do not write them again"
        ]
        for w in done:
            planned = self._entry(w.path)
            line = f"- `{w.path}`"
            if w.purpose:
                line += f" — {w.purpose}"
            # The names its code really has (#101), not what the plan guessed; the plan's
            # list only for a file no reader here understands.
            real = w.real_exports()
            if real is not None and not real.empty():
                line += f". Exports: {real.render()}"
                missing = self._unexported(planned, real)
                if missing:
                    line += f" (planned, not exported: {', '.join(missing[:6])})"
            elif planned is not None and planned.exports:
                # Read nothing — a reader can miss what it can't parse: the plan's list,
                # which is what the plan shown beside this says too.
                line += f". Exports: {', '.join(planned.exports[:12])}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def _bodies(self, batch: list[_Planned], room: int) -> str:
        """The code of written files this batch imports, whole, while they fit."""
        mine = {p.path for p in batch}
        # A file in this call is shown as itself, not again as something it imports.
        wanted = [i for p in batch for i in p.imports if i in self.written and i not in mine]
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
        plan allows that order. Each import is read by the plan gate's own rule
        (`_plan_import`, #119); one meant as a plan file that no plan file is at is
        dropped, and the record says so.
        """
        from app.build.scaffold import platform_owned

        out: list[_Planned] = []
        #: path -> each import it kept -> the import as the plan wrote it.
        written_as: dict[str, dict[str, str]] = {}
        for w in _plan_wiring(self.agent.key, (output or {}).get("files") or [], _language(self.ctx.charter)):
            if platform_owned(w.path):
                self.left_to_platform.append(w.path)
                continue
            written_as[w.path] = w.written_as
            out.append(
                _Planned(
                    path=w.path,
                    purpose=str(w.plan.get("purpose") or "").strip(),
                    exports=_strings(w.plan.get("exports"))[:20],
                    imports=list(w.imports),
                    dropped=list(w.dropped),
                )
            )
        out = out[: max(settings.code_max_files, 1)]
        known = {p.path for p in out}
        for p in out:
            # A plan file past the cap is never written: its import is dropped, and said.
            p.dropped += [written_as[p.path][i] for i in p.imports if i not in known]
            p.imports = [i for i in p.imports if i in known]
        return _ordered(out)

    # ── the deliverable ──────────────────────────────────────────────────────
    def _finish(self) -> AgentResult:
        agent, ctx = self.agent, self.ctx
        activity.stage("checking", detail="", total=len(self.planned))
        output, errors = self._assemble()
        stack = charter_violations(ctx.charter, agent.key, output) if settings.enforce_stack_charter else []
        build = agent._build_check(ctx, output)
        self._take_exports(build)
        if self.fix_mode:
            # A fix round is one pass (#103): what its edits left is the next round's,
            # with the problems found afresh — not the same files sent straight back.
            build, output, errors, stack = self._keep_no_worse(build, output, errors, stack)
        # What the round's own edits did, before the platform mends anything (#103): its
        # record credits the model with its own changes, not the platform's.
        self.edited = {p: w.code for p, w in self.written.items()}
        # What the platform can mend without a model, before any model round (#103).
        build, output, errors, stack = self._mend(build, output, errors, stack)
        if (
            not self.fix_mode
            and build is not None
            and build.status == BuildStatus.FAILED.value
            and settings.schema_repair_rounds > 0
        ):
            build, output, errors, stack, _ = self._fix_build(build, output, errors, stack)
        # Once it parses, for real (#75): installed, built and started in a sandbox —
        # and one round on the files that build names, before the fix loop sees it.
        build_run = agent._run_build(ctx, output, build)
        if (
            build is not None
            and build_run is not None
            and build_run.get("status") == BuildStatus.FAILED.value
            and settings.schema_repair_rounds > 0
            and not self.fix_mode
        ):
            build, output, errors, stack, rerun = self._fix_build(build, output, errors, stack, real=True)
            if rerun is not _UNCHANGED:
                # The repaired tree is what ships: its own build, or none at all when
                # the repair stopped it parsing — never the failed build it replaced.
                build_run = rerun

        never = [
            p for p in self.unwritten
            if p not in self.written and (self._entry(p) or _Planned(p)).origin == "plan"
        ]
        if build is not None and never:
            # A file the plan promised and the model never returned: often a page
            # nothing imports, so no import problem would ever name it.
            build.problems = build.problems + [
                Problem(path, "was planned but never written. Write it, or drop it and every import of it.", "missing")
                for path in never
            ]
            build.status = BuildStatus.FAILED.value
        notes = [
            Problem(p.path, "was not in the plan; kept", NOTE_KIND)
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
            # What the platform mended itself before any model round (#103).
            **({"wiring": list(self.mended)} if self.mended else {}),
            "truncated_replies": self.truncated,
            "generation": {
                "mode": mode,
                "files_per_call": self.largest,
                "calls": len(self.calls),
                "plan_calls": len([c for c in (plan.calls or [plan.response]) if c is not None]),
                "files_planned": len([p for p in self.planned if p.origin == "plan"]),
                "files_written": len(written),
                "truncated_replies": self.truncated,
                "repairs": self.repairs,
                "plan": [
                    {"path": p.path, "purpose": p.purpose, "exports": p.exports, "imports": p.imports, "origin": p.origin,
                     **({"dropped_imports": p.dropped} if p.dropped else {})}
                    for p in self.planned
                ],
                # Imports the plan meant as its own files and no plan file is at (#119).
                "imports_dropped": sum(len(p.dropped) for p in self.planned),
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
            # A fix round with nothing it could show the model whole makes no call.
            response=_merge(self.calls) if self.calls else self._no_call(),
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
            build_run=build_run,
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
        self, build: BuildCheck, output: dict, errors: list[str], stack: list[str], real: bool = False
    ) -> tuple[BuildCheck, dict, list[str], list[str], object]:
        """The whole-tree check failed: one round on the files it names, then check again.

        Only for what the per-file check could not see — an import, a name, a JavaScript
        parse. A file that already failed its own repair round has had its round.

        `real`: the problems came from the real build (#75), so the re-check builds
        again once the files parse, and the new build's record is returned last — None
        when the repaired files no longer parse, `_UNCHANGED` when the repair wasn't kept.
        """
        named: dict[str, list[Problem]] = {}
        for p in build.problems:
            w = self.written.get(p.path)
            if w is not None and not w.faults():
                named.setdefault(p.path, []).append(p)

        if not named:
            return build, output, errors, stack, _UNCHANGED
        failing: list[_Written] = []
        for path, problems in named.items():
            w = self.written[path]
            failing.append(_Written(w.path, w.code, w.language, w.purpose, w.origin, w.complete, problems, w.stack))
        before = dict(self.written)
        n = self._batch_size(len(failing))
        for i in range(0, len(failing), n):
            claim.between_calls()
            group = failing[i : i + n]
            for w in group:
                activity.file(w.path, "fixing")
            self.repairs += 1
            planned = [self._plan_entry(w.path) for w in group]
            resp = self._call(lambda group=group, planned=planned: self._messages(planned, fixing=group), planned, "fixing")
            self.truncated += 1 if _cut(resp) else 0
            for path, block in self._match(resp, planned):
                if block.complete and path in named:
                    trial = _Written(path, block.code, self.written[path].language,
                                     self.written[path].purpose, self.written[path].origin)
                    self._judge(trial)
                    if not trial.problems:
                        self.written[path] = trial
        output2, errors2 = self._assemble()
        stack2 = charter_violations(self.ctx.charter, self.agent.key, output2) if settings.enforce_stack_charter else []
        build2 = self.agent._build_check(self.ctx, output2)
        run2 = self.agent._run_build(self.ctx, output2, build2) if real else None
        for path in named:
            activity.file(path, "ok" if not any(p.path == path for p in (build2.problems if build2 else [])) else "failed")
        # The second check is kept only when it is no worse than the first.
        if build2 is not None and len(build2.problems) <= len(build.problems):
            self._take_exports(build2)
            return build2, output2, errors2, stack2, run2
        # The repair isn't kept, and neither is what its check said about exports (#101):
        # those were only ever taken on the branch above.
        self.written = before
        for path in named:
            activity.file(path, "failed")
        return build, output, errors, stack, _UNCHANGED


    def _no_call(self) -> LLMResponse:
        """The response of a round that sent the model nothing: every file it would have
        fixed was too long to show whole. Charged to the model the round was for."""
        from app.schemas.llm import Usage

        return LLMResponse(
            text="", provider=self.profile.provider, model=self.profile.model, usage=Usage(), latency_ms=0,
        )

    def _whole(self, reason: str) -> AgentResult:
        """No plan to write from: the one-reply path, with the plan's calls counted."""
        log.warning("%s: %s", self.agent.title, reason)
        activity.stage("writing", total=0)
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
def _counts(check: Optional[BuildCheck]) -> tuple[dict[str, int], set[str]]:
    """(problems per file, the files that don't parse) of a check."""
    counts: dict[str, int] = {}
    unparsed: set[str] = set()
    for p in (check.problems if check is not None else []):
        counts[p.path] = counts.get(p.path, 0) + 1
        if p.kind == "syntax":
            unparsed.add(p.path)
    return counts, unparsed


#: `_fix_build`'s answer when the tree it was given is still the one that ships.
_UNCHANGED = object()


class _NoRoom(Exception):
    """A file a change would edit doesn't fit in this model's window beside the ask."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _choice(value: object) -> object:
    """The eval harness's mode wins over the person's choice; `batch` is automatic."""
    if MODE_OVERRIDE == "one":
        return 1
    if MODE_OVERRIDE == "batch":
        return None
    return value


#: Which files a fence's language word can be, for a block that names no path.
_JS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_FENCE_FITS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("python", "py"), (".py",)),
    **dict.fromkeys(("javascript", "js", "jsx", "typescript", "ts", "tsx"), _JS),
    "json": (".json",), "css": (".css", ".scss"), "scss": (".scss", ".css"), "html": (".html",),
    "sql": (".sql",), "yaml": (".yml", ".yaml"), "yml": (".yml", ".yaml"), "toml": (".toml",),
    "markdown": (".md",), "md": (".md",), "ini": (".ini", ".cfg"), "env": (".env",),
    **dict.fromkeys(("bash", "sh", "shell"), (".sh",)), "dockerfile": ("dockerfile",), "diff": (),
}


def _suits(language: str, path: str) -> bool:
    """Whether a block fenced as `language` can be the file at `path`."""
    word = (language or "").lower()
    if word in ("", "text", "txt", "plaintext"):
        return True
    fits = _FENCE_FITS.get(word)
    return fits is None or path.lower().endswith(fits)


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _language(charter) -> Optional[str]:
    """The backend language the stack fixed, which decides where files are placed."""
    return charter.get("language").token if charter is not None and charter.get("language") else None


def _export_ask(wants: list[wiring.Wanted]) -> str:
    """"Export exactly …" for a new file, default against named as its importers' lines
    use them (#119)."""
    named = list(dict.fromkeys(n for w in wants for n in w.named + w.loose))
    whole = next((w for w in wants if w.default), None)
    parts = []
    if named:
        cjs = all(w.commonjs for w in wants if w.named or w.loose)
        parts.append(
            "Export exactly: " + ", ".join(f"`{n}`" for n in named) + ", each by that name"
            + (f" (`module.exports = {{ {', '.join(named)} }}`)" if cjs and not whole else "")
        )
    if whole is not None:
        parts.append(
            f"`module.exports` must be the value itself: `{whole.importer}` takes the whole module as `{whole.default}`"
            if whole.commonjs else f"Give it a default export: `{whole.importer}` imports it as `{whole.default}`"
        )
    return ". ".join(parts) + "." if parts else ""


def _missing_text(missing: list[str], wants: list[wiring.Wanted]) -> str:
    """A new file's problem when it lacks what its importers take (#119)."""
    names = [n for n in missing if n != "default"]
    # The importer whose own line needs what's missing: an ES default is an ES line's.
    who = next(
        (w for w in wants if any(n in w.named for n in names) or (w.default and not w.commonjs and "default" in missing)),
        wants[0],
    )
    said = []
    if names:
        said.append("doesn't export " + ", ".join(f"`{n}`" for n in names))
    if "default" in missing:
        said.append("has no default export" if not who.commonjs else "doesn't set `module.exports` to the value itself")
    return f"{' and '.join(said)}, which `{who.importer}` imports: `{who.line}`. {_export_ask(wants)}"


@dataclass
class _Wired:
    """One plan file as the plan gate and `_normalise` both read it (#119)."""

    path: str  # where the build places it
    plan: dict  # the plan's own entry
    #: The plan files it imports, placed — never itself, nor a file the platform writes.
    imports: list[str] = field(default_factory=list)
    #: Each of those -> the import as the plan wrote it.
    written_as: dict[str, str] = field(default_factory=dict)
    #: Imports written as paths that no plan file is at.
    dropped: list[str] = field(default_factory=list)


def _plan_wiring(phase: str, files: object, language: Optional[str]) -> list[_Wired]:
    """The plan's files at the paths the build places them, each with the plan files
    it imports (#119): the one reading of a plan's `imports` that the plan gate and
    `_normalise` share, so the gate can't pass a plan the writer then gets unwired.

    Read where the files are placed, so an import written in the plan's own terms
    (`app/models/todo`), from the side's root (`models/todo` beside a plan under
    `server/`) or as placed (`backend/app/models/todo`) all find the same file."""
    from app.build.scaffold import platform_owned

    entries = [f for f in (files if isinstance(files, list) else []) if isinstance(f, dict)]
    entries = [f for f in entries if isinstance(f.get("path"), str) and layout.clean(f["path"])]
    raw = [f["path"] for f in entries]
    placed = layout.Placer(language).place_all(phase, [(p, "") for p in raw])
    strip = layout.renamed_roots(phase, raw)
    where = {layout.clean(f["path"]): p for (p, *_), f in zip(placed, entries)}
    pool = {p for p in where.values() if p}

    def place(path: str) -> str:
        return where.get(path) or layout.place(phase, path, strip=strip)

    out: list[_Wired] = []
    seen: set[str] = set()
    for (path, *_), f in zip(placed, entries):
        if not path or path in seen:
            continue
        seen.add(path)
        w = _Wired(path, f)
        for item in _strings(f.get("imports")):
            hit = _plan_import(path, item, pool, place)
            if hit is None:
                if _meant_planned(item) and item not in w.dropped:
                    w.dropped.append(item)
            elif hit != path and hit not in w.imports and not platform_owned(hit):
                w.imports.append(hit)
                w.written_as[hit] = item
        out.append(w)
    return out


def _plan_import(
    importer: str, spec: str, paths: set[str], place: Optional[Callable[[str], str]] = None
) -> Optional[str]:
    """The plan file `importer` means by `spec`, one of its `imports` as a plan lists
    it — or None: a package, or a path no plan file is at (#119).

    A plan writes an import the way code does (`./models/user`, `../config`) or as a
    plan path (`app/models/user.js`), with the extension or without. The first plan
    file found wins, each path looked up the way the compile gate resolves an import
    (`check.resolve`: as written, with an extension, or a folder's `index.*`; Python's
    `x.py` or `x/__init__.py`):

      1. a bare path from the plan's root (`app/models/todo` -> `app/models/todo.js`);
      2. a relative import from the importer's own folder first (`../models/todo` from
         `app/controllers/x.js`), as JavaScript and Python resolve one;
      3. then from the plan's root (`./models/todo` -> `models/todo.js`), as `@/`,
         `~/` and `/` are;
      4. last, for a relative or rooted import only, the one plan file whose path ends
         in the import's own, in any case (`./models/todo` -> `app/models/Todo.js`) —
         none of two.

    `place` turns a path from the plan's root into where `paths` are (by default they
    are the same). A bare name (`express`, `firebase/auth`, `email.utils`) is a package
    unless a plan file is at it as written. A file never imports itself.
    """
    place = place or (lambda p: p)
    s = _spec(spec)
    if not s or not importer:
        return None
    python = importer.endswith(".py")
    here = posixpath.dirname(importer)
    if python and "/" not in s and not s.endswith(".py"):
        # A Python module: `app.models.user`, or `.models` from the importer's package.
        level = len(s) - len(s.lstrip("."))
        tail = layout.clean(s[level:].replace(".", "/"))
        base = here
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        bases = ([layout.join(base, tail)] if level else []) + ([place(tail)] if tail else [])
        by_tail = bool(level and tail)
    elif s.startswith(("./", "../")) or s in (".", ".."):
        tail = layout.clean(s)
        bases, by_tail = [layout.join(here, s)] + ([place(tail)] if tail else []), True
    elif s.startswith(("@/", "~/", "/")):
        tail = layout.clean(s.lstrip("@~"))
        bases, by_tail = [place(tail)], True
    else:
        tail = layout.clean(s)
        bases, by_tail = [place(tail)], False
    # TypeScript's NodeNext `./x.js` names `x.ts` — for a path, never a package's name.
    typescript = importer.endswith((".ts", ".tsx", ".mts", ".cts")) and (by_tail or "/" in s)
    for base in bases:
        hit = _planned_at(base, paths, python, typescript)
        if hit is not None:
            return None if hit == importer else hit
    if tail.endswith(_SOURCE_EXT):
        tail = posixpath.splitext(tail)[0]
    if not by_tail or not tail:
        return None
    end = "/" + tail.lower()
    hits = [
        p for p in paths
        if p != importer and p.endswith(_SOURCE_EXT) and p.endswith(".py") == python
        and ("/" + fixscope._module_path(p)).lower().endswith(end)  # noqa: SLF001 - one rule
    ]
    return hits[0] if len(hits) == 1 else None


def _planned_at(base: str, pool: set[str], python: bool, typescript: bool = False) -> Optional[str]:
    """The plan file at `base`, found the way the compile gate finds an import's file."""
    if not base:
        return None
    if python:
        return next((p for p in (base, f"{base}.py", f"{base}/__init__.py") if p in pool), None)
    found = resolve(base, pool, typescript)
    return found if found is not None and not found.endswith(".py") else None


def _spec(spec: object) -> str:
    return str(spec or "").strip().strip("'\"`").strip()


def _meant_planned(spec: str) -> bool:
    """Whether an import no plan file answered was written as a path to code rather
    than a package's name or an asset: relative (Python's `.models` too), rooted, or
    under a side's root (`backend/…`), with no extension or a source one. A bare name
    never is — `firebase/auth` and `chart.js` are packages, even beside a plan folder
    of that name — and `./globals.css` or `./logo.svg` is a stylesheet or an image."""
    s = _spec(spec)
    if s.lower().endswith(fixscope._TEXT_ASSETS + fixscope._BINARY + (".json",)):  # noqa: SLF001 - one list
        return False
    return s.startswith((".", "/", "@/", "~/")) or (
        "/" in s and s.split("/", 1)[0].lower() in (layout.BACKEND, layout.FRONTEND)
    )


#: Layers by folder or name, used where the plan never says which file uses which (#103):
#: what a file is called says, near enough, what it may import. The entry file is last.
_LAYERS = (
    ("config", "configs", "constants", "types", "type", "models", "model", "schemas", "schema",
     "entities", "entity", "dto", "dtos"),
    ("db", "database", "data", "repositories", "repository", "repos", "repo", "dal", "store", "stores",
     "utils", "util", "helpers", "helper", "lib"),
    ("services", "service", "providers", "provider", "hooks", "hook", "api", "clients", "client"),
    ("controllers", "controller", "handlers", "handler", "middleware", "middlewares", "components", "component"),
    ("routes", "route", "routers", "router", "views", "view", "pages", "page"),
)
#: What a plan names that is code, for the entry file an ask-again points at.
_SOURCE_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py")
_ENTRY = len(_LAYERS)
_ENTRY_NAMES = ("main", "index", "app", "server")
#: Between the routes and the entry file: `app.js` builds the app that `server.js`,
#: `index.js` or `main.tsx` start, so it is written first.
_APP = _ENTRY - 0.5


def _layer(path: str) -> float:
    """Where a file sits in the order a backend's files use each other: models first,
    the entry file (`main`, `index`, `app`, `server` at a side's root) last."""
    written = layout.relative_to_side(layout.clean(path)).split("/")
    parts = [p.lower() for p in written]
    stem = parts[-1].split(".")[0] if parts else ""
    folders = parts[:-1]
    if stem in _ENTRY_NAMES and all(f in ("src", "app") for f in folders):
        return _APP if stem == "app" else _ENTRY
    for folder in reversed(folders):
        for i, names in enumerate(_LAYERS):
            if folder in names:
                return i
    # `todo_service.js`, `userRoutes.js`: the layer is in the name, as it was written.
    words = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", written[-1] if written else "").lower())
    for word in reversed(words[:-1] if len(words) > 1 else words):
        for i, names in enumerate(_LAYERS):
            if word in names:
                return i
    return 2


def _ordered(files: list[_Planned]) -> list[_Planned]:
    """Each file after the plan files it imports, where a cycle allows.

    Where the plan doesn't say — no imports at all, or a file it says nothing about —
    the layer decides (#103): models, then data access, services, controllers, routes,
    and the entry file last, so every file is written after the files it most likely
    uses, and is shown their real exports. Plan order breaks a tie."""
    rank = {p.path: (_layer(p.path), i) for i, p in enumerate(files)}
    out: list[_Planned] = []
    done: set[str] = set()
    pending = list(files)
    while pending:
        ready = [p for p in pending if all(i in done for i in p.imports)]
        nxt = min(ready or pending, key=lambda p: rank[p.path])
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


def _echo(w: _Written, room: int, template: str = _FIX_ECHO) -> str:
    """The broken version of a file, clipped to `room`, for the repair call to correct."""
    if room <= 200:
        return ""
    code = w.code if len(w.code) <= room else w.code[:room] + "\n" + _TRUNCATED
    fence = _fence(code)
    nl = "" if code.endswith("\n") else "\n"
    return template.format(path=w.path, fence=fence, lang=w.language, code=code, nl=nl)


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
