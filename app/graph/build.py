"""Graph construction.

Five conditional edges, four terminal response nodes, and one cycle
(verify_grounding -> compose_answer). The cycle is the part a linear chain cannot
express: retry the composition under a stricter prompt, and if it fails again leave by
a different exit entirely.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph

from app.graph import nodes
from app.graph.state import AdvisoryState
from app.sops.engine import MatchedSOP
from app.sops.schema import SOP
from app.weather.openmeteo import ResolvedLocation
from app.weather.snapshot import WeatherSnapshot

# State carries a few rich objects between nodes. LangGraph's default checkpoint serde
# accepts any type but warns, and a future version will refuse. Naming them explicitly
# silences the warning, keeps the graph working when that default flips, and is the
# tighter setting: deserialisation is restricted to exactly these four types instead of
# anything that happens to be in the checkpoint.
CHECKPOINT_TYPES = [ResolvedLocation, WeatherSnapshot, SOP, MatchedSOP]


def _checkpointer() -> MemorySaver:
    return MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))


def build_graph(checkpointer=None):
    g = StateGraph(AdvisoryState)

    g.add_node("parse_intent", nodes.parse_intent)
    g.add_node("resolve_location", nodes.resolve_location)
    g.add_node("fetch_weather", nodes.fetch_weather)
    g.add_node("build_snapshot", nodes.build_snapshot)
    g.add_node("match_sops", nodes.match_sops)
    g.add_node("rank_and_select", nodes.rank_and_select)
    g.add_node("compose_answer", nodes.compose_answer)
    g.add_node("verify_grounding", nodes.verify_grounding)
    g.add_node("deterministic_render", nodes.deterministic_render)
    g.add_node("no_match_response", nodes.no_match_response)
    g.add_node("greeting_response", nodes.greeting_response)
    g.add_node("failure_response", nodes.failure_response)

    g.set_entry_point("parse_intent")

    # Out-of-scope questions never reach the weather API. Cheaper, and it keeps the
    # "no guidance" path visibly distinct from the "couldn't get data" path.
    g.add_conditional_edges(
        "parse_intent",
        nodes.route_after_intent,
        {"resolve": "resolve_location", "out_of_scope": "no_match_response",
         "greeting": "greeting_response", "fail": "failure_response"},
    )
    g.add_conditional_edges(
        "resolve_location",
        nodes.route_after_location,
        {"ok": "fetch_weather", "fail": "failure_response"},
    )
    g.add_conditional_edges(
        "fetch_weather",
        nodes.route_after_weather,
        {"ok": "build_snapshot", "fail": "failure_response"},
    )
    g.add_edge("build_snapshot", "match_sops")
    g.add_conditional_edges(
        "match_sops",
        nodes.route_after_match,
        {"matched": "rank_and_select", "none": "no_match_response"},
    )
    g.add_edge("rank_and_select", "compose_answer")
    g.add_edge("compose_answer", "verify_grounding")
    g.add_conditional_edges(
        "verify_grounding",
        nodes.route_after_verify,
        {"pass": END, "retry": "compose_answer", "fallback": "deterministic_render"},
    )

    for terminal in ("deterministic_render", "no_match_response", "greeting_response",
                     "failure_response"):
        g.add_edge(terminal, END)

    return g.compile(checkpointer=checkpointer or _checkpointer())


GRAPH = build_graph()


def ask(question: str, session_id: str, graph=None) -> dict:
    """Run one turn. `session_id` is the checkpointer thread, so two callers with
    different ids cannot see each other's history."""
    graph = graph or GRAPH
    result = graph.invoke(
        {"messages": [HumanMessage(content=question)]},
        config={"configurable": {"thread_id": session_id}},
    )
    snapshot = result.get("snapshot")
    return {
        "reply": result.get("final") or "",
        "citations": result.get("citations") or [],
        "no_guidance": bool(result.get("no_guidance")),
        "failed": bool(result.get("failed")),
        "trace": result.get("trace") or [],
        # Below here is for the eval suite, not the chat UI. Exposing the snapshot's
        # permitted numbers lets a test re-check grounding independently of the guard
        # that already ran, rather than taking the graph's word for it.
        "snapshot_fields": dict(snapshot.fields) if snapshot else {},
        "allowed_numbers": sorted(snapshot.allowed_numbers()) if snapshot else [],
        "grounding": result.get("grounding") or {},
        # The underlying cause, so a test can report why a run failed rather than
        # guessing at it. Never shown to a user -- see FAILURE_TEXTS in nodes.py.
        "failure": result.get("failure") or {},
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--mermaid" in sys.argv:
        print(GRAPH.get_graph().draw_mermaid())
    else:
        print("Nodes:", ", ".join(sorted(GRAPH.get_graph().nodes)))
