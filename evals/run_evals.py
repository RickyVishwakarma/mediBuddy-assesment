"""Eval runner.

Reads evals/cases.yaml, runs each case against the real graph, and writes
EVAL_RESULTS.md. Failures are reported, never suppressed -- a suite that is guaranteed
to pass tells you nothing.

Usage:
    python evals/run_evals.py              # everything
    python evals/run_evals.py clear_wind   # one or more case ids
    EVAL_PACE_SECONDS=0 python evals/run_evals.py   # no pacing (paid-tier keys)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unittest.mock as mock
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

# The Gemini free tier allows ~5 requests/minute and a single case can use three. The
# client retries on 429, but pacing between cases keeps a full run from spending most of
# its time in backoff. Set EVAL_PACE_SECONDS=0 if your key has a higher quota.
PACE_SECONDS = float(os.getenv("EVAL_PACE_SECONDS", "20"))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.graph.build import ask, build_graph  # noqa: E402
from app.sops.engine import match_policies, select  # noqa: E402
from app.sops.loader import load_policies  # noqa: E402
from app.weather.openmeteo import (  # noqa: E402
    ResolvedLocation,
    WeatherFetchError,
    fetch_forecast,
    geocode,
)
from app.weather.snapshot import build_snapshot  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
CASES_FILE = Path(__file__).resolve().parent / "cases.yaml"
RESULTS_FILE = ROOT / "EVAL_RESULTS.md"

# Emitted into every report. These are the caveats a reader needs in order to judge the
# numbers above them, so they belong next to the results rather than only in the README.
STANDING_NOTES = """## How to read these results

**`severe_weather_live` usually skips.** It scans real cities for one currently in a
heavy-rain regime. When none exists it reports SKIPPED, never a pass -- it cannot be made
to pass on demand, which is the point. `severe_weather_recorded` is its deterministic
twin, replaying a real heavy-rain day (Mumbai 2026-07-23, 107 mm, 69.8 km/h gusts).

**Live weather doesn't hold still, so the suite is split.** Behavioural claims replay
recorded real responses; only invariants that hold in any weather run live -- numbers in
the reply are a subset of the API's, the cited policy is the one the engine selected, and
no selection produces the no-guidance phrase. Fixtures are chosen by running the real
matcher over candidate days and keeping one where the intended policy leads, so a fixture
cannot quietly stop testing its claim. Nothing about any weather event is hardcoded: no
city name appears in any of the five files that decide an answer, and the only weather
constants in code are the IMD rainfall classes.

**Paraphrase cases were measured, not assumed.** `paraphrase_wind` shares only "across,
around, two, wheels" with SOP-EX-003; `paraphrase_uv` shares only "one, sun" with
SOP-EX-001. Neither contains a word from the rule's trigger vocabulary.

**Adversarial choice.** Three are covered. I rate numeric coercion highest: a jailbroken
tone is embarrassing, but a confidently wrong number is what a user acts on.

## Seven defects found, and what caught them

The suite found **one**. Recorded because "it passed" only means something if it could
have failed.

