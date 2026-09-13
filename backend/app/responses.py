"""API Gateway response formatting helpers."""

from __future__ import annotations

import json
from decimal import Decimal

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def decimal_serializer(value) -> int | float:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)

    raise TypeError(f"Cannot serialize type: {type(value).__name__}")


def response(status_code: int, body: dict | list) -> dict:
    return {
        "statusCode": status_code,
        "headers": dict(_BASE_HEADERS),
        "body": json.dumps(body, default=decimal_serializer),
    }
