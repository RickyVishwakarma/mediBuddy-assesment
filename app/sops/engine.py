"""The SOP matching engine: a small condition DSL plus deterministic ranking.

Deliberately has no LLM import. The semantic judge used by fuzzy rules is injected as
a callable, which keeps this module pure, unit-testable without network access, and
makes "which SOPs matched" a deterministic function of (snapshot, intent) for every
non-fuzzy rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.sops.schema import SOP
from app.weather.snapshot import WeatherSnapshot

# A judge answers one question: does this snapshot satisfy this prose condition?
SemanticJudge = Callable[[str, WeatherSnapshot], tuple[bool, str]]


@dataclass
class MatchedSOP:
    sop: SOP
    specificity: int                       # count of leaf conditions that fired
    reasons: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.sop.id


def _compare(op: str, actual, expected) -> bool:
    if op == "is_true":
        return bool(actual) is True
    if op == "is_false":
        return bool(actual) is False
    if op == "eq":
        return actual == expected
    if op == "neq":
        return actual != expected
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected

    # Ordered comparisons need numbers on both sides.
    try:
        left = float(actual)
    except (TypeError, ValueError):
        return False

    if op == "between":
        try:
            low, high = float(expected[0]), float(expected[1])
        except (TypeError, ValueError, IndexError):
            return False
        return low <= left <= high

    try:
        right = float(expected)
    except (TypeError, ValueError):
        return False

    return {
        "gte": left >= right,
        "gt": left > right,
        "lte": left <= right,
        "lt": left < right,
    }[op]


def evaluate(
    node: dict,
    snapshot: WeatherSnapshot,
    judge: SemanticJudge | None = None,
    reasons: list[str] | None = None,
) -> tuple[bool, int]:
    """Evaluate one condition node. Returns (matched, leaves_that_fired).

    The leaf count is the specificity score used to break severity ties: a rule that
    had to satisfy four conditions is a more precise description of the situation than
    one that satisfied a single threshold.
    """
    reasons = reasons if reasons is not None else []

    if "all" in node:
        total = 0
        for child in node["all"]:
            ok, count = evaluate(child, snapshot, judge, reasons)
            if not ok:
                return False, 0
            total += count
        return True, total

    if "any" in node:
        best = 0
        matched = False
        for child in node["any"]:
            ok, count = evaluate(child, snapshot, judge, reasons)
            if ok:
                matched = True
                best = max(best, count)
        return matched, best

    if "not" in node:
        ok, _ = evaluate(node["not"], snapshot, judge, reasons)
        return (not ok), (1 if not ok else 0)

    if "semantic" in node:
        if judge is None:
            # No judge available (e.g. the LLM is unreachable): a fuzzy rule cannot be
            # asserted, so it does not fire. Failing closed keeps us from inventing a match.
            return False, 0
        verdict, why = judge(node["semantic"], snapshot)
        if verdict and why:
            reasons.append(why)
        return bool(verdict), (1 if verdict else 0)

    # Leaf.
    name = node["field"]
    if name not in snapshot.fields:
        return False, 0
    actual = snapshot.fields[name]
    if actual is None:
        # We don't have this reading. An absent value must never be read as zero.
        return False, 0
    fired = _compare(node["op"], actual, node.get("value"))
    return fired, (1 if fired else 0)


def match_policies(
    policies: list[SOP],
    snapshot: WeatherSnapshot,
    activity: str | None,
    judge: SemanticJudge | None = None,
) -> list[MatchedSOP]:
    """Return every SOP whose activity scope and conditions both hold."""
    matches: list[MatchedSOP] = []
    for sop in policies:
        if not sop.applies_to.covers(activity):
            continue
        reasons: list[str] = []
        ok, specificity = evaluate(sop.match, snapshot, judge, reasons)
        if ok:
            matches.append(MatchedSOP(sop=sop, specificity=specificity, reasons=reasons))
    return matches


def rank(matches: list[MatchedSOP]) -> list[MatchedSOP]:
    """Deterministic ordering. Documented in the README because the brief asks us to
    choose on purpose and defend it:

      1. override rules pre-empt everything else
      2. then higher severity
      3. then higher specificity (more conditions had to hold)
      4. then SOP id, purely so the order is stable across runs
    """
    return sorted(
        matches,
        key=lambda m: (
            not m.sop.override,
            -m.sop.severity_rank,
            -m.specificity,
            m.sop.id,
        ),
    )


def select(matches: list[MatchedSOP], max_secondary: int = 2) -> tuple[MatchedSOP | None, list[MatchedSOP]]:
    """Split ranked matches into the one that leads and the ones mentioned briefly.

    We surface more than one because suppressing a second genuine hazard is a safety
    regression -- but only one leads, because a list of equal warnings gets ignored.

    Rules flagged `only_if_alone` assert that nothing notable was found, so they are
    dropped as soon as something else matched. Without this the all-clear gets listed
    under a warning and the reply contradicts itself: "postpone the ride" followed by
    "nothing notable, go ahead as planned".
    """
    if not matches:
        return None, []

    substantive = [m for m in matches if not m.sop.only_if_alone]
    ordered = rank(substantive if substantive else matches)
    return ordered[0], ordered[1 : 1 + max_secondary]
