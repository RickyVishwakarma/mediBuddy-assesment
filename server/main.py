"""FastAPI backend. Serves the chat UI and runs one graph turn per request."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.graph.build import ask
from app.sops.loader import load_policies

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Weather-Advisory Support Bot")


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)


class Citation(BaseModel):
    id: str
    title: str
    severity: str
    category: str = ""


class ChatResponse(BaseModel):
    reply: str
    # First-class, not parsed back out of the prose: this is what makes "why did it say
    # that" answerable without re-reading the answer.
    citations: list[Citation] = []
    no_guidance: bool = False
    failed: bool = False
    trace: list[str] = []


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    result = ask(request.message, session_id=request.session_id)
    return ChatResponse(**result)


@app.get("/policies")
def policies() -> dict:
    """Read-only view of the loaded policy set, so a reviewer can confirm which rules
    are live without reading the YAML directory."""
    return {
        "count": len(load_policies()),
        "policies": [
            {
                "id": p.id,
                "title": p.title,
                "category": p.category,
                "severity": p.severity,
                "override": p.override,
                "file": p.source_file,
            }
            for p in load_policies()
        ],
    }
