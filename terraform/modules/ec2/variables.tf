# modules/ec2/variables.tf

variable "environment" {
  description = "Environment name (dev, staging, prod) - used for tagging"
  type        = string
}

variable "ami_id" {
  description = "AMI ID to launch (e.g. an Ubuntu 22.04 AMI for your region)"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type (e.g. t3.micro)"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID to launch the instance in (comes from the vpc module's output)"
  type        = string
}

variable "security_group_id" {
  description = "Security group ID to attach (comes from the security-group module's output)"
  type        = string
}

variable "public_key_path" {
  description = "Local path to your SSH public key file (e.g. ~/.ssh/aws-infra-key.pub). Leave empty and use public_key instead if you prefer passing the key content directly."
  type        = string
  default     = ""
}

variable "public_key" {
  description = "SSH public key content directly, used only if public_key_path is empty"
  type        = string
  default     = ""
}

variable "root_volume_size" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 15
}
