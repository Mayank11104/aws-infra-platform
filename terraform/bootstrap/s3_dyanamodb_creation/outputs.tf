output "s3_bucket_name" {
  description = "Name of the S3 bucket created for remote backend"
  value       = aws_s3_bucket.remote_backend_bucket.bucket
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB table created for state locking"
  value       = aws_dynamodb_table.remote_backend_table.name
}
