"""File operations. The only module that talks to AWS."""

import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from auth import authenticated_user_id
from rate_limit import check_rate_limit
from responses import response
from validation import (
    MAX_FILE_SIZE,
    ValidationError,
    parse_body,
    require_fields,
    sanitize_filename,
    validate_content_type,
    validate_size,
)

logger = logging.getLogger(__name__)

STAGING_PREFIX = "staging/"
UPLOADS_PREFIX = "uploads/"

S3_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class NotFoundError(Exception):
    """Requested file does not exist. Maps to HTTP 404."""


class ConflictError(Exception):
    """File already confirmed. Maps to HTTP 409."""


def _s3():
    # Built lazily, never at import, so moto can intercept it in tests.
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    endpoint_url = f"https://s3.{region}.amazonaws.com" if region else None
    return boto3.client("s3", region_name=region, endpoint_url=endpoint_url)


def _table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def _bucket() -> str:
    return os.environ["BUCKET_NAME"]


def _expiry() -> int:
    return int(os.environ.get("PRESIGNED_URL_EXPIRY", "300"))


def create_upload_url(event: dict) -> dict:
    user_id = authenticated_user_id(event)
    check_rate_limit(user_id)
    body = parse_body(event)
    require_fields(body, ["fileName", "contentType", "size"])

    file_name = sanitize_filename(body["fileName"])
    content_type = validate_content_type(body["contentType"])
    validate_size(body["size"])

    file_id = str(uuid.uuid4())
    staging_key = f"{STAGING_PREFIX}{user_id}/{file_id}-{file_name}"
    expiry = _expiry()

    presigned = _s3().generate_presigned_post(
        Bucket=_bucket(),
        Key=staging_key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_FILE_SIZE],
        ],
        ExpiresIn=expiry,
    )

    logger.info("Issued upload policy fileId=%s", file_id)

    return response(
        200,
        {
            "fileId": file_id,
            "fileName": file_name,
            "s3Key": staging_key,
            "uploadUrl": presigned["url"],
            "fields": presigned["fields"],
            "expiresIn": expiry,
        },
    )


def confirm_upload(event: dict) -> dict:
    user_id = authenticated_user_id(event)
    check_rate_limit(user_id)
    body = parse_body(event)
    require_fields(body, ["fileId", "fileName", "s3Key", "contentType", "size"])

    file_id = body["fileId"]
    staging_key = body["s3Key"]
    if not isinstance(file_id, str) or not isinstance(staging_key, str):
        raise ValidationError("fileId and s3Key must be strings")

    file_name = sanitize_filename(body["fileName"])
    validate_content_type(body["contentType"])
    declared_size = validate_size(body["size"])

    if staging_key != f"{STAGING_PREFIX}{user_id}/{file_id}-{file_name}":
        raise ValidationError("Invalid S3 object key")

    item_key = {"userId": user_id, "fileId": file_id}
    table = _table()
    if "Item" in table.get_item(Key=item_key, ConsistentRead=True):
        raise ConflictError("File already confirmed")

    s3 = _s3()
    try:
        uploaded = s3.head_object(Bucket=_bucket(), Key=staging_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in S3_NOT_FOUND_CODES:
            raise ValidationError("File was not uploaded to S3") from exc
        raise

    actual_size = uploaded["ContentLength"]
    if actual_size != declared_size:
        raise ValidationError("Uploaded file size does not match")

    destination_key = f"{UPLOADS_PREFIX}{user_id}/{file_id}-{file_name}"
    s3.copy_object(
        Bucket=_bucket(),
        Key=destination_key,
        CopySource={"Bucket": _bucket(), "Key": staging_key},
    )
    s3.delete_object(Bucket=_bucket(), Key=staging_key)

    item = {
        "userId": user_id,
        "fileId": file_id,
        "fileName": file_name,
        "s3Key": destination_key,
        "contentType": uploaded.get("ContentType", "application/octet-stream"),
        "size": actual_size,
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }

    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(userId) AND attribute_not_exists(fileId)"
            ),
        )
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            raise ConflictError("File already confirmed") from exc
        raise

    logger.info("Confirmed upload fileId=%s", file_id)

    return response(201, {"message": "Upload confirmed", "file": item})


def list_files(event: dict) -> dict:
    user_id = authenticated_user_id(event)
    check_rate_limit(user_id)
    table = _table()
    items = []
    query_arguments = {"KeyConditionExpression": Key("userId").eq(user_id)}

    while True:
        page = table.query(**query_arguments)
        items.extend(page.get("Items", []))

        last_evaluated_key = page.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        query_arguments = {
            "KeyConditionExpression": Key("userId").eq(user_id),
            "ExclusiveStartKey": last_evaluated_key,
        }

    items.sort(key=lambda item: item.get("uploadedAt", ""), reverse=True)
    return response(200, {"files": items})


def _path_file_id(event: dict) -> str:
    parameters = event.get("pathParameters") or {}
    file_id = parameters.get("fileId")

    if not file_id:
        raise ValidationError("fileId is required")

    return file_id


def _get_item_or_404(user_id: str, file_id: str) -> dict:
    item = _table().get_item(
        Key={"userId": user_id, "fileId": file_id}, ConsistentRead=True
    ).get("Item")

    if not item:
        raise NotFoundError("File not found")

    return item


def create_download_url(event: dict) -> dict:
    user_id = authenticated_user_id(event)
    check_rate_limit(user_id)
    file_id = _path_file_id(event)
    item = _get_item_or_404(user_id, file_id)
    expiry = _expiry()

    download_url = _s3().generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": _bucket(),
            "Key": item["s3Key"],
            "ResponseContentDisposition": (
                f'attachment; filename="{item["fileName"]}"'
            ),
        },
        ExpiresIn=expiry,
    )

    logger.info("Issued download URL fileId=%s", file_id)

    return response(200, {"downloadUrl": download_url, "expiresIn": expiry})


def delete_file(event: dict) -> dict:
    user_id = authenticated_user_id(event)
    check_rate_limit(user_id)
    file_id = _path_file_id(event)
    item = _get_item_or_404(user_id, file_id)

    # Object first: a failure after this leaves a visible, retryable row.
    # The reverse order would orphan an object that nothing ever sweeps.
    _s3().delete_object(Bucket=_bucket(), Key=item["s3Key"])

    try:
        _table().delete_item(
            Key={"userId": user_id, "fileId": file_id},
            ConditionExpression=(
                "attribute_exists(fileId) AND uploadedAt = :uploaded_at"
            ),
            ExpressionAttributeValues={
                ":uploaded_at": item["uploadedAt"]
            },
        )
    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            raise NotFoundError("File not found") from exc
        raise

    logger.info("Deleted fileId=%s", file_id)

    return response(200, {"message": "File deleted successfully"})
