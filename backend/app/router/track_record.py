"""How each model has done for each agent, in one account's own builds (#96).

Auto used to send hard work to the first cloud key in a fixed order and everything
else to the local default. It never compared two local models, and it never looked at
what had actually happened — although every finished phase already says how it went:
whether its output matched its shape the first time, whether its code built, whether
QA's tests passed, whether a reply was cut off at the output limit, whether the chain
had to fall back off the model it started on.

This module turns those rows into a ranking with a reason, and nothing else: rows in,
a choice and a sentence out, no database and no router. `history` reads the rows;
the router hands over the candidates, best first by what it already knows (`prior`),
and asks `choose`.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, Mapping, Optional, Sequence

#: Fewer finished runs than this for an agent on a model, and the router's own
#: knowledge decides instead. Two runs are an anecdote.
MIN_SAMPLE = 3
#: Only the most recent runs count, so a model is judged on how it does now.
WINDOW = 20

#: What a schema outcome is worth: matched first time, matched after being sent back
#: with the errors, still didn't match.
_SCHEMA = {"valid": 1.0, "repaired": 0.5, "invalid": 0.0}

#: How a choice was made, for whoever shows it.
BASIS_RECORD = "record"
BASIS_PRIOR = "prior"


@dataclass(frozen=True)
class Outcome:
    """One finished run of one agent on one model, reduced to what judging it needs.

    Every fact but `failed` can be unknown. A row written before a check existed
    can't answer for it, and an unknown is left out of the score rather than counted
    as a pass or a fail.
    """

    model: str
    #: valid | repaired | invalid, or None when the row doesn't say.
    schema: Optional[str] = None
    #: The code built in the sandbox, or compiled where it wasn't built. None for a
    #: phase that writes no code, and for code nothing could check.
    built: Optional[bool] = None
    #: QA's tests passed. None for every other agent, and when they didn't run.
    tested: Optional[bool] = None
    #: A reply was cut off at the model's output limit.
    cut_off: Optional[bool] = None
    #: The chain had to fall back off this model for one of the phase's calls.
    fell_back: Optional[bool] = None
    #: The model never delivered: the provider failed, or it produced nothing.
    failed: bool = False
    latency_ms: int = 0

    def score(self) -> float:
        """0 to 1. The mean of what was graded, with a zero for each mishap.

        A mishap (cut off, fell back) adds a zero only when it happened: "not cut
        off" is the normal case, and counting it as a pass would water down every
        real result beside it.
        """
        if self.failed:
            return 0.0
        parts: list[float] = []
        if self.schema is not None:
            parts.append(_SCHEMA.get(self.schema, 0.0))
        for graded in (self.built, self.tested):
            if graded is not None:
                parts.append(1.0 if graded else 0.0)
        for mishap in (self.cut_off, self.fell_back):
            if mishap:
                parts.append(0.0)
        # Finished, and nothing on the row says how well: it did deliver.
        return sum(parts) / len(parts) if parts else 1.0


@dataclass(frozen=True)
class Record:
    """One model's record for one agent, over its most recent runs."""

    model: str
    sample: int
    score: float
    #: The middle run's time, for breaking ties and nothing else.
    latency_ms: int
    #: "valid first try in 5 of 5 runs, built in 4 of 5".
    reason: str


@dataclass(frozen=True)
class Pick:
    """The model Auto runs an agent on, and why."""

    model: str
    reason: str
    #: How many runs the reason rests on: 0 when it rests on none.
    sample: int
    basis: str


def judge(model: str, outcomes: Iterable[Outcome]) -> Record:
    """`model`'s record from its runs, newest first. Only the last `WINDOW` count."""
    recent = list(outcomes)[:WINDOW]
    if not recent:
        return Record(model=model, sample=0, score=0.0, latency_ms=0, reason="no record yet")
    timed = [o.latency_ms for o in recent if o.latency_ms and not o.failed]
    return Record(
        model=model,
        sample=len(recent),
        score=sum(o.score() for o in recent) / len(recent),
        latency_ms=int(median(timed)) if timed else 0,
        reason=_reason(recent),
    )


def _reason(recent: Sequence[Outcome]) -> str:
    n = len(recent)
    delivered = [o for o in recent if not o.failed]
    # The headline is the first-try rate when the rows can say it. A run that never
    # delivered didn't match first time either, so it stays in the count.
    judged = [o for o in delivered if o.schema is not None]
    if judged:
        first = sum(1 for o in judged if o.schema == "valid")
        out = [f"valid first try in {first} of {len(judged) + n - len(delivered)} runs"]
    else:
        out = [f"finished {len(delivered)} of {n} runs"]

    def tally(facts: list[Optional[bool]], words: str) -> None:
        known = [f for f in facts if f is not None]
        if known:
            out.append(f"{words} in {sum(1 for f in known if f)} of {len(known)}")

    tally([o.built for o in delivered], "built")
    tally([o.tested for o in delivered], "tests passed")
    failed = n - len(delivered)
    if failed:
        out.append(f"failed in {failed}")
    for attr, words in (("cut_off", "cut off"), ("fell_back", "fell back")):
        count = sum(1 for o in delivered if getattr(o, attr))
        if count:
            out.append(f"{words} in {count}")
    return ", ".join(out)


def choose(prior: Sequence[tuple[str, str]], records: Mapping[str, Record]) -> Optional[Pick]:
    """The model to run, from the candidates in `prior` and their records.

    `prior` is every candidate with the reason the router would give for it, best
    first by what the router already knows: the strongest cloud model for a hard
    step, the largest local model that fits this computer, the default.

    - The router's first choice has too few runs to judge: it runs. That is how a
      model gets a record at all — nothing here explores at random, in somebody's
      build, to find out.
    - Otherwise the best record among the candidates that have one wins. Speed
      breaks a tie and nothing else; the router's order breaks what is left.
    """
    if not prior:
        return None
    head, why = prior[0]
    judged = [
        (index, records[model])
        for index, (model, _) in enumerate(prior)
        if model in records and records[model].sample >= MIN_SAMPLE
    ]
    if not any(record.model == head for _, record in judged):
        n = records[head].sample if head in records else 0
        lead = "no record yet" if n == 0 else f"{n} run{'' if n == 1 else 's'} on record, too few to judge"
        return Pick(model=head, reason=f"{lead}, so {why}", sample=n, basis=BASIS_PRIOR)
    _, best = min(
        judged,
        # An unknown time loses a tie rather than winning it.
        key=lambda item: (-round(item[1].score, 3), item[1].latency_ms or float("inf"), item[0]),
    )
    return Pick(model=best.model, reason=best.reason, sample=best.sample, basis=BASIS_RECORD)
