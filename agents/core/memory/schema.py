"""
memory/schema.py — Graph schema bootstrap and constraint creation.

Run this ONCE when setting up Neo4j for the first time to create:
  - Uniqueness constraints (prevent duplicate nodes)
  - Indexes (for fast lookups by address, run_id, etc.)

This is idempotent — safe to run on every pipeline start.
"""

from __future__ import annotations

from .graph_client import get_session


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

CONSTRAINTS = [
    # Each resource address within an environment is unique
    "CREATE CONSTRAINT resource_unique IF NOT EXISTS "
    "FOR (r:TerraformResource) REQUIRE (r.address, r.environment) IS UNIQUE",

    # Each pipeline run ID is unique
    "CREATE CONSTRAINT run_unique IF NOT EXISTS "
    "FOR (r:AgentRun) REQUIRE r.run_id IS UNIQUE",

    # Each environment name is unique
    "CREATE CONSTRAINT env_unique IF NOT EXISTS "
    "FOR (e:Environment) REQUIRE e.name IS UNIQUE",

    # Each Ansible role name is unique within an environment
    "CREATE CONSTRAINT role_unique IF NOT EXISTS "
    "FOR (a:AnsibleRole) REQUIRE (a.name, a.environment) IS UNIQUE",
]

INDEXES = [
    # Fast lookup when querying history by resource type
    "CREATE INDEX resource_type_idx IF NOT EXISTS "
    "FOR (r:TerraformResource) ON (r.resource_type)",

    # Fast lookup for findings by severity
    "CREATE INDEX finding_severity_idx IF NOT EXISTS "
    "FOR (f:AgentFinding) ON (f.severity)",
]


def bootstrap_schema() -> None:
    """
    Create all constraints and indexes. Safe to call on every pipeline run.
    Prints a summary of what was applied.
    """
    print("🗄️  Knowledge Graph: Bootstrapping schema...")
    with get_session() as session:
        for constraint in CONSTRAINTS:
            session.run(constraint)
        for index in INDEXES:
            session.run(index)
    print("   ✅ Schema constraints and indexes verified.")
