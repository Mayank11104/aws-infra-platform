# AWS Infrastructure Platform — Terraform + Ansible

A production-grade Infrastructure as Code (IaC) setup provisioning a fully isolated, multi-environment AWS infrastructure using **Terraform modules** and **Ansible** for configuration management. Designed to demonstrate real-world DevOps discipline — not just "it works," but *why* each decision was made.

---

## 📁 Project Structure

```
Ansible+terraform/
│
├── terraform/
│   ├── bootstrap/
│   │   └── s3_dyanamodb_creation/   # Phase 1: Create backend storage (local state)
│   │
│   ├── modules/
│   │   ├── vpc/                     # VPC, subnet, IGW, route table
│   │   ├── security-group/          # Firewall rules (SSH + HTTP)
│   │   └── ec2/                     # Compute instance + SSH key pair
│   │
│   └── environments/
│       ├── dev/                     # t3.micro — lowest cost, fast iteration
│       ├── staging/                 # t3.small — mirrors prod shape
│       └── prod/                    # t3.small — isolated blast radius
│
├── ansible/
│   ├── inventory/
│   │   ├── hosts.ini                # Static inventory (grouped by environment)
│   │   └── aws_ec2.yml              # Dynamic inventory plugin config (future)
│   ├── roles/
│   │   ├── common/                  # Base utilities (curl, vim, git, htop)
│   │   ├── nginx/                   # Web server install + service
│   │   └── docker/                  # Docker CE install + group config
│   ├── site.yml                     # Main playbook entry point
│   └── ansible.cfg                  # Default user, key, inventory path
│
├── ssh-keys/                        # SSH keys (git-ignored — never committed)
├── .gitignore
└── README.md
```

---

## 🏗️ What We Built

### Phase 1 — Bootstrap: S3 + DynamoDB

Before any environment infrastructure could be created, we first had to solve a classic **"chicken and egg" problem**: Terraform needs an S3 bucket to store its state, but you need Terraform to create that S3 bucket.

**Solution:** A dedicated `bootstrap/` folder with its own Terraform configuration that runs **once** with local state, creates the shared backend, and is never run again.

What the bootstrap creates:
- **S3 Bucket** — Stores all `terraform.tfstate` files for every environment securely in one place.
- **DynamoDB Table** — Handles state locking. When someone runs `terraform apply`, it writes a lock to this table so no other run can execute at the same time, preventing state corruption.

---

### Phase 2 — Reusable Terraform Modules

Instead of one monolithic `main.tf`, the infrastructure is broken into three **standalone, reusable modules** that any environment can consume.

| Module | What it creates |
|---|---|
| `vpc` | VPC, public subnet, internet gateway, route table |
| `security-group` | Firewall rules — SSH (port 22) and HTTP (port 80) open to `0.0.0.0/0` |
| `ec2` | EC2 instance, SSH key pair, root EBS volume |

Each module has its own `variables.tf` and `outputs.tf`. The outputs of one module feed directly into the inputs of the next. For example, the `vpc` module outputs a `subnet_id` which the `ec2` module receives as an input variable.

---

### Phase 3 — Isolated Environments

Three environments consume the shared modules by passing different variable values via `terraform.tfvars`.

| Environment | Instance Type | VPC CIDR | State Key |
|---|---|---|---|
| `dev` | `t3.micro` | `10.0.0.0/16` | `env/dev/terraform.tfstate` |
| `staging` | `t3.small` | `10.1.0.0/16` | `env/staging/terraform.tfstate` |
| `prod` | `t3.small` | `10.2.0.0/16` | `env/prod/terraform.tfstate` |

Each environment has its own `backend.tf` pointing to a separate S3 key. This means destroying `dev` cannot ever touch `prod`'s state file.

