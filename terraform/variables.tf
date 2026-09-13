variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Name prefix applied to every resource"
  type        = string
  default     = "cloudvault"
}

variable "frontend_origin" {
  description = "Single origin allowed by API Gateway and S3 CORS"
  type        = string
  default     = "http://localhost:5173"
}

variable "presigned_url_expiry" {
  description = "Lifetime in seconds of presigned upload policies and download URLs"
  type        = number
  default     = 300
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the backend Lambda"
  type        = number
  default     = 7
}

variable "rate_limit_window_seconds" {
  description = "Length of each per-user rate-limit window"
  type        = number
  default     = 60
}

variable "rate_limit_max_requests" {
  description = "Maximum requests a single authenticated user may make per rate-limit window"
  type        = number
  default     = 30
}

variable "frontend_callback_url" {
  description = "Exact OAuth callback URL for the local frontend"
  type        = string
  default     = "http://localhost:5173/"
}

variable "frontend_logout_url" {
  description = "Exact OAuth logout URL for the local frontend"
  type        = string
  default     = "http://localhost:5173/"
}