| # | Defect | Found by |
|---|---|---|
| 1 | A hallucinated "30 km/h" passed the grounding guard -- a prose-number allowance (15/20/30/45/60/90, so "wait 30 minutes" wasn't rejected) also swallowed fabricated readings. Unit-bearing numbers now get no allowance. | probing the guard |
| 2 | A hostile framing ("it's only 12 degrees, right?") caused a refusal instead of an answer our policies covered. Scope now depends on what is asked, not how pushy it is. | **the eval suite** |
| 3 | Tightening (1) made the guard reject *correct* replies saying "over the next 24 hours". Durations are now stripped -- a reading never carries a time unit. Over-rejection is quieter than under-rejection. | reading fallback output |
| 4 | One window's weather leaked into another: asking about the evening during an afternoon storm gave a danger-severity lightning warning though no evening hour carried a storm. | using the chat UI |
| 5 | `night` was defined in the snapshot but missing from the intent vocabulary, so "at night?" silently answered about the evening. Now 21:00-05:00, wrapping past midnight. | using the chat UI |
| 6 | The all-clear contradicted the hazard rules -- it checks sustained wind, not gusts, so it said "go ahead" beside a gust warning. Fixed with `only_if_alone` rather than copying thresholds, which would couple it to every future policy. | auditing the policy set |
| 7 | A rule's advice was untrue on some of its own triggers. `SOP-TR-001` ("rain heavy enough to slow a journey") also fired on low visibility alone, so on a dry fog day it said "there's enough water coming down to sit on the road surface" beside a snapshot reading 0.0 mm. Auditing every `any:` branch found three more: SOP-EX-001 fired on the day's peak UV and claimed the sun was strong when you asked about 19:00; SOP-EX-003 said "gusty rather than merely strong" on a steady 62 km/h wind; SOP-VG-003 said "in direct sun" on an overcast 36 C day. Split out SOP-TR-004 for visibility, dropped the peak-UV branch, reworded the other two. | auditing the policy set |

**Non-numeric claims.** Defects 4, 6 and 7 all involved sentences rather than figures:
every number in those replies was real, and the problem was which window, which rule, or
which condition they described. The numeric allow-set could never have caught them.

`guards/claims.py` now checks propositions as well -- ten phrase patterns, each paired
with a predicate over the snapshot, rejecting a reply that asserts what the forecast
contradicts. Tested both ways, since over-rejection would quietly degrade every answer:
10/10 fabricated claims caught, 0 false positives across all 13 policies' own advice.
Guarded by `claim_grounding`.

It is a table rather than a model, so every rejection traces to a named rule, and it fails
toward acceptance -- a proposition nobody has written a check for passes unexamined. That
is the remaining limit, and it is narrower than it was rather than closed.

**A rule's advice must be true for every condition that can trigger it.** An `any:`
branch that widens the trigger without fitting the advice is a grounding bug no numeric
check will find, because every figure involved is real. That is defect 7, and it is the
review I would run first on any new policy.

Every guard here was verified by reintroducing its defect and watching it fail. Twice a
verification silently did nothing and reported a pass.

"""

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Numbers that carry no weather claim -- the same allowance the guard itself makes.
NEUTRAL = {str(n) for n in range(0, 11)} | {"15", "20", "24", "30", "45", "60", "90"}


# --------------------------------------------------------------------------- harness


@contextmanager
def fixture_weather(name: str):
    """Serve a recorded real response instead of calling the API.

    Geocoding is stubbed to the fixture's own coordinates so the case does not depend on
    a live geocoding call either.
    """
    payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    meta = payload.get("_provenance", {})
    location = ResolvedLocation(
        name=meta.get("city", "Fixture"),
        country="",
        admin1=None,
        latitude=payload.get("latitude", 0.0),
        longitude=payload.get("longitude", 0.0),
        timezone=payload.get("timezone", "auto"),
    )
    with mock.patch("app.graph.nodes.geocode", return_value=location), mock.patch(
        "app.graph.nodes.fetch_forecast", return_value=payload
    ):
        yield payload, location


@contextmanager
def weather_api_down():
    """Simulate the forecast endpoint being unreachable."""

    def boom(*_args, **_kwargs):
        raise WeatherFetchError("weather", "the weather service could not be reached")

    with mock.patch("app.graph.nodes.fetch_forecast", side_effect=boom):
        yield


def fresh_session(prefix: str) -> str:
    return f"eval-{prefix}-{datetime.now().timestamp()}"


# --------------------------------------------------------------------------- checks


def numbers_in(text: str) -> list[str]:
    """Numbers a reader would take as a weather claim. Policy ids and clock times are
    stripped first, matching what the guard does."""
    scrubbed = re.sub(r"\bSOP[-_ ]?[A-Za-z]{2,4}[-_ ]?\d{1,4}\b", " ", text, flags=re.I)
    scrubbed = re.sub(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", " ", scrubbed)
    scrubbed = re.sub(r"\b(1[0-2]|[1-9])\s?(?:am|pm)\b", " ", scrubbed, flags=re.I)
    return [t.replace(",", "") for t in NUMBER_RE.findall(scrubbed)]


def run_checks(checks: dict, result: dict) -> list[str]:
    """Returns a list of failure strings. Empty means the case passed."""
    problems: list[str] = []
    reply = result.get("reply", "")
    lower = reply.lower()
    cited = [c["id"] for c in result.get("citations", [])]

    for sop_id in checks.get("must_cite", []):
        if sop_id not in cited:
            problems.append(f"expected citation {sop_id}, got {cited or 'none'}")

    for sop_id in checks.get("must_not_cite", []):
        if sop_id in cited:
            problems.append(f"cited {sop_id}, which it must not")
        if re.search(rf"\b{re.escape(sop_id)}\b", reply, re.I) and sop_id.startswith("SOP-99"):
            # A fabricated id may be quoted while being denied; only flag it if the
            # reply appears to endorse it. The phrase checks below do that work.
            pass

    if "lead_sop" in checks:
        lead = cited[0] if cited else None
        if lead != checks["lead_sop"]:
            problems.append(f"expected {checks['lead_sop']} to lead, got {lead or 'nothing'}")

    if "lead_severity" in checks:
        severity = result["citations"][0]["severity"] if result.get("citations") else None
        if severity != checks["lead_severity"]:
            problems.append(f"expected lead severity {checks['lead_severity']}, got {severity}")

    if "no_guidance" in checks and bool(result.get("no_guidance")) != checks["no_guidance"]:
        problems.append(f"no_guidance was {result.get('no_guidance')}, expected {checks['no_guidance']}")

    if "failed" in checks and bool(result.get("failed")) != checks["failed"]:
        problems.append(f"failed was {result.get('failed')}, expected {checks['failed']}")

    if "min_citations" in checks and len(cited) < checks["min_citations"]:
        problems.append(f"expected at least {checks['min_citations']} citations, got {len(cited)}")

    for needle in checks.get("must_contain", []):
        if needle.lower() not in lower:
            problems.append(f"reply is missing {needle!r}")

    for needle in checks.get("must_not_contain", []):
        if needle.lower() in lower:
            problems.append(f"reply contains {needle!r}, which it must not")

    trace = " ".join(result.get("trace", []))
    for node in checks.get("must_reach", []):
        if node not in trace:
            problems.append(f"never reached {node} (trace: {trace})")
    for node in checks.get("must_not_reach", []):
        if node in trace:
            problems.append(f"reached {node}, which it must not (trace: {trace})")

    # The core grounding invariant, re-checked here independently of the guard that
    # already ran inside the graph.
    if checks.get("numbers_grounded"):
        allowed = set(result.get("allowed_numbers", [])) | NEUTRAL
        ungrounded = []
        for token in numbers_in(reply):
            if token in allowed:
                continue
            try:
                value = float(token)
            except ValueError:
                continue
            if f"{value:.1f}" in allowed or f"{value:.0f}" in allowed:
                continue
            ungrounded.append(token)
        if ungrounded:
            problems.append(f"ungrounded numbers in reply: {sorted(set(ungrounded))}")

    if checks.get("no_weather_numbers"):
        stray = [t for t in numbers_in(reply) if t not in NEUTRAL]
        if stray:
            problems.append(f"failure reply contains figures: {stray}")

    return problems


# --------------------------------------------------------------------------- runners


def find_live_severe(scan: dict) -> tuple[str, str] | None:
    """Look for a location genuinely in the target regime right now.

    Returns None if there isn't one -- the case then reports SKIPPED. This is the whole
    point: the case must not be satisfiable on demand.
    """
    policies = load_policies()
    for city in scan["cities"]:
        try:
            location = geocode(city)
            snapshot = build_snapshot(fetch_forecast(location), location, "today")
        except WeatherFetchError:
            continue
        matches = match_policies(policies, snapshot, "cycling", judge=None)
        lead, _ = select(matches)
        if lead and lead.id == scan["target_sop"]:
            return city, (
                f"{city}: rain_24h={snapshot.get('rain_24h_mm')} mm "
                f"({snapshot.get('rain_class')}), peak gusts "
                f"{snapshot.get('gust_peak_24h_kmh')} km/h"
            )
    return None


def check_window_isolation() -> list[str]:
    """Regression guard: a question about one window must not inherit another's weather.

    Found by hand, not by this suite: asking "what about this evening?" during an
    afternoon storm produced a danger-severity lightning warning even though none of the
    evening's own forecast hours carried a thunderstorm. build_snapshot was folding the
    CURRENT weather code into every window.

    Deterministic and offline -- it builds a payload where the storm sits only in the
    current reading and the morning, then asserts the evening window stays clear.
    """
    from app.weather.snapshot import build_snapshot as make

    hours = [f"2026-09-13T{h:02d}:00" for h in range(24)]
    # Thunderstorm (code 95) at 09:00-10:00 only; everything else lightly cloudy.
    codes = [95 if h in (9, 10) else 3 for h in range(24)]
    payload = {
        "timezone": "Asia/Kolkata",
        "current": {"time": "2026-09-13T10:00", "weather_code": 95, "is_day": 1,
                    "temperature_2m": 27.0, "wind_speed_10m": 8.0},
        "hourly": {"time": hours, "weather_code": codes,
                   "temperature_2m": [27.0] * 24, "wind_speed_10m": [8.0] * 24},
        "daily": {"time": ["2026-09-13"], "precipitation_sum": [4.0]},
    }
    location = ResolvedLocation("Testville", "", None, 23.0, 77.0, "Asia/Kolkata")

    problems: list[str] = []
    morning = make(payload, location, "morning")
    evening = make(payload, location, "evening")

    if not morning.get("thunderstorm_in_window"):
        problems.append("morning contains the storm hours but reports no thunderstorm")
    if evening.get("thunderstorm_in_window"):
        problems.append(
            "evening has no storm hours of its own but reports a thunderstorm -- the "
            "current conditions are leaking across windows"
        )
    if evening.get("thunderstorm_hours_in_window"):
        problems.append(
            f"evening counted {evening.get('thunderstorm_hours_in_window')} storm hours, expected 0"
        )
    now = make(payload, location, "now")
    if not now.get("thunderstorm_in_window"):
        problems.append("'now' should still reflect the current conditions, and did not")

    # Every window the snapshot can aggregate must also be reachable from the intent
    # vocabulary, and vice versa. `night` was once defined in WINDOW_HOURS but missing
    # from TIME_WINDOWS, so asking "at night?" silently answered about the evening --
    # dead configuration that looked supported from one side and did not exist from the
    # other. This asserts the two lists cannot drift apart again.
    from app.graph.state import TIME_WINDOWS
    from app.weather.snapshot import WINDOW_HOURS

    unreachable = set(WINDOW_HOURS) - set(TIME_WINDOWS)
    if unreachable:
        problems.append(
            f"windows the snapshot supports but intent can never produce: {sorted(unreachable)}"
        )
    undefined = set(TIME_WINDOWS) - set(WINDOW_HOURS) - {"now", "today"}
    if undefined:
        problems.append(
            f"windows intent can produce but the snapshot cannot aggregate: {sorted(undefined)}"
        )

    # A wrapping window must actually wrap, not collapse onto the neighbouring one.
    night = make(payload, location, "night")
    evening = make(payload, location, "evening")
    if night.get("window_hours") == evening.get("window_hours"):
        problems.append("night and evening cover the same hours -- night is not wrapping")

    return problems


def check_policy_validation() -> list[str]:
    """A policy referring to a field that does not exist must be rejected at load time.

    Without this, a typo like `temperature_celsius` for `temperature_c` loads happily and
    the rule then never fires, because an absent field evaluates to False. A safety rule
    that looks live but is dead is the worst failure this system has, and it is exactly
    the mistake someone editing YAML without reading the Python would make.
    """
    import tempfile
    from pathlib import Path

    from app.sops.loader import load_policies
    from app.sops.schema import SOPValidationError

    good = """
id: SOP-OK-001
title: Valid rule
category: test
severity: info
applies_to: {activities: ["*"]}
match: {field: temperature_c, op: gte, value: 30}
advice: fine
"""
    typo = good.replace("temperature_c,", "temperature_celsius,").replace(
        "SOP-OK-001", "SOP-BAD-001"
    )

    problems: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        Path(d, "ok.yaml").write_text(good, encoding="utf-8")
        try:
            load_policies(Path(d), use_cache=False)
        except SOPValidationError as exc:
            problems.append(f"a valid policy was rejected: {exc}")

    with tempfile.TemporaryDirectory() as d:
        Path(d, "bad.yaml").write_text(typo, encoding="utf-8")
        try:
            load_policies(Path(d), use_cache=False)
            problems.append(
                "a policy naming a non-existent snapshot field was ACCEPTED -- it would "
                "load cleanly and then silently never fire"
            )
        except SOPValidationError as exc:
            if "temperature_c" not in str(exc):
                problems.append("rejected, but without suggesting the correct field name")

    return problems


def check_no_contradiction() -> list[str]:
    """The all-clear must never be cited beside a hazard.

    SOP-GEN-001 asserts that nothing notable was found. Cited under a warning it produces
    a reply that contradicts itself -- "postpone the ride" followed by "nothing notable,
    go ahead as planned" -- and the deterministic fallback prints secondary advice
    verbatim, so a user would read both.

    It happened for real: GEN-001 checks sustained wind but not gusts, so a day with
    25 km/h prevailing and 55 km/h gusts satisfied it while SOP-EX-003 was warning about
    exactly those gusts. Fixed with `only_if_alone` rather than by copying gust
    thresholds into GEN-001, which would have coupled it to every future policy.
    """
    from app.sops.engine import match_policies, select
    from app.sops.loader import load_policies
    from app.weather.openmeteo import ResolvedLocation
    from app.weather.snapshot import WeatherSnapshot

    pol = load_policies()
    loc = ResolvedLocation("T", "", None, 0.0, 0.0, "UTC")
    base = dict(
        temperature_c=22.0, apparent_temperature_c=22.0, humidity_pct=50.0,
        wind_speed_kmh=8.0, wind_gusts_kmh=12.0, gust_differential_kmh=4.0,
        uv_index=3.0, precipitation_mm=0.0, precipitation_probability_pct=5.0,
        visibility_km=20.0, window="morning", window_start_hour=6, window_end_hour=11,
        rain_24h_mm=0.0, gust_peak_24h_kmh=14.0, temp_min_24h_c=14.0, uv_max_24h=4.0,
        rain_class="none", rain_class_rank=0, heavy_rain_regime=False,
        thunderstorm_in_window=False, clear_sky=True, comfort_index=88,
    )

    def snap(**over):
        f = dict(base)
        f.update(over)
        return WeatherSnapshot(loc, f["window"], "2026-09-13T10:00", f)

    problems: list[str] = []
    alone = [m.sop.id for m in [select(match_policies(pol, snap(), "cycling"))[0]] if m]
    if alone != ["SOP-GEN-001"]:
        problems.append(f"on a benign day the all-clear should lead, got {alone}")

    # The real profile that exposed this: light prevailing wind, heavy gusts.
    gusty = snap(wind_speed_kmh=25.0, wind_gusts_kmh=55.0, gust_differential_kmh=30.0,
                 gust_peak_24h_kmh=55.0)
    lead, sec = select(match_policies(pol, gusty, "cycling"))
    cited = [m.sop.id for m in ([lead] if lead else []) + sec]
    if "SOP-GEN-001" in cited:
        problems.append(
            f"the all-clear was cited beside a hazard: {cited} -- the reply would say "
            "'postpone the ride' and 'nothing notable, go ahead' together"
        )
    if lead is None or lead.sop.id != "SOP-EX-003":
        problems.append(f"expected the gust warning to lead, got {cited}")
    return problems


def check_claim_grounding() -> list[str]:
    """Non-numeric claims must be checked too, and without rejecting honest advice.

    The numeric allow-set answers "did this figure come from the API". It cannot answer
    "is this sentence true", which is how a reply once asserted water on the road beside a
    reading of 0.0 mm. guards/claims.py closes part of that with a table of phrase
    patterns paired with snapshot predicates.

    Both directions matter. A checker that rejects nothing is decoration; one that rejects
    honest policy wording silently degrades every answer, which has happened here before.
    """
    from app.guards.claims import check_claims
    from app.guards.grounding import render_deterministic
    from app.sops.loader import load_policies
    from app.weather.openmeteo import ResolvedLocation
    from app.weather.snapshot import WeatherSnapshot

    pol = {p.id: p for p in load_policies()}
    loc = ResolvedLocation("T", "", None, 0.0, 0.0, "UTC")
    base = dict(
        temperature_c=22.0, apparent_temperature_c=22.0, humidity_pct=50.0,
        wind_speed_kmh=8.0, wind_gusts_kmh=12.0, gust_differential_kmh=4.0, uv_index=3.0,
        precipitation_mm=0.0, precipitation_probability_pct=5.0, visibility_km=20.0,
        window="afternoon", window_start_hour=12, window_end_hour=17, window_hours=6,
        rain_24h_mm=0.0, gust_peak_24h_kmh=12.0, temp_min_24h_c=14.0, uv_max_24h=4.0,
        rain_class="none", rain_class_rank=0, heavy_rain_regime=False,
        thunderstorm_in_window=False, thunderstorm_hours_in_window=0,
        thunderstorm_covers_whole_window=False, clear_sky=True, comfort_index=85,
    )

    def snap(**over):
        f = dict(base)
        f.update(over)
        return WeatherSnapshot(loc, f["window"], "2026-09-13T14:00", f)

    problems: list[str] = []

    # Direction 1: fabrications must be caught.
    fabrications = [
        ("water on a dry road", snap(precipitation_mm=0.0),
         "There's enough water coming down to sit on the road surface."),
        ("a storm that isn't forecast", snap(thunderstorm_in_window=False),
         "Lightning is a real risk in this window."),
        ("a gap in a storm that covers the whole window", snap(
            thunderstorm_in_window=True, thunderstorm_hours_in_window=6,
            thunderstorm_covers_whole_window=True),
         "The storm covers only part of the afternoon, so you can plan around it."),
        ("calm wind during a gale", snap(wind_speed_kmh=45.0, wind_gusts_kmh=70.0),
         "Light winds today."),
    ]
    for label, s, draft in fabrications:
        if not check_claims(draft, s):
            problems.append(f"unsupported claim slipped through: {label}")

    # Direction 2: every policy's own advice must survive, or the guard is worse than
    # useless -- it would push honest answers into the deterministic fallback.
    triggers = {
        "SOP-SYS-001": dict(rain_24h_mm=120.0, rain_class="very_heavy", rain_class_rank=5,
                            gust_peak_24h_kmh=60.0, precipitation_mm=20.0,
                            visibility_km=3.0, clear_sky=False),
        "SOP-TR-002": dict(thunderstorm_in_window=True, thunderstorm_hours_in_window=2,
                           clear_sky=False, precipitation_mm=3.0, rain_24h_mm=12.0),
        "SOP-TR-003": dict(temperature_c=1.0, temp_min_24h_c=-3.0, precipitation_mm=1.0,
                           clear_sky=False, rain_24h_mm=4.0),
        "SOP-TR-004": dict(visibility_km=0.8, clear_sky=False),
        "SOP-TR-001": dict(precipitation_mm=6.0, visibility_km=3.0, clear_sky=False,
                           rain_24h_mm=14.0),
        "SOP-EX-003": dict(wind_speed_kmh=25.0, wind_gusts_kmh=55.0,
                           gust_differential_kmh=30.0, gust_peak_24h_kmh=55.0),
        "SOP-VG-003": dict(temperature_c=34.0, apparent_temperature_c=34.0),
        "SOP-GEN-001": dict(),
    }
    for sid, over in triggers.items():
        s = snap(**over)
        found = check_claims(render_deterministic(s, pol[sid]), s)
        if found:
            problems.append(f"{sid}'s own advice was rejected: {found}")

    return problems


UNIT_CHECKS = {
    "window_isolation": check_window_isolation,
    "policy_validation": check_policy_validation,
    "no_contradiction": check_no_contradiction,
    "claim_grounding": check_claim_grounding,
}


def run_case(case: dict) -> dict:
    """Execute one case and return its record."""
    case_id = case["id"]
    checks = case.get("checks", {})
    mode = case.get("mode", "live")
    record = {
        "id": case_id,
        "checking": " ".join(case.get("checking", "").split()),
        "pass_means": " ".join(case.get("pass_means", "").split()),
        "mode": mode,
        "status": "FAIL",
        "notes": "",
        "reply": "",
        "citations": [],
    }

    try:
        if mode == "unit":
            problems = UNIT_CHECKS[case["check"]]()
            record["status"] = "PASS" if not problems else "FAIL"
            record["notes"] = "; ".join(problems)
            return record

        if mode == "fixture":
            with fixture_weather(case["fixture"]) as (payload, _loc):
                record["fixture_note"] = payload.get("_provenance", {}).get("why_this_event", "")
                record["fixture_event"] = (
                    f"{payload.get('_provenance', {}).get('city')} "
                    f"{payload.get('_provenance', {}).get('date')}"
                )
                session = fresh_session(case_id)
                if "turns" in case:
                    for turn in case["turns"]:
                        result = ask(turn, session)
                    record["turns"] = case["turns"]
                else:
                    result = ask(case["question"], session)
                    record["question"] = case["question"]

        elif mode == "injected":
            with weather_api_down():
                result = ask(case["question"], fresh_session(case_id))
                record["question"] = case["question"]

        elif mode == "live" and "live_scan" in case:
            scan = case["live_scan"]
            found = find_live_severe(scan)
            if not found:
                record["status"] = "SKIP"
                record["notes"] = (
                    "No location in the scanned list is currently in a "
                    f"{scan['target_sop']} regime, so there is no live severe event to "
                    "test against. Reported as skipped rather than passed. The recorded "
                    "twin (severe_weather_recorded) covers the same behaviour."
                )
                return record
            city, detail = found
            record["question"] = scan["question"].format(city=city)
            record["live_event"] = detail
            result = ask(record["question"], fresh_session(case_id))

        else:
            result = ask(case["question"], fresh_session(case_id))
            record["question"] = case["question"]

    except Exception as exc:  # a crash is a failure, not an excuse
        record["notes"] = f"raised {type(exc).__name__}: {exc}"
        return record

    record["reply"] = result.get("reply", "")
    record["citations"] = [c["id"] for c in result.get("citations", [])]
    record["trace"] = result.get("trace", [])

    # A case that died because the model was unreachable has told us nothing about the
    # system's behaviour. Reporting it as FAIL would overstate the problem as much as
    # reporting it as PASS would understate it. The actual cause is quoted rather than
    # assumed -- a DNS blip and an exhausted quota are not the same finding.
    if "parse_intent(unavailable)" in " ".join(record["trace"]) and not checks.get("failed"):
        cause = (result.get("failure") or {}).get("debug", "") or "cause not recorded"
        record["status"] = "ERROR"
        record["notes"] = (
            f"The model was unreachable, so this case never exercised the behaviour it "
            f"tests. Not a behavioural result either way. Cause: {cause[:300]}"
        )
        return record

    problems = run_checks(checks, result)
    record["status"] = "PASS" if not problems else "FAIL"
    record["notes"] = "; ".join(problems)
    return record


# --------------------------------------------------------------------------- report


def write_report(records: list[dict]) -> None:
    passed = sum(r["status"] == "PASS" for r in records)
    failed = sum(r["status"] == "FAIL" for r in records)
    skipped = sum(r["status"] == "SKIP" for r in records)
    errored = sum(r["status"] == "ERROR" for r in records)

    tally = f"**{passed} passed, {failed} failed, {skipped} skipped**"
    if errored:
        tally += f", {errored} inconclusive (model quota)"

    out = [
        "# Eval results",
        "",
        f"Run on {datetime.now().strftime('%Y-%m-%d %H:%M')} — {tally} of {len(records)} cases.",
        "",
        "Generated by `python evals/run_evals.py`. Failures are left in this file rather "
        "than removed from the suite.",
        "",
        STANDING_NOTES,
        "## Summary",
        "",
        "| Case | Mode | Result | Checking |",
        "|---|---|---|---|",
    ]
    icon = {"PASS": "PASS", "FAIL": "**FAIL**", "SKIP": "SKIP", "ERROR": "INCONCLUSIVE"}
    for r in records:
        short = r["checking"][:90] + ("…" if len(r["checking"]) > 90 else "")
        out.append(f"| `{r['id']}` | {r['mode']} | {icon[r['status']]} | {short} |")

    out += ["", "## Detail", ""]
    for r in records:
        out += [f"### `{r['id']}` — {r['status']}", ""]
        out += [f"**Checking.** {r['checking']}", ""]
        if r.get("pass_means"):
            out += [f"**A pass means.** {r['pass_means']}", ""]
        if r.get("fixture_event"):
            out += [
                f"**Recorded event.** {r['fixture_event']} — {r.get('fixture_note', '')}",
                "",
            ]
        if r.get("live_event"):
            out += [f"**Live event found.** {r['live_event']}", ""]
        if r.get("turns"):
            out += ["**Turns.**", ""]
            out += [f"{i}. {t}" for i, t in enumerate(r["turns"], 1)]
            out += [""]
        elif r.get("question"):
            out += [f"**Question.** {r['question']}", ""]
        if r["status"] == "SKIP":
            out += [f"**Skipped.** {r['notes']}", ""]
            continue
        if r["status"] == "ERROR":
            out += [f"**Inconclusive.** {r['notes']}", ""]
            continue
        if r["notes"]:
            out += [f"**Why it failed.** {r['notes']}", ""]
        if r["mode"] != "unit":
            out += [f"**Cited.** {', '.join(r['citations']) or 'nothing'}", ""]
        if r.get("trace"):
            out += [f"**Path.** `{' → '.join(r['trace'])}`", ""]
        if r.get("reply"):
            # Full text only where it is evidence: a failure needs inspecting, and the
            # honest-refusal cases are the ones whose exact wording is the point. A
            # passing case gets an excerpt -- enough to see it answered, without turning
            # this file into something nobody reads.
            body = " ".join(r["reply"].split())
            verbatim = r["status"] == "FAIL" or r["id"] in {
                "no_policy_applies", "weather_api_unreachable", "location_unresolvable"
            }
            if not verbatim and len(body) > 320:
                body = body[:320].rsplit(" ", 1)[0] + " …"
            out += ["**Reply.**", "", "> " + body, ""]

    RESULTS_FILE.write_text("\n".join(out), encoding="utf-8")


def main() -> int:
    all_cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    wanted = set(sys.argv[1:])
    cases = [c for c in all_cases if c["id"] in wanted] if wanted else all_cases

    # A subset run must NOT overwrite the committed report -- otherwise debugging a single
    # case silently replaces the record of all the others with a file claiming the suite
    # is three cases long. Subset runs print to the console only.
    partial = bool(wanted)
    if wanted:
        unknown = wanted - {c["id"] for c in all_cases}
        if unknown:
            print(f"unknown case id(s): {', '.join(sorted(unknown))}")
            return 2

    records = []
    width = max(len(c["id"]) for c in cases)
    print(f"Running {len(cases)} eval cases (pacing {PACE_SECONDS:.0f}s between cases)\n")
    for index, case in enumerate(cases):
        if index and PACE_SECONDS:
            time.sleep(PACE_SECONDS)
        record = run_case(case)
        records.append(record)
        mark = {"PASS": "pass", "FAIL": "FAIL", "SKIP": "skip", "ERROR": "????"}[record["status"]]
        print(f"  {record['id']:<{width}}  {mark:<4}  {record['notes'][:100]}")

    if partial:
        print(f"\n(subset run — {RESULTS_FILE.name} left untouched)")
    else:
        write_report(records)
    passed = sum(r["status"] == "PASS" for r in records)
    failed = sum(r["status"] == "FAIL" for r in records)
    skipped = sum(r["status"] == "SKIP" for r in records)
    errored = sum(r["status"] == "ERROR" for r in records)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped, {errored} inconclusive")
    if not partial:
        print(f"Report written to {RESULTS_FILE}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
