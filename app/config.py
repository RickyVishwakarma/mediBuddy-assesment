"""Central configuration. Reads .env once at import."""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# Two models, split by what the call actually needs.
#
# FAST handles intent extraction and the fuzzy-condition judge: both are constrained
# classification into a fixed schema, where a small model is as good as a large one.
# COMPOSE handles the user-facing prose, which is the only place output quality shows.
#
# It also spreads usage across two per-model quota buckets, which matters on the free
# tier where the daily allowance is counted per model.
GEMINI_MODEL_FAST = os.getenv("GEMINI_MODEL_FAST", "gemini-3.5-flash-lite")
GEMINI_MODEL_COMPOSE = os.getenv("GEMINI_MODEL_COMPOSE", "gemini-3.5-flash")

HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))

POLICY_DIR = REPO_ROOT / "app" / "sops" / "policies"
PROMPT_DIR = REPO_ROOT / "app" / "llm" / "prompts"

# How many prior turns parse_intent gets to see when resolving a follow-up.
CONVERSATION_WINDOW = 6

# Max compose attempts before the grounding guard gives up and renders
# the SOP deterministically (see app/guards/grounding.py).
MAX_COMPOSE_ATTEMPTS = 2

# Retries for transient model errors. The Gemini free tier allows only a few requests
# per minute and one question can use three calls, so a burst hits the limit easily.
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
