"""What one phase hands the next: a typed digest first, the full output if it fits.

Hand-offs used to be `json.dumps(output, indent=2)` cut from the front. On a small
window the QA phase got 1,842 characters of each dependency — the Backend's summary
and half of `db_models`, rarely a single file path — and the JSON it was shown no
longer parsed. Reviewers then reported findings in code they never saw.

Now every dependency is framed as one JSON object that always parses:

    {"digest": {...}, "output": {...whole fields..., "_cut": "files 7-19 omitted"}}

  * **The digest** is built here, server-side, from the validated output — free and
    deterministic. It is small and capped, so it is always printed, and it is charged
    to the prompt's measured overhead like the charter is. Paths, endpoints, entities,
    criteria: the index of what the phase produced, plus a short rationale from the
    model's own summary (the "hybrid" hand-off: structured fields and why).
  * **The output** gets whatever share of the budget is left, redistributed so a 3 KB
    spec does not hold room a 200 KB backend could use. It is cut at a field boundary,
    or between whole items of a list, never mid-string — and the note saying so is a
    key *inside* the object.

The name registry is System Design's digest, reduced to names: entities, endpoint
paths, page routes. Every phase after it is told to use exactly those names.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.build import layout, routes
from app.core.constants import Phase
from app.core.logging import get_logger

log = get_logger(__name__)

_RATIONALE = 400
_TEXT = 160


class _Digest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str = Field("", max_length=_RATIONALE)


class _NameDesc(BaseModel):
    name: str
    description: str = ""


class _Story(BaseModel):
    as_a: str = ""
    i_want: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=8)


class PMDigest(_Digest):
    product_name: str = ""
    p0_features: list[_NameDesc] = Field(default_factory=list, max_length=10)
    p0_stories: list[_Story] = Field(default_factory=list, max_length=12)
    non_goals: list[str] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=10)


class _Entity(BaseModel):
    name: str
    fields: list[str] = Field(default_factory=list, max_length=16)


class _Endpoint(BaseModel):
    method: str = ""
    path: str
    purpose: str = ""
    auth: str = ""


class SDDigest(_Digest):
    entities: list[_Entity] = Field(default_factory=list, max_length=24)
    endpoints: list[_Endpoint] = Field(default_factory=list, max_length=40)
    pages_expected: list[str] = Field(default_factory=list, max_length=24)
    decisions: list[str] = Field(default_factory=list, max_length=12)
    open_questions: list[str] = Field(default_factory=list, max_length=10)


class _FileRef(BaseModel):
    path: str
    purpose: str = ""


class _Implemented(BaseModel):
    method: str
    path: str
    file: str


class BEDigest(_Digest):
    framework: str = ""
    files: list[_FileRef] = Field(default_factory=list, max_length=60)
    endpoints_implemented: list[_Implemented] = Field(default_factory=list, max_length=60)
    auth_flow: str = Field("", max_length=_RATIONALE)
    env_vars: list[str] = Field(default_factory=list, max_length=30)


class _PageRef(BaseModel):
    route: str
    purpose: str = ""


class _FEFile(BaseModel):
    path: str
    endpoints_used: list[str] = Field(default_factory=list, max_length=12)


class FEDigest(_Digest):
    framework: str = ""
    pages: list[_PageRef] = Field(default_factory=list, max_length=24)
    components: list[str] = Field(default_factory=list, max_length=40)
    files: list[_FEFile] = Field(default_factory=list, max_length=60)


class QADigest(_Digest):
    frameworks: list[str] = Field(default_factory=list, max_length=6)
    test_files: list[str] = Field(default_factory=list, max_length=60)
    command_backend: str = ""
    command_frontend: str = ""


class _Finding(BaseModel):
    severity: str = ""
    title: str = ""
    location: str = ""


class SecurityDigest(_Digest):
    findings: list[_Finding] = Field(default_factory=list, max_length=20)
    overall_posture: str = Field("", max_length=_TEXT)


class DevOpsDigest(_Digest):
    env_vars: list[str] = Field(default_factory=list, max_length=30)
    health_check_path: str = ""
    migration_command: str = ""
    ci_workflow: str = ""


class CostDigest(_Digest):
    total_monthly_low_usd: Optional[float] = None
    total_monthly_high_usd: Optional[float] = None
    estimated_timeline_weeks: Optional[float] = None
    assumptions: list[str] = Field(default_factory=list, max_length=10)


# ── building ─────────────────────────────────────────────────────────────────
def _s(value: Any, limit: int = _TEXT) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _items(value: Any) -> list:
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def _strs(value: Any, cap: int, limit: int = _TEXT) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [_s(v, limit) for v in (value if isinstance(value, list) else []) if _s(v)][:cap]


def _cap(model: type[BaseModel], name: str) -> int:
    for meta in model.model_fields[name].metadata:
        found = getattr(meta, "max_length", None)
        if found:
            return found
    return 50


def _is_p0(priority: Any) -> bool:
    return str(priority or "").strip().upper().startswith("P0")


def _pm(out: dict) -> PMDigest:
    features = [f for f in _items(out.get("features")) if isinstance(f, dict)]
    p0 = [f for f in features if _is_p0(f.get("priority"))] or features
    p0_names = {str(f.get("name", "")).strip().lower() for f in p0}
    stories = [s for s in _items(out.get("user_stories")) if isinstance(s, dict)]

    def story_is_p0(s: dict) -> bool:
        if str(s.get("priority") or "").strip():
            return _is_p0(s.get("priority"))
        feature = str(s.get("feature") or "").strip().lower()
        return not feature or feature in p0_names

    return PMDigest(
        rationale=_s(out.get("problem_statement"), _RATIONALE),
        product_name=_s(out.get("product_name")),
        p0_features=[
            _NameDesc(name=_s(f.get("name")), description=_s(f.get("description")))
            for f in p0[: _cap(PMDigest, "p0_features")]
        ],
        p0_stories=[
            _Story(
                as_a=_s(s.get("as_a"), 60),
                i_want=_s(s.get("i_want")),
                acceptance_criteria=_strs(s.get("acceptance_criteria"), 8),
            )
            for s in [s for s in stories if story_is_p0(s)][: _cap(PMDigest, "p0_stories")]
        ],
        non_goals=_strs(out.get("out_of_scope"), 10),
        assumptions=_strs(out.get("assumptions"), 10),
    )


def _sd(out: dict) -> SDDigest:
    stack = out.get("tech_stack") if isinstance(out.get("tech_stack"), dict) else {}
    decisions = [
        f"{k}: {', '.join(_strs(v, 4, 40))}" for k, v in stack.items() if _strs(v, 4, 40)
    ]
    return SDDigest(
        rationale=_s(out.get("architecture_overview"), _RATIONALE),
        entities=[
            _Entity(name=_s(e.get("entity") or e.get("name"), 60), fields=_strs(e.get("fields"), 16, 60))
            for e in _items(out.get("data_model"))
            if isinstance(e, dict) and _s(e.get("entity") or e.get("name"))
        ][: _cap(SDDigest, "entities")],
        endpoints=[
            _Endpoint(
                method=_s(e.get("method"), 10).upper(),
                path=_s(e.get("path"), 120),
                purpose=_s(e.get("purpose"), 100),
                auth=_s(e.get("auth"), 20),
            )
            for e in _items(out.get("api_endpoints"))
            if isinstance(e, dict) and _s(e.get("path"))
        ][: _cap(SDDigest, "endpoints")],
        pages_expected=_strs(out.get("pages_expected"), 24, 80),
        decisions=decisions[:12],
        open_questions=_strs(out.get("open_questions"), 10),
    )


def _code_files(out: dict, key: str = "files") -> dict[str, str]:
    files = {}
    for f in _items(out.get(key)):
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            files[f["path"].strip().lstrip("/")] = f.get("code") or f.get("content") or ""
    return files


def _placed(phase: str, files: dict[str, str]) -> dict[str, str]:
    """The same files at the paths the build will place them, so routes resolve by side."""
    placer = layout.Placer()
    return {
        placed: content
        for placed, _p, content in placer.place_all(phase, [(p, c) for p, c in files.items()])
        if placed
    }


def _be(out: dict) -> BEDigest:
    files = _code_files(out)
    placed = _placed(Phase.BACKEND_ENGINEER.value, files)
    served = routes.served(placed, layout.BACKEND)
    return BEDigest(
        rationale=_s(out.get("summary"), _RATIONALE),
        framework=_s(out.get("framework"), 60),
        files=[
            _FileRef(path=_s(f.get("path"), 120), purpose=_s(f.get("purpose"), 100))
            for f in _items(out.get("files"))
            if isinstance(f, dict) and _s(f.get("path"))
        ][: _cap(BEDigest, "files")],
        endpoints_implemented=[
            _Implemented(method=r.method, path=r.path, file=r.file) for r in served
        ][: _cap(BEDigest, "endpoints_implemented")],
        auth_flow=_s(out.get("auth_flow"), _RATIONALE),
        env_vars=routes.env_names(placed)[:30],
    )


def _fe(out: dict) -> FEDigest:
    files = _code_files(out)
    placed = _placed(Phase.FRONTEND_ENGINEER.value, files)
    used: dict[str, list[str]] = {}
    for call in routes.calls(placed):
        bucket = used.setdefault(call.file, [])
        label = f"{call.method} {call.path}"
        if label not in bucket:
            bucket.append(label)
    return FEDigest(
        rationale=_s(out.get("summary"), _RATIONALE),
        framework=_s(out.get("framework"), 60),
        pages=[
            _PageRef(route=_s(p.get("route"), 80), purpose=_s(p.get("purpose"), 100))
            for p in _items(out.get("pages"))
            if isinstance(p, dict) and _s(p.get("route"))
        ][: _cap(FEDigest, "pages")],
        components=[
            _s(c.get("name"), 60) for c in _items(out.get("components")) if isinstance(c, dict) and _s(c.get("name"))
        ][: _cap(FEDigest, "components")],
        files=[
            _FEFile(path=p, endpoints_used=used.get(p, [])[:12]) for p in placed
        ][: _cap(FEDigest, "files")],
    )


def _qa(out: dict) -> QADigest:
    tests = [t for t in _items(out.get("test_files")) if isinstance(t, dict)]
    frameworks = list(dict.fromkeys(_s(t.get("framework"), 30) for t in tests if _s(t.get("framework"))))
    return QADigest(
        rationale=_s(out.get("summary"), _RATIONALE),
        frameworks=frameworks[:6],
        test_files=[_s(t.get("path"), 120) for t in tests if _s(t.get("path"))][:60],
        command_backend=_s(out.get("command_backend"), 120),
        command_frontend=_s(out.get("command_frontend"), 120),
    )


def _security(out: dict) -> SecurityDigest:
    return SecurityDigest(
        rationale=_s(out.get("summary"), _RATIONALE),
        findings=[
            _Finding(severity=_s(f.get("severity"), 12), title=_s(f.get("title"), 100), location=_s(f.get("location"), 100))
            for f in _items(out.get("findings"))
            if isinstance(f, dict)
        ][:20],
        overall_posture=_s(out.get("overall_posture")),
    )


def _devops(out: dict) -> DevOpsDigest:
    notes = out.get("deployment_notes") if isinstance(out.get("deployment_notes"), dict) else {}
    ci = out.get("ci_workflow") if isinstance(out.get("ci_workflow"), dict) else {}
    return DevOpsDigest(
        rationale=_s(out.get("summary"), _RATIONALE),
        env_vars=[
            _s(v.get("name"), 60) for v in _items(notes.get("env_vars")) if isinstance(v, dict) and _s(v.get("name"))
        ][:30],
        health_check_path=_s(notes.get("health_check_path"), 80),
        migration_command=_s(notes.get("migration_command"), 120),
        ci_workflow=_s(ci.get("path"), 120),
    )


def _num(value: Any) -> Optional[float]:
    from app.core.reading import as_number

    return as_number(value)


def _cost(out: dict) -> CostDigest:
    return CostDigest(
        rationale=_s(out.get("summary"), _RATIONALE),
        total_monthly_low_usd=_num(out.get("total_monthly_low_usd")),
        total_monthly_high_usd=_num(out.get("total_monthly_high_usd")),
        estimated_timeline_weeks=_num(out.get("estimated_timeline_weeks")),
        assumptions=_strs(out.get("assumptions"), 10),
    )


_BUILDERS = {
    Phase.PRODUCT_MANAGER.value: _pm,
    Phase.SYSTEM_DESIGN.value: _sd,
    Phase.BACKEND_ENGINEER.value: _be,
    Phase.FRONTEND_ENGINEER.value: _fe,
    Phase.QA_ENGINEER.value: _qa,
    Phase.SECURITY_ENGINEER.value: _security,
    Phase.DEVOPS_ENGINEER.value: _devops,
    Phase.COST_ESTIMATION.value: _cost,
}


def digest(phase: str, output: Any) -> dict:
    """The typed digest of one phase's output, as plain JSON. `{}` for an unknown phase.

    Built from whatever the phase returned, valid or not — an invalid Backend output
    still has file paths worth handing on — and never raises: a digest that cannot be
    built is an empty one, and the full output still follows it.
    """
    build = _BUILDERS.get(phase)
    if build is None or not isinstance(output, dict):
        return {}
    try:
        return build(output).model_dump(mode="json", exclude_defaults=True)
    except (ValidationError, Exception) as e:  # noqa: BLE001 - a digest never fails a phase
        log.warning("Couldn't build the %s digest (the full output still goes): %s", phase, e)
        return {}


def fit(d: dict, limit: int) -> str:
    """The digest as JSON in at most `limit` characters, shortening its longest lists.

    Digests are capped per field already; this is the bound for a small window, where
    three of them must still leave room for the instructions. Lists are halved from
    the longest down — a digest with ten endpoints and no file list is worth less than
    one with five of each — and `_trimmed` says which were shortened.
    """
    d = json.loads(_dumps(d))
    text = _dumps(d)
    trimmed: dict[str, int] = {}
    while len(text) > limit:
        lists = [(len(_dumps(v)), k) for k, v in d.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            # Nothing left to shorten but prose: cut the rationale, then give up on
            # being smaller than the floor — the instructions still come first.
            if isinstance(d.get("rationale"), str) and len(d["rationale"]) > 60:
                d["rationale"] = d["rationale"][:60] + "…"
                text = _dumps(d)
                continue
            break
        _, key = max(lists)
        total = trimmed.get(key) or len(d[key])
        d[key] = d[key][: max(len(d[key]) // 2, 1)]
        trimmed[key] = total
        d["_trimmed"] = {k: f"{len(d[k])} of {n}" for k, n in trimmed.items()}
        text = _dumps(d)
    return text


# ── the name registry ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Registry:
    entities: tuple[str, ...] = ()
    endpoints: tuple[str, ...] = ()  # "GET /api/todos"
    pages: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(e.split(" ", 1)[-1] for e in self.endpoints)

    def __bool__(self) -> bool:
        return bool(self.entities or self.endpoints or self.pages)

    def prompt_block(self) -> str:
        lines = [
            "NAME REGISTRY — System Design fixed these names. Use exactly these, spelled the "
            "same: no synonyms, no plurals changed, no new names for the same thing."
        ]
        if self.entities:
            lines.append("- Entities: " + ", ".join(self.entities))
        if self.endpoints:
            lines.append("- Endpoints: " + "; ".join(self.endpoints))
        if self.pages:
            lines.append("- Pages: " + ", ".join(self.pages))
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"entities": list(self.entities), "endpoints": list(self.endpoints), "pages": list(self.pages)}


def registry(prior_outputs: dict) -> Registry:
    """System Design's names, from its output in this build. Empty before it has run.

    Read from the output the charter was frozen from, so it is frozen with it: the two
    change together, when System Design itself is re-run, and never otherwise.
    """
    design = prior_outputs.get(Phase.SYSTEM_DESIGN.value) if isinstance(prior_outputs, dict) else None
    if not isinstance(design, dict):
        return Registry()
    d = _sd(design)
    return Registry(
        entities=tuple(dict.fromkeys(e.name for e in d.entities if e.name)),
        endpoints=tuple(dict.fromkeys(f"{e.method or 'GET'} {e.path}" for e in d.endpoints)),
        pages=tuple(dict.fromkeys(d.pages_expected)),
    )


# ── rendering one dependency ─────────────────────────────────────────────────
DEP_FRAME = "# From {dep}\n```json\n{body}\n```\n"
#: What the skeleton prints for the full output, and what a dependency gets when not
#: even one field fits. Constant, so the overhead measured from it is exact.
_OMITTED = {"_cut": "all"}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass
class Rendered:
    """One dependency's frame, and what of it the agent actually saw."""

    phase: str
    text: str
    full: str  # whole | cut | digest_only
    omitted: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return {"phase": self.phase, "digest": True, "full": self.full, "omitted": self.omitted}


