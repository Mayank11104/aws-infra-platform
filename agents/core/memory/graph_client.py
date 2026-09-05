"""
memory/graph_client.py — Neo4j connection manager for the Infrastructure Knowledge Graph.

Provides a single, reusable session factory for the entire agents package.
Connection settings are read from environment variables so Jenkins can inject
them as credentials without hardcoding.

Connectivity:
  - Bolt port  7687: used by Python driver for all reads/writes
  - HTTP port  7474: used by Neo4j Browser (web UI) for visualization only

Run Neo4j locally with Docker Desktop:
  docker run -d --name neo4j-memory \\
    -p 7474:7474 -p 7687:7687 \\
    -e NEO4J_AUTH=neo4j/password123 \\
    neo4j:latest
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from neo4j import GraphDatabase, Driver


# ---------------------------------------------------------------------------
# Singleton driver — created once per Python process, reused across all calls.
# Neo4j's Python driver is thread-safe and manages its own connection pool.
# ---------------------------------------------------------------------------

_driver: Driver | None = None


def get_driver() -> Driver:
    """
    Returns the singleton Neo4j driver, creating it on first call.
    Reads NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD from environment.
    Raises clearly if credentials are missing — no silent fallback.
    """
    global _driver
    if _driver is not None:
        return _driver

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD")

    if not password:
        raise EnvironmentError(
            "NEO4J_PASSWORD is not set. "
            "Add it as a Jenkins Secret Text credential and inject it into the pipeline."
        )

    _driver = GraphDatabase.driver(uri, auth=(user, password))
    # Verify connectivity immediately — fail fast if Neo4j is not reachable
    _driver.verify_connectivity()
    return _driver


@contextmanager
def get_session():
    """
    Context manager that yields a Neo4j session and closes it cleanly.

    Usage:
        with get_session() as session:
            result = session.run("MATCH (n) RETURN n LIMIT 1")
    """
    driver = get_driver()
    session = driver.session()
    try:
        yield session
    finally:
        session.close()


def close_driver() -> None:
    """Close the singleton driver. Call once at process exit in run_analysis.py."""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def is_available() -> bool:
    """
    Returns True if Neo4j is reachable, False otherwise.
    Used at ingestion time — if Neo4j is down, agents degrade gracefully
    (they run without historical context rather than failing the whole pipeline).
    """
    try:
        get_driver()
        return True
    except Exception:
        return False
