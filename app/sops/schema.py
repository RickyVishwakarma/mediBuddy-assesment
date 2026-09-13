"""Validation for SOP policy files.

Policy files are authored by hand, so a malformed one must fail loudly at load time
rather than silently never matching. A rule that quietly stops firing is the worst
failure mode this system has.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.vocabulary import SNAPSHOT_FIELDS

# Ordered least -> most severe. Index is the rank used when ranking matches.
SEVERITY_ORDER = ["info", "advisory", "caution", "warning", "danger"]

LEAF_OPS = {"gte", "gt", "lte", "lt", "eq", "neq", "in", "not_in", "between", "is_true", "is_false"}

ANY_ACTIVITY = "*"


class SOPValidationError(Exception):
    pass


def _validate_condition(node: Any, path: str = "match") -> None:
    """Walk the condition tree and reject anything the engine could not evaluate."""
    if not isinstance(node, dict):
        raise SOPValidationError(f"{path}: expected a mapping, got {type(node).__name__}")

    keys = set(node)

    if "all" in keys or "any" in keys:
        key = "all" if "all" in keys else "any"
        if len(keys) != 1:
            raise SOPValidationError(f"{path}: '{key}' must be the only key in its block")
        if not isinstance(node[key], list) or not node[key]:
            raise SOPValidationError(f"{path}.{key}: expected a non-empty list")
        for i, child in enumerate(node[key]):
            _validate_condition(child, f"{path}.{key}[{i}]")
        return

    if "not" in keys:
        if len(keys) != 1:
            raise SOPValidationError(f"{path}: 'not' must be the only key in its block")
        _validate_condition(node["not"], f"{path}.not")
        return

    if "semantic" in keys:
        if len(keys) != 1:
            raise SOPValidationError(f"{path}: 'semantic' must be the only key in its block")
        if not isinstance(node["semantic"], str) or not node["semantic"].strip():
            raise SOPValidationError(f"{path}.semantic: expected a non-empty description")
        return

    if "field" in keys:
        # Reject unknown field names at load time. Without this a typo -- say
        # `temperature_celsius` for `temperature_c` -- is accepted happily and the rule
        # then never fires, because an absent field evaluates to False. A safety policy
        # that looks live but is dead is the worst outcome this system has, and it is
        # exactly the mistake a policy author working without the Python would make.
        name = node["field"]
        if name not in SNAPSHOT_FIELDS:
            close = difflib.get_close_matches(str(name), SNAPSHOT_FIELDS, n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise SOPValidationError(
                f"{path}: unknown snapshot field {name!r}. A rule referring to a field "
                f"that does not exist would never fire.{hint} "
                f"See app/vocabulary.py for the full list."
            )

        op = node.get("op")
        if op not in LEAF_OPS:
            raise SOPValidationError(
                f"{path}: unknown op {op!r}; expected one of {sorted(LEAF_OPS)}"
            )
        if op in {"is_true", "is_false"}:
            return
        if "value" not in node:
            raise SOPValidationError(f"{path}: op {op!r} requires a 'value'")
        if op in {"in", "not_in"} and not isinstance(node["value"], list):
            raise SOPValidationError(f"{path}: op {op!r} requires a list value")
        if op == "between":
            value = node["value"]
            if not isinstance(value, list) or len(value) != 2:
                raise SOPValidationError(f"{path}: op 'between' requires a [low, high] value")
        return

    raise SOPValidationError(
        f"{path}: block must contain one of 'all', 'any', 'not', 'semantic', or 'field'"
    )


class AppliesTo(BaseModel):
    activities: list[str] = Field(default_factory=lambda: [ANY_ACTIVITY])
    intent_hint: str = ""

    @field_validator("activities")
    @classmethod
    def _lower(cls, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values] or [ANY_ACTIVITY]

    def covers(self, activity: str | None) -> bool:
        if ANY_ACTIVITY in self.activities:
            return True
        if not activity:
            return False
        return activity.strip().lower() in self.activities


class SOP(BaseModel):
    id: str
    title: str
    category: str
    severity: Literal["info", "advisory", "caution", "warning", "danger"]
    override: bool = False
    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    match: dict[str, Any]

    # What the user is told. Must read as guidance addressed to a person, because the
    # deterministic fallback prints it verbatim when the model is unavailable.
    advice: str

    # Directions to the composing model -- which figures to surface, what tone to avoid.
    # Kept separate from `advice` precisely because the fallback path would otherwise
    # read authoring instructions out to the user ("Quote the UV index figure...").
    compose_notes: str = ""

    rationale: str = ""

    # Set by the loader so error messages can name the offending file.
    source_file: str = ""

    @model_validator(mode="after")
    def _check_match(self) -> "SOP":
        _validate_condition(self.match)
        if not self.advice.strip():
            raise SOPValidationError(f"{self.id}: 'advice' must not be empty")
        return self

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.index(self.severity)

    def referenced_fields(self) -> set[str]:
        """Snapshot fields this policy's conditions actually test.

        Lets the deterministic fallback show the readings that drove the decision,
        rather than every field it happens to hold.
        """
        found: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if "field" in node and isinstance(node["field"], str):
                    found.add(node["field"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.match)
        return found

    def numeric_literals(self) -> set[str]:
        """Every number this policy itself contains -- both the thresholds in its match
        conditions and any figure written into its advice text.

        The grounding guard allows these through, so a reply may quote its own rule
        ("gusts above 50 km/h") or repeat a policy-authored instruction ("wait 30 minutes
        after the last thunder") without being rejected. These numbers are safe precisely
        because a human wrote them into a reviewed policy file -- they are as auditable as
        the API's own values, and they are the ONLY non-API numbers permitted.
        """
        found: set[str] = set()

        def add(value: float) -> None:
            found.add(f"{value:.1f}")
            found.add(f"{value:.0f}")
            found.add(str(value))
            if float(value).is_integer():
                found.add(str(int(value)))

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, (int, float)) and not isinstance(node, bool):
                add(float(node))

        walk(self.match)
        text = f"{self.advice}\n{self.compose_notes}\n{self.title}"
        for token in re.findall(r"\d+(?:\.\d+)?", text):
            add(float(token))
        return found
