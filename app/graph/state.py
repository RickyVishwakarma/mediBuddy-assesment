"""Graph state and the structured types that move through it."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from app.vocabulary import ACTIVITIES

TIME_WINDOWS = ["now", "morning", "afternoon", "evening", "night", "today"]

TRACE_RESET = "__reset__"


def trace_reducer(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Accumulates node names within a turn, and starts fresh at the top of each turn.

    State persists across turns via the checkpointer, so without the reset marker the
    trace would grow for the life of the session and no longer describe the path this
    particular question took.
    """
    right = right or []
    if right and right[0] == TRACE_RESET:
        return right[1:]
    return (left or []) + right


class Intent(BaseModel):
    """What parse_intent is allowed to produce. Note the absence of any advice field --
    the intake step has no way to express an opinion about safety."""

    in_scope: bool = False
    location: str = ""
    activity: str = "general_outdoor"
    time_window: str = "now"
    is_followup: bool = False
    user_asserted_facts: list[str] = Field(default_factory=list)

    def normalised(self) -> "Intent":
        """Coerce the model's output onto the known vocabulary. An unrecognised activity
        becomes general_outdoor rather than silently failing every scope check."""
        activity = (self.activity or "").strip().lower()
        window = (self.time_window or "").strip().lower()
        return Intent(
            in_scope=self.in_scope,
            location=(self.location or "").strip(),
            activity=activity if activity in ACTIVITIES else "general_outdoor",
            time_window=window if window in TIME_WINDOWS else "now",
            is_followup=self.is_followup,
            user_asserted_facts=self.user_asserted_facts,
        )


class FailureInfo(BaseModel):
    kind: str            # "geocode" | "weather" | "llm"
    detail: str = ""     # safe to show a user -- currently only ever the place they typed
    debug: str = ""      # raw cause, for logs only, never rendered into a reply


class AdvisoryState(TypedDict, total=False):
    """One conversation thread.

    The split matters: `messages` and the `last_*` fields survive across turns via the
    checkpointer, while everything below them is scratch for the current turn. Notably
    the snapshot is NOT carried forward -- weather is re-fetched every turn so the bot
    can never answer turn three with turn one's numbers.
    """

    # --- persists across turns ---
    messages: Annotated[list[AnyMessage], add_messages]
    last_location: dict[str, Any] | None
    last_activity: str | None

    # --- per-turn scratch ---
    intent: dict[str, Any] | None
    location: dict[str, Any] | None
    raw_payload: dict[str, Any] | None
    snapshot: Any | None
    matched: list[Any]
    selected: Any | None
    secondary: list[Any]
    draft: str | None
    grounding: dict[str, Any] | None
    compose_attempts: int
    failure: dict[str, Any] | None

    # --- output ---
    final: str | None
    citations: list[dict[str, str]]
    no_guidance: bool
    failed: bool
    trace: Annotated[list[str], trace_reducer]
