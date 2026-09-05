# IaC Sentinel Autopilot

> **AI-Powered Infrastructure Risk Gate for CI/CD Pipelines**
> The Sentinel watches. You decide. The Autopilot executes.

A production-grade Infrastructure as Code (IaC) platform that provisions a fully isolated, multi-environment AWS infrastructure using **Terraform** and **Ansible** — guarded by a **multi-agent AI system** that reviews every infrastructure change before a single resource is touched.

The AI is a **reviewer, never a decision-maker**. It intercepts the CI/CD pipeline, analyzes the Terraform plan from four specialized angles (Security, Cost, Blast Radius, Integrity), generates a professional **Infrastructure Audit Report PDF**, and emails it to the administrator. The pipeline then waits for a human to approve or reject the deployment.

---

## Architecture Overview


![IaC Sentinel Autopilot — Architecture Diagram](docs/images/infra (2).png)

---

## Project Structure

```
iac-sentinel-autopilot/
│
├── agents/                  # AI Sentinel Gate (LangGraph + AWS Bedrock + Neo4j)
│   ├── core/
│   │   ├── ingestion.py     # Parses & tiers the Terraform plan JSON
│   │   ├── state.py         # LangGraph PipelineState TypedDict
│   │   └── memory/          # Neo4j Knowledge Graph client + reader/writer
│   ├── nodes/
│   │   ├── secops_agent.py      # Network & IAM security analysis
│   │   ├── finops_agent.py      # Cost delta analysis (Infracost)
│   │   ├── blast_radius_agent.py # Destructive change detection
│   │   ├── integrity_agent.py   # Tag alignment & state drift
│   │   └── synthesizer.py       # Fan-in: generates the Audit Report
│   ├── graph.py             # LangGraph graph definition
│   ├── prompts.py           # All agent system prompts
│   ├── run_analysis.py      # CLI entrypoint — called by Jenkins
│   ├── run_graph_update.py  # CLI entrypoint — writes results to Neo4j
│   ├── requirements.txt
│   └── README.md            # Deep dive into the AI architecture
│
├── terraform/
│   ├── bootstrap/
│   │   └── s3_dynamodb_creation/  # Phase 1: Create backend storage (local state)
│   ├── modules/
│   │   ├── vpc/                   # VPC, subnet, IGW, route table
│   │   ├── security-group/        # Firewall rules (SSH + HTTP)
│   │   └── ec2/                   # Compute instance + SSH key pair
│   └── environments/
│       ├── dev/                   # t3.micro — fast iteration
│       ├── staging/               # t3.small — mirrors prod shape
│       └── prod/                  # t3.small — isolated blast radius
│
├── ansible/
│   ├── inventory/
│   │   └── aws_ec2.yml        # Dynamic inventory (AWS EC2 plugin)
│   ├── roles/
│   │   ├── docker/            # Docker CE install + group config
│   │   └── nginx/             # Web server install + service
│   ├── playbooks/
│   │   ├── server_update.yml  # Flat playbook — apt update + base packages
│   │   └── install_services.yml # Role-based playbook — Docker + Nginx
│   └── ansible.cfg            # Remote user, SSH key, inventory, roles path
│
├── ci/
│   └── jenkins/
│       └── Jenkinsfile        # Full CI/CD pipeline definition
│
├── ssh-keys/                  # SSH keys (git-ignored — never committed)
├── .gitignore
├── CI_CD_TROUBLESHOOTING.md
└── README.md
```

---

## Stage 1: Infrastructure Foundation

### Phase 1 — Bootstrap: S3 + DynamoDB

Before any environment infrastructure could be created, we first had to solve a classic **"chicken and egg" problem**: Terraform needs an S3 bucket to store its state, but you need Terraform to create that S3 bucket.

**Solution:** A dedicated `bootstrap/` folder with its own Terraform configuration that runs **once** with local state, creates the shared backend, and is never run again.

| Resource | Purpose |
|---|---|
| **S3 Bucket** | Stores all `terraform.tfstate` files for every environment |
| **DynamoDB Table** | Handles state locking — prevents concurrent `apply` runs |

---

### Phase 2 — Reusable Terraform Modules

Instead of one monolithic `main.tf`, the infrastructure is broken into three **standalone, reusable modules** that any environment can consume.

| Module | What it creates |
|---|---|
| `vpc` | VPC, public subnet, internet gateway, route table |
| `security-group` | Firewall rules — SSH (port 22) and HTTP (port 80) |
| `ec2` | EC2 instance, SSH key pair, root EBS volume |

---

### Phase 3 — Isolated Environments

Three environments consume the shared modules by passing different variable values via `terraform.tfvars`.

