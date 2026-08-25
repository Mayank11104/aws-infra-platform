# modules/security-group/variables.tf

variable "environment" {
  description = "Environment name (dev, staging, prod) - used for tagging"
  type        = string
}

variable "vpc_id" {
  description = "ID of the VPC this security group belongs to (comes from the vpc module's output)"
  type        = string
}

variable "ssh_allowed_cidr" {
  description = "CIDR block(s) allowed to SSH in - restrict this to your own IP, not 0.0.0.0/0"
  type        = list(string)
}
