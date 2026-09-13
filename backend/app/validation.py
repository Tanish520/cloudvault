"""Pure input validation. No AWS, no I/O -- everything here is unit-testable."""

import base64
import json
import os
import re

MAX_FILE_SIZE = 10 * 1024 * 1024

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "text/plain",
    }
)

MAX_FILENAME_LENGTH = 255

_UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")


class ValidationError(Exception):
    """Client input is bad. Maps to HTTP 400."""


def parse_body(event: dict) -> dict:
    body = event.get("body")

    if not body:
        return {}

    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValidationError("Request body is not valid base64 UTF-8") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValidationError("Request body must contain valid JSON") from exc

    if not isinstance(parsed, dict):
        raise ValidationError("Request body must be a JSON object")

    return parsed


def sanitize_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise ValidationError("fileName must be a string")

    cleaned = os.path.basename(filename.strip())
    cleaned = _UNSAFE_CHARACTERS.sub("_", cleaned)
    cleaned = cleaned.lstrip(".")[:MAX_FILENAME_LENGTH]

    if not cleaned:
        raise ValidationError("Invalid file name")

    return cleaned


def validate_content_type(content_type: str) -> str:
    if not isinstance(content_type, str) or content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationError("Unsupported file type")

    return content_type


def validate_size(size: int) -> int:
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValidationError("Invalid file size")

    if size <= 0:
        raise ValidationError("Invalid file size")

    if size > MAX_FILE_SIZE:
        raise ValidationError("Maximum file size is 10 MB")

    return size


def require_fields(body: dict, fields: list[str]) -> None:
    missing = [field for field in fields if body.get(field) is None]

    if missing:
        raise ValidationError(f"Missing required field(s): {', '.join(missing)}")
