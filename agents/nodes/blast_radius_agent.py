"""
nodes/blast_radius_agent.py — The Blast Radius Agent Node.

This agent has the highest architectural priority in the graph.
It detects destructive and disruptive changes that could cause:
  - Data loss (delete/replace on stateful resources without backup evidence)
  - Service downtime (replace without create_before_destroy)
  - Identity disruption (ARN/DNS changes from replace operations)

It is the ONLY agent whose output can trigger a hard-stop routing decision
in the risk_gate node, regardless of what other agents concluded.

This node writes ONLY to the `blast_radius_finding` state key.
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from ..core.ingestion import STATEFUL_RESOURCE_TYPES, tier_resource_changes
from ..core.state import AgentFinding, PipelineState, ResourceChange, RiskLevel
from ..prompts import BLAST_RADIUS_SYSTEM_PROMPT
from ..graph_context_builder import build_resource_history_context

# Normalize any LLM vocabulary drift back to Terraform's canonical vocabulary.
# The routing function in graph.py depends on "delete" — never "destroy" — so
# this alias map is the single enforcement point regardless of what the LLM emits.
_ACTION_ALIASES: dict[str, str] = {"destroy": "delete"}


def _pre_classify_destructive(
    resource_changes: list[ResourceChange],
) -> list[ResourceChange]:
    """
    Pre-filter changes that are destructive or potentially disruptive.
    This deterministic Python check runs BEFORE the LLM to ensure we
    never miss a destroy/replace due to LLM reasoning failure.
    """
    return [
        rc for rc in resource_changes
        if rc.action in ("delete", "replace")
        or (rc.action == "update" and rc.resource_type in STATEFUL_RESOURCE_TYPES)
    ]


def blast_radius_agent_node(state: PipelineState, llm) -> dict:
    """
    Blast Radius Agent — identifies destructive and disruptive changes.
    Pre-classifies in Python first (deterministic), then uses LLM to
    assess severity and provide nuanced context for the approver.
    """
    print("💥  Blast Radius Agent: Scanning for destructive changes...")

    resource_changes = state["resource_changes"]
    environment = state["environment"]

    # --- Deterministic pre-classification (does not rely on LLM) ---
    destructive_changes = _pre_classify_destructive(resource_changes)

    if not destructive_changes:
        print("  ✅ No destructive or disruptive changes detected.")
        finding = AgentFinding(
            agent_name="BlastRadius",
            risk_level=RiskLevel.LOW,
            summary="No destructive or disruptive changes detected. No resources are being destroyed or replaced.",
            findings=[],
            confidence=1.0,
        )
        return {
            "blast_radius_finding": finding,
            "messages": [{"role": "blast_radius", "content": "No destructive changes"}],
        }

    print(f"  ⚠️  Found {len(destructive_changes)} potentially destructive change(s). Analyzing...")

    # --- Build context: only destructive changes, full detail ---
    import json as _json
    context_data = []
    for rc in destructive_changes:
        graph_history = build_resource_history_context(rc)
        context_data.append({
            "address": rc.address,
            "resource_type": rc.resource_type,
            "action": rc.action,
            "is_stateful": rc.resource_type in STATEFUL_RESOURCE_TYPES,
            "module": rc.module,
            "changed_attributes": rc.changed_attributes,
            "graph_history": graph_history,
            "before_snapshot": {
                k: v for k, v in (rc.before or {}).items()
                if k in ("lifecycle", "prevent_destroy", "create_before_destroy",
                          "skip_final_snapshot", "deletion_protection", "id", "arn")
            },
            "after_snapshot": {
                k: v for k, v in (rc.after or {}).items()
                if k in ("lifecycle", "prevent_destroy", "create_before_destroy",
                          "skip_final_snapshot", "deletion_protection")
            } if rc.after else None,
            "tags_after": rc.tags_after,
        })

    context = (
        f"## Environment: {environment.upper()}\n\n"
        f"## Potentially Destructive/Disruptive Changes\n"
        f"{_json.dumps(context_data, indent=2, default=str)[:6000]}"
    )

    system_prompt = BLAST_RADIUS_SYSTEM_PROMPT.replace(
        "{environment}", environment.upper()
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=(
            f"Analyze the following destructive and potentially disruptive changes for data-loss "
            f"and downtime risk.\n\n{context}\n\n"
            f"Return ONLY a valid JSON array of finding objects. No prose, no markdown fences."
        )),
    ]

    response = llm.invoke(messages)
    raw_content = response.content.strip()

    try:
        if raw_content.startswith("```"):
            raw_content = raw_content.split("```")[1]
            if raw_content.startswith("json"):
                raw_content = raw_content[4:]
        findings_list = json.loads(raw_content)
        # Normalize LLM vocabulary drift ("destroy" → "delete") on every finding.
        # This is the single enforcement point — routing logic must not trust LLM compliance.
        for f in findings_list:
            if "action" in f:
                f["action"] = _ACTION_ALIASES.get(f["action"], f["action"])
    except (json.JSONDecodeError, IndexError):
        # Fallback: if LLM fails, generate findings deterministically from
        # our pre-classified list — we CANNOT silently pass destructive changes.
        findings_list = [
            {
                "resource_address": rc.address,
                "action": rc.action,
                "is_stateful": rc.resource_type in STATEFUL_RESOURCE_TYPES,
                "data_loss_risk": rc.action == "delete" and rc.resource_type in STATEFUL_RESOURCE_TYPES,
                "downtime_risk": rc.action == "replace",
                "create_before_destroy": None,
                "prevent_destroy": None,
                "severity": "high",
                "finding": f"LLM analysis failed. Deterministic check: {rc.action} on {rc.resource_type} requires manual review.",
                "evidence": f"action={rc.action}, stateful={rc.resource_type in STATEFUL_RESOURCE_TYPES}",
            }
            for rc in destructive_changes
        ]

    # --- Determine overall risk level ---
    severity_map = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    max_severity = max(
        (severity_map.get(f.get("severity", "low"), 1) for f in findings_list),
        default=1,
    )
    risk_level = {4: RiskLevel.CRITICAL, 3: RiskLevel.HIGH, 2: RiskLevel.MEDIUM, 1: RiskLevel.LOW}[max_severity]

    # Hard escalation: any data_loss_risk on stateful resources in production = CRITICAL
    has_data_loss = any(f.get("data_loss_risk") and f.get("is_stateful") for f in findings_list)
    if has_data_loss and environment == "production":
        risk_level = RiskLevel.CRITICAL

    finding = AgentFinding(
        agent_name="BlastRadius",
        risk_level=risk_level,
        summary=(
            f"{len(findings_list)} destructive/disruptive change(s) detected. "
            f"{'⚠️ DATA LOSS RISK on stateful resource(s).' if has_data_loss else ''}"
        ),
        findings=findings_list,
        confidence=0.95,  # High confidence — backed by deterministic pre-check
    )

    print(f"  ✅ Blast Radius Agent complete. Risk: {risk_level.value.upper()}")

    return {
        "blast_radius_finding": finding,
        "messages": [{"role": "blast_radius", "content": f"Risk: {risk_level.value}, findings: {len(findings_list)}"}],
    }
