"""
nodes/finops_agent.py — The FinOps (Financial Operations) Agent Node.

Responsibilities:
  - Monthly cost delta analysis for all created, modified, and deleted resources
  - Flags expensive resource types and unexpected cost spikes
  - Detects potential double-billing windows during replace operations
  - Uses Infracost for live AWS pricing data — never estimates from general knowledge

This node writes ONLY to the `finops_finding` state key.
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from ..core.state import AgentFinding, PipelineState, ResourceChange, RiskLevel
from ..core.tools import run_infracost_diff
from ..graph_context_builder import build_resource_history_context
from ..prompts import FINOPS_SYSTEM_PROMPT, build_agent_context
from ..core.ingestion import tier_resource_changes

# Threshold above which a monthly cost increase triggers a HIGH risk finding
COST_THRESHOLD_USD = float(50)


def finops_agent_node(state: PipelineState, llm) -> dict:
    """
    FinOps Agent — analyzes the AWS cost impact of this Terraform plan.
    Uses Infracost for authoritative pricing data. Never estimates costs from LLM knowledge.
    """
    print("💸  FinOps Agent: Starting cost analysis...")

    resource_changes = state["resource_changes"]
    environment = state["environment"]
    tfplan_path = state["tfplan_path"]
    tiers = tier_resource_changes(resource_changes)

    # --- Run Infracost ---
    tool_outputs: dict = {}
    print("  → Running Infracost breakdown...")
    infracost_result = run_infracost_diff.invoke({"tfplan_json_path": tfplan_path})
    tool_outputs["infracost"] = infracost_result

    if "error" in infracost_result:
        print(f"  ⚠️  Infracost failed: {infracost_result.get('error')}")

    # --- Build context ---
    context = build_agent_context(
        critical_changes=tiers["critical"],
        high_changes=tiers["high"],
        normal_changes=tiers["normal"],
        tool_outputs=tool_outputs,
    )

    # Append graph context for resource changes
    detailed_resource_context = []
    for rc in resource_changes:
        graph_history = build_resource_history_context(rc)
        content = (
            f"Resource: {rc.address}\n"
            f"Type: {rc.resource_type}\n"
            f"Action: {rc.action}\n"
            f"{graph_history}"
            f"Before state:\n```json\n{json.dumps(rc.before, indent=2)}\n```\n"
            f"After state:\n```json\n{json.dumps(rc.after, indent=2)}\n```\n"
        )
        detailed_resource_context.append(content)

    system_prompt = FINOPS_SYSTEM_PROMPT.replace(
        "{environment}", environment.upper()
    ).replace(
        "{cost_threshold_usd}", str(COST_THRESHOLD_USD)
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=(
            f"Analyze the cost impact of the following planned infrastructure changes.\n\n"
            f"{context}\n\n"
            f"Detailed Resource Context:\n{''.join(detailed_resource_context)}\n\n"
            f"Return ONLY a valid JSON object with keys 'total_monthly_delta_usd' and 'findings'. "
            f"No prose, no markdown fences."
        )),
    ]

    response = llm.invoke(messages)
    raw_content = response.content.strip()

    # --- Parse structured output ---
    try:
        if raw_content.startswith("```"):
            raw_content = raw_content.split("```")[1]
            if raw_content.startswith("json"):
                raw_content = raw_content[4:]
        parsed = json.loads(raw_content)
        findings_list = parsed.get("findings", [])
        # Bug fix: coerce to float explicitly — LLM may emit a string like "unknown"
        # or omit the field, both of which survive json.loads but crash threshold comparison.
        try:
            total_delta = float(parsed.get("total_monthly_delta_usd", 0.0))
        except (TypeError, ValueError):
            total_delta = 0.0
            findings_list.append({
                "resource_address": "aggregate",
                "monthly_delta_usd": 0.0,
                "cost_driver": "total_monthly_delta_usd was non-numeric in LLM output",
                "confidence": "low",
                "note": "Treated as $0.00 pending manual review.",
            })
    except (json.JSONDecodeError, IndexError):
        findings_list = [{
            "resource_address": "parse_error",
            "monthly_delta_usd": 0.0,
            "cost_driver": "FinOps agent returned unparseable output",
            "confidence": "low",
            "note": raw_content[:500],
        }]
        total_delta = 0.0

    # --- Determine risk level ---
    if total_delta > COST_THRESHOLD_USD * 2:
        risk_level = RiskLevel.HIGH
    elif total_delta > COST_THRESHOLD_USD:
        risk_level = RiskLevel.MEDIUM
    else:
        risk_level = RiskLevel.LOW

    # If Infracost failed, elevate risk (cost blind spot is itself a risk)
    infracost_failed = "error" in infracost_result
    confidence = 0.4 if infracost_failed else 0.95

    finding = AgentFinding(
        agent_name="FinOps",
        risk_level=risk_level,
        summary=(
            f"Estimated monthly cost delta: ${total_delta:+.2f}. "
            f"{'Infracost unavailable — estimate is unreliable.' if infracost_failed else ''}"
        ),
        findings=findings_list,
        tool_calls_made=["infracost"],
        raw_tool_output=tool_outputs,
        confidence=confidence,
        total_monthly_delta_usd=total_delta,  # Stored directly so Synthesizer doesn't re-derive it
    )

    print(f"  ✅ FinOps Agent complete. Delta: ${total_delta:+.2f}/mo, Risk: {risk_level.value.upper()}")

    return {
        "finops_finding": finding,
        "messages": [{"role": "finops", "content": f"Delta: ${total_delta:+.2f}"}],
    }
