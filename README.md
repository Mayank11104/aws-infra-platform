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
│   │   ├── hosts.ini                # Static inventory (grouped by environment, git-ignored)
│   │   └── aws_ec2.yml              # Dynamic inventory plugin config (future)
│   ├── roles/
│   │   ├── docker/                  # Docker CE install + group config
│   │   └── nginx/                   # Web server install + service
│   ├── playbooks/
│   │   ├── server_update.yml        # Flat playbook — apt update + base packages
│   │   └── install_services.yml     # Role-based playbook — Docker + Nginx
│   └── ansible.cfg                  # Default user, key, inventory + roles path
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

### Phase 5 — Ansible: From Ad-hoc to Roles

With the infrastructure fully provisioned by Terraform, Ansible takes over for configuration management. The entire Ansible journey is documented here — from the very first ping to fully automated role-based deployments.

#### Step 1: Inventory Setup

We started by configuring a static inventory (`inventory/hosts.ini`) that organises all three environments into named groups:

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

The `[role_web:children]` group is a **"Group of Groups"** — a parent group that automatically inherits all the IPs from the child groups. This means if any IP changes, it only needs to be updated in one place.

The `ansible.cfg` file was configured so Ansible always uses the project-local inventory, not the system-wide `/etc/ansible/hosts`:

```ini
[defaults]
inventory  = inventory/hosts.ini
remote_user = ubuntu
private_key_file = ../ssh-keys/aws-infra-key
host_key_checking = False
roles_path = roles
```

> **Why a project-local inventory instead of `/etc/ansible/hosts`?**
> The default system file cannot be committed to Git and shared with teammates. Storing the inventory inside the project folder means anyone who clones the repository gets a fully working Ansible setup immediately.

> **Why `roles_path = roles`?**
> When playbooks live inside a `playbooks/` subfolder, Ansible looks for roles relative to that playbook's location (i.e., `playbooks/roles/`). Setting `roles_path = roles` in `ansible.cfg` tells Ansible to always look in the top-level `ansible/roles/` folder, regardless of where the playbook file lives.

#### Step 2: Ad-hoc Commands (Learning and Verification)

Before writing any playbooks, ad-hoc commands were used to verify connectivity and understand how Ansible modules work directly from the terminal:

```bash
ansible all -m ping           # Verified SSH connectivity to all 3 servers
ansible env_dev -m ping       # Targeted a single environment
ansible all -m command -a "uptime"   # Checked server uptime
```

All three environments (dev, staging, prod) returned `pong` successfully.

---

#### Step 3: First Playbook — Flat Structure

The first playbook was written intentionally as a **flat playbook** (no roles) to understand the raw mechanics before adding any abstraction. The goal was simple: update all servers and install basic utilities.

**`playbooks/server_update.yml`:**
```yaml
- name: Server Initialisation and Update
  hosts: all
  become: yes

  tasks:
    - name: Update apt cache and upgrade all packages
      apt:
        update_cache: yes
        upgrade: dist
        cache_valid_time: 3600

    - name: Install basic troubleshooting utilities
      apt:
        name:
          - curl
          - git
          - vim
          - htop
        state: present
```

**Result:**
```
TASK [Update apt cache and upgrade all packages] → changed (on all 3)
TASK [Install basic troubleshooting utilities]  → ok (already installed — idempotency in action)
```

**Key lesson observed:** The `ok` status on the utilities task demonstrated **idempotency** — Ansible checked the servers, found the packages were already installed (pre-installed on the Ubuntu AMI), and did nothing rather than wastefully reinstalling them.

---

#### Step 4: Moving to Roles (Production Structure)

After understanding flat playbooks, we rebuilt the Ansible configuration using **Roles** — the industry-standard approach for organising reusable configuration code.

**Why Roles over multiple flat playbooks?**

| Multiple Flat Playbooks | Roles |
|---|---|
| Run 10 `ansible-playbook` commands to set up a new server | One master playbook, one command |
| Code is scattered across many files | All Docker code lives in `roles/docker/`, all Nginx code in `roles/nginx/` |
| Can't be shared or reused across projects | Copy the `roles/docker/` folder to any new project and it works instantly |
| No access to Ansible Galaxy | Can install community roles: `ansible-galaxy install geerlingguy.docker` |

The master playbook became beautifully clean:

**`playbooks/install_services.yml`:**
```yaml
- name: Install Docker and Nginx using Roles
  hosts: role_web
  become: yes
  roles:
    - docker
    - nginx
```

---

#### Step 5: Errors Faced and How They Were Fixed

The path to a working deployment involved four real-world bugs, each teaching a valuable lesson.

