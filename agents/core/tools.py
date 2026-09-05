"""
tools.py — LangChain @tool wrappers for external static analysis tools.

Each tool is wrapped with the Harness Layer guardrails:
  1. Path validation — no directory traversal, must be inside WORKSPACE_DIR
  2. File type validation — reject unexpected extensions
  3. Subprocess timeout — CI pipelines can't hang
  4. Output bounding — cap stdout before it reaches LLM context
  5. No shell=True — always use list args to prevent shell injection
  6. Structured error returns — agents know when a tool failed and degrade
     their confidence accordingly, rather than silently hallucinating

Tools NEVER accept paths from LLM output. Paths are always constructed by
the Python node functions and passed directly — the LLM cannot control
the filesystem operations.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from langchain.tools import tool

# ---------------------------------------------------------------------------
# Workspace boundary — ALL tool file access is restricted to this directory.
# Adjust at runtime via RISK_GATE_WORKSPACE_DIR environment variable.
# ---------------------------------------------------------------------------
WORKSPACE_DIR = os.environ.get(
    "RISK_GATE_WORKSPACE_DIR",
    ".",  # Default: current directory (Jenkins workspace)
)

OUTPUT_MAX_BYTES = 500_000  # ~500KB ceiling on tool stdout before LLM sees it


def _validate_path(path_str: str, expected_suffix: str | None = None) -> Path:
    """
    Harness guardrail: ensure a path is inside WORKSPACE_DIR and
    optionally validate its file extension.
    Raises ValueError with a clear message rather than silently proceeding.
    """
    resolved = Path(path_str).resolve()
    allowed_root = Path(WORKSPACE_DIR).resolve()

    # Case-insensitive prefix check for Windows compatibility (C:\ vs c:\)
    if not str(resolved).lower().startswith(str(allowed_root).lower()):
        raise ValueError(
            f"Path '{resolved}' is outside the allowed workspace '{allowed_root}'. "
            f"Tool execution refused."
        )
    if not resolved.exists():
        raise ValueError(f"Path does not exist: {resolved}")
    if expected_suffix and resolved.suffix != expected_suffix:
        raise ValueError(
            f"Expected file with suffix '{expected_suffix}', got '{resolved.suffix}'"
        )
    return resolved


def _run_subprocess(cmd: list[str], timeout: int = 120) -> tuple[str, str, int]:
    """
    Harness guardrail: run an external command with:
      - No shell interpolation (list args, never shell=True)
      - Timeout to prevent CI hangs
      - Output captured (not streamed to console mid-analysis)
    Returns (stdout, stderr, returncode).
    """
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,  # Explicit — never enable shell interpolation
    )
    stdout = result.stdout[:OUTPUT_MAX_BYTES]
    stderr = result.stderr[:10_000]
    return stdout, stderr, result.returncode


# ---------------------------------------------------------------------------
# Checkov — Security & misconfiguration static analysis on tfplan JSON
# ---------------------------------------------------------------------------

@tool
def run_checkov_scan(tfplan_json_path: str) -> dict:
    """
    Runs Checkov static analysis against a Terraform plan JSON file.
    Returns structured findings: failed checks with severity, resource address,
    check ID, and check description.

    USE THIS instead of reasoning about security from raw resource attributes.
    Checkov runs actual rule engines — its findings are authoritative for
    known misconfiguration patterns. The LLM's job is to explain and prioritize
    findings, not to independently derive security semantics.

    Returns a dict with key 'results' containing failed checks, or
    an 'error' key if the tool failed (in which case, set confidence < 0.5
    and explicitly flag that automated scanning was unavailable).
    """
    try:
        resolved = _validate_path(tfplan_json_path, ".json")
    except ValueError as e:
        return {"error": "path_validation_failed", "detail": str(e)}

    try:
        stdout, stderr, returncode = _run_subprocess(
            ["checkov", "-f", str(resolved), "--compact", "--output", "json"],
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"error": "checkov_timeout", "detail": "Checkov did not complete within 120s"}
    except FileNotFoundError:
        return {"error": "checkov_not_installed", "detail": "checkov binary not found in PATH"}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "error": "checkov_parse_failed",
            "returncode": returncode,
            "stderr": stderr[:1000],
        }


# ---------------------------------------------------------------------------
# tfsec / Trivy — Security analysis on Terraform source HCL
# ---------------------------------------------------------------------------

@tool
def run_tfsec_scan(tf_directory: str) -> dict:
    """
    Runs tfsec (or Trivy in config mode if tfsec is unavailable) against
    the Terraform source directory for security misconfigurations.

    NOTE: tfsec analyzes HCL source files directly, not the plan JSON.
    It has a different rule set from Checkov — use both; deduplicate findings
    at the Synthesizer rather than treating overlap as double evidence.

    Returns structured findings or an 'error' key on failure.
    """
    try:
        resolved = _validate_path(tf_directory)
    except ValueError as e:
        return {"error": "path_validation_failed", "detail": str(e)}

    if not resolved.is_dir():
        return {"error": "not_a_directory", "detail": str(resolved)}

    try:
        stdout, stderr, returncode = _run_subprocess(
            ["tfsec", str(resolved), "--format", "json", "--no-color"],
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"error": "tfsec_timeout"}
    except FileNotFoundError:
        # tfsec not installed — try Trivy as fallback
        try:
            stdout, stderr, returncode = _run_subprocess(
                ["trivy", "config", str(resolved), "--format", "json"],
                timeout=90,
            )
        except FileNotFoundError:
            return {
                "error": "scanner_not_installed",
                "detail": "Neither tfsec nor trivy found in PATH. Install one.",
            }

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "tfsec_parse_failed", "stderr": stderr[:1000]}


# ---------------------------------------------------------------------------
# Infracost — Cost delta analysis using live AWS pricing data
# ---------------------------------------------------------------------------

@tool
def run_infracost_diff(tfplan_json_path: str) -> dict:
    """
    Runs Infracost against the Terraform plan to compute actual AWS cost
    deltas using live pricing data from the AWS Pricing API.

    USE THIS instead of estimating costs from instance types or resource sizes.
    AWS pricing varies by region, changes over time, and is affected by reserved
    instances and savings plans that this script cannot see. Infracost queries
    current public pricing — it is authoritative for cost delta estimates.

    Returns monthly cost before/after and per-resource breakdown, or
    an 'error' key on failure.
    """
    try:
        resolved = _validate_path(tfplan_json_path, ".json")
    except ValueError as e:
        return {"error": "path_validation_failed", "detail": str(e)}

    try:
        stdout, stderr, returncode = _run_subprocess(
            [
                "infracost", "breakdown",
                "--path", str(resolved),
                "--format", "json",
            ],
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"error": "infracost_timeout"}
    except FileNotFoundError:
        return {
            "error": "infracost_not_installed",
            "detail": "infracost binary not found in PATH",
        }

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "infracost_parse_failed", "stderr": stderr[:1000]}


# ---------------------------------------------------------------------------
# Ansible-lint — Playbook quality and anti-pattern analysis
# ---------------------------------------------------------------------------

@tool
def run_ansible_lint(playbook_path: str) -> dict:
    """
    Runs ansible-lint against an Ansible playbook to detect anti-patterns
    such as using 'shell' or 'command' instead of native modules,
    missing 'no_log: true' on tasks that handle secrets, or deprecated syntax.

    Returns structured findings list, or an 'error' key on failure.
    """
    try:
        resolved = _validate_path(playbook_path, ".yml")
    except ValueError as e:
        return {"error": "path_validation_failed", "detail": str(e)}

    try:
        stdout, stderr, returncode = _run_subprocess(
            ["ansible-lint", str(resolved), "-f", "json"],
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"error": "ansible_lint_timeout"}
    except FileNotFoundError:
        return {"error": "ansible_lint_not_installed"}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "ansible_lint_parse_failed", "stderr": stderr[:1000]}


# ---------------------------------------------------------------------------
# Convenience: all tools as a list for binding to LangChain agents
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    run_checkov_scan,
    run_tfsec_scan,
    run_infracost_diff,
    run_ansible_lint,
]
