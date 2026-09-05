"""
nodes/synthesizer.py — The Synthesizer Agent Node.

This is the final aggregation node. It runs AFTER all four domain agents complete (fan-in).
It receives structured findings from SecOps, FinOps, BlastRadius, and Integrity agents
and produces the final human-readable Risk Brief in Markdown.

Strict rules:
  - Only reports what the four agents returned — never invents findings
  - Explicitly notes any agent tool failures or low-confidence assessments
  - Keeps the output scannable (< 600 words) for time-pressed approvers
  - Does NOT make an approve/reject decision — only characterizes risk level
"""

from __future__ import annotations

import json
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage

from ..core.state import AgentFinding, PipelineState, RiskLevel
from ..prompts import SYNTHESIZER_SYSTEM_PROMPT

RISK_EMOJI = {
    RiskLevel.LOW: "🟢",
    RiskLevel.MEDIUM: "🟡",
    RiskLevel.HIGH: "🟠",
    RiskLevel.CRITICAL: "🔴",
}

AI_RECOMMENDATION = {
    RiskLevel.LOW: "SAFE TO DEPLOY",
    RiskLevel.MEDIUM: "DEPLOY WITH CAUTION",
    RiskLevel.HIGH: "REQUIRES CAREFUL REVIEW",
    RiskLevel.CRITICAL: "DO NOT DEPLOY",
}


def _determine_overall_risk(
    secops: AgentFinding | None,
    finops: AgentFinding | None,
    blast: AgentFinding | None,
    integrity: AgentFinding | None,
) -> RiskLevel:
    """Overall risk is the highest risk level across all four agents."""
    level_map = {
        RiskLevel.LOW: 1,
        RiskLevel.MEDIUM: 2,
        RiskLevel.HIGH: 3,
        RiskLevel.CRITICAL: 4,
    }
    reverse_map = {v: k for k, v in level_map.items()}

    agents = [a for a in [secops, finops, blast, integrity] if a is not None]
    if not agents:
        return RiskLevel.LOW

    max_level = max(level_map.get(a.risk_level, 1) for a in agents)
    return reverse_map[max_level]


def _build_findings_summary(findings: list[dict], max_items: int = 5) -> str:
    """Format findings as a compact bullet list, capped to avoid overly long output."""
    if not findings:
        return "_No findings._"
    lines = []
    for f in findings[:max_items]:
        severity = f.get("severity", "").upper()
        finding_text = f.get("finding", f.get("consequence", str(f)))
        evidence = f.get("evidence", "")
        addr = f.get("resource_address", "")
        line = f"- **[{severity}]** `{addr}`: {finding_text}"
        if evidence:
            line += f"\n  - *Evidence:* `{evidence[:150]}`"
        lines.append(line)
    if len(findings) > max_items:
        lines.append(f"- _...and {len(findings) - max_items} more findings. See full artifact._")
    return "\n".join(lines)


