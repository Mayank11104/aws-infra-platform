# environments/staging/backend.tf
# Per-environment remote state: staging has its own isolated state file path.

terraform {
  backend "s3" {
    bucket         = "aws-infra-state-locking-bucket"
    key            = "env/staging/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "terraform-lock-table"
    encrypt        = true
  }
}
