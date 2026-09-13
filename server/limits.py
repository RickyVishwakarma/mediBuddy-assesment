"""Request limiting for the public demo.

Only active when DEMO_MODE=1, so local development is untouched.

A public URL on a free-tier key is a shared, exhaustible budget: without a cap, one
person clicking around spends the day's allowance and everyone after them sees failures.
That degrades honestly -- the bot says it cannot answer rather than guessing -- but it
looks broken, which is a worse outcome than a link that politely says "come back in a
minute".

Two limits, both deliberately conservative:

  * per visitor, a short window, so nobody can loop on it
  * a global daily cap well under the model quota, so the demo cannot spend the whole
    budget before anyone else arrives

State is in-process, which is the right scope: the Space runs one container, and a
restart resetting the counters is acceptable for a demo. Anything durable would be more
machinery than the problem deserves.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque

DEMO_MODE = os.getenv("DEMO_MODE", "").strip() in {"1", "true", "yes"}

PER_VISITOR_REQUESTS = int(os.getenv("DEMO_PER_VISITOR", "6"))
PER_VISITOR_WINDOW_S = int(os.getenv("DEMO_WINDOW_SECONDS", "600"))
DAILY_TOTAL = int(os.getenv("DEMO_DAILY_TOTAL", "60"))

_lock = threading.Lock()
_visitors: dict[str, deque[float]] = defaultdict(deque)
_day: list = [time.strftime("%Y-%m-%d"), 0]  # [date, count]

TOO_MANY = (
    "This public demo limits how many questions it can answer, so the free API budget "
    "isn't spent by one visitor. Please try again in a few minutes.\n\n"
    "To use it without limits, clone the repo and run it with your own API key -- the "
    "README has the two commands."
)

BUDGET_SPENT = (
    "This public demo has used its free API budget for today. Nothing is broken: the "
    "model is simply unavailable, and this bot is built to say so rather than invent an "
    "answer.\n\n"
    "Clone the repo and run it with your own key to try it properly -- the README has "
    "the two commands, and the eval suite runs offline for five of its cases."
)


def check(visitor: str) -> str | None:
    """Return a message to show instead of answering, or None to proceed."""
    if not DEMO_MODE:
        return None

    now = time.time()
    today = time.strftime("%Y-%m-%d")

    with _lock:
        if _day[0] != today:
            _day[0], _day[1] = today, 0
            _visitors.clear()

        if _day[1] >= DAILY_TOTAL:
            return BUDGET_SPENT

        seen = _visitors[visitor]
        while seen and now - seen[0] > PER_VISITOR_WINDOW_S:
            seen.popleft()
        if len(seen) >= PER_VISITOR_REQUESTS:
            return TOO_MANY

        seen.append(now)
        _day[1] += 1
        return None


def status() -> dict:
    with _lock:
        return {
            "demo_mode": DEMO_MODE,
            "used_today": _day[1],
            "daily_total": DAILY_TOTAL,
            "per_visitor": PER_VISITOR_REQUESTS,
            "window_seconds": PER_VISITOR_WINDOW_S,
        }
