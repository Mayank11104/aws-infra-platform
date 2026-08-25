# modules/security-group/outputs.tf

output "security_group_id" {
  description = "The ID of the created security group - needed by the ec2 and alb modules"
  value       = aws_security_group.this.id
}
