"""
memory/writer.py — Post-run graph writer.

Persists the results of every successful pipeline run to the Neo4j graph.
This is the function that BUILDS the knowledge base over time.

Called once by run_analysis.py AFTER the LangGraph graph completes successfully.
Never called mid-run or by any agent node — agents are read-only consumers.

Write order:
  1. MERGE Environment node
  2. MERGE AgentRun node
  3. MERGE TerraformResource nodes and their DEPENDS_ON relationships
  4. CREATE AgentFinding nodes, linked to resources and the run
  5. MERGE AnsibleRole nodes and link to EC2s via CONFIGURED_BY
  6. Update the AgentRun node with approval status (called again when admin decides)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..state import AgentFinding, PipelineState, ResourceChange
from .graph_client import get_session

# Resource types that have upstream dependencies we can infer from Terraform's module
# wiring in the plan. We extract these from the `before`/`after` dicts.
_DEPENDENCY_ATTRIBUTE_MAP = {
    "aws_instance": ["subnet_id", "security_group_ids", "vpc_security_group_ids"],
    "aws_db_instance": ["db_subnet_group_name", "vpc_security_group_ids"],
    "aws_lb": ["subnets", "security_groups"],
    "aws_security_group": ["vpc_id"],
    "aws_subnet": ["vpc_id"],
}

# Ansible tag-to-role mapping derived from your playbooks.
# Extend this as you add more playbooks.
_TAG_ROLE_MAP = {
    "web": ["nginx", "docker"],   # tag_Role_web → install_services.yml
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_run_to_graph(
    state: PipelineState,
    approved: bool = False,
    approved_by: str = "",
) -> None:
    """
    Main entry point. Persists the complete run to Neo4j.

    Args:
        state:       Final PipelineState after graph.invoke() completes.
        approved:    True if the admin clicked Proceed in Jenkins.
        approved_by: Jenkins user who clicked Proceed (from APPROVAL_REASON env var).
    """
    environment = state["environment"]
    run_id = state["pipeline_run_id"]
    triggered_by = state["triggered_by"]
    resource_changes = state["resource_changes"]
    plan_summary = state.get("plan_summary", {})
    overall_risk = state.get("overall_risk")
    finops = state.get("finops_finding")
    injection_flags = state.get("injection_flags", [])

    total_delta = finops.total_monthly_delta_usd if finops else None

    print(f"\n📝  Knowledge Graph: Persisting run {run_id} to Neo4j...")

    with get_session() as session:
        # ── 1. MERGE Environment node ──────────────────────────────────────────
        session.run(
            "MERGE (e:Environment {name: $env})",
            env=environment,
        )

        # ── 2. MERGE AgentRun node ─────────────────────────────────────────────
        session.run(
            """
            MERGE (run:AgentRun {run_id: $run_id})
            SET run.timestamp              = $ts,
                run.environment            = $env,
                run.triggered_by           = $triggered_by,
                run.overall_risk           = $overall_risk,
                run.approved               = $approved,
                run.approved_by            = $approved_by,
                run.total_monthly_delta_usd = $delta,
                run.creates                = $creates,
                run.updates                = $updates,
                run.deletes                = $deletes,
                run.replaces               = $replaces,
                run.injection_flags_count  = $injection_count
            """,
            run_id=run_id,
            ts=_utc_now(),
            env=environment,
            triggered_by=triggered_by,
            overall_risk=overall_risk.value if overall_risk else "unknown",
            approved=approved,
            approved_by=approved_by,
            delta=total_delta,
            creates=plan_summary.get("create", 0),
            updates=plan_summary.get("update", 0),
            deletes=plan_summary.get("delete", 0),
            replaces=plan_summary.get("replace", 0),
            injection_count=len(injection_flags),
        )

        # ── 3. MERGE TerraformResource nodes and their relationships ───────────
        for rc in resource_changes:
            if rc.action == "no-op":
                continue  # No point recording resources that didn't change

            # MERGE resource node — identified by (address, environment)
            session.run(
                """
                MERGE (r:TerraformResource {address: $address, environment: $env})
                SET r.resource_type   = $rtype,
                    r.module          = $module,
                    r.last_action     = $action,
                    r.last_run_id     = $run_id,
                    r.last_seen       = $ts,
                    r.risk_tier       = $tier
                """,
                address=rc.address,
                env=environment,
                rtype=rc.resource_type,
                module=rc.module or "",
                action=rc.action,
                run_id=run_id,
                ts=_utc_now(),
                tier=rc.risk_tier,
            )

            # ANALYZED_IN relationship: resource ← → run
            session.run(
                """
                MATCH (r:TerraformResource {address: $address, environment: $env})
                MATCH (run:AgentRun {run_id: $run_id})
                MERGE (r)-[:ANALYZED_IN {action: $action}]->(run)
                """,
                address=rc.address,
                env=environment,
                run_id=run_id,
                action=rc.action,
            )

            # LIVES_IN relationship: resource → environment
            session.run(
                """
                MATCH (r:TerraformResource {address: $address, environment: $env})
                MATCH (e:Environment {name: $env})
                MERGE (r)-[:LIVES_IN]->(e)
                """,
                address=rc.address,
                env=environment,
            )

            # DEPENDS_ON relationships: derived from resource attributes
            _write_dependencies(session, rc, environment)

            # CONFIGURED_BY relationships: EC2 → AnsibleRole (from tags)
            if rc.resource_type == "aws_instance":
                _write_ansible_roles(session, rc, environment, run_id)

        # ── 4. Write agent findings ────────────────────────────────────────────
        for agent_key in ["secops_finding", "finops_finding", "blast_radius_finding", "integrity_finding"]:
            finding: AgentFinding | None = state.get(agent_key)
            if not finding or not finding.findings:
                continue
            _write_agent_findings(session, finding, run_id, environment)

    print(f"   ✅ Run {run_id} persisted. Nodes: {len(resource_changes)} resources, "
          f"environment: {environment}, risk: {overall_risk.value if overall_risk else 'unknown'}")


def update_approval_status(run_id: str, approved: bool, approved_by: str) -> None:
    """
    Updates the AgentRun node after the admin makes their decision in Jenkins.
    Called from run_analysis.py after the pipeline completes.
    """
    with get_session() as session:
        session.run(
            """
            MATCH (run:AgentRun {run_id: $run_id})
            SET run.approved    = $approved,
                run.approved_by = $approved_by,
                run.approved_at = $ts
            """,
            run_id=run_id,
            approved=approved,
            approved_by=approved_by,
            ts=_utc_now(),
        )
    print(f"   ✅ Approval status updated: run={run_id}, approved={approved}, by={approved_by}")


def write_ansible_run(
    environment: str,
    run_id: str,
    playbook: str,
    host_group: str,
    target_addresses: list[str],
    role_name: str,
    status: str,
) -> None:
    """
    Records that an Ansible role was successfully applied to a set of EC2 instances.
    Called from the Jenkins Ansible post-step (Windows agent, after WSL ansible-playbook).

    This is what builds the (:EC2)-[:CONFIGURED_BY]->(:AnsibleRole) edges over time.
    """
    print(f"📝  Knowledge Graph: Recording Ansible run — {role_name} on {host_group}...")

    with get_session() as session:
        # MERGE the AnsibleRole node
        session.run(
            """
            MERGE (role:AnsibleRole {name: $name, environment: $env})
            SET role.playbook    = $playbook,
                role.host_group  = $host_group,
                role.last_run_id = $run_id,
                role.last_applied = $ts
            """,
            name=role_name,
            env=environment,
            playbook=playbook,
            host_group=host_group,
            run_id=run_id,
            ts=_utc_now(),
        )

        # Link each targeted EC2 to this role
        for address in target_addresses:
            session.run(
                """
                MATCH (ec2:TerraformResource {address: $address, environment: $env})
                MATCH (role:AnsibleRole {name: $role_name, environment: $env})
                MERGE (ec2)-[rel:CONFIGURED_BY]->(role)
                SET rel.last_applied = $ts,
                    rel.status       = $status,
                    rel.run_id       = $run_id
                """,
                address=address,
                env=environment,
                role_name=role_name,
                ts=_utc_now(),
                status=status,
                run_id=run_id,
            )

    print(f"   ✅ Ansible role '{role_name}' linked to {len(target_addresses)} resource(s).")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_dependencies(session, rc: ResourceChange, environment: str) -> None:
    """Infer DEPENDS_ON edges from resource attributes in the after-state."""
    dep_attrs = _DEPENDENCY_ATTRIBUTE_MAP.get(rc.resource_type, [])
    if not dep_attrs or not rc.after:
        return

    for attr in dep_attrs:
        value = rc.after.get(attr)
        if not value:
            continue
        # Values can be a single ID string or a list of IDs
        ids = value if isinstance(value, list) else [value]
        for dep_id in ids:
            if not dep_id:
                continue
            # Find any resource in the same environment whose `id` or `address`
            # matches this dependency ID. This is a best-effort match.
            session.run(
                """
                MATCH (r:TerraformResource {address: $address, environment: $env})
                MATCH (dep:TerraformResource {environment: $env})
                WHERE dep.aws_id = $dep_id OR dep.address CONTAINS $dep_id
                MERGE (r)-[:DEPENDS_ON {attribute: $attr}]->(dep)
                """,
                address=rc.address,
                env=environment,
                dep_id=dep_id,
                attr=attr,
            )


def _write_ansible_roles(session, rc: ResourceChange, environment: str, run_id: str) -> None:
    """
    Creates CONFIGURED_BY edges for EC2 instances based on their Role tag.
    This records the INTENDED configuration, even before Ansible actually runs.
    The write_ansible_run() function records actual successful application later.
    """
    role_tag = rc.tags_after.get("Role", "")
    roles = _TAG_ROLE_MAP.get(role_tag, [])

    for role_name in roles:
        session.run(
            """
            MERGE (role:AnsibleRole {name: $role_name, environment: $env})
            MERGE (r:TerraformResource {address: $address, environment: $env})
            MERGE (r)-[:CONFIGURED_BY {source: 'tag_inference', run_id: $run_id}]->(role)
            """,
            role_name=role_name,
            env=environment,
            address=rc.address,
            run_id=run_id,
        )


def _write_agent_findings(
    session,
    finding: AgentFinding,
    run_id: str,
    environment: str,
) -> None:
    """Persist each structured finding dict as an AgentFinding node in the graph."""
    severity_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}

    for f in finding.findings:
        resource_address = f.get("resource_address", "")
        severity = f.get("severity", "low")

        # CREATE a unique finding node (not MERGE — same finding can recur across runs)
        session.run(
            """
            MATCH (run:AgentRun {run_id: $run_id})
            CREATE (f:AgentFinding {
                agent:          $agent,
                severity:       $severity,
                severity_rank:  $rank,
                category:       $category,
                finding:        $finding_text,
                evidence:       $evidence,
                run_id:         $run_id,
                timestamp:      $ts
            })
            CREATE (run)-[:PRODUCED]->(f)
            """,
            run_id=run_id,
            agent=finding.agent_name,
            severity=severity,
            rank=severity_rank.get(severity, 1),
            category=f.get("category", f.get("action", "general")),
            finding_text=f.get("finding", f.get("consequence", str(f)))[:500],
            evidence=str(f.get("evidence", ""))[:300],
            ts=_utc_now(),
        )

        # Link finding to its resource if we can find it
        if resource_address and resource_address != "parse_error":
            session.run(
                """
                MATCH (f:AgentFinding {run_id: $run_id, agent: $agent, finding: $finding_text})
                MATCH (r:TerraformResource {address: $address, environment: $env})
                MERGE (f)-[:ABOUT]->(r)
                """,
                run_id=run_id,
                agent=finding.agent_name,
                finding_text=f.get("finding", f.get("consequence", str(f)))[:500],
                address=resource_address,
                env=environment,
            )
