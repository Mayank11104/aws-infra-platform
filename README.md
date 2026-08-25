# Multi-Environment AWS Infrastructure Platform

This project demonstrates a production-grade Infrastructure as Code (IaC) setup using Terraform. It provisions an AWS infrastructure architecture across three isolated environments (`dev`, `staging`, `prod`) using reusable modules, remote state management, and strict security practices.

## 🏗️ Architecture & What We've Built So Far

### 1. Reusable Terraform Modules (`terraform/modules/`)
Instead of one monolithic `main.tf` file, the infrastructure is broken down into modular, reusable components. This demonstrates **composition and reuse**, ensuring the codebase is DRY (Don't Repeat Yourself).
*   **VPC Module:** Provisions the core network, public subnets, internet gateway, and route tables.
*   **Security Group Module:** Acts as a firewall, allowing controlled inbound SSH and HTTP traffic while managing outbound rules.
*   **EC2 Module:** Provisions the compute instances, automatically attaching them to the correct subnets and security groups while configuring SSH access via a generated Key Pair.

### 2. Isolated Environments (`terraform/environments/`)
We have three distinct environments that consume the shared modules by passing different variables (e.g., smaller instances for `dev`, larger for `prod`):
*   `dev` (`t3.micro`)
*   `staging` (`t3.small`)
*   `prod` (`t3.medium`)

> **Architectural Decision: Per-Environment State vs. Workspaces**
> I deliberately chose to use separate `backend.tf` files with distinct S3 keys (e.g., `env/prod/terraform.tfstate`) instead of relying solely on Terraform Workspaces. While workspaces are fine for lightweight separation, a `terraform workspace select prod` typo is a one-command path to disaster. Separate state paths reduce the blast radius and ensure true production isolation without relying on the developer remembering which workspace is currently active.

### 3. Remote State & Locking (`terraform/bootstrap/`)
A remote backend is essential for team collaboration and CI/CD pipelines. This project includes the "chicken and egg" bootstrap code that creates:
*   An **AWS S3 Bucket** (`aws-infra-state-locking-bucket`) to securely store the `terraform.tfstate` files.
*   An **AWS DynamoDB Table** (`terraform-lock-table`) to handle state locking, preventing concurrent infrastructure runs from corrupting the state.

### 4. Security & Best Practices
*   **SSH Key Management:** SSH keys are securely managed within a local `ssh-keys/` directory and explicitly ignored in `.gitignore` to prevent secret leaks into version control.
*   **No Hardcoded Secrets:** Environment-specific variables (like AMI IDs and CIDR blocks) are managed via `terraform.tfvars`.

## 🚀 Next Steps (Roadmap)
*   **Ansible Configuration:** Implement dynamic AWS inventory to automatically discover EC2 instances based on Terraform tags, and write roles for base hardening and application deployment (e.g., Nginx, Docker).
*   **Dual CI/CD Pipelines:** Build identical pipeline logic in both Jenkins and GitLab CI to enforce Git-based environment promotion (dev branch → dev environment, main branch → prod environment).
*   **Security Scanning:** Integrate `checkov` and `ansible-lint` as hard gates in the pipeline.
*   **AI Advisory Layer:** Implement scoped, read-only AI touchpoints to review Terraform plans and Ansible playbooks for risk analysis prior to execution.
