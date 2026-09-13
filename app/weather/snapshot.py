"""Turns a raw Open-Meteo payload into a typed, flat fact snapshot.

This module is the single source of numeric truth for the whole system:
  * the SOP engine matches only against snapshot.fields
  * the compose prompt sees only snapshot.fact_block()
  * the grounding guard builds its allow-set only from snapshot.allowed_numbers()

Derived fields exist so that a policy can name a *regime* ("a heavy-rain system is
active") rather than only a single reading. Aggregation lives here, in code, so new
SOPs stay declarative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.weather.openmeteo import ResolvedLocation

# --- WMO weather code groups -------------------------------------------------
THUNDERSTORM_CODES = {95, 96, 99}
CLEAR_CODES = {0, 1}
FOG_CODES = {45, 48}

# --- IMD 24h rainfall classification (mm) ------------------------------------
# Published thresholds, used verbatim. This is what lets SOP-SYS-001 describe the
# signature of a heavy-rain system instead of guessing at an arbitrary cutoff.
RAIN_CLASSES: list[tuple[str, float]] = [
    ("none", 0.0),
    ("very_light", 0.1),
    ("light", 2.5),
    ("moderate", 15.6),
    ("heavy", 64.5),
    ("very_heavy", 115.6),
    ("extremely_heavy", 204.5),
]

# Local-hour ranges per named window. "today" and "now" are handled separately.
WINDOW_HOURS: dict[str, tuple[int, int]] = {
    "morning": (6, 11),
    "afternoon": (12, 17),
    "evening": (18, 22),
    "night": (22, 23),
}


# Display labels for the readings a person would recognise. Used by both the model-facing
# fact block and the user-facing fallback, so the two can never drift apart.
FACT_LABELS: list[tuple[str, str, str]] = [
    ("temperature_c", "Temperature", "°C"),
    ("apparent_temperature_c", "Feels like", "°C"),
    ("humidity_pct", "Relative humidity", "%"),
    ("wind_speed_kmh", "Wind speed", "km/h"),
    ("wind_gusts_kmh", "Wind gusts", "km/h"),
    ("uv_index", "UV index", ""),
    ("precipitation_mm", "Precipitation", "mm"),
    ("precipitation_probability_pct", "Chance of precipitation", "%"),
    ("cloud_cover_pct", "Cloud cover", "%"),
    ("visibility_km", "Visibility", "km"),
    ("rain_24h_mm", "Rain total next 24h", "mm"),
    ("gust_peak_24h_kmh", "Peak gusts next 24h", "km/h"),
]


def classify_rain(mm_24h: float) -> tuple[str, int]:
    """IMD rainfall class and its ordinal rank (0=none .. 6=extremely heavy)."""
    label, rank = "none", 0
    for index, (name, floor) in enumerate(RAIN_CLASSES):
        if mm_24h >= floor:
            label, rank = name, index
    return label, rank


def _num(value) -> float | None:
    """Coerce to float, or None. Never substitutes a default -- a missing value stays
    missing so it can't become an invented number downstream."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _agg(values: list, how: str) -> float | None:
    clean = [v for v in (_num(x) for x in values) if v is not None]
    if not clean:
        return None
    if how == "max":
        return max(clean)
    if how == "min":
        return min(clean)
    if how == "sum":
        return round(sum(clean), 2)
    raise ValueError(how)


