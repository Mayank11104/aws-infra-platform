"""
nodes/integrity_agent.py — The Integrity Agent Node.

Two responsibilities in one agent (both are "reconcile against a second source of truth"):

  1. TAG ALIGNMENT — Cross-references Terraform's planned EC2 tags against
     the Ansible dynamic inventory group names. Flags any EC2 instance that will
     be provisioned but NOT targeted by any Ansible playbook due to missing/wrong tags.

  2. DRIFT DETECTION — Cross-references the Terraform plan against the git diff
     of .tf source files. If a resource shows a change in the plan but its source
     config is UNCHANGED in this PR, someone modified it directly in AWS (bypassing
     Terraform). Terraform is about to silently overwrite that manual change.

This node writes ONLY to the `integrity_finding` state key.
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from ..core.state import AgentFinding, PipelineState, ResourceChange, RiskLevel
from ..prompts import INTEGRITY_SYSTEM_PROMPT, build_agent_context
from ..graph_context_builder import build_resource_history_context

# Tags that Ansible's dynamic inventory requires on EC2 instances
REQUIRED_ANSIBLE_TAGS = {"Role", "Environment"}

# Pattern that matches Ansible's dynamic group name from a tag
# e.g., tag Role=web → group tag_Role_web
def _expected_ansible_group(tag_key: str, tag_value: str) -> str:
    return f"tag_{tag_key}_{tag_value}"


def _deterministic_tag_check(
    resource_changes: list[ResourceChange],
    environment: str,
) -> list[dict]:
    """
    Python-level tag alignment check — runs before LLM for deterministic coverage.
    Any EC2 instance missing required Ansible tags is flagged here unconditionally.
    """
    findings = []
    for rc in resource_changes:
        if rc.resource_type != "aws_instance":
            continue
        if rc.action not in ("create", "update", "replace"):
            continue

        tags = rc.tags_after
        missing = [tag for tag in REQUIRED_ANSIBLE_TAGS if tag not in tags]

        if missing:
            findings.append({
                "resource_address": rc.address,
                "missing_tags": missing,
                "consequence": (
                    f"Ansible will NOT configure this instance. "
                    f"Playbooks targeting `tag_Role_*` or `tag_Environment_*` will skip it."
                ),
                "severity": "high",
                "present_tags": tags,
            })
        else:
            # Check that Environment tag matches the expected deployment environment
            env_tag = tags.get("Environment", "")
            if env_tag.lower() != environment.lower():
                findings.append({
                    "resource_address": rc.address,
                    "missing_tags": [],
                    "consequence": (
                        f"Environment tag mismatch: tag says '{env_tag}' but pipeline is "
                        f"deploying to '{environment}'. This instance may be targeted by "
                        f"the wrong environment's Ansible runs."
                    ),
                    "severity": "medium",
                    "present_tags": tags,
                })
    return findings


def _deterministic_drift_check(
    resource_changes: list[ResourceChange],
    git_diff_summary: str,
) -> list[dict]:
    """
    Detects state drift by comparing plan changes against the PR's git diff.
    A resource that shows a change in the plan but no change in the .tf source
    was modified directly in AWS (bypassing Terraform).

    Known limitation: this regex matches resource block declarations in the diff,
    but matches only on local name (not full module path). Two different modules
    with same-named resources will share the extracted name, so a change to
    module.A.aws_instance.web can mask drift on module.B.aws_instance.web if both
    appear in the same PR diff. Where is_in_module=True, findings carry a
    confidence_note so the approver is aware of this limitation rather than silently
    receiving wrong output.
    """
    if not git_diff_summary:
        return []

    # Extract resource local names that appear in the diff
    changed_in_source = set(re.findall(r'resource\s+"[\w]+"\s+"([\w-]+)"', git_diff_summary))

    findings = []
    for rc in resource_changes:
        if rc.action not in ("update", "replace"):
            continue

        local_name = rc.address.split(".")[-1].split("[")[0]
        is_in_module = rc.address.startswith("module.")

        # Combine standard context with Phase 3 Graph History
        graph_history = build_resource_history_context(rc)

        if local_name not in changed_in_source and rc.address not in changed_in_source:
            findings.append({
                "resource_address": rc.address,
                "resource_type": rc.resource_type,
                "changed_attributes": rc.changed_attributes,
                "severity": "medium",
                "graph_history": graph_history,
                # Explicitly surface the false-negative risk for module resources
                # rather than silently producing potentially wrong output.
                "confidence_note": (
                    "matched by local resource name only — module-scoped false "
                    "negatives are possible if another module in this PR shares "
                    "the same local resource name"
                ) if is_in_module else None,
                "finding": (
                    f"State drift detected: '{rc.address}' shows a planned {rc.action} "
                    f"but was NOT modified in this PR's .tf source files. "
                    f"This may indicate a manual AWS console change being overwritten."
                ),
            })
    return findings


def integrity_agent_node(state: PipelineState, llm) -> dict:
    """
    Integrity Agent — checks tag alignment and state drift.
    Uses deterministic Python checks first, then enriches with LLM analysis.
    """
    print("🔗  Integrity Agent: Checking tag alignment and state drift...")

    resource_changes = state["resource_changes"]
    environment = state["environment"]
    git_diff_summary = state.get("git_diff_summary", "")

    # --- Deterministic checks (no LLM involvement) ---
    tag_findings = _deterministic_tag_check(resource_changes, environment)
    drift_findings = _deterministic_drift_check(resource_changes, git_diff_summary)

    print(f"  → Tag issues: {len(tag_findings)}, Drift issues: {len(drift_findings)}")

    # If both deterministic checks found nothing, return early without calling LLM
    if not tag_findings and not drift_findings:
        print("  ✅ No tag alignment or drift issues detected.")
        finding = AgentFinding(
            agent_name="Integrity",
            risk_level=RiskLevel.LOW,
            summary="Tag alignment is correct. No state drift detected.",
            findings=[],
            confidence=1.0,
        )
        return {
            "integrity_finding": finding,
            "messages": [{"role": "integrity", "content": "Clean"}],
        }

    # --- LLM enrichment for complex cases ---
    context = json.dumps({
        "environment": environment,
        "tag_alignment_issues": tag_findings,
        "drift_issues": drift_findings,
        "ec2_instances_in_plan": [
            {"address": rc.address, "tags_after": rc.tags_after, "action": rc.action}
            for rc in resource_changes
            if rc.resource_type == "aws_instance"
        ],
        "git_diff_available": bool(git_diff_summary),
    }, indent=2, default=str)

    system_prompt = INTEGRITY_SYSTEM_PROMPT.replace(
        "{environment}", environment.upper()
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=(
            f"Review the following tag alignment and drift findings. "
            f"Add any additional context or consequences the approver should know.\n\n"
            f"{context}\n\n"
            f"Return ONLY a valid JSON object with keys 'tag_alignment_findings' and "
            f"'drift_findings'. No prose, no markdown fences."
        )),
    ]

    response = llm.invoke(messages)
    raw_content = response.content.strip()

    try:
        # Extract JSON block even if the LLM wraps it in markdown or adds prose
        json_match = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if json_match:
            raw_content = json_match.group(0)
            
        enriched = json.loads(raw_content)
        all_findings = (
            enriched.get("tag_alignment_findings", tag_findings) +
            enriched.get("drift_findings", drift_findings)
        )
    except (json.JSONDecodeError, IndexError):
        # LLM parse failure — use deterministic findings (safe fallback)
        all_findings = tag_findings + drift_findings

    severity_map = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    max_severity = max(
        (severity_map.get(f.get("severity", "low"), 1) for f in all_findings),
        default=1,
    )
    risk_level = {4: RiskLevel.CRITICAL, 3: RiskLevel.HIGH, 2: RiskLevel.MEDIUM, 1: RiskLevel.LOW}[max_severity]

    finding = AgentFinding(
        agent_name="Integrity",
        risk_level=risk_level,
        summary=(
            f"{len(tag_findings)} tag issue(s), {len(drift_findings)} drift finding(s)."
        ),
        findings=all_findings,
        confidence=0.9,
    )

    print(f"  ✅ Integrity Agent complete. Risk: {risk_level.value.upper()}")

    return {
        "integrity_finding": finding,
        "messages": [{"role": "integrity", "content": f"Risk: {risk_level.value}"}],
    }
