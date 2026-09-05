"""
run_analysis.py — CLI Entrypoint for the LangGraph AI Risk Gate.

Usage:
    python -m agents.run_analysis --tfplan-json tfplan.json --env dev --run-id 123

Exit codes (read by Jenkins Jenkinsfile):
    0: Analysis complete. AI recommends auto-approve.
    2: Analysis complete. AI recommends human review. (Jenkins shows `input` step)
    1: Analysis failed with an error.

IMPORTANT: The agents NEVER approve or reject a deployment.
The only job of this script is to:
  1. Run the 4 AI agents
  2. Generate a Risk Brief markdown file
  3. Exit with a code that Jenkins uses to decide whether to show its approval button

Jenkins handles approval via its native `input` step.
Jenkins handles notification via its Email Extension Plugin.
"""

import argparse
import os
import sys

from .graph import build_graph
from .core.state import PipelineState
from .core.memory.graph_client import close_driver
from .core.memory.writer import write_run_to_graph


def get_args():
    parser = argparse.ArgumentParser(description="AI Risk Gate — Analysis Only")
    parser.add_argument("--tfplan-json", required=True, help="Path to terraform plan JSON")
    parser.add_argument("--env", required=True, choices=["dev", "staging", "production"],
                        help="Deployment environment")
    parser.add_argument("--run-id", required=True, help="Jenkins BUILD_ID (used for logging)")
    return parser.parse_args()


def main():
    args = get_args()

    print(f"\n{'='*60}")
    print(f"  AI Infrastructure Risk Gate")
    print(f"  Environment : {args.env.upper()}")
    print(f"  Pipeline Run: {args.run_id}")
    print(f"  Plan File   : {args.tfplan_json}")
    print(f"{'='*60}\n")

    graph = build_graph()

    initial_state = PipelineState(
        tfplan_path=args.tfplan_json,
        ansible_playbook_paths=[],
        tf_source_dir=os.environ.get("RISK_GATE_WORKSPACE_DIR", "."),
        git_diff_summary="",
        environment=args.env,
        pipeline_run_id=args.run_id,
        triggered_by=os.environ.get("BUILD_USER", "Jenkins_Automation"),
        resource_changes=[],
        plan_summary={},
        injection_flags=[],
        secops_finding=None,
        finops_finding=None,
        blast_radius_finding=None,
        integrity_finding=None,
        messages=[],
        overall_risk=None,
        risk_brief_markdown=None,
        risk_brief_slack_blocks=None,
        requires_human_approval=False,
        auto_approved=False,
        human_decision=None,
        human_decision_reason=None,
    )

    try:
        # Run the graph to completion — no pausing, no resuming
        final_state = graph.invoke(initial_state)

        # Write Risk Brief to disk — Jenkins will archive this and email it
        report_path = "risk-brief/report.md"
        pdf_path = "risk-brief/report.pdf"
        os.makedirs("risk-brief", exist_ok=True)
        brief = final_state.get("risk_brief_markdown", "_No report generated._")
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(brief)
            
        try:
            from markdown_pdf import Section, MarkdownPdf
            pdf = MarkdownPdf(toc_level=0)
            pdf.add_section(Section(brief))
            pdf.save(pdf_path)
            print(f"\n📄 Risk Brief PDF generated: {pdf_path}")
        except Exception as e:
            print(f"\n⚠️ Failed to generate PDF: {e}")

        print(f"📄 Risk Brief Markdown written to: {report_path}")

        # ── Phase 3: Write to Knowledge Graph ──────────────────────────────
        # Only write to graph if graph is available. The agents are advisory,
        # but the decision defaults to false until Jenkins approves.
        if final_state.get("graph_available"):
            write_run_to_graph(
                state=final_state,
                approved=False,  # Still pending Jenkins approval
                approved_by="pending"
            )

        # Exit code tells Jenkins whether to show the manual approval `input` step
        requires_human = final_state.get("requires_human_approval", True)

        if requires_human:
            print("\n🔴 AI RECOMMENDATION: Human review required before applying.")
            print("   Jenkins will send the report by email and pause for your approval.\n")
            sys.exit(2)  # Jenkins: show `input` step before proceeding
        else:
            print("\n🟢 AI RECOMMENDATION: Safe to proceed.")
            print("   Jenkins will still show the approval button — you always have final say.\n")
            sys.exit(0)  # Jenkins: can proceed (but still shows button if configured to do so)

    except SystemExit:
        raise
    except Exception as e:
        print(f"\n❌ FATAL ERROR in AI Risk Gate: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        # Write a minimal error report so Jenkins still has an artifact to publish
        os.makedirs("risk-brief", exist_ok=True)
        with open("risk-brief/report.md", "w", encoding="utf-8") as f:
            f.write(f"# ❌ AI Risk Gate Error\n\n```\n{traceback.format_exc()}\n```\n\n"
                    f"**The AI Risk Gate failed. Human review is required before proceeding.**")
        sys.exit(1)
    finally:
        # Clean up the singleton Neo4j driver
        close_driver()

if __name__ == "__main__":
    main()
