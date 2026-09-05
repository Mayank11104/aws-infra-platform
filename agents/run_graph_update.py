"""
run_graph_update.py — CLI to update Knowledge Graph state from Jenkins.

Usage (Update approval status):
    python -m agents.run_graph_update --action approval --run-id 123 --decision approved --user mayank

Usage (Record Ansible run):
    python -m agents.run_graph_update --action ansible --env dev --run-id 123 --playbook install_services.yml --role nginx --host-group tag_Role_web --addresses module.ec2.aws_instance.web[0]
"""

import argparse
import sys
from .core.memory.graph_client import close_driver
from .core.memory.writer import update_approval_status, write_ansible_run


def main():
    parser = argparse.ArgumentParser(description="Knowledge Graph Update CLI")
    parser.add_argument("--action", required=True, choices=["approval", "ansible"])
    parser.add_argument("--run-id", required=True)
    
    # For approval
    parser.add_argument("--decision", choices=["approved", "rejected"])
    parser.add_argument("--user", default="jenkins")
    
    # For ansible
    parser.add_argument("--env", choices=["dev", "staging", "production"])
    parser.add_argument("--playbook")
    parser.add_argument("--role")
    parser.add_argument("--host-group")
    parser.add_argument("--addresses", nargs="+", help="Space-separated list of EC2 addresses")

    args = parser.parse_args()

    try:
        if args.action == "approval":
            if not args.decision:
                print("ERROR: --decision required for approval action")
                sys.exit(1)
            is_approved = (args.decision == "approved")
            update_approval_status(args.run_id, is_approved, args.user)
            
        elif args.action == "ansible":
            if not all([args.env, args.playbook, args.role, args.host_group, args.addresses]):
                print("ERROR: Missing arguments for ansible action")
                sys.exit(1)
            write_ansible_run(
                environment=args.env,
                run_id=args.run_id,
                playbook=args.playbook,
                host_group=args.host_group,
                target_addresses=args.addresses,
                role_name=args.role,
                status="success"
            )
            
    finally:
        close_driver()


if __name__ == "__main__":
    main()
