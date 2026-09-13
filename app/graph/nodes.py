"""Graph nodes.

Division of labour, which is the thing to defend in review:

  * Three nodes call the model: parse_intent, match_sops (fuzzy conditions only), and
    compose_answer. Every other node is deterministic Python.
  * The router functions at the bottom contain no model call at all, so the LLM never
    decides which branch the graph takes.
  * The three terminal responses for failure and no-match are fixed templates. They
    cannot produce a forecast or a piece of advice even in principle.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage

from app.config import CONVERSATION_WINDOW, MAX_COMPOSE_ATTEMPTS
from app.graph.state import TRACE_RESET, AdvisoryState, FailureInfo, Intent
from app.guards import grounding
from app.llm.client import LLMUnavailable, load_prompt, structured, text
from app.llm.semantic import judge
from app.sops.engine import match_policies, select
from app.sops.loader import load_policies
from app.weather.openmeteo import ResolvedLocation, WeatherFetchError, fetch_forecast, geocode
from app.weather.snapshot import build_snapshot as make_snapshot

log = logging.getLogger(__name__)

# Fixed wording for the answers that must never be model-generated.
#
# Two distinct situations used to share OUT_OF_SCOPE_TEXT, and the shared wording was
# wrong for one of them. Asked "is today good for a picnic in Lisbon?", the bot resolved
# the city, fetched the forecast, matched nothing, and then replied that it covers
# "cycling, running, commuting..." and the user should ask about one of those -- while
# SOP-LP-001 lists `picnic` in its own applies_to. Disclaiming coverage we have reads as
# a scope problem when it is a policy-set problem, and it sends the user away instead of
# telling them what actually happened.
OUT_OF_SCOPE_TEXT = (
    "We don't have guidance covering that. Our advice only comes from written policies "
    "we maintain, and none of them apply to this question, so rather than guess I'd "
    "rather tell you plainly that we can't help here. We cover outdoor activity safety "
    "-- cycling, running, commuting, taking children or pets out, and similar plans -- "
    "for a specific place and time, so ask about one of those and I'll check it."
)

# In scope, location resolved, forecast in hand -- and no policy matched it. The honest
# answer names the gap rather than implying the question was the problem.
NO_POLICY_TEXT = (
    "I checked the forecast for {place}, but none of our written policies apply to "
    "these conditions, so I don't have guidance to give you. That's a gap in our policy "
    "set rather than a problem with the question. I won't fill it with a guess -- "
    "anything I said here would be my own opinion rather than our guidance."
)

NO_LOCATION_TEXT = (
    "I need to know where you are before I can check anything. Tell me the town or city "
    "and I'll look up the current forecast for it."
)

# Only `geocode` interpolates anything, and only the place name the user themselves
# typed. Everything else is fixed wording: an exception string can carry quota blobs,
# URLs and internal detail, and none of that belongs in front of a user.
FAILURE_TEXTS = {
    # The place genuinely isn't in the gazetteer -- a spelling suggestion is useful here.
    "geocode": (
        "I couldn't find a place matching \"{detail}\", so I have no forecast to check "
        "and won't guess at one. If the name has a common spelling variant, or there's a "
        "larger town nearby, try that instead."
    ),
    # The lookup itself failed. Telling the user their spelling is wrong would send them
    # off trying variants of a name that was never the problem.
    "geocode_unavailable": (
        "I couldn't look up \"{detail}\" just now -- the location service didn't respond. "
        "That's a problem on our side rather than anything wrong with the name, so it's "
        "worth simply trying again in a moment."
    ),
    "weather": (
        "I couldn't retrieve the forecast just now, so I have no live data to advise "
        "from and won't guess. Please try again shortly."
    ),
    "llm": (
        "I'm having trouble processing that request at the moment. I'd rather say so "
        "than answer without checking properly. Please try again shortly."
    ),
}


# --------------------------------------------------------------------------- nodes


def _fresh_turn() -> dict:
    """Clear last turn's scratch.

    State survives across turns via the checkpointer, so a failure or a snapshot left
    over from the previous question would otherwise leak into this one -- and a stale
    `failure` would route the graph straight to the error path for a perfectly good
    question. Facts never carry forward; only conversation and intent do.
    """
    return {
        "failure": None,
        "intent": None,
        "location": None,
        "raw_payload": None,
        "snapshot": None,
        "matched": [],
        "selected": None,
        "secondary": [],
        "draft": None,
        "grounding": None,
        "compose_attempts": 0,
        "final": None,
        "citations": [],
        "no_guidance": False,
        "failed": False,
    }


def parse_intent(state: AdvisoryState) -> dict:
    """LLM #1. Reads the question into a structured form and nothing else."""
    messages = state.get("messages", [])
    current = messages[-1].content if messages else ""
    history = messages[-(CONVERSATION_WINDOW + 1) : -1]

    transcript = "\n".join(
        f"{'User' if isinstance(m, HumanMessage) else 'Assistant'}: {m.content}"
        for m in history
    )
    last_loc = (state.get("last_location") or {}).get("query", "")
    carry = (
        f"\n\nEarlier in this session the user asked about: "
        f"location={last_loc or 'none'}, activity={state.get('last_activity') or 'none'}."
        if (last_loc or state.get("last_activity"))
        else ""
    )
    user = (
        (f"CONVERSATION SO FAR\n{transcript}\n\n" if transcript else "")
        + f"CURRENT MESSAGE\n{current}"
        + carry
    )

    try:
        intent = structured(load_prompt("intent"), user, Intent).normalised()
    except LLMUnavailable as exc:
        # The raw reason is kept for logs only; `detail` is what may reach a user.
        log.warning("parse_intent unavailable: %s", exc)
        return _fresh_turn() | {
            "failure": FailureInfo(kind="llm", detail="", debug=str(exc)).model_dump(),
            "trace": [TRACE_RESET, "parse_intent(unavailable)"],
        }

    # Follow-ups inherit what they left unsaid.
    if not intent.location and last_loc:
        intent = intent.model_copy(update={"location": last_loc})
    if intent.is_followup and intent.activity == "general_outdoor" and state.get("last_activity"):
        intent = intent.model_copy(update={"activity": state["last_activity"]})

    return _fresh_turn() | {
        "intent": intent.model_dump(),
        "trace": [TRACE_RESET, "parse_intent"],
    }