| Environment | Instance Type | VPC CIDR | State Key |
|---|---|---|---|
| `dev` | `t3.micro` | `10.0.0.0/16` | `env/dev/terraform.tfstate` |
| `staging` | `t3.small` | `10.1.0.0/16` | `env/staging/terraform.tfstate` |
| `prod` | `t3.small` | `10.2.0.0/16` | `env/prod/terraform.tfstate` |

> **Why per-environment state files instead of Terraform Workspaces?**
> With separate backend keys, the only way to touch prod state is to be physically inside the `environments/prod/` directory. The isolation is **structural**, not reliant on the developer remembering which workspace is active.

---

### Phase 4 — Ansible: Configuration Management

With infrastructure provisioned by Terraform, Ansible handles server configuration via the dynamic AWS EC2 inventory plugin (`aws_ec2.yml`). Tags applied by Terraform (`Environment=dev`, `Role=web`) are used directly as Ansible inventory groups.

The master playbook is clean and declarative:

```yaml
# playbooks/install_services.yml
- name: Install Docker and Nginx using Roles
  hosts: role_web
  become: yes
  roles:
    - docker
    - nginx
```

---

## Stage 2: The AI Sentinel Gate

### How the AI Gate Works

The `AI Risk Gate` stage in Jenkins intercepts the pipeline between `plan` and `apply`:

1. **Ingestion:** The raw `tfplan.json` is parsed and filtered. No-op resources are dropped, and changes are tiered (`CRITICAL`, `HIGH`, `NORMAL`) based on resource type.
2. **Parallel Agent Analysis:** Four domain-expert AI agents analyze the plan simultaneously using **AWS Bedrock (Claude)** via **LangGraph**.
3. **Graph Enrichment:** Each agent queries the **Neo4j Knowledge Graph** for historical context — previous runs, past human approvals, and cost baselines.
4. **Synthesis:** A Synthesizer node aggregates all findings into a professionally formatted **Infrastructure Audit Report**.
5. **PDF Generation:** The report is converted to a PDF and emailed to the administrator.
6. **Human Gate:** Jenkins pauses and waits for the administrator to Approve or Abort from the email link.
7. **State Persistence:** Post-deployment, the result is written back to Neo4j, making the AI smarter on the next run.

| Agent | Domain | External Grounding |
|---|---|---|
| SecOps | Network & IAM security | `checkov`, `tfsec` |
| FinOps | Cloud cost deltas | `infracost` |
| Blast Radius | Destructive operations & downtime | Terraform action types |
| Integrity | Tag alignment & state drift | Ansible inventory, git diff |

*(For a deep dive into the AI architecture, see [agents/README.md](agents/README.md))*

---

## Jenkins Pipeline Stages

| Stage | What happens |
|---|---|
| `Terraform Validate` | Syntax check without connecting to AWS |
| `Terraform Plan (Dev)` | Full init, plan, and JSON export |
| `AI Risk Gate` | 4 AI agents analyze, Audit Report PDF emailed |
| `Manual Approval` | Admin reviews PDF and approves/rejects in Jenkins |
| `Deploy to Dev` | `terraform apply`, then Ansible via WSL |

---

## Key Design Decisions

| Decision | Why |
|---|---|
| AI is reviewer only | Keeps humans in control. The AI advises, never acts. |
| Per-environment state files over Workspaces | Structural isolation — impossible to accidentally apply to the wrong environment |
| Bootstrap folder with local state | Solves the "who creates the S3 bucket?" chicken-and-egg problem |
| LangGraph multi-agent architecture | Parallel specialized agents outperform a single monolithic prompt |
| Neo4j Knowledge Graph memory | Prevents alert fatigue — if a human approved a risk once, the AI remembers |
| PDF over Markdown for reports | Standardized, professional format suitable for audit trails |
| Dynamic EC2 inventory (aws_ec2.yml) | No hardcoded IPs — the pipeline discovers instances by their AWS Tags |
| WSL for Ansible on Windows Jenkins | Allows Linux-native Ansible tooling on a Windows-hosted Jenkins server |
| `curl` over `get_url` for Docker GPG | Bypassed a known Python/urllib SSL bug in the specific Ansible/urllib3 version |

---

## Roadmap

- [x] S3 + DynamoDB remote state backend
- [x] Reusable Terraform modules (VPC, SG, EC2)
- [x] Multi-environment isolation (dev/staging/prod)
- [x] Dynamic Ansible inventory via AWS EC2 plugin
- [x] Ansible roles for Docker CE and Nginx
- [x] Jenkins CI/CD Pipeline (validate → plan → gate → apply → configure)
- [x] LangGraph multi-agent AI Risk Gate
- [x] Neo4j Knowledge Graph memory core
- [x] Infrastructure Audit Report PDF generation and email delivery
- [ ] Staging and Production pipeline stages
- [ ] ALB module + multi-AZ VPC expansion
