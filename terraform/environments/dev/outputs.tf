# environments/dev/outputs.tf

output "environment_name" {
  description = "The name of the environment"
  value       = var.environment
}

output "ec2_public_ip" {
  description = "The public IP address of the EC2 instance"
  value       = module.ec2.public_ip
}

output "ec2_instance_type" {
  description = "The instance type of the EC2 instance"
  value       = var.instance_type
}

