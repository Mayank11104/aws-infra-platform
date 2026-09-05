"""
ingestion.py — The Ingestion node for the LangGraph AI Risk Gate.

This is the first node the graph executes. It is responsible for:
  1. DATA GUARDRAIL: Structural validation of tfplan.json
  2. DATA GUARDRAIL: Version check on plan format_version
  3. DATA GUARDRAIL: Prompt injection scan across all resource tag/name fields
  4. Normalizing raw resource_changes into clean ResourceChange Pydantic objects
  5. Pre-classifying resources into risk tiers using deterministic Python logic
     (not LLM reasoning — cheap, fast, and deterministic)

The LLM agents never see the raw tfplan JSON. Everything is normalized here first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .state import PipelineState, ResourceChange, RiskLevel
from .memory.graph_client import is_available as graph_is_available
from .memory.queries import (
    get_resource_history,
    get_recurring_findings,
    get_downstream_resources,
    get_environment_baseline,
    get_environment_cost_trend,
    get_ansible_coverage,
)
from .memory.schema import bootstrap_schema

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_PLAN_FORMAT_VERSIONS = {"1.0", "1.1", "1.2"}

# Resource types where a replace or delete action is HIGH or CRITICAL risk
# because they hold stateful data or are difficult/expensive to recreate.
HIGH_SENSITIVITY_TYPES = {
    "aws_iam_policy",
    "aws_iam_role",
    "aws_iam_role_policy",
    "aws_iam_role_policy_attachment",
    "aws_security_group",
    "aws_security_group_rule",
    "aws_network_acl",
    "aws_network_acl_rule",
    "aws_route_table",
    "aws_route",
    "aws_kms_key",
    "aws_db_instance",
    "aws_rds_cluster",
    "aws_rds_cluster_instance",
    "aws_s3_bucket",
    "aws_s3_bucket_policy",
    "aws_s3_bucket_public_access_block",
    "aws_elasticache_cluster",
    "aws_lb",
    "aws_lb_listener",
}

STATEFUL_RESOURCE_TYPES = {
    "aws_db_instance",
    "aws_rds_cluster",
    "aws_rds_cluster_instance",
    "aws_s3_bucket",
    "aws_elasticache_cluster",
    "aws_ebs_volume",
    "aws_efs_file_system",
}

DESTRUCTIVE_ACTIONS = {"delete", "replace"}

# Regex patterns that indicate prompt injection attempts embedded in
# tag values, resource names, or Ansible variable strings.
INJECTION_PATTERNS = [
    r"ignore\s+(previous|above|prior|all)\s+instructions?",
    r"system\s+prompt",
    r"you\s+(are|must|should)\s+now",
    r"disregard\s+(the|your|all|previous)",
    r"new\s+instructions?:",
    r"override\s+(previous|prior|all)\s+",
    r"assistant\s*:",
    r"<\s*system\s*>",
]


# ---------------------------------------------------------------------------
# Custom exceptions — fail loudly and specifically at the data layer
# ---------------------------------------------------------------------------

class IntegrityError(Exception):
    """Raised when the tfplan JSON fails data-layer validation."""
    pass


# ---------------------------------------------------------------------------
# Core parsing and normalization logic
# ---------------------------------------------------------------------------

def compute_field_diff(
    before: dict | None,
    after: dict | None,
) -> dict[str, dict[str, Any]]:
    """
    Returns only the fields that actually changed between before and after.
    Sending only the diff to the LLM (not the full before + full after) is
    the single biggest context-window optimization available to us.
    """
    if before is None:
        return {"_action": "create", "all_fields": after or {}}
    if after is None:
        return {"_action": "delete", "all_fields": before}

    diff: dict[str, dict[str, Any]] = {}
    all_keys = set(before.keys()) | set(after.keys())
    for key in all_keys:
        b_val = before.get(key)
        a_val = after.get(key)
        if b_val != a_val:
            diff[key] = {"before": b_val, "after": a_val}
    return diff


def _classify_risk_tier(
    action: str,
    resource_type: str,
) -> str:
    """
    Classify resources deterministically in Python before the LLM ever
    sees them. This is cheap, fast, and perfectly consistent — unlike
    letting the LLM decide what counts as "critical" from first principles.
    """
    if action in DESTRUCTIVE_ACTIONS and resource_type in HIGH_SENSITIVITY_TYPES:
        return "critical"
    elif resource_type in HIGH_SENSITIVITY_TYPES or action in DESTRUCTIVE_ACTIONS:
        return "high"
    return "normal"


def normalize_resource_change(rc: dict) -> ResourceChange:
    """
    Convert a raw resource_changes entry from tfplan JSON into a clean,
    validated ResourceChange object. Also normalizes Terraform's multi-value
    actions array into a single semantic action string.
    """
    change = rc["change"]
    actions: list[str] = change["actions"]

    # Terraform represents "replace" as ["delete", "create"] in JSON.
    # Normalize this into a single semantic concept so agents don't
    # have to reverse-engineer it from the raw array.
    if set(actions) == {"delete", "create"}:
        action = "replace"
    elif actions == ["create"]:
        action = "create"
    elif actions == ["update"]:
        action = "update"
    elif actions == ["delete"]:
        action = "delete"
    elif actions == ["read"]:
        action = "read"
    else:
        action = "no-op"

    before = change.get("before") or {}
    after = change.get("after") or {}

    diff = compute_field_diff(before, after)
    changed_attrs = [k for k in diff if not k.startswith("_")]

    resource_type = rc.get("type", "")

    return ResourceChange(
        address=rc["address"],
        resource_type=resource_type,
        action=action,
        module=rc.get("module_address"),
        before=before,
        after=after,
        changed_attributes=changed_attrs,
        tags_before=before.get("tags", {}),
        tags_after=after.get("tags", {}),
        risk_tier=_classify_risk_tier(action, resource_type),
    )


def tier_resource_changes(
    changes: list[ResourceChange],
) -> dict[str, list[ResourceChange]]:
    """Group normalized changes by their pre-classified risk tier."""
    tiers: dict[str, list[ResourceChange]] = {
        "critical": [],
        "high": [],
        "normal": [],
    }
    for rc in changes:
        tiers[rc.risk_tier].append(rc)
    return tiers


# ---------------------------------------------------------------------------
# Prompt injection scanner — Data Layer guardrail
# ---------------------------------------------------------------------------

def scan_for_injection_attempts(
    resource_changes: list[ResourceChange],
) -> list[dict]:
    """
    Scans resource tags and name fields for prompt injection patterns
    BEFORE any LLM sees the data. This is the data-layer guardrail that
    works in concert with the prompt-layer instruction in agent system prompts.
    A finding here routes the graph to mandatory human approval regardless
    of what agents conclude.
    """
    flags: list[dict] = []
    compiled = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

    for rc in resource_changes:
        # Check tags, name field, description field, and full after blob
        check_fields = {
            "tags_after": json.dumps(rc.tags_after),
            "name": str(rc.after.get("name", "") if rc.after else ""),
            "description": str(rc.after.get("description", "") if rc.after else ""),
        }

        for field_name, text in check_fields.items():
            if not text:
                continue
            for pattern in compiled:
                match = pattern.search(text)
                if match:
                    flags.append({
                        "resource_address": rc.address,
                        "field": field_name,
                        "pattern_matched": pattern.pattern,
                        "matched_text": match.group(0),
                        "excerpt": text[:300],
                        "severity": "critical",
                        "finding": (
                            f"Possible prompt injection attempt detected in "
                            f"'{field_name}' of {rc.address}. "
                            f"This run MUST require human approval."
                        ),
                    })
    return flags


# ---------------------------------------------------------------------------
# Plan summary helper
# ---------------------------------------------------------------------------

def build_plan_summary(changes: list[ResourceChange]) -> dict:
    summary = {"create": 0, "update": 0, "delete": 0, "replace": 0, "no-op": 0}
    for rc in changes:
        if rc.action in summary:
            summary[rc.action] += 1
    return summary


# ---------------------------------------------------------------------------
# The LangGraph Ingestion Node
# ---------------------------------------------------------------------------

def ingestion_node(state: PipelineState) -> dict:
    """
    Entry node — validates, normalizes, and pre-classifies the Terraform plan.
    Raises IntegrityError on any data guardrail failure, which LangGraph will
    surface as a pipeline failure with a clear, specific error message.
    """
    tfplan_path = state["tfplan_path"]

    # --- Guardrail 1: File must exist and be readable ---
    path = Path(tfplan_path)
    if not path.exists():
        raise IntegrityError(f"tfplan.json not found at: {tfplan_path}")
    if path.suffix != ".json":
        raise IntegrityError(
            f"Expected a .json file (from `terraform show -json`), got: {path.name}"
        )

    # --- Guardrail 2: Structural validation — must have required top-level keys ---
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise IntegrityError(f"tfplan.json is not valid JSON: {e}") from e

    required_keys = {"format_version", "terraform_version", "resource_changes"}
    missing = required_keys - raw.keys()
    if missing:
        raise IntegrityError(
            f"tfplan.json is missing required top-level keys: {missing}. "
            f"Ensure this file was produced by `terraform show -json tfplan`, "
            f"not created or edited manually."
        )

    # --- Guardrail 3: Format version check ---
    fmt_version = raw["format_version"]
    if fmt_version not in SUPPORTED_PLAN_FORMAT_VERSIONS:
        raise IntegrityError(
            f"Unsupported plan format_version '{fmt_version}'. "
            f"Supported versions: {SUPPORTED_PLAN_FORMAT_VERSIONS}. "
            f"Update the risk gate to handle the new format before proceeding."
        )

    # --- Normalize resource_changes, filter true no-ops immediately ---
    raw_changes = raw.get("resource_changes", [])
    meaningful = [
        rc for rc in raw_changes
        if rc["change"]["actions"] != ["no-op"]
    ]
    normalized: list[ResourceChange] = [
        normalize_resource_change(rc) for rc in meaningful
    ]

    # --- Data Layer: Prompt injection scan ---
    injection_flags = scan_for_injection_attempts(normalized)
    if injection_flags:
        # Log it clearly — this is immediately visible in Jenkins console output
        print(
            f"\n⚠️  WARNING: {len(injection_flags)} potential prompt injection "
            f"attempt(s) detected in plan data. "
            f"This deployment will require mandatory human approval.\n"
        )

    plan_summary = build_plan_summary(normalized)

    # ── Phase 3: Knowledge Graph enrichment ───────────────────────────────────
    # Check Neo4j availability. If unavailable, degrade gracefully — agents
    # will still run but without historical context.
    graph_available = graph_is_available()
    environment_baseline: dict = {}
    environment_cost_trend: list[dict] = []
    ansible_coverage: dict = {}

    if graph_available:
        print("\n🔗  Knowledge Graph: Enriching resource changes with historical context...")
        try:
            bootstrap_schema()  # Idempotent — creates constraints if not yet created

            # Environment-level context (used by Synthesizer)
            environment_baseline = get_environment_baseline(state["environment"])
            environment_cost_trend = get_environment_cost_trend(state["environment"])
            ansible_coverage = get_ansible_coverage(state["environment"])

            # Per-resource context (attached to each ResourceChange)
            enriched: list[ResourceChange] = []
            for rc in normalized:
                history = get_resource_history(rc.address, state["environment"])
                recurring = get_recurring_findings(rc.address, state["environment"])
                downstream = get_downstream_resources(rc.address, state["environment"])

                # Return a new ResourceChange with graph context populated
                enriched.append(rc.model_copy(update={
                    "resource_history": history,
                    "recurring_findings": recurring,
                    "downstream_resources": downstream,
                }))
            normalized = enriched

            history_count = sum(1 for rc in normalized if rc.resource_history)
            print(f"   ✅ Graph enrichment complete. {history_count}/{len(normalized)} "
                  f"resources have historical context.")

        except Exception as e:
            # Graph query failed mid-run — log and continue without history.
            # This is non-fatal: a graph DB failure should NOT block a deployment.
            print(f"   ⚠️  Graph enrichment failed: {e}. Running without historical context.")
            graph_available = False
    else:
        print("\n⚠️  Knowledge Graph: Neo4j is not reachable. Running without historical context.")
        print("   Start Neo4j with: docker start neo4j-memory")

    print(
        f"\n✅ Ingestion complete: {len(normalized)} meaningful changes "
        f"({plan_summary['create']} create, {plan_summary['update']} update, "
        f"{plan_summary['replace']} replace, {plan_summary['delete']} delete)"
    )

    return {
        "resource_changes": normalized,
        "plan_summary": plan_summary,
        "injection_flags": injection_flags,
        "graph_available": graph_available,
        "environment_baseline": environment_baseline,
        "environment_cost_trend": environment_cost_trend,
        "ansible_coverage": ansible_coverage,
        "messages": [{
            "role": "system",
            "content": f"Ingestion complete. Plan summary: {plan_summary}. Graph: {graph_available}",
        }],
    }
