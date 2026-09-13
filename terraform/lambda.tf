data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../backend/app"
  output_path = "${path.module}/lambda_function.zip"
  excludes    = ["__pycache__"]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}-backend"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "backend" {
  function_name = "${var.project_name}-backend"
  role          = aws_iam_role.lambda.arn
  runtime       = "python3.13"
  handler       = "handler.lambda_handler"

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  timeout     = 10
  memory_size = 256

  environment {
    variables = {
      BUCKET_NAME               = aws_s3_bucket.files.id
      TABLE_NAME                = aws_dynamodb_table.files.name
      PRESIGNED_URL_EXPIRY      = tostring(var.presigned_url_expiry)
      RATE_LIMIT_TABLE_NAME     = aws_dynamodb_table.rate_limits.name
      RATE_LIMIT_WINDOW_SECONDS = tostring(var.rate_limit_window_seconds)
      RATE_LIMIT_MAX_REQUESTS   = tostring(var.rate_limit_max_requests)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_logs,
    aws_iam_role_policy_attachment.lambda_access,
  ]
}
