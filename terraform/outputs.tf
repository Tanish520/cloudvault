output "api_url" {
  description = "CloudVault API base URL"
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "bucket_name" {
  description = "S3 bucket holding staged and confirmed files"
  value       = aws_s3_bucket.files.id
}

output "dynamodb_table_name" {
  description = "DynamoDB metadata table"
  value       = aws_dynamodb_table.files.name
}

output "rate_limit_table_name" {
  description = "DynamoDB per-user rate-limit counter table"
  value       = aws_dynamodb_table.rate_limits.name
}

output "lambda_function_name" {
  description = "Backend Lambda function"
  value       = aws_lambda_function.backend.function_name
}

output "cognito_user_pool_id" {
  description = "Cognito user pool for CloudVault users"
  value       = aws_cognito_user_pool.users.id
}

output "cognito_user_pool_client_id" {
  description = "Public Cognito SPA app-client ID"
  value       = aws_cognito_user_pool_client.frontend.id
}

output "cognito_domain" {
  description = "Cognito managed-login domain without a URL scheme"
  value = format(
    "%s.auth.%s.amazoncognito.com",
    aws_cognito_user_pool_domain.frontend.domain,
    var.aws_region,
  )
}

output "aws_region" {
  description = "AWS region used by CloudVault"
  value       = var.aws_region
}
