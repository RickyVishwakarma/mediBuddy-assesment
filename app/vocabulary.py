"""The contract between policy authors and the intent parser.

A policy file scopes itself with `applies_to.activities`, and parse_intent must emit
one of the same strings or the scope check can never succeed. Keeping the list in one
place -- referenced by the prompt and documented in the README -- is what makes a new
SOP writable by someone who has never read the Python.
"""

# Canonical activity labels. parse_intent maps free text onto exactly one of these.
ACTIVITIES = [
    "cycling",          # pedal bike
    "motorcycle",       # powered two-wheeler, scooter
    "running",
    "walking",
    "hiking",
    "sports",           # football, cricket, any field sport
    "commute",          # getting to work/school, mode unspecified
    "driving",          # enclosed vehicle
    "picnic",           # relaxed sit-down outing
    "children_play",    # a child outdoors
    "elderly_outing",   # an older adult outdoors
    "pet_walk",
    "gardening",
    "general_outdoor",  # outdoors, nothing more specific stated
    "indoor_games",     # a game played indoors, under a roof -- chess, board games, cards
]

# Every field a policy may reference in a `field:` leaf. Anything not listed here does
# not exist on the snapshot and the rule would silently never fire, so the loader's
# validation and this list are what a policy author checks against.
SNAPSHOT_FIELDS = [
    # current / windowed readings
    "temperature_c",
    "apparent_temperature_c",
    "humidity_pct",
    "wind_speed_kmh",
    "wind_gusts_kmh",
    "gust_differential_kmh",
    "uv_index",
    "precipitation_mm",
    "precipitation_probability_pct",
    "cloud_cover_pct",
    "visibility_km",
    # time
    "local_hour",
    "is_daytime",
    "window",
    "window_start_hour",
    "window_end_hour",
    # 24h aggregates -- these describe a regime rather than a moment
    "rain_24h_mm",
    "rain_hours_24h",
    "gust_peak_24h_kmh",
    "apparent_temp_max_24h_c",
    "temp_max_24h_c",
    "temp_min_24h_c",
    "uv_max_24h",
    "rain_class",
    "rain_class_rank",
    "heavy_rain_regime",
    # weather codes
    "weather_code",
    "thunderstorm_in_window",
    "thunderstorm_hours_in_window",
    "thunderstorm_covers_whole_window",
    "window_hours",
    "fog_in_window",
    "clear_sky",
    # composite
    "comfort_index",
]
