"""Deterministic checking of NON-numeric weather claims.

The numeric guard in grounding.py answers "did this figure come from the API". It cannot
answer "is this statement true", and that gap produced real bugs: a reply asserting "the
storm covers only part of today" from a snapshot holding nothing but a true/false flag,
and another describing a road as wet on a day with 0.0 mm of rain. Every figure in both
was genuine; the sentence around them was not.

So this module does for propositions what the allow-set does for numbers. Each entry pairs
a phrase pattern with a predicate over the snapshot. If the draft makes the claim and the
snapshot does not support it, the reply is rejected exactly as an ungrounded number is.

Two properties make this safe to rely on:

  * It is a *table*, not a model. Every rejection traces to one named rule that a reviewer
    can read, argue with, and change -- the same reason policies live in YAML.
  * It fails toward acceptance. A proposition nobody has written a check for passes
    unexamined. This narrows the gap; it does not close it, and the write-up says so.

Adding a check is adding a row. Keep them unambiguous: a pattern that fires on ordinary
advice wording is worse than no pattern at all, because over-rejection silently degrades
good answers (that has happened here too -- see defect 3 in EVAL_RESULTS.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from app.weather.snapshot import WeatherSnapshot


@dataclass(frozen=True)
class ClaimCheck:
    name: str
    pattern: re.Pattern[str]
    holds: Callable[[WeatherSnapshot], bool]
    message: str
    # Only test the claim when this is true of the snapshot. Lets a check stay silent
    # where the data cannot settle the question either way.
    applies: Callable[[WeatherSnapshot], bool] = lambda _s: True


def _f(snapshot: WeatherSnapshot, field: str, default: float = 0.0) -> float:
    value = snapshot.get(field)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _has(snapshot: WeatherSnapshot, field: str) -> bool:
    return snapshot.get(field) is not None


# Ordered for readability, not precedence -- every check runs.
CHECKS: list[ClaimCheck] = [
    ClaimCheck(
        name="dry",
        # "it's dry", "stays dry", "no rain". Not "dry your kit" or "dry clothing".
        pattern=re.compile(
            r"\b(?:it(?:'s| is)\s+dry\b|stay(?:s|ing)?\s+dry\b|remain(?:s)?\s+dry\b"
            r"|no\s+rain\b|rain[-\s]free\b|without\s+rain\b)",
            re.IGNORECASE,
        ),
        applies=lambda s: _has(s, "precipitation_mm"),
        holds=lambda s: _f(s, "precipitation_mm") < 0.2,
        message="claims it is dry, but the forecast shows precipitation",
    ),
    ClaimCheck(
        name="wet_roads",
        pattern=re.compile(
            r"\b(?:water\s+(?:on|sitting\s+on)\s+the\s+road|standing\s+water"
            r"|wet\s+road|water\s+coming\s+down|surface\s+water)\b",
            re.IGNORECASE,
        ),
        applies=lambda s: _has(s, "precipitation_mm"),
        holds=lambda s: _f(s, "precipitation_mm") >= 0.2 or _f(s, "rain_24h_mm") >= 1.0,
        message="describes water on the road, but the forecast shows no precipitation",
    ),
    ClaimCheck(
        name="clear_sky",
        pattern=re.compile(r"\b(?:clear\s+skies?|bright\s+sunshine|cloudless)\b", re.IGNORECASE),
        applies=lambda s: _has(s, "clear_sky"),
        holds=lambda s: bool(s.get("clear_sky")),
        message="claims clear skies, but the forecast does not show them",
    ),
    ClaimCheck(
        name="storm_present",
        pattern=re.compile(r"\b(?:thunderstorm|lightning|thunder)\b", re.IGNORECASE),
        applies=lambda s: _has(s, "thunderstorm_in_window"),
        holds=lambda s: bool(s.get("thunderstorm_in_window")),
        message="refers to a thunderstorm, but none is forecast for this window",
    ),
    ClaimCheck(
        name="storm_absent",
        pattern=re.compile(
            r"\bno\s+(?:thunderstorms?|lightning|thunder)\b|storm[-\s]free\b", re.IGNORECASE
        ),
        applies=lambda s: _has(s, "thunderstorm_in_window"),
        holds=lambda s: not s.get("thunderstorm_in_window"),
        message="says there is no thunderstorm, but one is forecast",
    ),
    ClaimCheck(
        name="partial_coverage",
        # The exact bug this module was written for: "only part of the day", "a short
        # spell", "it'll clear up" -- asserting a gap in the weather we cannot see.
        pattern=re.compile(
            r"\b(?:only\s+(?:part|some)\s+of\b|part\s+of\s+the\s+(?:day|window|afternoon|morning|evening)"
            r"|for\s+(?:a\s+)?(?:short|brief)\s+(?:spell|while|period)"
            r"|clears?\s+(?:up|later)\b|pass(?:es|ing)\s+(?:over|through)\b)",
            re.IGNORECASE,
        ),
        applies=lambda s: bool(s.get("thunderstorm_in_window")) and _has(s, "window_hours"),
        holds=lambda s: not s.get("thunderstorm_covers_whole_window"),
        message="says the weather covers only part of the window, but it covers all of it",
    ),
    ClaimCheck(
        name="calm_wind",
        pattern=re.compile(
            r"\b(?:calm\s+(?:conditions|winds?|air)|light\s+winds?|little\s+wind|barely\s+any\s+wind)\b",
            re.IGNORECASE,
        ),
        applies=lambda s: _has(s, "wind_speed_kmh"),
        holds=lambda s: _f(s, "wind_speed_kmh") < 20.0 and _f(s, "wind_gusts_kmh") < 30.0,
        message="describes the wind as light, but the forecast shows it is not",
    ),
    ClaimCheck(
        name="good_visibility",
        pattern=re.compile(
            r"\b(?:good|clear|fine|excellent)\s+visibility\b|\bsee\s+clearly\b", re.IGNORECASE
        ),
        applies=lambda s: _has(s, "visibility_km"),
        holds=lambda s: _f(s, "visibility_km", 99.0) > 5.0,
        message="claims good visibility, but the forecast shows it is reduced",
    ),
    ClaimCheck(
        name="heavy_rain",
        pattern=re.compile(r"\bheavy\s+rain(?:fall)?\b|\btorrential\b|\bdownpour\b", re.IGNORECASE),
        applies=lambda s: _has(s, "rain_24h_mm"),
        holds=lambda s: _f(s, "rain_24h_mm") >= 35.0 or _f(s, "precipitation_mm") >= 4.0,
        message="describes heavy rain, but the forecast does not show it",
    ),
    ClaimCheck(
        name="freezing",
        pattern=re.compile(r"\b(?:below\s+freezing|sub[-\s]zero|icy?\b|black\s+ice)\b", re.IGNORECASE),
        applies=lambda s: _has(s, "temp_min_24h_c"),
        holds=lambda s: _f(s, "temp_min_24h_c", 99.0) <= 2.0,
        message="refers to freezing conditions, but the forecast is above freezing",
    ),
]


def check_claims(draft: str, snapshot: WeatherSnapshot) -> list[str]:
    """Return one message per unsupported claim. Empty means nothing was contradicted."""
    problems: list[str] = []
    for check in CHECKS:
        if not check.pattern.search(draft):
            continue
        if not check.applies(snapshot):
            continue
        if not check.holds(snapshot):
            problems.append(f"{check.name}: {check.message}")
    return problems
