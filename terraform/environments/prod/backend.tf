# environments/prod/backend.tf
# Per-environment remote state: prod has its own isolated state file path.
# Even though the same S3 bucket and DynamoDB table are shared, the isolated key
# means a mistake in dev/staging cannot corrupt prod state.

terraform {
  backend "s3" {
    bucket         = "aws-infra-state-locking-bucket"
    key            = "env/prod/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "terraform-lock-table"
    encrypt        = true
  }
}
