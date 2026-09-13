"""Per-user request rate limiting backed by DynamoDB."""

import os
import time

import boto3

_TTL_BUFFER_SECONDS = 60


class RateLimitError(Exception):
    """User has exceeded their request budget. Maps to HTTP 429."""


def _table():
    # Built lazily, never at import, so moto can intercept it in tests.
    return boto3.resource("dynamodb").Table(os.environ["RATE_LIMIT_TABLE_NAME"])


def check_rate_limit(user_id: str) -> None:
    window_seconds = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
    max_requests = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "30"))

    now = int(time.time())
    window_start = now - (now % window_seconds)
    expires_at = window_start + window_seconds + _TTL_BUFFER_SECONDS

    # A single atomic ADD, so concurrent requests from the same user can never
    # undercount each other the way a read-then-write counter could.
    result = _table().update_item(
        Key={"userId": user_id, "window": str(window_start)},
        UpdateExpression="SET expiresAt = :expires_at ADD requestCount :incr",
        ExpressionAttributeValues={":incr": 1, ":expires_at": expires_at},
        ReturnValues="UPDATED_NEW",
    )

    if int(result["Attributes"]["requestCount"]) > max_requests:
        raise RateLimitError("Too many requests. Please slow down.")
