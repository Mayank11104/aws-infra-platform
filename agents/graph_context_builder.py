import json
from .core.state import ResourceChange

def build_resource_history_context(rc: ResourceChange) -> str:
    """
    Builds the Knowledge Graph history context block injected into the LLM prompt.
    Only included if the resource has prior history.
    """
    if not rc.resource_history:
        return ""

    lines = [
        f"\n--- KNOWLEDGE GRAPH HISTORY FOR {rc.address} ---",
        "This resource has appeared in previous pipeline runs. Use this context to avoid flagging previously approved or known issues.",
        ""
    ]
    
    # 1. Recurring Findings
    if rc.recurring_findings:
        lines.append("⚠️ RECURRING UNRESOLVED ISSUES:")
        for r in rc.recurring_findings:
            app_str = "ALWAYS APPROVED" if r.get('always_approved') else "SOMETIMES REJECTED"
            lines.append(f"  - [{r.get('severity', 'unknown').upper()}] {r.get('finding')} (Seen {r.get('occurrences')} times, {app_str})")
        lines.append("")

    # 2. Downstream Blast Radius
    if rc.downstream_resources:
        lines.append("📉 DOWNSTREAM DEPENDENCIES (Blast Radius):")
        for d in rc.downstream_resources:
            roles = ", ".join(d.get('ansible_roles', []))
            role_str = f" [Ansible Roles: {roles}]" if roles else ""
            lines.append(f"  - {d.get('address')} ({d.get('hops')} hops away){role_str}")
        lines.append("")

    # 3. Recent Run History
    lines.append("🕒 RECENT PIPELINE RUNS:")
    for run in rc.resource_history[:3]:  # Show last 3 runs
        status = "✅ Approved" if run.get("approved") else "❌ Rejected"
        by_user = f" by {run.get('approved_by')}" if run.get('approved_by') else ""
        lines.append(f"  Run {run.get('run_id')} ({run.get('action')}): {status}{by_user}")
        if run.get("findings"):
            for f in run["findings"]:
                lines.append(f"    - [{f.get('severity', 'unknown').upper()}] {f.get('finding')}")
        else:
            lines.append("    - No findings.")
    
    lines.append("--------------------------------------------------\n")
    return "\n".join(lines)
