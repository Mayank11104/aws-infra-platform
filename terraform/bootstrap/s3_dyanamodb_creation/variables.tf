#bucket-name
variable "remote_backend_bucket" {
  description = "Name of the S3 bucket for storing Terraform state"
  type        = string
  default     = "aws-infra-state-locking-bucket"
}

#table-name
variable "remote_backend_table" {
  description = "Name of the DynamoDB table for state locking"
  type        = string
  default     = "terraform-lock-table"
}

#billing mode
variable "billing_mode" {
  description = "Dynamodb billing mode"
  type        = string
  default     = "PAY_PER_REQUEST"
}

#hash-key
variable "hash_key" {
  description = "Dynamodb hash key"
  type        = string
  default     = "LockID"
}