@dataclass
class WeatherSnapshot:
    """A flat, typed view of one forecast fetch for one question."""

    location: ResolvedLocation
    window: str                      # now | morning | afternoon | evening | night | today
    observed_at: str                 # local ISO timestamp from the API
    fields: dict[str, float | int | bool | str | None] = field(default_factory=dict)

    def get(self, name: str):
        return self.fields.get(name)

    # -- consumed by the grounding guard --------------------------------------
    def allowed_numbers(self) -> set[str]:
        """Every numeric string the model is permitted to write, in the forms a model
        would plausibly render them (7, 7.0, 7.2)."""
        allowed: set[str] = set()

        # The forecast timestamp is part of the fact block the model is shown, so its
        # components are grounded data too. Without this, a reply that correctly states
        # which day the forecast is for gets rejected for "inventing" the year.
        for part in re.findall(r"\d+", self.observed_at or ""):
            allowed.add(part)
            allowed.add(str(int(part)))

        for value in self.fields.values():
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                allowed.add(f"{float(value):.1f}")
                allowed.add(f"{float(value):.0f}")
                allowed.add(str(value))
                if float(value).is_integer():
                    allowed.add(str(int(value)))
        return allowed

    # -- consumed by the deterministic fallback --------------------------------
    def relevant_facts(self, field_names: set[str]) -> str:
        """A short, readable set of readings, for showing a user directly.

        Unlike fact_block() -- which hands the model everything and lets it choose --
        this shows only the fields the cited policies actually test, plus the couple a
        reader always wants. Internal flags are omitted unless they are true, because
        "Heavy-rain system active: False" is debug output, not information.
        """
        wanted = set(field_names) | {"temperature_c", "apparent_temperature_c"}
        lines: list[str] = []
        for key, label, unit in FACT_LABELS:
            if key not in wanted:
                continue
            value = self.fields.get(key)
            if value is None:
                continue
            lines.append(f"{label}: {value}{(' ' + unit) if unit else ''}")

        if self.fields.get("heavy_rain_regime"):
            lines.append(f"IMD 24h rainfall class: {self.fields.get('rain_class')}")
        if self.fields.get("thunderstorm_in_window"):
            lines.append("Thunderstorm forecast in this window: yes")
        return "\n".join(lines)

    # -- consumed by the compose prompt ---------------------------------------
    def fact_block(self) -> str:
        """Pre-rendered strings handed to the LLM. It never sees the raw JSON, so the
        only numbers in its context are the ones the guard will accept."""
        lines = [
            f"Location: {self.location.label}",
            f"Local time of forecast: {self.observed_at}",
            f"Window asked about: {self.window}",
        ]
        for key, label, unit in FACT_LABELS:
            value = self.fields.get(key)
            if value is None:
                continue
            suffix = f" {unit}" if unit else ""
            lines.append(f"{label}: {value}{suffix}")

        lines.append(f"IMD 24h rainfall class: {self.fields.get('rain_class')}")
        lines.append(f"Heavy-rain system active: {self.fields.get('heavy_rain_regime')}")
        lines.append(f"Thunderstorm in window: {self.fields.get('thunderstorm_in_window')}")
        if self.fields.get("thunderstorm_in_window"):
            storm = self.fields.get("thunderstorm_hours_in_window")
            total = self.fields.get("window_hours")
            lines.append(
                f"Thunderstorm covers {storm} of the {total} forecast hours in this window "
                f"(whole window: {self.fields.get('thunderstorm_covers_whole_window')})"
            )
        return "\n".join(lines)


def _window_hours(payload: dict, window: str) -> list[int]:
    """Indices into the hourly arrays that belong to the asked-about window."""
    times: list[str] = payload.get("hourly", {}).get("time", []) or []
    if not times:
        return []

    current_time = payload.get("current", {}).get("time", "")
    today = current_time[:10] if current_time else times[0][:10]
    now_hour = int(current_time[11:13]) if len(current_time) >= 13 else 0

    indices: list[int] = []
    for i, stamp in enumerate(times):
        if stamp[:10] != today:
            continue
        hour = int(stamp[11:13])
        if window == "today":
            if hour >= now_hour:
                indices.append(i)
        elif window in WINDOW_HOURS:
            low, high = WINDOW_HOURS[window]
            if low <= hour <= high:
                indices.append(i)

    # Asking about a window that has already passed today falls back to the rest of
    # today rather than silently returning an empty aggregate.
    if not indices and window != "today":
        return _window_hours(payload, "today")
    return indices


