resource "aws_s3_bucket" "remote_backend_bucket" {
  bucket = var.remote_backend_bucket

  tags = {
    Name = var.remote_backend_bucket
  }
}

resource "aws_dynamodb_table" "remote_backend_table" {
  name         = var.remote_backend_table
  billing_mode = var.billing_mode
  hash_key     = var.hash_key

  attribute {
    name = var.hash_key
    type = "S"
  }

  tags = {
    Name = var.remote_backend_table
  }
}
