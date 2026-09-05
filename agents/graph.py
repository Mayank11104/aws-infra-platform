"""
graph.py — The LangGraph StateGraph that wires all agents together.

DESIGN PHILOSOPHY (simplified):
  The graph's only job is analysis and reporting.
  It has NO human approval node.
  It has NO interrupt/resume mechanism.
  It has NO SQLite checkpointer.

  The graph runs to completion, produces a Risk Brief markdown file,
  and exits. Jenkins handles ALL approval logic natively via its
  `input` step and Email Extension Plugin. The agents are purely
  advisory — they have zero involvement in the approve/reject decision.

Graph topology (linear):
  ingestion → supervisor → [secops, finops, blast_radius, integrity] (parallel)
                         → synthesizer (fan-in)
                         → risk_gate (sets requires_human_approval flag)
                         → END

The exit code from run_analysis.py tells Jenkins what the AI recommends,
but Jenkins always makes the final decision independently.
"""

from __future__ import annotations

import functools
import os

from langgraph.graph import END, StateGraph

from .bedrock_client import get_bedrock_llm
from .core.ingestion import ingestion_node
from .core.state import PipelineState, RiskLevel
from .nodes.blast_radius_agent import blast_radius_agent_node
from .nodes.finops_agent import finops_agent_node
from .nodes.integrity_agent import integrity_agent_node
from .nodes.secops_agent import secops_agent_node
from .nodes.synthesizer import synthesizer_node

# ---------------------------------------------------------------------------
# Model configuration
# ---------------------------------------------------------------------------
MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "mistral.mistral-7b-instruct-v0:2",  # Verified working in ap-south-1 via IAM
)
SYNTHESIZER_MODEL_ID = os.environ.get(
    "BEDROCK_SYNTHESIZER_MODEL_ID",
    "mistral.mistral-7b-instruct-v0:2",
)


# ---------------------------------------------------------------------------
# Risk gate node
# Determines requires_human_approval flag — does NOT route to an approval node.
# This flag is read by run_analysis.py to set the Jenkins exit code.
# Jenkins reads the exit code and decides whether to show its `input` step.
# ---------------------------------------------------------------------------

def risk_gate_node(state: PipelineState) -> dict:
    """
    Evaluates the overall risk and sets the `requires_human_approval` flag.
    Does NOT pause execution or involve agents in any approval decision.

    Rules for requiring human approval:
      - Production environment: ALWAYS
      - Overall risk is not LOW
      - Any agent has confidence < 0.8 (uncertain analysis)
      - Prompt injection flags detected
      - Stateful resource being destroyed or replaced
    """
    environment = state["environment"]
    overall_risk = state.get("overall_risk")
    injection_flags = state.get("injection_flags", [])
    blast = state.get("blast_radius_finding")

    reasons = []

    if environment == "production":
        reasons.append("production environment always requires human approval")

    if injection_flags:
        reasons.append(f"{len(injection_flags)} prompt injection flag(s) detected in resource data")

    if blast and blast.findings:
        has_stateful_destructive = any(
            f.get("is_stateful") and f.get("action") in ("delete", "replace")
            for f in blast.findings
        )
        if has_stateful_destructive:
            reasons.append("stateful resource is being destroyed or replaced")

    if overall_risk and overall_risk != RiskLevel.LOW:
        reasons.append(f"overall risk level is {overall_risk.value.upper()}")

    agents = [
        state.get("secops_finding"),
        state.get("finops_finding"),
        state.get("blast_radius_finding"),
        state.get("integrity_finding"),
    ]
    low_confidence_agents = [
        a.agent_name for a in agents
        if a is not None and a.confidence < 0.8
    ]
    if low_confidence_agents:
        reasons.append(f"low analysis confidence in: {low_confidence_agents}")

    requires_human = bool(reasons)

    if requires_human:
        print("\n🔴 Risk Gate: HUMAN APPROVAL RECOMMENDED")
        for r in reasons:
            print(f"   - {r}")
    else:
        print("\n🟢 Risk Gate: SAFE TO AUTO-APPROVE (advisory)")

    print("\n   NOTE: Final approve/reject decision is made by the admin in Jenkins.")

    return {
        "requires_human_approval": requires_human,
        "auto_approved": not requires_human,
        "messages": [{"role": "risk_gate", "content": f"Requires human: {requires_human}. Reasons: {reasons}"}],
    }


# ---------------------------------------------------------------------------
# Supervisor node
# ---------------------------------------------------------------------------

def supervisor_node(state: PipelineState) -> dict:
    """
    Lightweight supervisor — logs the plan summary before fan-out to agents.
    """
    changes = state.get("resource_changes", [])
    summary = state.get("plan_summary", {})

    print(f"\n🎯  Supervisor: Routing to 4 parallel agents.")
    print(f"    Plan: {summary.get('create', 0)} create, {summary.get('update', 0)} update, "
          f"{summary.get('replace', 0)} replace, {summary.get('delete', 0)} delete")

    if not changes:
        print("    ℹ️  No changes in this plan — agents will find nothing to report.\n")
    else:
        print()

    return {
        "messages": [{"role": "supervisor", "content": f"Routing to agents. Changes: {len(changes)}"}],
    }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph() -> any:
    """
    Builds and compiles the LangGraph StateGraph.
    No checkpointer — the graph runs to completion in a single Jenkins step.
    No human_approval node — approval is entirely Jenkins' responsibility.
    """
    domain_llm = get_bedrock_llm(MODEL_ID)
    synth_llm = get_bedrock_llm(SYNTHESIZER_MODEL_ID)

    secops_fn = functools.partial(secops_agent_node, llm=domain_llm)
    finops_fn = functools.partial(finops_agent_node, llm=domain_llm)
    blast_fn = functools.partial(blast_radius_agent_node, llm=domain_llm)
    integrity_fn = functools.partial(integrity_agent_node, llm=domain_llm)
    synthesizer_fn = functools.partial(synthesizer_node, llm=synth_llm)

    graph = StateGraph(PipelineState)

    # Register nodes
    graph.add_node("ingestion", ingestion_node)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("secops_agent", secops_fn)
    graph.add_node("finops_agent", finops_fn)
    graph.add_node("blast_radius_agent", blast_fn)
    graph.add_node("integrity_agent", integrity_fn)
    graph.add_node("synthesizer", synthesizer_fn)
    graph.add_node("risk_gate", risk_gate_node)

    # Linear entry
    graph.set_entry_point("ingestion")
    graph.add_edge("ingestion", "supervisor")

    # Fan-out: all four agents run in parallel
    graph.add_edge("supervisor", "secops_agent")
    graph.add_edge("supervisor", "finops_agent")
    graph.add_edge("supervisor", "blast_radius_agent")
    graph.add_edge("supervisor", "integrity_agent")

    # Fan-in: synthesizer waits for all four
    graph.add_edge("secops_agent", "synthesizer")
    graph.add_edge("finops_agent", "synthesizer")
    graph.add_edge("blast_radius_agent", "synthesizer")
    graph.add_edge("integrity_agent", "synthesizer")

    # Risk gate sets the flag, then we're done
    graph.add_edge("synthesizer", "risk_gate")
    graph.add_edge("risk_gate", END)

    # No checkpointer needed — single-run, no resume
    return graph.compile()
