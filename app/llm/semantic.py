"""The semantic judge used by fuzzy SOP conditions.

One generic judge serves every `semantic:` condition in the policy set, so a new fuzzy
rule is still just a YAML file. The judge decides only whether a condition holds -- the
advice text it unlocks is entirely the policy's.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.client import LLMUnavailable, load_prompt, structured
from app.weather.snapshot import WeatherSnapshot


class Verdict(BaseModel):
    matches: bool = Field(description="Does the condition hold for these figures?")
    reason: str = Field(description="One sentence, referring only to the given figures.")


def judge(condition: str, snapshot: WeatherSnapshot) -> tuple[bool, str]:
    """Evaluate one prose condition. Fails closed.

    If the model is unreachable the condition is treated as not holding, because a rule
    we cannot assess must not fire. Every fuzzy policy in the set is paired with a
    deterministic backstop so a clearly bad day is still caught when this returns False.
    """
    user = (
        "FACTS\n"
        f"{snapshot.fact_block()}\n\n"
        "CONDITION TO EVALUATE\n"
        f"{condition.strip()}"
    )
    try:
        verdict = structured(load_prompt("semantic_judge"), user, Verdict)
    except LLMUnavailable:
        return False, ""
    return bool(verdict.matches), verdict.reason.strip()
