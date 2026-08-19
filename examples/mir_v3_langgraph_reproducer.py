"""LangGraph target for the MIR-v3 comprehensive demo's `handoff` op step.

The AgenticPipelineComponent's `handoff` op imports this module + calls
`run_reproduction_analysis(**initial_state)` — it's ONE step of a Dagster
pipeline. Inside, we run a real LangGraph state-machine graph:

    plan → hypothesize → verify → refine → finalize

Each node adds to the shared state. Terminal node returns a dict with:
  - final_answer      (the text downstream Dagster steps consume)
  - n_nodes_executed  (surfaces to asset metadata via handoff's roll-up)
  - cost_usd          (rolled up to asset metadata)
  - trajectory        (per-node trace)

Point is: user's LangGraph code sits in the customer's own project;
Dagster's `handoff` op materializes the final answer as one asset with
the framework's per-node trace in metadata. Framework's internal graph
is opaque to Dagster's asset graph — that's the intentional trade-off
at ONE step of the pipeline. Adjacent Dagster steps (fan-out, MCP calls,
Slack HITL) stay first-class assets.
"""

import os
import time
from typing import Any, Dict, List, TypedDict


class ReproState(TypedDict, total=False):
    issue_facts: str
    hypothesis: str
    verification: str
    refined: str
    final_answer: str
    trajectory: List[Dict[str, Any]]
    total_cost_usd: float
    n_nodes: int


def _llm(messages, model="gpt-4o-mini", max_tokens=400) -> Dict[str, Any]:
    """Thin LiteLLM wrapper — returns (content, cost)."""
    import litellm
    litellm.drop_params = True
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if os.environ.get("OPENAI_API_KEY"):
        kwargs["api_key"] = os.environ["OPENAI_API_KEY"]
    t0 = time.time()
    resp = litellm.completion(**kwargs)
    latency = int((time.time() - t0) * 1000)
    try:
        cost = float(litellm.completion_cost(completion_response=resp))
    except Exception:  # noqa: BLE001
        cost = 0.0
    return {"content": resp.choices[0].message.content or "", "cost_usd": cost, "latency_ms": latency}


def _plan_node(state: ReproState) -> ReproState:
    r = _llm([
        {"role": "system", "content": "You are a bug reproduction planner. Given the issue facts, outline a 2-sentence PLAN to reproduce the bug."},
        {"role": "user", "content": state["issue_facts"]},
    ])
    trajectory = list(state.get("trajectory") or [])
    trajectory.append({"node": "plan", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]})
    return {**state, "hypothesis": r["content"], "trajectory": trajectory, "total_cost_usd": (state.get("total_cost_usd") or 0.0) + r["cost_usd"]}


def _hypothesize_node(state: ReproState) -> ReproState:
    r = _llm([
        {"role": "system", "content": "You are a bug hypothesis generator. Given the reproduction plan, propose ONE testable hypothesis about the root cause. Be specific."},
        {"role": "user", "content": f"PLAN:\n{state['hypothesis']}\n\nISSUE:\n{state['issue_facts']}"},
    ])
    trajectory = list(state["trajectory"])
    trajectory.append({"node": "hypothesize", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]})
    return {**state, "hypothesis": r["content"], "trajectory": trajectory, "total_cost_usd": state["total_cost_usd"] + r["cost_usd"]}


def _verify_node(state: ReproState) -> ReproState:
    r = _llm([
        {"role": "system", "content": "You are a devil's advocate reviewer. Given the hypothesis, list ONE reason it might be WRONG. Be concrete."},
        {"role": "user", "content": f"HYPOTHESIS:\n{state['hypothesis']}\n\nISSUE:\n{state['issue_facts']}"},
    ])
    trajectory = list(state["trajectory"])
    trajectory.append({"node": "verify", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]})
    return {**state, "verification": r["content"], "trajectory": trajectory, "total_cost_usd": state["total_cost_usd"] + r["cost_usd"]}


def _refine_node(state: ReproState) -> ReproState:
    r = _llm([
        {"role": "system", "content": "Rewrite the reproduction hypothesis addressing the verifier's concern. Output the refined hypothesis + a suggested minimal repro (2-3 lines)."},
        {"role": "user", "content": f"HYPOTHESIS:\n{state['hypothesis']}\n\nVERIFIER CONCERN:\n{state['verification']}\n\nISSUE:\n{state['issue_facts']}"},
    ])
    trajectory = list(state["trajectory"])
    trajectory.append({"node": "refine", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]})
    return {**state, "refined": r["content"], "trajectory": trajectory, "total_cost_usd": state["total_cost_usd"] + r["cost_usd"]}


def _finalize_node(state: ReproState) -> ReproState:
    final = (
        "## Reproduction analysis (LangGraph handoff)\n\n"
        f"**Hypothesis:** {state['hypothesis']}\n\n"
        f"**Verifier concern:** {state['verification']}\n\n"
        f"**Refined + minimal repro:**\n{state['refined']}\n"
    )
    trajectory = list(state["trajectory"])
    trajectory.append({"node": "finalize", "text": final, "cost_usd": 0.0, "latency_ms": 0})
    return {**state, "final_answer": final, "trajectory": trajectory, "n_nodes": 5}


def _build_graph():
    """Compile the LangGraph state machine."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:
        raise ImportError(
            "This LangGraph handoff target requires langgraph: "
            "pip install 'langgraph>=0.2'"
        ) from e

    g = StateGraph(dict)
    g.add_node("plan", _plan_node)
    g.add_node("hypothesize", _hypothesize_node)
    g.add_node("verify", _verify_node)
    g.add_node("refine", _refine_node)
    g.add_node("finalize", _finalize_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "hypothesize")
    g.add_edge("hypothesize", "verify")
    g.add_edge("verify", "refine")
    g.add_edge("refine", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def run_reproduction_analysis(issue_facts: str) -> Dict[str, Any]:
    """Entry point invoked by the AgenticPipelineComponent's `handoff` op.

    Signature: `def fn(**initial_state) -> dict` — user's callable contract.
    Returns keys the Dagster asset metadata can roll up:
      - final_answer      → downstream `text` (via output_text_key='final_answer')
      - n_nodes_executed  → asset metadata rollup
      - cost_usd          → asset metadata rollup
      - trajectory        → full per-node trace, materialized as JSON metadata
    """
    graph = _build_graph()
    initial: ReproState = {"issue_facts": issue_facts, "trajectory": [], "total_cost_usd": 0.0}
    result = graph.invoke(initial)
    return {
        "final_answer": result["final_answer"],
        "n_nodes_executed": result.get("n_nodes", 0),
        "cost_usd": round(result.get("total_cost_usd") or 0.0, 6),
        "trajectory": result.get("trajectory") or [],
    }
