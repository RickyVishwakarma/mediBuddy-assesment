"""Open-Meteo access: geocoding + forecast.

Both endpoints fail into the same shape (WeatherFetchError) so the graph can route
every "we couldn't get data" case down one honest-failure edge, per the brief.

The field lists below are deliberately generous. An Open-Meteo variable that we never
request is the one thing that would force a code change when adding a new SOP, so we
pay for breadth here to keep app/sops/policies/ purely declarative.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import HTTP_TIMEOUT_SECONDS

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo returns metadata with no values unless these are named explicitly.
CURRENT_FIELDS = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "uv_index",
    "cloud_cover",
    "weather_code",
    "is_day",
]

HOURLY_FIELDS = [
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_gusts_10m",
    "uv_index",
    "weather_code",
    "visibility",
]

DAILY_FIELDS = [
    "precipitation_sum",
    "precipitation_hours",
    "precipitation_probability_max",
    "temperature_2m_max",
    "temperature_2m_min",
    "apparent_temperature_max",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
    "uv_index_max",
    "weather_code",
]


class WeatherFetchError(Exception):
    """Any reason we do not have trustworthy data: HTTP error, timeout, empty
    geocoding result, or a response missing the blocks we asked for."""

    def __init__(self, kind: str, detail: str):
        self.kind = kind  # "geocode" | "weather"
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True)
class ResolvedLocation:
    name: str
    country: str
    admin1: str | None
    latitude: float
    longitude: float
    timezone: str

    @property
    def label(self) -> str:
        """Echoed back in every reply so a wrong same-name match is visible to the user."""
        parts = [self.name]
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


def geocode(city: str) -> ResolvedLocation:
    """Resolve a place name to coordinates.

    Takes the first candidate, which the brief accepts as a reasonable default. The
    resolved label is surfaced in the reply so the user can catch a wrong Springfield.
    """
    try:
        resp = httpx.get(
            GEOCODE_URL,
            params={"name": city, "count": 5, "language": "en", "format": "json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
    # The lookup being unreachable is a different thing from the place not existing, and
    # telling a user their spelling is wrong when the service merely timed out sends them
    # off trying variants of a name that was never the problem. Both still take the same
    # honest-failure path; only the wording differs.
    except httpx.TimeoutException as exc:
        raise WeatherFetchError("geocode_unavailable", f"timed out for {city!r}") from exc
    except httpx.HTTPError as exc:
        raise WeatherFetchError("geocode_unavailable", f"request failed for {city!r}") from exc
    except ValueError as exc:
        raise WeatherFetchError("geocode_unavailable", "non-JSON body") from exc

    results = payload.get("results") or []
    if not results:
        # Same failure class as the API being down: we cannot honestly proceed.
        raise WeatherFetchError("geocode", f"no location found matching {city!r}")

    top = results[0]
    try:
        return ResolvedLocation(
            name=top["name"],
            country=top.get("country", ""),
            admin1=top.get("admin1"),
            latitude=float(top["latitude"]),
            longitude=float(top["longitude"]),
            timezone=top.get("timezone", "auto"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherFetchError(
            "geocode_unavailable", "result was missing coordinates"
        ) from exc


def fetch_forecast(location: ResolvedLocation) -> dict:
    """Fetch the raw forecast payload. Returns the untouched JSON dict.

    Normalisation happens in snapshot.py; this function's only job is to either return
    real API data or raise. It never substitutes defaults for missing values, because a
    fabricated default would become a fabricated number in the reply.
    """
    params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "timezone": "auto",  # load-bearing: SOP time windows are in local hours
        "forecast_days": 2,
        "wind_speed_unit": "kmh",
        "current": ",".join(CURRENT_FIELDS),
        "hourly": ",".join(HOURLY_FIELDS),
        "daily": ",".join(DAILY_FIELDS),
    }
    try:
        resp = httpx.get(FORECAST_URL, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.TimeoutException as exc:
        raise WeatherFetchError("weather", "the weather service timed out") from exc
    except httpx.HTTPError as exc:
        raise WeatherFetchError("weather", "the weather service could not be reached") from exc
    except ValueError as exc:
        raise WeatherFetchError("weather", "the weather service returned a non-JSON body") from exc

    # A 200 with no "current" block is the documented trap in the brief. Treat it as a
    # failure rather than letting downstream code read None and call it a forecast.
    if not isinstance(payload.get("current"), dict) or not payload["current"]:
        raise WeatherFetchError("weather", "the weather service returned no current conditions")

    return payload