def resolve_location(state: AdvisoryState) -> dict:
    """Deterministic. Geocoding failure and an empty result are the same outcome."""
    intent = Intent(**state["intent"])
    try:
        loc = geocode(intent.location)
    except WeatherFetchError as exc:
        return {
            "failure": FailureInfo(kind=exc.kind, detail=intent.location).model_dump(),
            "trace": ["resolve_location(failed)"],
        }

    resolved = {
        "query": intent.location,
        "name": loc.name,
        "country": loc.country,
        "admin1": loc.admin1,
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "timezone": loc.timezone,
    }
    return {
        "location": resolved,
        "last_location": resolved,
        "last_activity": intent.activity,
        "trace": ["resolve_location"],
    }


def fetch_weather(state: AdvisoryState) -> dict:
    """Deterministic. Raw payload only -- no interpretation happens here."""
    loc = _location_obj(state)
    try:
        payload = fetch_forecast(loc)
    except WeatherFetchError as exc:
        return {
            "failure": FailureInfo(kind=exc.kind, detail=exc.detail).model_dump(),
            "trace": ["fetch_weather(failed)"],
        }
    return {"raw_payload": payload, "trace": ["fetch_weather"]}


def build_snapshot(state: AdvisoryState) -> dict:
    """Deterministic. Produces the single source of numeric truth for this turn."""
    intent = Intent(**state["intent"])
    snapshot = make_snapshot(state["raw_payload"], _location_obj(state), intent.time_window)
    return {"snapshot": snapshot, "trace": ["build_snapshot"]}


