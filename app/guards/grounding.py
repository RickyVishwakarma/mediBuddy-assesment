"""THE GROUNDING GUARD.

This is the file to point at when asked where "the model cannot invent numbers" is
enforced. Nothing the model writes reaches a user without passing check() first.

It enforces three things:

  1. Every number in the reply is one the API actually returned for this request, or a
     threshold named in the policy being cited.
  2. The reply cites the SOP that the engine selected.
  3. The reply cites no OTHER policy id -- which is what stops a user talking the model
     into confirming a policy that does not exist.

A reply that fails is never patched up or shipped with a warning. It is discarded: the
graph retries once, then renders the policy deterministically instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.guards.claims import check_claims
from app.sops.schema import SOP
from app.weather.snapshot import WeatherSnapshot

# Numbers as a model would write them: 34, 34.5, 1,200, 70%.
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Matches real ids (SOP-EX-003) and malformed ones (SOP-99, SOP99). Catching the
# malformed shapes matters: a user inventing "SOP-99" is exactly the case the guard
# exists for, and an id it cannot even see is an id it cannot police.
SOP_ID_RE = re.compile(r"\bSOP[-_ ]?(?:[A-Za-z]{1,5}[-_ ]?)?\d{1,4}\b", re.IGNORECASE)

# Small unit-less counts that appear in ordinary prose -- "both hands", "two hours",
# "a couple of layers". Deliberately capped at ten: anything larger is far more likely
# to be a weather figure than a count, and letting 30 or 60 through unchecked was a real
# hole (a hallucinated "30 km/h" passed as a prose number until this was tightened).
SMALL_COUNTS = {str(n) for n in range(0, 11)}

# A number carrying a unit is making a weather claim, and gets NO prose allowance: it
# must come from the API or from the cited policy's own text.
#
# Crucially it must also be the RIGHT KIND of number. The allow-set used to be one flat
# bag, so any value from any field vouched for any claim: with wind at 11.7 km/h, "12"
# was in the bag, and "it's only about 12 degrees" passed while the real temperature was
# 28.7 C. That is the numeric-coercion attack the guard exists to stop, and it walked
# through. Each unit is therefore mapped to the snapshot fields it can legitimately
# describe, and a temperature claim is checked against temperatures alone.
_N = r"(\d[\d,]*(?:\.\d+)?)"

UNIT_AFTER_PATTERNS = {
    # Bare "C" and "F" are included deliberately. They were missing, so "12 C" never
    # reached this pass at all -- it fell through to the prose rules, which are laxer.
    # The degree signs are written as escapes rather than literals: this file has been
    # corrupted twice by an editor writing them in the wrong encoding, and a mangled
    # byte inside a character class silently stops the whole alternation matching.
    "temperature": re.compile(
        _N + r"\s*(?:\u00b0\s*[CF]\b|\u00ba\s*[CF]\b|[CF]\b"
             r"|\bdeg(?:rees)?\b|\bcelsius\b|\bfahrenheit\b)",
        re.IGNORECASE),
    "wind": re.compile(
        _N + r"\s*(?:\bkm\s*/\s*h\b|\bkmh\b|\bkph\b|\bmph\b|\bm\s*/\s*s\b)",
        re.IGNORECASE),
    "rain": re.compile(_N + r"\s*(?:\bmm\b|\bcm\b)", re.IGNORECASE),
    "percent": re.compile(_N + r"\s*(?:%|\bpercent\b|\bper\s?cent\b)", re.IGNORECASE),
    # The lookahead keeps "12 km/h" out of the distance class; that is wind, above.
    "distance": re.compile(_N + r"\s*(?:\bkm\b(?!\s*/)|\bmiles?\b)", re.IGNORECASE),
}

# ... and the same claim written the other way round: "a UV index of 8". The leading
# noun is what names the quantity here, so it selects the class directly.
_LEAD = r"\s*(?:of|is|at|around|near|reaching|to|sits at)?\s*"
UNIT_BEFORE_PATTERNS = {
    "temperature": re.compile(r"(?:temperature|feels\s+like|apparent)" + _LEAD + _N, re.IGNORECASE),
    "wind": re.compile(r"(?:wind|gusts?)" + _LEAD + _N, re.IGNORECASE),
    "rain": re.compile(r"(?:rainfall|precipitation)" + _LEAD + _N, re.IGNORECASE),
    "percent": re.compile(r"(?:humidity|chance|probability)" + _LEAD + _N, re.IGNORECASE),
    "distance": re.compile(r"(?:visibility)" + _LEAD + _N, re.IGNORECASE),
    "uv": re.compile(r"(?:uv(?:\s+index)?|index)" + _LEAD + _N, re.IGNORECASE),
}

# Which snapshot fields each quantity may be checked against.
QUANTITY_FIELDS = {
    "temperature": {"temperature_c", "apparent_temperature_c", "temp_max_24h_c",
                    "temp_min_24h_c", "apparent_temp_max_24h_c"},
    "wind": {"wind_speed_kmh", "wind_gusts_kmh", "gust_differential_kmh",
             "gust_peak_24h_kmh"},
    "rain": {"precipitation_mm", "rain_24h_mm"},
    "percent": {"precipitation_probability_pct", "humidity_pct", "cloud_cover_pct"},
    "distance": {"visibility_km"},
    "uv": {"uv_index", "uv_max_24h"},
}

CLOCK_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
HOUR_RE = re.compile(r"\b(1[0-2]|[1-9])\s?(?:am|pm)\b", re.IGNORECASE)

# A number attached to a time unit is a duration, never a weather reading -- "over the
# next 24 hours", "wait 30 minutes", "for seven seconds". Without this the guard rejects
# its own fact labels ("Rain total next 24h") and, worse, would reject a correct reply
# that simply mentioned a 24-hour outlook, forcing a needless fallback.
DURATION_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*-?\s*"
    r"(?:h|hr|hrs|hour|hours|min|mins|minute|minutes|sec|secs|second|seconds"
    r"|day|days|week|weeks|month|months)\b",
    re.IGNORECASE,
)


@dataclass
class GroundingReport:
    ok: bool
    ungrounded_numbers: list[str] = field(default_factory=list)
    missing_citation: bool = False
    foreign_sop_ids: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)

    def reason(self) -> str:
        bits = []
        if self.ungrounded_numbers:
            bits.append(f"numbers not in the forecast: {', '.join(self.ungrounded_numbers)}")
        if self.unsupported_claims:
            bits.append(f"claims the forecast contradicts: {'; '.join(self.unsupported_claims)}")
        if self.missing_citation:
            bits.append("the required policy id was not cited")
        if self.foreign_sop_ids:
            bits.append(f"cited unknown policy ids: {', '.join(self.foreign_sop_ids)}")
        return "; ".join(bits) or "ok"


def _normalise_id(raw: str) -> str:
    return re.sub(r"[-_ ]", "-", raw.strip().upper())


def _renderings(value: float) -> set[str]:
    """The forms a model might write one number in: 7, 7.0, 7.35."""
    out = {f"{value:.1f}", f"{value:.0f}", str(value)}
    if float(value).is_integer():
        out.add(str(int(value)))
    return out


def _walk_conditions(node, out: list[tuple[str, float]]) -> None:
    """Collect (field, threshold) pairs from a policy's match tree.

    A policy's own thresholds are quotable -- "our guidance applies above 50 km/h" -- but
    only as the quantity they actually constrain. Pairing each number with its field is
    what keeps a rain threshold from vouching for a temperature claim.
    """
    if isinstance(node, list):
        for item in node:
            _walk_conditions(item, out)
        return
    if not isinstance(node, dict):
        return
    if "field" in node and isinstance(node.get("value"), (int, float)) and not isinstance(node.get("value"), bool):
        out.append((node["field"], float(node["value"])))
    if "field" in node and isinstance(node.get("value"), list):
        for v in node["value"]:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((node["field"], float(v)))
    for key in ("all", "any", "not"):
        if key in node:
            _walk_conditions(node[key], out)


def build_allowset(snapshot: WeatherSnapshot, sops: list[SOP]) -> set[str]:
    """Every number the reply may contain, regardless of what it claims to be.

    Used only for numbers that carry NO unit, where the text gives us nothing to check
    the quantity against. Unit-bearing claims go through build_typed_allowset instead.
    """
    allowed = snapshot.allowed_numbers()
    for sop in sops:
        allowed |= sop.numeric_literals()
    return allowed


def build_typed_allowset(snapshot: WeatherSnapshot, sops: list[SOP]) -> dict[str, set[str]]:
    """Per-quantity allow-sets: what a temperature claim may say, what a wind claim may say.

    Deliberately excludes the forecast timestamp, which build_allowset does include. Its
    digits are real data for an unqualified number ("the forecast is for the 13th") but
    they are not readings, and while they sat in one flat bag "2026 C" and "13 C" both
    passed as temperatures.
    """
    typed: dict[str, set[str]] = {}
    for quantity, fields in QUANTITY_FIELDS.items():
        values: set[str] = set()
        for name in fields:
            value = snapshot.fields.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values |= _renderings(float(value))
        typed[quantity] = values

    field_to_quantity = {f: q for q, fs in QUANTITY_FIELDS.items() for f in fs}
    for sop in sops:
        pairs: list[tuple[str, float]] = []
        _walk_conditions(sop.match, pairs)
        for name, value in pairs:
            quantity = field_to_quantity.get(name)
            if quantity:
                typed[quantity] |= _renderings(value)
    return typed


def _matches(token: str, allowed: set[str]) -> bool:
    """A model may write 7 for 7.0, or 21.8 for 21.80 -- compare numerically too."""
    cleaned = token.replace(",", "")
    if cleaned in allowed:
        return True
    try:
        value = float(cleaned)
    except ValueError:
        return True  # not a number we can judge; leave it alone
    return f"{value:.1f}" in allowed or f"{value:.0f}" in allowed


def check(
    draft: str,
    snapshot: WeatherSnapshot,
    selected: SOP,
    secondary: list[SOP] | None = None,
    question: str = "",
) -> GroundingReport:
    """Verify one composed reply. Returns a report; never modifies the draft.

    `question` is the user's own message. Policy ids THEY raised may appear in the reply,
    so the bot can say "we have no SOP-99"; ids it introduces on its own may not. Without
    that distinction the guard blocks the very correction it exists to enable -- it
    rejected two drafts for the crime of naming the fabricated policy they were refuting,
    and the bot's defence collapsed into silence.
    """
    secondary = secondary or []
    cited_sops = [selected, *secondary]
    allowed = build_allowset(snapshot, cited_sops)

    # Strip clock times and policy ids before scanning for numbers, so "16:00" and the
    # digits inside "SOP-EX-003" are not mistaken for weather claims.
    scrubbed = SOP_ID_RE.sub(" ", draft)
    scrubbed = CLOCK_RE.sub(" ", scrubbed)
    scrubbed = HOUR_RE.sub(" ", scrubbed)
    scrubbed = DURATION_RE.sub(" ", scrubbed)

    ungrounded: list[str] = []

    # Pass 1 -- numbers making an explicit weather claim ("30 km/h", "20 C", "60%",
    # "a UV index of 8"). These are held to the strict allow-set with no prose
    # allowance, because this is exactly where a fabricated figure would hide.
    claimed: set[str] = set()
    typed = build_typed_allowset(snapshot, cited_sops)
    for patterns in (UNIT_AFTER_PATTERNS, UNIT_BEFORE_PATTERNS):
        for quantity, pattern in patterns.items():
            for token in pattern.findall(scrubbed):
                claimed.add(token)
                if not _matches(token, typed.get(quantity, set())):
                    ungrounded.append(token)

    # Pass 2 -- every other number. Small unit-less counts are ordinary prose.
    for token in NUMBER_RE.findall(scrubbed):
        if token in claimed:
            continue
        if token.replace(",", "") in SMALL_COUNTS:
            continue
        if not _matches(token, allowed):
            ungrounded.append(token)

    # Pass 3 -- non-numeric claims. The allow-set cannot judge a sentence like "the storm
    # covers only part of today", which contains no figure at all. See guards/claims.py.
    claims = check_claims(draft, snapshot)

    # Ids the user raised themselves are allowed to appear -- the reply needs to name a
    # fabricated policy in order to deny it. Ids the model introduces are not.
    allowed_ids = {_normalise_id(s.id) for s in cited_sops}
    allowed_ids |= {_normalise_id(m) for m in SOP_ID_RE.findall(question or "")}
    found_ids = {_normalise_id(m) for m in SOP_ID_RE.findall(draft)}

    return GroundingReport(
        ok=not ungrounded
        and not claims
        and _normalise_id(selected.id) in found_ids
        and not (found_ids - allowed_ids),
        ungrounded_numbers=sorted(set(ungrounded)),
        unsupported_claims=claims,
        missing_citation=_normalise_id(selected.id) not in found_ids,
        foreign_sop_ids=sorted(found_ids - allowed_ids),
    )


# Split on a full stop that ends a sentence -- preceded by a letter or bracket, followed
# by a capital. Decimals ("4.5 mm") and clock times ("before 10:00.") are left intact,
# which a naive split on ". " would not manage.
_SENTENCE_END = re.compile(r"(?<=[a-z\)])\.\s+(?=[A-Z])")


def _gist(advice: str) -> str:
    """The first sentence of a policy's advice.

    Used for the policies that trail the leading one. Printing all three in full is how
    a thunderstorm warning ends up followed by six paragraphs on sunscreen: everything
    on screen is true, and the thing you needed is buried. A secondary hazard still has
    to be surfaced -- suppressing it would be a safety regression -- but surfacing is
    naming it, not reciting it.
    """
    first_para = advice.strip().split("\n\n")[0].replace("\n", " ").strip()
    parts = _SENTENCE_END.split(first_para, maxsplit=1)
    sentence = parts[0].strip()
    return sentence if sentence.endswith((".", "!", "?")) else sentence + "."


def render_deterministic(
    snapshot: WeatherSnapshot,
    selected: SOP,
    secondary: list[SOP] | None = None,
) -> str:
    """The fallback when the model cannot produce a grounded reply.

    Plainer than a composed answer, but it cannot be wrong: the advice is the policy
    text verbatim and the figures are formatted straight from the snapshot.
    """
    secondary = secondary or []
    when = {
        "now": "right now",
        "today": "today",
        "morning": "this morning",
        "afternoon": "this afternoon",
        "evening": "this evening",
        "night": "tonight",
    }.get(snapshot.window, snapshot.window)

    # Show the readings the cited policies actually tested, not the whole snapshot.
    fields: set[str] = set()
    for sop in [selected, *secondary]:
        fields |= sop.referenced_fields()

    lines = [
        f"For {snapshot.location.label}, {when} — {selected.title.lower()}.",
        "",
        selected.advice.strip(),
    ]
    # One line each. The full text of every cited policy is what made a storm warning
    # arrive with six paragraphs of sun advice attached; the id is printed so anyone who
    # wants the rest can read the policy itself.
    for sop in secondary:
        lines += ["", f"Also relevant — {sop.title.lower()} ({sop.id}):", _gist(sop.advice)]

    lines += ["", "Based on this forecast:", snapshot.relevant_facts(fields)]
    ids = ", ".join([selected.id, *(s.id for s in secondary)])
    lines += ["", f"Policy: {ids}"]
    return "\n".join(lines)