---

**Bug #0 — SSH Key Permission Denied on WSL**

```
Warning: Unprotected private key file!
Permissions 0777 for 'ssh-keys/aws-infra-key' are too open.
It is required that your private key files are NOT accessible by others.
```

**Root Cause:** The SSH private key was stored on the Windows drive (`D:\`) and accessed from WSL via `/mnt/d/`. By default, WSL mounts Windows drives without Linux permission metadata — every file appears as `-rwxrwxrwx` (world-readable, 777). Linux SSH refuses to use any key that isn't strictly locked down to the owner only (`chmod 400`). Running `chmod 400` appeared to work but the permissions would revert, because the NTFS filesystem had nowhere to store Linux permission bits.

**Fix:** WSL supports a `metadata` mount option that stores Linux permission data inside a hidden NTFS extended attribute on each file. Adding the following to `/etc/wsl.conf` and restarting WSL with `wsl --shutdown` enabled `chmod` to work permanently on Windows-mounted drives:

```ini
[automount]
options = "metadata"
```

After restart, `chmod 400 ssh-keys/aws-infra-key` set the correct `-r--------` permissions and SSH accepted the key without issue.

---

**Bug #1 — Role Not Found**

```
ERROR! the role 'docker' was not found in
/ansible/playbooks/roles:/home/spydy/.ansible/roles...
```

**Root Cause:** Because we organised playbooks inside a `playbooks/` subfolder, Ansible searched for roles at `playbooks/roles/docker` instead of `ansible/roles/docker`.

**Fix:** Added `roles_path = roles` to `ansible.cfg`. This tells Ansible to always resolve roles relative to the `ansible.cfg` file's location, not the playbook's location.

---

**Bug #2 — `apt-key` Not Found**

```
FAILED! => {"msg": "Failed to find required executable 'apt-key'..."}
```

**Root Cause:** The original Docker role used `apt_key` (the old way to add GPG keys). Modern Ubuntu (22.04+) has completely removed the `apt-key` command as it was deemed insecure. Our EC2 instances were running a newer Ubuntu AMI, so the command no longer existed on the server.

**Fix:** Replaced `apt_key` with the modern **keyring approach** — creating a secure `/etc/apt/keyrings/` directory and downloading Docker's GPG key directly into it:

```yaml
- name: Create directory for Docker GPG key
  file:
    path: /etc/apt/keyrings
    state: directory
    mode: '0755'

- name: Add Docker official GPG key using curl
  command: curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  args:
    creates: /etc/apt/keyrings/docker.asc
```

The repository string was also updated to reference the new key location:
```
deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://...
```

---

**Bug #3 — `get_url` Python SSL Bug**

```
FAILED! => {"msg": "An unknown error occurred: 'CustomHTTPSConnection'
object has no attribute 'cert_file'"}
```

**Root Cause:** The `get_url` Ansible module internally uses Python's `urllib` library to make HTTPS requests. There is a known compatibility bug between a specific version of Python and certain versions of the `urllib3` library where the `cert_file` attribute is missing. This is an Ansible control-node bug — nothing wrong with the servers or our code.

**Fix:** Bypassed the buggy Ansible module entirely and used the `command` module to run raw `curl` instead. The `creates:` argument was added to keep the task idempotent (skip if the file already exists):

```yaml
- name: Add Docker official GPG key using curl (bypassing Ansible Python bug)
  command: curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  args:
    creates: /etc/apt/keyrings/docker.asc
```

---

#### Final Result ✅

After all fixes, the playbook ran cleanly across all three environments:

```
PLAY RECAP
13.203.73.243  : ok=11  changed=5  unreachable=0  failed=0
13.232.74.58   : ok=11  changed=5  unreachable=0  failed=0
15.206.189.32  : ok=11  changed=5  unreachable=0  failed=0
```

All three servers now have:
- ✅ Docker CE (latest) installed and running
- ✅ Docker service enabled on boot
- ✅ `ubuntu` user added to the `docker` group
- ✅ Nginx installed and running (verified by opening the server's IP in a browser)

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
| Flat playbook first, then roles | Understanding raw task execution before adding abstraction layers builds real knowledge |
| `docker-ce` over `docker.io` | `docker-ce` is the latest, official Docker release; `docker.io` is maintained by Ubuntu and lags months behind |
| `curl` over `get_url` for GPG key | Bypassed a known Python/urllib SSL bug in the specific Ansible version on the control node |
| `roles_path` in `ansible.cfg` | Prevents role resolution errors when playbooks are organised inside a `playbooks/` subfolder |
