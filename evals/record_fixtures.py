"""Records eval fixtures from real Open-Meteo responses.

Why fixtures exist at all: live weather will not hold still for a test suite. A case
that asserts "a heavy-rain system produces a danger-severity answer" can only run when
a heavy-rain system exists somewhere, which is not something a reviewer running this
repo next month can count on.

So the behavioural cases replay recorded responses. These are not invented numbers --
each is a real response for a real place on a real date, pulled from the same
/v1/forecast endpoint the application uses (via `past_days`, so the payload shape is
identical to a live call) and stamped with its provenance.

Days are chosen by running the real matching engine over each candidate and keeping the
one where the policy we want to exercise actually leads. That way a fixture cannot
quietly stop testing what it claims to test.

Re-record with:  python evals/record_fixtures.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sops.engine import match_policies, select  # noqa: E402
from app.sops.loader import load_policies  # noqa: E402
from app.weather.openmeteo import (  # noqa: E402
    CURRENT_FIELDS,
    DAILY_FIELDS,
    FORECAST_URL,
    HOURLY_FIELDS,
    ResolvedLocation,
)
from app.weather.snapshot import build_snapshot  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# Candidate places to scan. Several are offered per fixture because whether a given city
# saw a qualifying day in the last 90 varies with when this is run.
SPECS = [
    {
        "name": "severe_rain",
        "target_sop": "SOP-SYS-001",
        "activity": "cycling",
        "window": "today",
        "hour": 14,
        "places": [
            ("Mumbai", 19.07283, 72.88261),
            ("Kolkata", 22.56263, 88.36304),
            ("Guwahati", 26.1844, 91.7458),
            ("Bhopal", 23.25469, 77.40289),
            ("Ho Chi Minh City", 10.82302, 106.62965),
        ],
        "why": "A real heavy-rain event, to exercise the situational override SOP-SYS-001.",
        # Take the wettest qualifying day available, not just the most recent, so the
        # fixture exercises the override at full strength.
        "prefer": lambda s: s.get("rain_24h_mm") or 0,
    },
    {
        "name": "high_wind",
        "target_sop": "SOP-EX-003",
        "activity": "cycling",
        "window": "afternoon",
        "hour": 14,
        "places": [
            ("Wellington", -41.28664, 174.77557),
            ("Reykjavik", 64.13548, -21.89541),
            ("Punta Arenas", -53.15483, -70.91129),
        ],
        "why": "A real gale with little rain, so the wind rule leads rather than the rain override.",
    },
    {
        "name": "high_uv",
        "target_sop": "SOP-EX-001",
        "activity": "running",
        "window": "afternoon",
        "hour": 13,
        "places": [
            ("Chennai", 13.08784, 80.27847),
            ("Singapore", 1.28967, 103.85007),
            ("Nairobi", -1.28333, 36.81667),
            ("Darwin", -12.46113, 130.84185),
        ],
        "why": "A real clear high-UV day, to exercise SOP-EX-001 and SOP-VG-001.",
    },
    {
        "name": "benign",
        "target_sop": "SOP-GEN-001",
        "activity": "cycling",
        "window": "morning",
        "hour": 9,
        "places": [
            ("Lisbon", 38.71667, -9.13333),
            ("Christchurch", -43.53333, 172.63333),
            ("Cape Town", -33.92584, 18.42322),
            ("Melbourne", -37.814, 144.96332),
        ],
        "why": "A real unremarkable day, to exercise the all-clear policy SOP-GEN-001.",
    },
]


def fetch_window(latitude: float, longitude: float, past_days: int = 90) -> dict:
    """Pull a long window from the same endpoint the app uses, so the recorded payload
    has exactly the shape (and every field) a live call would return."""
    response = httpx.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "past_days": past_days,
            "forecast_days": 1,
            "wind_speed_unit": "kmh",
            "current": ",".join(CURRENT_FIELDS),
            "hourly": ",".join(HOURLY_FIELDS),
            "daily": ",".join(DAILY_FIELDS),
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def slice_day(payload: dict, day: str, hour: int, meta: dict | None = None) -> dict:
    """Cut one real day out of the window and present it as a single-day forecast.

    `current` is that day's readings at `hour`, addressed the way a live response would
    address them. No value is altered.
    """
    hourly = payload["hourly"]
    indices = [i for i, t in enumerate(hourly["time"]) if t.startswith(day)]
    if not indices:
        raise ValueError(f"no hourly rows for {day}")
    target = next((i for i in indices if hourly["time"][i].endswith(f"T{hour:02d}:00")), indices[0])

    daily = payload["daily"]
    d_index = daily["time"].index(day)

    current = {"time": hourly["time"][target], "interval": 900}
    for name in CURRENT_FIELDS:
        if name in hourly:
            current[name] = hourly[name][target]
    # is_day is not an hourly variable; derive it from the hour.
    current["is_day"] = 1 if 6 <= hour <= 18 else 0

    fixture = {
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
        "timezone_abbreviation": payload.get("timezone_abbreviation"),
        "utc_offset_seconds": payload.get("utc_offset_seconds"),
        "current_units": payload.get("current_units", {}),
        "current": current,
        "hourly_units": payload.get("hourly_units", {}),
        "hourly": {k: [v[i] for i in indices] for k, v in hourly.items()},
        "daily_units": payload.get("daily_units", {}),
        "daily": {k: [v[d_index]] for k, v in daily.items()},
    }
    if meta:
        fixture = {"_provenance": meta, **fixture}
    return fixture


def find_day(spec: dict) -> tuple[dict, dict] | None:
    """Scan recent real days for one where the wanted policy actually leads.

    `prefer` picks among qualifying days -- the severe case wants the most extreme real
    event available, not merely the most recent one, so the fixture tests the policy at
    full strength.
    """
    policies = load_policies()
    prefer = spec.get("prefer")
    best: tuple[float, dict, dict] | None = None

    for city, lat, lon in spec["places"]:
        try:
            payload = fetch_window(lat, lon)
        except httpx.HTTPError:
            continue
        location = ResolvedLocation(city, "", None, lat, lon, payload.get("timezone", "auto"))
        days = payload.get("daily", {}).get("time", [])

        # Most recent first: a fresher recording is easier to sanity-check by hand.
        for day in reversed(days):
            try:
                candidate = slice_day(payload, day, spec["hour"])
            except ValueError:
                continue
            snapshot = build_snapshot(candidate, location, spec["window"])
            # No judge here: fixture selection must be reproducible without the LLM.
            matches = match_policies(policies, snapshot, spec["activity"], judge=None)
            lead, _ = select(matches)
            if not lead or lead.id != spec["target_sop"]:
                continue

            meta = {
                "recorded_from": FORECAST_URL,
                "city": city,
                "date": day,
                "hour_used_as_current": spec["hour"],
                "window_under_test": spec["window"],
                "activity_under_test": spec["activity"],
                "leads_with": spec["target_sop"],
                "why_this_event": spec["why"],
                "recorded_on": date.today().isoformat(),
                "note": "Real API response. No value has been edited.",
            }
            found = (
                slice_day(payload, day, spec["hour"], meta),
                {"city": city, "day": day, "snapshot": snapshot, "matched": [m.id for m in matches]},
            )
            if not prefer:
                return found
            score = prefer(snapshot)
            if best is None or score > best[0]:
                best = (score, *found)

    return (best[1], best[2]) if best else None


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    print("Recording fixtures from real Open-Meteo responses\n")
    missing = []
    for spec in SPECS:
        found = find_day(spec)
        if not found:
            missing.append(spec["name"])
            print(f"  {spec['name']:<12} NOT FOUND -- no recent real day led with {spec['target_sop']}")
            continue
        fixture, info = found
        (FIXTURE_DIR / f"{spec['name']}.json").write_text(
            json.dumps(fixture, indent=1), encoding="utf-8"
        )
        snapshot = info["snapshot"]
        print(
            f"  {spec['name']:<12} {info['city']:<12} {info['day']}  "
            f"leads={spec['target_sop']:<12} matched={info['matched']}"
        )
        print(
            f"               rain24={snapshot.get('rain_24h_mm')} "
            f"class={snapshot.get('rain_class')} "
            f"wind={snapshot.get('wind_speed_kmh')} gusts={snapshot.get('wind_gusts_kmh')} "
            f"uv={snapshot.get('uv_index')} feels={snapshot.get('apparent_temperature_c')}"
        )
    if missing:
        print(f"\nWARNING: no fixture recorded for {', '.join(missing)}")
    print(f"\nWritten to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