#: Keys holding code, rendered last whatever order the model wrote them in.
_CODE_KEYS = ("files", "test_files", "db_models", "dockerfiles", "compose_or_manifests", "ci_cd", "ci_workflow")


def _ordered(output: dict) -> list[tuple[str, Any]]:
    head = [(k, v) for k, v in output.items() if k not in _CODE_KEYS]
    tail = [(k, output[k]) for k in _CODE_KEYS if k in output]
    return head + tail


def full_cost(output: dict) -> int:
    """What the whole output costs over the skeleton's omitted note."""
    return len(_dumps(dict(_ordered(output)))) - len(_dumps(_OMITTED))


def _cut_output(output: dict, room: int) -> tuple[str, list[str]]:
    """The output as JSON in at most `len(_OMITTED json) + room` characters.

    Whole top-level fields in order (code last); a list that does not fit whole keeps
    as many whole items as fit. What was left out is named in a `_cut` key inside the
    object. Returns (json, omitted) — `omitted` empty means it went whole.
    """
    limit = len(_dumps(_OMITTED)) + max(room, 0)
    whole = _dumps(dict(_ordered(output)))
    if len(whole) <= limit:
        return whole, []

    kept: list[tuple[str, str]] = []  # (key json, value json)
    omitted: list[str] = []

    def size(parts: list[tuple[str, str]], note: str) -> int:
        body = [f"{k}: {v}" for k, v in parts] + [f'"_cut": {_dumps(note)}']
        return 2 + sum(len(b) for b in body) + 2 * (len(body) - 1)

    def note_for(names: list[str]) -> str:
        return "omitted to fit this model's window: " + ", ".join(names) + " (the digest lists them)"

    # Reserve for the note naming every key, a safe upper bound on the real one.
    worst = note_for([f"{k}[999-999 of 999]" for k, _ in _ordered(output)])
    for key, value in _ordered(output):
        kj, vj = _dumps(key), _dumps(value)
        if size(kept + [(kj, vj)], worst) <= limit:
            kept.append((kj, vj))
            continue
        if isinstance(value, list) and value:
            items: list[str] = []
            for item in value:
                trial = "[" + ", ".join(items + [_dumps(item)]) + "]"
                if size(kept + [(kj, trial)], worst) > limit:
                    break
                items.append(_dumps(item))
            if items:
                kept.append((kj, "[" + ", ".join(items) + "]"))
                omitted.append(f"{key}[{len(items) + 1}-{len(value)} of {len(value)}]")
                continue
        omitted.append(key)
    note = note_for(omitted)
    body = [f"{k}: {v}" for k, v in kept] + [f'"_cut": {_dumps(note)}']
    text = "{" + ", ".join(body) + "}"
    if len(text) > limit:
        return _dumps(_OMITTED), [k for k, _ in _ordered(output)]
    return text, omitted


