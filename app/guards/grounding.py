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

from app.sops.schema import SOP
from app.weather.snapshot import WeatherSnapshot

# Numbers as a model would write them: 34, 34.5, 1,200, 70%.
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
SOP_ID_RE = re.compile(r"\bSOP[-_ ]?[A-Za-z]{2,4}[-_ ]?\d{1,4}\b", re.IGNORECASE)

# Small unit-less counts that appear in ordinary prose -- "both hands", "two hours",
# "a couple of layers". Deliberately capped at ten: anything larger is far more likely
# to be a weather figure than a count, and letting 30 or 60 through unchecked was a real
# hole (a hallucinated "30 km/h" passed as a prose number until this was tightened).
SMALL_COUNTS = {str(n) for n in range(0, 11)}

# A number carrying one of these is making a weather claim, and gets NO prose allowance:
# it must come from the API or from the cited policy's own text.
UNIT_AFTER_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:°\s*[CF]\b|º\s*[CF]\b|\bdeg(?:rees)?\b|\bcelsius\b|\bfahrenheit\b"
    r"|\bkm\s*/?\s*h\b|\bkmh\b|\bkph\b|\bmph\b|\bm/s\b"
    r"|\bmm\b|\bcm\b|%|\bpercent\b|\bper\s?cent\b"
    r"|\bkm\b(?!\s*/)|\bmiles?\b)",
    re.IGNORECASE,
)
# ... and the same claim written the other way round: "a UV index of 8".
UNIT_BEFORE_RE = re.compile(
    r"(?:uv(?:\s+index)?|index|humidity|temperature|wind|gusts?|rainfall|precipitation|"
    r"visibility|chance|probability)\s*(?:of|is|at|around|near|reaching|to)?\s*"
    r"(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)

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

    def reason(self) -> str:
        bits = []
        if self.ungrounded_numbers:
            bits.append(f"numbers not in the forecast: {', '.join(self.ungrounded_numbers)}")
        if self.missing_citation:
            bits.append("the required policy id was not cited")
        if self.foreign_sop_ids:
            bits.append(f"cited unknown policy ids: {', '.join(self.foreign_sop_ids)}")
        return "; ".join(bits) or "ok"


def _normalise_id(raw: str) -> str:
    return re.sub(r"[-_ ]", "-", raw.strip().upper())


def build_allowset(snapshot: WeatherSnapshot, sops: list[SOP]) -> set[str]:
    """Numbers the reply is permitted to contain.

    Two sources, both auditable: values the API returned for THIS request, and numbers
    written into the policies being cited (so a reply may quote its own rule, "gusts
    above 50 km/h", or a policy-authored instruction, "wait 30 minutes").

    Note this does NOT include the prose small-count allowance -- see check(), which
    applies that only to numbers that carry no weather unit.
    """
    allowed = snapshot.allowed_numbers()
    for sop in sops:
        allowed |= sop.numeric_literals()
    return allowed


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
) -> GroundingReport:
    """Verify one composed reply. Returns a report; never modifies the draft."""
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
    for pattern in (UNIT_AFTER_RE, UNIT_BEFORE_RE):
        for token in pattern.findall(scrubbed):
            claimed.add(token)
            if not _matches(token, allowed):
                ungrounded.append(token)

    # Pass 2 -- every other number. Small unit-less counts are ordinary prose.
    for token in NUMBER_RE.findall(scrubbed):
        if token in claimed:
            continue
        if token.replace(",", "") in SMALL_COUNTS:
            continue
        if not _matches(token, allowed):
            ungrounded.append(token)

    allowed_ids = {_normalise_id(s.id) for s in cited_sops}
    found_ids = {_normalise_id(m) for m in SOP_ID_RE.findall(draft)}

    return GroundingReport(
        ok=not ungrounded
        and _normalise_id(selected.id) in found_ids
        and not (found_ids - allowed_ids),
        ungrounded_numbers=sorted(set(ungrounded)),
        missing_citation=_normalise_id(selected.id) not in found_ids,
        foreign_sop_ids=sorted(found_ids - allowed_ids),
    )


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
    for sop in secondary:
        lines += ["", f"Also worth knowing — {sop.title.lower()}:", sop.advice.strip()]

    lines += ["", "Based on this forecast:", snapshot.relevant_facts(fields)]
    ids = ", ".join([selected.id, *(s.id for s in secondary)])
    lines += ["", f"Policy: {ids}"]
    return "\n".join(lines)
