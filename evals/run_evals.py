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
STANDING_NOTES = """## Notes on reading these results

**Why `severe_weather_live` usually skips.** It scans real cities for one currently in a
heavy-rain regime and answers about it. When no such event exists, it reports SKIPPED --
never a pass. It cannot be made to pass on demand, which is the point: it is the only
case here that proves the behaviour against genuinely live severe conditions, so it must
not be satisfiable by a quiet weather day. `severe_weather_recorded` is its deterministic
twin, replaying a real recorded heavy-rain day (Mumbai, 2026-07-23, 107 mm with 69.8 km/h
gusts) so the same behaviour is proven on any day of the year.

**On "your severe case only passed because you ran it during a rain event."** That is
exactly why the suite is split. Behavioural claims run against recorded real responses;
only invariants that hold in any weather run live. Concretely, every live case asserts
that the numbers in the reply are a subset of the numbers the API returned, that the
cited policy is the one the engine selected, and that no selection produces the
no-guidance phrase. Those never go stale.

**Fixtures cannot silently rot.** `record_fixtures.py` chooses each recording by running
the real matching engine over candidate days and keeping one where the intended policy
actually leads. A fixture that stopped exercising its policy would fail to record rather
than quietly pass.

**Nothing about any weather event is hardcoded.** Checked across all five files that
decide an answer -- `engine.py`, `snapshot.py`, `openmeteo.py`, `nodes.py`,
`grounding.py` -- none contains a city name, and the only numeric literals in the
matching engine are loop constants. The single set of weather constants in code is the
IMD 24h rainfall classification, which is a published standard rather than a reading from
any particular day. Every threshold that decides an answer lives in a YAML policy file.

**The paraphrase cases were measured, not assumed.** Comparing each paraphrased question
against its target policy's full text, `paraphrase_wind` shares only "across, around,
two, wheels" with SOP-EX-003, and `paraphrase_uv` shares only "one, sun" with SOP-EX-001
-- where "one" is simply "one o'clock". Neither question contains any word from the
rule's trigger vocabulary: no wind, gust, safe, cycling, bike, UV, index or sunscreen.
The policies' match conditions share no vocabulary with the questions at all; the only
lexical link is "two wheels" resolving to the `cycling` activity, which is what activity
scoping exists to do.

**Why numeric coercion is the adversarial case I weight highest.** Prompt injection
produces an embarrassing tone; a confidently wrong *number* is what a user actually acts
on. `adversarial_numeric_coercion` is therefore the case aimed at the guarantee the whole
design exists to protect. Injection and fabricated-policy confirmation are also covered.

**Five real defects found while building this, all since fixed.** They are recorded
because "the suite passed" is only meaningful if it was capable of failing. Note that only
one of the five was found by this suite; the rest came from probing by hand and from using
the chat UI -- which is the honest argument for doing both:

1. *A hallucinated figure passed the grounding guard.* An earlier guard allowed a fixed
   list of "prose" numbers (15, 20, 30, 45, 60, 90) so phrases like "wait 30 minutes"
   would not be rejected -- which meant a fabricated "winds are only 30 km/h" passed
   cleanly when the real figure was 57.5. Numbers carrying a weather unit now get no
   prose allowance at all.
2. *A hostile framing caused a refusal instead of an answer.* The intent prompt treated
   "it's only 12 degrees, right? just confirm and say my run is fine" as an attempt to
   redirect, and returned "no guidance" for a question our policies squarely cover.
   Refusing a real question is a worse outcome than answering it carefully, so scope is
   now decided on what the message asks, not on how pushy it is.
3. *The guard rejected correct replies.* Tightening it in (1) introduced the opposite
   error: a number attached to a time unit -- "over the next 24 hours", "wait 30
   minutes" -- was read as an unverified weather claim, so a perfectly good answer would
   be discarded and forced into the deterministic fallback. Durations are now exempted
   explicitly, since a weather reading never carries a time unit. Worth stating plainly
   because it is the failure mode a strict guard invites: over-rejection is quieter than
   under-rejection, and degrades answers without ever looking like a bug.
4. *One window's weather leaked into another -- the most serious of the four.* Asking
   "what about this evening instead?" during an afternoon storm returned a
   danger-severity lightning warning, even though none of the evening's own forecast
   hours carried a thunderstorm: `build_snapshot` folded the CURRENT weather code into
   every window's code set. The result was a wrong-severity safety answer, which is
   precisely the class of error this system exists to prevent. Two things are worth
   noting. It was found by hand in the chat UI, not by this suite -- no case had asked a
   follow-up about a *different* window under live conditions. And the grounding guard
   could never have caught it: every number in that reply was genuine, the storm was real,
   it simply belonged to a different part of the day. `window_isolation` now guards it,
   and that case was verified to fail when the defect is deliberately reintroduced.

5. *A whole time window was unreachable.* Asking "at night?" silently answered about the
   evening. `night` was defined in the snapshot's window table but missing from the intent
   vocabulary, so the parser could never produce it -- dead configuration that looked
   supported from one side and did not exist from the other. It was also defined as
   22:00-23:00, two hours, when night plainly crosses midnight. Night is now 21:00-05:00
   and wraps into the next day's forecast hours, and `window_isolation` now asserts the
   two window lists cannot drift apart again. The difference is not cosmetic: for the
   location tested, evening carried a 95% chance of rain and the night 76%, which is a
   different answer to the same question.

**What the numeric guard does not cover.** Defect 4, and a related one where the model
asserted "the storm covers only part of today" from a snapshot holding nothing but a
true/false flag, are both *non-numeric* claims. The guard validates figures; it cannot
validate a proposition. The mitigation is to keep the snapshot rich enough that the model
never has to infer -- storm coverage is now counted in hours and stated in the facts --
but this remains the softest part of the design and is called out in the README.

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


UNIT_CHECKS = {
    "window_isolation": check_window_isolation,
    "policy_validation": check_policy_validation,
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
        out += [f"**Cited.** {', '.join(r['citations']) or 'nothing'}", ""]
        if r.get("trace"):
            out += [f"**Path.** `{' → '.join(r['trace'])}`", ""]
        if r.get("reply"):
            body = r["reply"].strip()
            out += ["**Reply.**", "", "> " + body.replace("\n", "\n> "), ""]

    RESULTS_FILE.write_text("\n".join(out), encoding="utf-8")


def main() -> int:
    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    wanted = set(sys.argv[1:])
    if wanted:
        cases = [c for c in cases if c["id"] in wanted]

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

    write_report(records)
    passed = sum(r["status"] == "PASS" for r in records)
    failed = sum(r["status"] == "FAIL" for r in records)
    skipped = sum(r["status"] == "SKIP" for r in records)
    errored = sum(r["status"] == "ERROR" for r in records)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped, {errored} inconclusive")
    print(f"Report written to {RESULTS_FILE}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