def synthesizer_node(state: PipelineState, llm) -> dict:
    """
    Synthesizer — aggregates all four agent findings into the final Risk Brief.
    Uses the LLM to write the narrative sections, but the structure and data
    are dictated by the agent findings (not invented by the LLM).
    """
    print("📋  Synthesizer: Building Risk Brief...")

    secops = state.get("secops_finding")
    finops = state.get("finops_finding")
    blast = state.get("blast_radius_finding")
    integrity = state.get("integrity_finding")
    plan_summary = state.get("plan_summary", {})
    environment = state["environment"]
    pipeline_run_id = state["pipeline_run_id"]
    triggered_by = state["triggered_by"]
    injection_flags = state.get("injection_flags", [])

    overall_risk = _determine_overall_risk(secops, finops, blast, integrity)

    # --- Build structured input for the LLM ---
    agent_summary = {
        "secops": {
            "risk_level": secops.risk_level.value if secops else "unknown",
            "confidence": secops.confidence if secops else 0.0,
            "findings": secops.findings[:8] if secops else [],
            "summary": secops.summary if secops else "Agent did not run",
        },
        "finops": {
            "risk_level": finops.risk_level.value if finops else "unknown",
            "confidence": finops.confidence if finops else 0.0,
            "findings": finops.findings[:8] if finops else [],
            "summary": finops.summary if finops else "Agent did not run",
            "total_delta": next(
                (f.get("monthly_delta_usd", 0) for f in (finops.findings if finops else [])), 0
            ),
        },
        "blast_radius": {
            "risk_level": blast.risk_level.value if blast else "unknown",
            "confidence": blast.confidence if blast else 0.0,
            "findings": blast.findings[:8] if blast else [],
            "summary": blast.summary if blast else "Agent did not run",
        },
        "integrity": {
            "risk_level": integrity.risk_level.value if integrity else "unknown",
            "confidence": integrity.confidence if integrity else 0.0,
            "findings": integrity.findings[:8] if integrity else [],
            "summary": integrity.summary if integrity else "Agent did not run",
        },
        "injection_flags": injection_flags,
        "plan_summary": plan_summary,
        "overall_risk": overall_risk.value,
        "environment": environment,
    }

    messages = [
        SystemMessage(content=SYNTHESIZER_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"Generate the Risk Brief for pipeline run `{pipeline_run_id}` "
            f"triggered by `{triggered_by}` targeting `{environment.upper()}`.\n\n"
            f"Agent findings:\n{json.dumps(agent_summary, indent=2, default=str)}"
        )),
    ]

    response = llm.invoke(messages)
    llm_brief = response.content.strip()

    # Strip out ```markdown code block wrappers if the LLM includes them
    if llm_brief.startswith("```"):
        llm_brief = llm_brief.removeprefix("```markdown").removeprefix("```md").removeprefix("```").removesuffix("```").strip()

    # --- Build final Markdown with a guaranteed header regardless of LLM output ---
    graph_available = state.get("graph_available", False)
    env_baseline = state.get("environment_baseline", {})
    cost_trend = state.get("environment_cost_trend", [])
    ansible_cov = state.get("ansible_coverage", {})

    graph_section = ""
    if graph_available:
        trend_str = " → ".join(f"${t['total_monthly_delta_usd']:+.2f}" for t in cost_trend)
        graph_section = (
            f"\n## 🧠 Infrastructure Knowledge Graph Context\n\n"
            f"- **Historical Cost Trend:** {trend_str or 'Insufficient data'}\n"
            f"- **Environment Baseline:** {env_baseline.get('total_runs', 0)} previous runs "
            f"(Avg: ${env_baseline.get('avg_monthly_delta_usd', 0):+.2f}/mo)\n"
            f"- **Ansible Coverage:** {len(ansible_cov)} EC2 instances actively managed\n"
        )

    header = (
        f"# AI Risk Gate Brief: `{environment.upper()}` Environment\n\n"
        f"**Run ID:** `{pipeline_run_id}` | **Triggered by:** `{triggered_by}`\n"
        f"**Overall Risk:** {RISK_EMOJI[overall_risk]} {overall_risk.value.upper()}\n"
        f"{graph_section}\n"
        f"---\n\n"
    )

    # Injection flag warning (prepended as a hard banner if present)
    injection_banner = ""
    if injection_flags:
        injection_banner = (
            f"> [!CAUTION]\n"
            f"> **⚠️ PROMPT INJECTION ATTEMPT DETECTED**\n"
            f"> {len(injection_flags)} suspicious string(s) found in resource tags/names. "
            f"This run has been automatically escalated to mandatory human approval. "
            f"Do not approve if you cannot verify the source of these tags.\n\n"
        )

    full_brief = header + injection_banner + llm_brief

    # --- Build compact Slack blocks ---
    # Pull the aggregate delta directly from the dedicated field on AgentFinding,
    # not by scanning the findings list (which would only get the first item's delta).
    if finops and finops.total_monthly_delta_usd is not None:
        finops_delta_str = f"${finops.total_monthly_delta_usd:+.2f}/mo"
        if finops.confidence < 0.8:
            finops_delta_str += " ⚠️ low confidence"
    else:
        finops_delta_str = "Unavailable"
    slack_blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Risk Gate: {environment.upper()} Deployment"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Overall Risk:*\n{RISK_EMOJI[overall_risk]} {overall_risk.value.upper()}"},
                {"type": "mrkdwn", "text": f"*Pipeline:*\n`{pipeline_run_id}`"},
                {"type": "mrkdwn", "text": f"*Cost Impact:*\n{finops_delta_str}"},
                {"type": "mrkdwn", "text": f"*Blast Radius:*\n{blast.summary if blast else 'N/A'}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*AI Recommendation:* `{AI_RECOMMENDATION[overall_risk]}`\n"
                        f"Review the full report before deciding.",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "action_id": "approve_deployment",
                    "value": pipeline_run_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "style": "danger",
                    "action_id": "reject_deployment",
                    "value": pipeline_run_id,
                },
            ],
        },
    ]

    print(f"  ✅ Synthesizer complete. Overall Risk: {overall_risk.value.upper()}")
    print(f"     AI Recommendation: {AI_RECOMMENDATION[overall_risk]}")

    return {
        "overall_risk": overall_risk,
        "risk_brief_markdown": full_brief,
        "risk_brief_slack_blocks": slack_blocks,
        "messages": [{"role": "synthesizer", "content": f"Brief generated. Risk: {overall_risk.value}"}],
    }
