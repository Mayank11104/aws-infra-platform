"""
nodes/secops_agent.py — The SecOps (Security Operations) Agent Node.

Responsibilities:
  - Network exposure analysis (Security Groups, NACLs, route tables)
  - IAM policy risk analysis (wildcard permissions, overly broad roles)
  - Encryption and public access flags
  - Runs checkov and tfsec tools, then has the LLM interpret and prioritize findings

This node writes ONLY to the `secops_finding` state key — never to any other key.
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from ..core.ingestion import tier_resource_changes
from ..core.state import AgentFinding, PipelineState, RiskLevel
from ..core.tools import run_checkov_scan, run_tfsec_scan
from ..graph_context_builder import build_resource_history_context
from ..prompts import SECOPS_SYSTEM_PROMPT, build_agent_context


def secops_agent_node(state: PipelineState, llm) -> dict:
    """
    SecOps Agent — analyzes security posture of the planned infrastructure changes.
    Runs checkov and tfsec, then sends the combined context to the LLM for interpretation.
    """
    print("🛡️  SecOps Agent: Starting security analysis...")

    resource_changes = state["resource_changes"]
    environment = state["environment"]
    tfplan_path = state["tfplan_path"]
    tf_source_dir = state.get("tf_source_dir", "")

    # Tier resources by sensitivity for context assembly
    tiers = tier_resource_changes(resource_changes)

    # --- Run external security scanning tools ---
    tool_outputs: dict = {}

    print("  → Running checkov scan...")
    checkov_result = run_checkov_scan.invoke({"tfplan_json_path": tfplan_path})
    tool_outputs["checkov"] = checkov_result
    if "error" in checkov_result:
        print(f"  ⚠️  Checkov failed: {checkov_result.get('error')}")

    if tf_source_dir:
        print("  → Running tfsec scan...")
        tfsec_result = run_tfsec_scan.invoke({"tf_directory": tf_source_dir})
        tool_outputs["tfsec"] = tfsec_result
        if "error" in tfsec_result:
            print(f"  ⚠️  tfsec failed: {tfsec_result.get('error')}")

    # --- Build structured context with source labels ---
    context = build_agent_context(
        critical_changes=tiers["critical"],
        high_changes=tiers["high"],
        normal_changes=tiers["normal"],
        tool_outputs=tool_outputs,
    )

    # Inject graph history for each resource in context
    enhanced_context = ""
    for rc in resource_changes:
        graph_history = build_resource_history_context(rc)
        enhanced_context += (
            f"Resource: {rc.address}\n"
            f"Type: {rc.resource_type}\n"
            f"Action: {rc.action}\n"
            f"{graph_history}"
            f"Changed Attributes: {rc.changed_attributes}\n"
            f"Before state:\n```json\n{json.dumps(rc.before, indent=2)}\n```\n"
            f"After state:\n```json\n{json.dumps(rc.after, indent=2)}\n```\n"
        )

    # --- Format system prompt with environment ---
    system_prompt = SECOPS_SYSTEM_PROMPT.replace(
        "{environment}", environment.upper()
    )

    # --- Call the LLM ---
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=(
            f"Review the following infrastructure changes for security risks.\n\n"
            f"{context}\n\n"
            f"Resource Graph Context:\n{enhanced_context}\n\n"
            f"Return ONLY a valid JSON array of finding objects. No prose, no markdown fences."
        )),
    ]

    response = llm.invoke(messages)
    raw_content = response.content.strip()

    # --- Parse structured output ---
    import re
    try:
        # Extract JSON array even if the LLM wraps it in markdown or adds prose
        json_match = re.search(r'\[.*\]', raw_content, re.DOTALL)
        if json_match:
            raw_content = json_match.group(0)
            
        findings_list = json.loads(raw_content)
    except (json.JSONDecodeError, IndexError):
        findings_list = [{
            "resource_address": "parse_error",
            "severity": "medium",
            "category": "analysis_error",
            "finding": "SecOps agent returned unparseable output. Manual review required.",
            "evidence": raw_content[:500],
            "caught_by_scanner": False,
        }]

    # --- Determine risk level from findings ---
    severity_map = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    max_severity = max(
        (severity_map.get(f.get("severity", "low"), 1) for f in findings_list),
        default=1,
    )
    risk_level = {4: RiskLevel.CRITICAL, 3: RiskLevel.HIGH, 2: RiskLevel.MEDIUM, 1: RiskLevel.LOW}[max_severity]

    # Degrade confidence if any tool failed
    tool_errors = sum(1 for v in tool_outputs.values() if isinstance(v, dict) and "error" in v)
    confidence = max(0.4, 1.0 - (tool_errors * 0.3))

    finding = AgentFinding(
        agent_name="SecOps",
        risk_level=risk_level,
        summary=f"Security analysis complete. {len(findings_list)} finding(s) identified.",
        findings=findings_list,
        tool_calls_made=list(tool_outputs.keys()),
        raw_tool_output=tool_outputs,
        confidence=confidence,
    )

    print(f"  ✅ SecOps Agent complete. Risk: {risk_level.value.upper()}, Confidence: {confidence:.0%}")

    return {
        "secops_finding": finding,
        "messages": [{"role": "secops", "content": f"Risk: {risk_level.value}"}],
    }
