# environments/staging/variables.tf

variable "environment" {
  description = "Environment name"
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
}

variable "public_subnet_cidr" {
  description = "CIDR block for the public subnet"
  type        = string
}

variable "ami_id" {
  description = "AMI ID for the EC2 instance"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
}

variable "root_volume_size" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 15
}

variable "ssh_allowed_cidr" {
  description = "CIDR blocks allowed to SSH into the EC2 instance"
  type        = list(string)
}

variable "public_key_path" {
  description = "Path to the SSH public key file"
  type        = string
}