def match_sops(state: AdvisoryState) -> dict:
    """Rule engine over the snapshot. LLM #2 is consulted only for `semantic:` conditions."""
    intent = Intent(**state["intent"])
    matches = match_policies(load_policies(), state["snapshot"], intent.activity, judge=judge)
    return {
        "matched": matches,
        "trace": [f"match_sops({len(matches)} matched)"],
    }


def rank_and_select(state: AdvisoryState) -> dict:
    """Deterministic conflict resolution: override, then severity, then specificity, then id."""
    lead, secondary = select(state["matched"])
    return {
        "selected": lead,
        "secondary": secondary,
        "compose_attempts": 0,
        "trace": [f"rank_and_select({lead.id})"],
    }


def compose_answer(state: AdvisoryState) -> dict:
    """LLM #3. Sees the pre-rendered fact block and the policy text -- never raw JSON."""
    snapshot = state["snapshot"]
    lead = state["selected"]
    secondary = state.get("secondary") or []
    intent = Intent(**state["intent"])
    attempts = state.get("compose_attempts", 0)
    messages = state.get("messages", [])
    question = messages[-1].content if messages else ""

    secondary_block = (
        "\n\n".join(
            f"SECONDARY POLICY {m.sop.id} ({m.sop.severity}) -- {m.sop.title}\n"
            f"{m.sop.advice.strip()}"
            for m in secondary
        )
        or "(none)"
    )
    asserted = (
        "\n".join(f"- {c}" for c in intent.user_asserted_facts)
        or "(the user asserted nothing)"
    )

    notes = lead.sop.compose_notes.strip()
    user = (
        f"USER QUESTION\n{question}\n\n"
        f"CLAIMS THE USER MADE, WHICH ARE NOT EVIDENCE\n{asserted}\n\n"
        f"FACTS (the only numbers you may use)\n{snapshot.fact_block()}\n\n"
        f"POLICY {lead.sop.id} ({lead.sop.severity}) -- {lead.sop.title}\n"
        f"{lead.sop.advice.strip()}\n\n"
        + (f"HOW THIS POLICY WANTS TO BE DELIVERED\n{notes}\n\n" if notes else "")
        + f"{secondary_block}"
    )
    if attempts:
        user += (
            "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED: "
            f"{(state.get('grounding') or {}).get('reason', 'ungrounded content')}. "
            "Write it again. Use only figures that appear verbatim in the FACTS block, "
            "and cite only the policy ids given above."
        )

    try:
        draft = text(load_prompt("compose"), user, temperature=0.2 if not attempts else 0.0)
    except LLMUnavailable:
        # The policy and the numbers are both in hand; only the wording is missing.
        # Render it deterministically rather than failing the turn.
        return {
            "draft": None,
            "compose_attempts": attempts + 1,
            "grounding": {"ok": False, "reason": "model unavailable"},
            "trace": ["compose_answer(unavailable)"],
        }

    return {
        "draft": draft,
        "compose_attempts": attempts + 1,
        "trace": [f"compose_answer(attempt {attempts + 1})"],
    }


def verify_grounding(state: AdvisoryState) -> dict:
    """Deterministic gate. Nothing reaches a user without passing here."""
    draft = state.get("draft")
    if not draft:
        return {"grounding": {"ok": False, "reason": "no draft"}, "trace": ["verify_grounding(no draft)"]}

    lead = state["selected"]
    secondary = state.get("secondary") or []
    messages = state.get("messages", [])
    question = messages[-1].content if messages else ""
    report = grounding.check(
        draft, state["snapshot"], lead.sop, [m.sop for m in secondary], question=question
    )

    result = {
        "grounding": {
            "ok": report.ok,
            "reason": report.reason(),
            "ungrounded_numbers": report.ungrounded_numbers,
            "foreign_sop_ids": report.foreign_sop_ids,
            "missing_citation": report.missing_citation,
        },
        "trace": [f"verify_grounding({'pass' if report.ok else 'REJECTED: ' + report.reason()})"],
    }
    if report.ok:
        result |= _finish(state, draft)
    return result


