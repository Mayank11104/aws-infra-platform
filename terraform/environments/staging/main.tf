# environments/staging/main.tf
# Wires together the vpc, security-group, and ec2 modules for the staging environment.

module "vpc" {
  source = "../../modules/vpc"

  environment        = var.environment
  vpc_cidr           = var.vpc_cidr
  public_subnet_cidr = var.public_subnet_cidr
}

module "security_group" {
  source = "../../modules/security-group"

  environment      = var.environment
  vpc_id           = module.vpc.vpc_id
  ssh_allowed_cidr = var.ssh_allowed_cidr
}

module "ec2" {
  source = "../../modules/ec2"

  environment       = var.environment
  ami_id            = var.ami_id
  instance_type     = var.instance_type
  subnet_id         = module.vpc.public_subnet_id
  security_group_id = module.security_group.security_group_id
  public_key_path   = var.public_key_path
  root_volume_size  = var.root_volume_size
}
