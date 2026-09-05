"""
memory/queries.py — All Cypher READ queries used by the agents.

Design rules:
  1. ALL graph reads are in this file. Agents never write Cypher inline.
  2. Queries return plain Python dicts — no neo4j Record objects leak out.
  3. Each function has a docstring explaining exactly what it returns and why.
  4. If Neo4j is unavailable, functions return empty structures (not exceptions).
     The caller (ingestion.py) checks graph_client.is_available() before calling.
"""

from __future__ import annotations

from datetime import datetime

from .graph_client import get_session


# ---------------------------------------------------------------------------
# Core query: per-resource history
# This is the primary query called for every ResourceChange at ingestion time.
# ---------------------------------------------------------------------------

def get_resource_history(address: str, environment: str, limit: int = 5) -> list[dict]:
    """
    Returns the last `limit` agent runs in which this resource appeared,
    along with every finding that was recorded about it in each run.

    Used by: all 4 agents (injected into their context as resource_history)

    Returns:
        [
          {
            "run_id": "build_42",
            "timestamp": "2026-09-01T10:00:00Z",
            "action": "update",
            "overall_risk": "medium",
            "approved": True,
            "approved_by": "mayank",
            "findings": [
              {"agent": "SecOps", "severity": "high", "finding": "SG open to 0.0.0.0/0"}
            ]
          },
          ...
        ]
    """
    cypher = """
    MATCH (r:TerraformResource {address: $address, environment: $environment})
          -[:ANALYZED_IN]->(run:AgentRun)
    OPTIONAL MATCH (run)-[:PRODUCED]->(f:AgentFinding)-[:ABOUT]->(r)
    RETURN run.run_id            AS run_id,
           run.timestamp         AS timestamp,
           run.action            AS action,
           run.overall_risk      AS overall_risk,
           run.approved          AS approved,
           run.approved_by       AS approved_by,
           collect({
               agent:    f.agent,
               severity: f.severity,
               category: f.category,
               finding:  f.finding
           }) AS findings
    ORDER BY run.timestamp DESC
    LIMIT $limit
    """
    with get_session() as session:
        result = session.run(cypher, address=address, environment=environment, limit=limit)
        rows = []
        for record in result:
            rows.append({
                "run_id": record["run_id"],
                "timestamp": str(record["timestamp"]),
                "action": record["action"],
                "overall_risk": record["overall_risk"],
                "approved": record["approved"],
                "approved_by": record["approved_by"],
                # Filter out null findings (OPTIONAL MATCH may produce null rows)
                "findings": [f for f in record["findings"] if f.get("agent") is not None],
            })
        return rows


def get_recurring_findings(address: str, environment: str, severity: str = "high") -> list[dict]:
    """
    Returns findings of severity >= `severity` that appeared on this resource
    in MORE THAN ONE run. These are recurring unresolved issues.

    Used by: SecOps agent (to flag "this misconfiguration was approved without fixing")

    Returns:
        [
          {
            "finding": "Security group open to 0.0.0.0/0 on port 22",
            "severity": "high",
            "occurrences": 3,
            "first_seen": "build_38",
            "last_seen": "build_42",
            "always_approved": True
          }
        ]
    """
    severity_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    min_rank = severity_rank.get(severity, 3)

    cypher = """
    MATCH (r:TerraformResource {address: $address, environment: $environment})
    MATCH (run:AgentRun)-[:PRODUCED]->(f:AgentFinding)-[:ABOUT]->(r)
    WHERE f.severity_rank >= $min_rank
    WITH f.finding AS finding_text, f.severity AS severity,
         count(run) AS occurrences,
         min(run.run_id) AS first_seen,
         max(run.run_id) AS last_seen,
         all(r2 IN collect(run) WHERE r2.approved = true) AS always_approved
    WHERE occurrences > 1
    RETURN finding_text, severity, occurrences, first_seen, last_seen, always_approved
    ORDER BY occurrences DESC
    """
    with get_session() as session:
        result = session.run(cypher, address=address, environment=environment, min_rank=min_rank)
        return [
            {
                "finding": record["finding_text"],
                "severity": record["severity"],
                "occurrences": record["occurrences"],
                "first_seen": record["first_seen"],
                "last_seen": record["last_seen"],
                "always_approved": record["always_approved"],
            }
            for record in result
        ]