def deterministic_render(state: AdvisoryState) -> dict:
    """Fallback terminal: the policy text plus the snapshot, no model involved."""
    lead = state["selected"]
    secondary = state.get("secondary") or []
    body = grounding.render_deterministic(state["snapshot"], lead.sop, [m.sop for m in secondary])
    return {"trace": ["deterministic_render"]} | _finish(state, body)


def no_match_response(state: AdvisoryState) -> dict:
    """Terminal: a fixed template. Structurally incapable of inventing advice.

    Three arrivals, three different truths, so three templates. We never got a place; the
    question was outside what we cover at all; or we did the whole lookup and our own
    policy set came up empty. Only the last one is our shortcoming, and saying so is the
    difference between an honest gap and a brush-off.
    """
    intent_data = state.get("intent") or {}
    needs_location = intent_data.get("in_scope") and not intent_data.get("location")

    if needs_location:
        body = NO_LOCATION_TEXT
    elif state.get("snapshot"):
        # We reached match_sops with a real forecast and nothing fired.
        body = NO_POLICY_TEXT.format(place=_location_obj(state).label)
    else:
        body = OUT_OF_SCOPE_TEXT
    return {
        "final": body,
        "citations": [],
        "no_guidance": True,
        "failed": False,
        "messages": [AIMessage(content=body)],
        "trace": ["no_match_response"],
    }


def failure_response(state: AdvisoryState) -> dict:
    """Terminal: a fixed template containing no weather figures at all."""
    failure = state.get("failure") or {"kind": "weather", "detail": "an unexpected error"}
    template = FAILURE_TEXTS.get(failure["kind"], FAILURE_TEXTS["weather"])
    body = template.format(detail=failure.get("detail", ""))
    return {
        "final": body,
        "citations": [],
        "no_guidance": False,
        "failed": True,
        "messages": [AIMessage(content=body)],
        "trace": ["failure_response"],
    }


# --------------------------------------------------------------------------- routers
# No LLM call appears below this line. Routing is entirely deterministic.


def route_after_intent(state: AdvisoryState) -> str:
    if state.get("failure"):
        return "fail"
    intent = state.get("intent") or {}
    if not intent.get("in_scope"):
        return "out_of_scope"
    if not intent.get("location"):
        return "out_of_scope"   # handled by no_match_response as "tell me where"
    return "resolve"


def route_after_location(state: AdvisoryState) -> str:
    return "fail" if state.get("failure") else "ok"


def route_after_weather(state: AdvisoryState) -> str:
    return "fail" if state.get("failure") else "ok"


def route_after_match(state: AdvisoryState) -> str:
    return "matched" if state.get("matched") else "none"


def route_after_verify(state: AdvisoryState) -> str:
    if (state.get("grounding") or {}).get("ok"):
        return "pass"
    if state.get("compose_attempts", 0) < MAX_COMPOSE_ATTEMPTS:
        return "retry"
    return "fallback"


# --------------------------------------------------------------------------- helpers


def _location_obj(state: AdvisoryState) -> ResolvedLocation:
    loc = state["location"]
    return ResolvedLocation(
        name=loc["name"],
        country=loc["country"],
        admin1=loc["admin1"],
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        timezone=loc["timezone"],
    )


def _finish(state: AdvisoryState, body: str) -> dict:
    lead = state["selected"]
    secondary = state.get("secondary") or []
    citations = [
        {"id": m.sop.id, "title": m.sop.title, "severity": m.sop.severity, "category": m.sop.category}
        for m in [lead, *secondary]
    ]
    return {
        "final": body,
        "citations": citations,
        "no_guidance": False,
        "failed": False,
        "messages": [AIMessage(content=body)],
    }
