# environments/dev/backend.tf
# Per-environment remote state: each env gets its own state file path.
# This avoids workspace-switching mistakes (terraform workspace select prod is one command away from disaster).
# All three environments share the same S3 bucket and DynamoDB lock table, but use isolated state file keys.

terraform {
  backend "s3" {
    bucket         = "aws-infra-state-locking-bucket"
    key            = "env/dev/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "terraform-lock-table"
    encrypt        = true
  }
}