> **Why per-environment state files instead of Terraform Workspaces?**
> Workspaces are convenient, but a `terraform workspace select prod && terraform apply` typo is a single command away from disaster. With separate backend keys, the only way to touch prod state is to be physically inside the `environments/prod/` directory. The isolation is **structural**, not reliant on the developer remembering which workspace is active.

Each environment also outputs:
- `environment_name` — the name of the environment
- `ec2_public_ip` — the public IP of the provisioned instance
- `ec2_instance_type` — the instance type used

---

### Phase 4 — SSH Key Management

SSH keys are stored inside the project under `ssh-keys/` and are committed to **neither** Git nor any public location.

**`.gitignore` entries:**
```
ssh-keys/
*.pem
*.pub
```

#### The WSL/Windows Permission Problem (and the fix)

Using SSH keys stored on a Windows drive (`D:\`) from inside WSL required solving a real, non-trivial problem:

- **The conflict:** Linux SSH requires strict `chmod 400` permissions on private keys. By default, WSL treats files on Windows-mounted drives (like `/mnt/d/`) as world-readable (`-rwxrwxrwx`) because Windows NTFS doesn't natively speak Linux permission numbers. SSH sees this as insecure and refuses to use the key.

- **The fix:** WSL supports a `metadata` option that allows Linux permission commands to be stored inside a hidden field on Windows NTFS files. By creating `/etc/wsl.conf` with the following content and running `wsl --shutdown` to restart the engine, Linux permission commands like `chmod 400` work correctly on files inside `/mnt/d/`:

```ini
[automount]
options = "metadata"
```

After restarting WSL, running `chmod 400 ssh-keys/aws-infra-key` set the correct `-r--------` permissions, and SSH accepted the key without issue.

---

### Phase 5 — Ansible: Inventory + Roles

With the infrastructure fully provisioned by Terraform, Ansible takes over for configuration management.

#### Inventory

A static inventory (`inventory/hosts.ini`) organises the three environments into named groups:

```ini
[env_dev]
<dev-ip>

[env_staging]
<staging-ip>

[env_prod]
<prod-ip>

[role_web:children]
env_dev
env_staging
env_prod
```

This grouping allows you to target specific environments with ad-hoc commands:
```bash
ansible env_dev -m ping       # Ping only dev
ansible env_prod -m command -a "uptime"   # Check prod uptime
ansible all -m ping           # Ping all three environments
```

The `ansible.cfg` file configures the default settings globally so you don't need to pass flags on every command:
```ini
[defaults]
inventory = inventory/hosts.ini
remote_user = ubuntu
private_key_file = ../ssh-keys/aws-infra-key
host_key_checking = False
```


#### Ad-hoc Commands Verified ✅

The following ad-hoc commands were run and all three servers (dev, staging, prod) returned successful responses:

```bash
ansible all -m ping          # All three returned "pong"
ansible env_dev -m ping      # Targeted single environment ping
```

---

## 🚀 Roadmap

- [x] Create Ansible playbooks and roles for configuration management (Nginx, Docker)
- [ ] Jenkins CI/CD Pipeline — Terraform lint → plan → security scan → apply (with manual approval for prod)
- [ ] Integrate `checkov` and `ansible-lint` as hard security gates in the pipeline
- [ ] Expand VPC module to support 2 AZs and wire in the ALB module

---

## 🔑 Key Design Decisions

| Decision | Why |
|---|---|
| Per-environment state files over Workspaces | Structural isolation — impossible to accidentally apply to the wrong environment |
| Bootstrap folder with local state | Solves the "who creates the S3 bucket?" chicken-and-egg problem cleanly |
| SSH keys in `ssh-keys/` + `.gitignore` | Keeps secrets out of version control completely |
| WSL metadata for key permissions | Required to enforce strict Linux `chmod 400` on Windows-mounted NTFS drives |
| Static `hosts.ini` for Ansible inventory | Simpler to understand and debug while learning; dynamic inventory (`aws_ec2.yml`) prepared for CI/CD |