def _comfort_index(fields: dict) -> int:
    """Deterministic 0-100 pleasantness composite, used by the fuzzy leisure SOP.

    Kept in code (not in the model) so that two identical forecasts always produce the
    same number. The fuzzy SOP still reasons in language, but it reasons over a stable
    figure rather than inventing one.
    """
    score = 100.0
    feels = fields.get("apparent_temperature_c")
    if feels is not None:
        score -= min(abs(feels - 22.0) * 3.0, 45.0)
    prob = fields.get("precipitation_probability_pct")
    if prob is not None:
        score -= prob * 0.45
    wind = fields.get("wind_speed_kmh")
    if wind is not None:
        score -= max(wind - 12.0, 0.0) * 1.1
    uv = fields.get("uv_index")
    if uv is not None:
        score -= max(uv - 6.0, 0.0) * 3.0
    rain = fields.get("precipitation_mm")
    if rain is not None:
        score -= min(rain * 6.0, 25.0)
    return int(max(0.0, min(100.0, round(score))))


def build_snapshot(payload: dict, location: ResolvedLocation, window: str) -> WeatherSnapshot:
    """Normalise a raw Open-Meteo payload into the fact snapshot.

    Every value here traces to a key in `payload`. Nothing is estimated or defaulted.
    """
    window = window if window in {"now", "today", *WINDOW_HOURS} else "now"
    current = payload.get("current", {}) or {}
    hourly = payload.get("hourly", {}) or {}
    daily = payload.get("daily", {}) or {}

    observed_at = current.get("time", "")
    idx = _window_hours(payload, window)

    def hourly_values(name: str) -> list:
        series = hourly.get(name, []) or []
        return [series[i] for i in idx if i < len(series)]

    fields: dict[str, float | int | bool | str | None] = {}

    # --- point-in-time vs windowed readings ---------------------------------
    # "now" reads the current block; a named window aggregates the hourly slice.
    if window == "now":
        fields["temperature_c"] = _num(current.get("temperature_2m"))
        fields["apparent_temperature_c"] = _num(current.get("apparent_temperature"))
        fields["wind_speed_kmh"] = _num(current.get("wind_speed_10m"))
        fields["wind_gusts_kmh"] = _num(current.get("wind_gusts_10m"))
        fields["uv_index"] = _num(current.get("uv_index"))
        fields["precipitation_mm"] = _num(current.get("precipitation"))
        fields["precipitation_probability_pct"] = _agg(
            hourly_values("precipitation_probability"), "max"
        )
        fields["visibility_km"] = (
            round(v / 1000.0, 1) if (v := _agg(hourly_values("visibility"), "min")) is not None
            else None
        )
    else:
        fields["temperature_c"] = _agg(hourly_values("temperature_2m"), "max")
        fields["apparent_temperature_c"] = _agg(hourly_values("apparent_temperature"), "max")
        fields["wind_speed_kmh"] = _agg(hourly_values("wind_speed_10m"), "max")
        fields["wind_gusts_kmh"] = _agg(hourly_values("wind_gusts_10m"), "max")
        fields["uv_index"] = _agg(hourly_values("uv_index"), "max")
        fields["precipitation_mm"] = _agg(hourly_values("precipitation"), "sum")
        fields["precipitation_probability_pct"] = _agg(
            hourly_values("precipitation_probability"), "max"
        )
        fields["visibility_km"] = (
            round(v / 1000.0, 1) if (v := _agg(hourly_values("visibility"), "min")) is not None
            else None
        )

    fields["humidity_pct"] = _num(current.get("relative_humidity_2m"))
    fields["cloud_cover_pct"] = _num(current.get("cloud_cover"))

    # Gust differential: how much harder the peak gust hits than the prevailing wind.
    # This is the figure that matters to a rider. A steady 45 km/h headwind is hard work
    # but predictable; 20 km/h sustained with 55 km/h gusts is what actually puts someone
    # across a lane, because the load arrives without warning. Raw wind speed alone
    # cannot distinguish the two.
    gust = fields.get("wind_gusts_kmh")
    sustained = fields.get("wind_speed_kmh")
    fields["gust_differential_kmh"] = (
        round(gust - sustained, 1) if gust is not None and sustained is not None else None
    )

    # --- time ---------------------------------------------------------------
    try:
        local_hour = datetime.fromisoformat(observed_at).hour
    except (TypeError, ValueError):
        local_hour = None
    fields["local_hour"] = local_hour
    fields["is_daytime"] = bool(current.get("is_day", 0))
    fields["window"] = window

    # The span the user actually asked about, so a policy can say "overlaps 11:00-16:00"
    # declaratively instead of the code hard-coding which windows count as midday.
    if window == "now":
        start_hour = end_hour = local_hour
    elif window == "today":
        start_hour, end_hour = (local_hour if local_hour is not None else 0), 23
    else:
        start_hour, end_hour = WINDOW_HOURS[window]
    fields["window_start_hour"] = start_hour
    fields["window_end_hour"] = end_hour

    # --- daily aggregates that describe a regime rather than a reading -------
    rain_24h = _num((daily.get("precipitation_sum") or [None])[0]) or 0.0
    gust_peak = _num((daily.get("wind_gusts_10m_max") or [None])[0])
    fields["rain_24h_mm"] = rain_24h
    fields["rain_hours_24h"] = _num((daily.get("precipitation_hours") or [None])[0])
    fields["gust_peak_24h_kmh"] = gust_peak
    fields["apparent_temp_max_24h_c"] = _num((daily.get("apparent_temperature_max") or [None])[0])
    fields["temp_max_24h_c"] = _num((daily.get("temperature_2m_max") or [None])[0])
    fields["temp_min_24h_c"] = _num((daily.get("temperature_2m_min") or [None])[0])
    fields["uv_max_24h"] = _num((daily.get("uv_index_max") or [None])[0])

    rain_class, rain_rank = classify_rain(rain_24h)
    fields["rain_class"] = rain_class
    fields["rain_class_rank"] = rain_rank

    # A heavy-rain system, expressed as its observable signature: either the IMD class
    # is heavy-or-worse, or substantial rain is arriving alongside squally gusts.
    # This is what makes the situational override matchable without an IMD bulletin.
    fields["heavy_rain_regime"] = bool(
        rain_rank >= 4 or (rain_24h >= 35.0 and (gust_peak or 0.0) >= 45.0)
    )

    # --- weather codes ------------------------------------------------------
    hourly_codes = [int(c) for c in hourly_values("weather_code") if c is not None]
    window_codes = set(hourly_codes)
    current_code = current.get("weather_code")

    # Only fold the current conditions in when the question IS about now. Doing it
    # unconditionally let a storm happening right this minute leak into a question about
    # this evening -- producing a danger-severity lightning warning for a window whose
    # own forecast hours are clear. The window asked about decides which hours count.
    if current_code is not None and window == "now":
        window_codes.add(int(current_code))
    fields["weather_code"] = int(current_code) if current_code is not None else None
    fields["thunderstorm_in_window"] = bool(window_codes & THUNDERSTORM_CODES)
    fields["fog_in_window"] = bool(window_codes & FOG_CODES)
    fields["clear_sky"] = bool(current_code is not None and int(current_code) in CLEAR_CODES)

    # How much of the window the storm actually covers. Without these, a reply saying
    # "the storm only covers part of the day, so plan around it" would be the model
    # inferring a fact the snapshot never supplied -- true or not, we could not stand
    # behind it. Counting the hours makes the claim checkable.
    storm_hours = sum(1 for c in hourly_codes if c in THUNDERSTORM_CODES)
    fields["window_hours"] = len(hourly_codes) or None
    fields["thunderstorm_hours_in_window"] = storm_hours
    fields["thunderstorm_covers_whole_window"] = bool(
        hourly_codes and storm_hours == len(hourly_codes)
    )

    fields["comfort_index"] = _comfort_index(fields)

    return WeatherSnapshot(
        location=location,
        window=window,
        observed_at=observed_at,
        fields=fields,
    )