def render(phase: str, output: dict, room: Optional[int], digest_json: Optional[str] = None) -> Rendered:
    """The frame for one dependency. `room=None` (or 0) is the skeleton: digest only.

    `room` is what this dependency may spend *beyond* the skeleton. The result never
    costs more than `len(skeleton) + room`, so the prompt the budget was measured
    from and the prompt sent cannot disagree.
    """
    dj = digest_json if digest_json is not None else _dumps(digest(phase, output))
    if not room or room <= 0 or not isinstance(output, dict):
        body = '{"digest": ' + dj + ', "output": ' + _dumps(_OMITTED) + "}"
        return Rendered(phase, DEP_FRAME.format(dep=phase, body=body), "digest_only", ["*"])
    out_json, omitted = _cut_output(output, room)
    body = '{"digest": ' + dj + ', "output": ' + out_json + "}"
    if out_json == _dumps(_OMITTED):
        state = "digest_only"
    else:
        state = "cut" if omitted else "whole"
    return Rendered(phase, DEP_FRAME.format(dep=phase, body=body), state, omitted)


def share(wants: dict[str, int], budget: int) -> dict[str, int]:
    """Split `budget` so a small dependency's unused share goes to the larger ones.

    Water-filling: the smallest want is met first, and what it does not need is
    divided among the rest — rather than every dependency getting `budget / n` and a
    3 KB spec sitting on room a 200 KB backend was cut short for.
    """
    out = {k: 0 for k in wants}
    left = max(budget, 0)
    pending = sorted(wants, key=lambda k: wants[k])
    while pending:
        each = left // len(pending)
        k = pending.pop(0)
        give = min(max(wants[k], 0), each)
        out[k] = give
        left -= give
    return out