def get_environment_cost_trend(environment: str, last_n_runs: int = 5) -> list[dict]:
    """
    Returns the monthly cost delta for the last N runs in this environment.
    Used by: FinOps agent to detect escalating cost trends.

    Returns:
        [
          {"run_id": "build_40", "timestamp": "...", "total_monthly_delta_usd": 20.0},
          {"run_id": "build_42", "timestamp": "...", "total_monthly_delta_usd": 40.0},
        ]
    """
    cypher = """
    MATCH (run:AgentRun {environment: $environment})
    WHERE run.total_monthly_delta_usd IS NOT NULL
    RETURN run.run_id AS run_id, run.timestamp AS timestamp,
           run.total_monthly_delta_usd AS delta
    ORDER BY run.timestamp DESC
    LIMIT $limit
    """
    with get_session() as session:
        result = session.run(cypher, environment=environment, limit=last_n_runs)
        rows = [
            {"run_id": r["run_id"], "timestamp": str(r["timestamp"]), "total_monthly_delta_usd": r["delta"]}
            for r in result
        ]
        # Return chronological order (oldest first for trend display)
        return list(reversed(rows))


def get_downstream_resources(address: str, environment: str) -> list[dict]:
    """
    Returns all resources that DEPEND_ON the given resource (directly or transitively,
    up to 3 hops). Used by: Blast Radius agent to assess downstream impact.

    Example: deleting a VPC would return: subnets, EC2 instances, security groups.

    Returns:
        [
          {"address": "module.ec2.aws_instance.web", "resource_type": "aws_instance",
           "hops": 1, "ansible_roles": ["nginx", "docker"]},
          ...
        ]
    """
    cypher = """
    MATCH path = (r:TerraformResource {address: $address, environment: $environment})
                 <-[:DEPENDS_ON*1..3]-(downstream:TerraformResource)
    OPTIONAL MATCH (downstream)-[:CONFIGURED_BY]->(role:AnsibleRole)
    RETURN DISTINCT
           downstream.address       AS address,
           downstream.resource_type AS resource_type,
           length(path) - 1         AS hops,
           collect(role.name)       AS ansible_roles
    ORDER BY hops
    """
    with get_session() as session:
        result = session.run(cypher, address=address, environment=environment)
        return [
            {
                "address": r["address"],
                "resource_type": r["resource_type"],
                "hops": r["hops"],
                "ansible_roles": r["ansible_roles"],
            }
            for r in result
        ]


def get_ansible_coverage(environment: str) -> dict[str, list[str]]:
    """
    Returns a mapping of EC2 instance address → applied Ansible roles.
    Used by: Integrity agent to check if new/replaced EC2s will be configured.

    Returns:
        {
          "module.ec2.aws_instance.web": ["nginx", "docker"],
          ...
        }
    """
    cypher = """
    MATCH (ec2:TerraformResource {environment: $environment, resource_type: "aws_instance"})
          -[:CONFIGURED_BY]->(role:AnsibleRole)
    RETURN ec2.address AS address, collect(role.name) AS roles
    """
    with get_session() as session:
        result = session.run(cypher, environment=environment)
        return {record["address"]: record["roles"] for record in result}


def get_environment_baseline(environment: str) -> dict:
    """
    Returns statistical baseline for this environment across all historical runs.
    Used by: Synthesizer to put the current run in context.

    Returns:
        {
          "total_runs": 10,
          "avg_monthly_delta_usd": 15.0,
          "avg_creates_per_run": 0.5,
          "avg_deletes_per_run": 0.1,
          "most_flagged_resource": "module.security_group.aws_sg.web",
        }
    """
    cypher = """
    MATCH (run:AgentRun {environment: $environment})
    WITH count(run) AS total_runs,
         avg(run.total_monthly_delta_usd) AS avg_delta,
         avg(run.creates) AS avg_creates,
         avg(run.deletes) AS avg_deletes
    RETURN total_runs, avg_delta, avg_creates, avg_deletes
    """
    with get_session() as session:
        result = session.run(cypher, environment=environment)
        record = result.single()
        if not record:
            return {}
        return {
            "total_runs": record["total_runs"],
            "avg_monthly_delta_usd": round(record["avg_delta"] or 0.0, 2),
            "avg_creates_per_run": round(record["avg_creates"] or 0.0, 1),
            "avg_deletes_per_run": round(record["avg_deletes"] or 0.0, 1),
        }
