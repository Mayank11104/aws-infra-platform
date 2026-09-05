"""
state.py — The shared PipelineState schema for the LangGraph AI Risk Gate.

This is the single most important artifact in the whole system.
All agents read from and write to this state object.
The schema is designed with parallel-safe keys so agents can run concurrently
without race conditions.
"""

from __future__ import annotations

import json
from enum import Enum
from operator import add
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Pydantic models — validated sub-structures nested inside PipelineState
# ---------------------------------------------------------------------------

class ResourceChange(BaseModel):
    """
    A single normalized resource change extracted from tfplan JSON.
    Computed at Ingestion time — agents never see the raw tfplan JSON directly.
    """
    address: str                          # e.g. "aws_security_group.web[0]"
    resource_type: str                    # e.g. "aws_security_group"
    action: Literal[                      # normalized from actions[] array
        "create", "update", "delete", "replace", "no-op", "read"
    ]
    module: str | None = None             # e.g. "module.vpc"
    provider: str = "aws"

    # Field-level diff — precomputed at Ingestion, not inside LLM prompts.
    # This keeps context sizes small and prevents agents from re-deriving diffs.
    before: dict | None = None
    after: dict | None = None
    changed_attributes: list[str] = Field(default_factory=list)

    # Tag snapshots — extracted separately for quick cross-referencing
    # by the Integrity agent without it having to parse full before/after.
    tags_before: dict = Field(default_factory=dict)
    tags_after: dict = Field(default_factory=dict)

    # Risk pre-classification performed by deterministic Python code
    # (not by the LLM) during Ingestion, using HIGH_SENSITIVITY_TYPES logic.
    risk_tier: Literal["critical", "high", "normal"] = "normal"

    # ── Knowledge Graph context (Phase 3) ─────────────────────────────────────
    # Populated by ingestion.py after querying Neo4j for this resource's history.
    # Empty list means either: first time this resource appears, or graph unavailable.
    resource_history: list[dict] = Field(default_factory=list)

    # Recurring issues found in > 1 previous run for this resource (HIGH/CRITICAL only)
    recurring_findings: list[dict] = Field(default_factory=list)

    # Resources that depend on this one (blast radius context from graph)
    downstream_resources: list[dict] = Field(default_factory=list)


class AgentFinding(BaseModel):
    """
    Normalized output shape every domain agent MUST return.
    The Synthesizer aggregates these four findings into the final brief.
    """
    agent_name: str
    risk_level: RiskLevel
    summary: str                          # One-paragraph human-readable summary
    findings: list[dict]                  # Structured, not free text — see prompts.py

    # Audit trail — store raw tool output so a human can verify
    # the LLM's interpretation six months from now.
    tool_calls_made: list[str] = Field(default_factory=list)
    raw_tool_output: dict = Field(default_factory=dict)

    # Confidence: < 0.8 means agent was uncertain (tool failure, ambiguous data).
    # The risk_gate routing node checks this — low confidence blocks auto-approve.
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)

    # FinOps-specific: aggregate monthly cost delta. Stored here so the Synthesizer
    # doesn't have to unreliably re-derive it by scanning the findings list.
    total_monthly_delta_usd: float | None = None


# ---------------------------------------------------------------------------
# The main shared state — the single graph-wide data structure
# ---------------------------------------------------------------------------

class PipelineState(TypedDict):
    # ------------------------------------------------------------------
    # Immutable inputs — set once at ingestion, never mutated by agents
    # ------------------------------------------------------------------
    tfplan_path: str
    ansible_playbook_paths: list[str]
    tf_source_dir: str                    # Terraform .tf source for tfsec + drift detection
    git_diff_summary: str                 # PR's .tf file diff for Integrity drift check
    environment: Literal["dev", "staging", "production"]
    pipeline_run_id: str                  # Jenkins BUILD_ID
    triggered_by: str                     # Jenkins user/service account

    # ------------------------------------------------------------------
    # Parsed IaC data — written by Ingestion, read by all agents
    # ------------------------------------------------------------------
    resource_changes: list[ResourceChange]
    plan_summary: dict                    # {create: N, update: N, delete: N, replace: N}
    injection_flags: list[dict]           # Prompt injection strings found in tags/names

    # ------------------------------------------------------------------
    # Agent outputs — each agent writes ONLY to its own dedicated key.
    # This is the critical design that makes parallel execution race-condition-free.
    # NEVER have two agents write to the same key.
    # ------------------------------------------------------------------
    secops_finding: AgentFinding | None
    finops_finding: AgentFinding | None
    blast_radius_finding: AgentFinding | None
    integrity_finding: AgentFinding | None

    # ------------------------------------------------------------------
    # Append-only log — safe for concurrent writes via LangGraph's
    # `add` reducer, which concatenates rather than overwrites.
    # ------------------------------------------------------------------
    messages: Annotated[list[dict], add]

    # ------------------------------------------------------------------
    # Synthesis output — written by Synthesizer after fan-in
    # ------------------------------------------------------------------
    overall_risk: RiskLevel | None
    audit_report_markdown: str | None
    audit_report_slack_blocks: list[dict] | None

    # ------------------------------------------------------------------
    # Gate decision — written by risk_gate node
    # ------------------------------------------------------------------
    requires_human_approval: bool
    auto_approved: bool
    human_decision: Literal["approved", "rejected", "pending"] | None
    human_decision_reason: str | None

    # ------------------------------------------------------------------
    # Knowledge Graph (Phase 3) — graph-level context for the Synthesizer
    # ------------------------------------------------------------------
    graph_available: bool                  # False if Neo4j is unreachable (graceful degradation)
    environment_baseline: dict             # Stats across all historical runs for this environment
    environment_cost_trend: list[dict]     # Monthly delta trend for the last N runs
    ansible_coverage: dict                 # EC2 address → applied Ansible roles
