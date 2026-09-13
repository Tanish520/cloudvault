import json
from datetime import datetime
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

import files
from conftest import USER_A, with_auth
from files import ConflictError, confirm_upload, create_upload_url
from validation import ValidationError


def _upload_request(*, user_id=USER_A, **overrides):
    body = {
        "fileName": "resume.pdf",
        "contentType": "application/pdf",
        "size": 12,
    }
    body.update(overrides)
    return with_auth({"body": json.dumps(body)}, user_id=user_id)


def _stage_upload(
    aws, *, content=b"hello world!", user_id=USER_A, **overrides
):
    requested = json.loads(
        create_upload_url(_upload_request(user_id=user_id, **overrides))["body"]
    )
    aws["s3"].put_object(
        Bucket="cloudvault-test-bucket",
        Key=requested["s3Key"],
        Body=content,
        ContentType=overrides.get("contentType", "application/pdf"),
    )
    event_body = {
        "fileId": requested["fileId"],
        "fileName": overrides.get("fileName", "resume.pdf"),
        "s3Key": requested["s3Key"],
        "contentType": overrides.get("contentType", "application/pdf"),
        "size": overrides.get("size", len(content)),
    }
    return with_auth(
        {"body": json.dumps(event_body)}, user_id=user_id
    ), requested


def test_confirm_returns_created_file_without_status(aws):
    event, requested = _stage_upload(aws)

    result = confirm_upload(event)
    body = json.loads(result["body"])

    assert result["statusCode"] == 201
    assert body["message"] == "Upload confirmed"
    assert body["file"] == {
        "userId": USER_A,
        "fileId": requested["fileId"],
        "fileName": "resume.pdf",
        "s3Key": f"uploads/{USER_A}/{requested['fileId']}-resume.pdf",
        "contentType": "application/pdf",
        "size": 12,
        "uploadedAt": body["file"]["uploadedAt"],
    }
    assert body["file"]["uploadedAt"].endswith("+00:00")
    assert "status" not in body["file"]


def test_confirm_stores_uploads_key_in_response(aws):
    event, requested = _stage_upload(aws, fileName="../../new resume.pdf")

    item = json.loads(confirm_upload(event)["body"])["file"]

    assert item["s3Key"] == (
        f"uploads/{USER_A}/{requested['fileId']}-new_resume.pdf"
    )


def test_confirm_moves_object_and_removes_staging_copy(aws):
    content = b"hello world!"
    event, requested = _stage_upload(aws, content=content)

    item = json.loads(confirm_upload(event)["body"])["file"]

    promoted = aws["s3"].get_object(
        Bucket="cloudvault-test-bucket", Key=item["s3Key"]
    )
    assert promoted["Body"].read() == content
    with pytest.raises(ClientError) as excinfo:
        aws["s3"].head_object(
            Bucket="cloudvault-test-bucket", Key=requested["s3Key"]
        )
    assert excinfo.value.response["Error"]["Code"] == "404"


def test_confirm_writes_dynamodb_metadata_with_utc_time(aws):
    event, requested = _stage_upload(aws)

    confirm_upload(event)

    item = aws["table"].get_item(
        Key={"userId": USER_A, "fileId": requested["fileId"]}
    )["Item"]
    assert item == {
        "userId": USER_A,
        "fileId": requested["fileId"],
        "fileName": "resume.pdf",
        "s3Key": f"uploads/{USER_A}/{requested['fileId']}-resume.pdf",
        "contentType": "application/pdf",
        "size": 12,
        "uploadedAt": item["uploadedAt"],
    }
    uploaded_at = datetime.fromisoformat(item["uploadedAt"])
    assert uploaded_at.utcoffset().total_seconds() == 0


def test_confirm_rejects_when_staged_object_does_not_exist(aws):
    requested = json.loads(create_upload_url(_upload_request())["body"])
    event = with_auth(
        {
            "body": json.dumps(
                {
                    "fileId": requested["fileId"],
                    "fileName": "resume.pdf",
                    "s3Key": requested["s3Key"],
                    "contentType": "application/pdf",
                    "size": 12,
                }
            )
        }
    )

    with pytest.raises(ValidationError, match="^File was not uploaded to S3$"):
        confirm_upload(event)


def test_confirm_rejects_staging_key_for_another_file_id(aws):
    event, _ = _stage_upload(aws)
    body = json.loads(event["body"])
    body["fileId"] = "different-file-id"

    with pytest.raises(ValidationError, match="^Invalid S3 object key$"):
        confirm_upload(with_auth({"body": json.dumps(body)}))


def test_confirm_rejects_uuid_prefix_as_file_id(aws):
    event, requested = _stage_upload(aws)
    body = json.loads(event["body"])
    body["fileId"] = requested["fileId"].split("-", 1)[0]

    with pytest.raises(ValidationError, match="^Invalid S3 object key$"):
        confirm_upload(with_auth({"body": json.dumps(body)}))


def test_confirm_rejects_file_name_that_does_not_match_staging_key(aws):
    event, _ = _stage_upload(aws)
    body = json.loads(event["body"])
    body["fileName"] = "different.pdf"

    with pytest.raises(ValidationError, match="^Invalid S3 object key$"):
        confirm_upload(with_auth({"body": json.dumps(body)}))


def test_confirm_rejects_key_outside_staging(aws):
    event, _ = _stage_upload(aws)
    body = json.loads(event["body"])
    body["s3Key"] = f"uploads/{body['fileId']}-resume.pdf"

    with pytest.raises(ValidationError, match="^Invalid S3 object key$"):
        confirm_upload(with_auth({"body": json.dumps(body)}))


def test_confirm_rejects_another_users_staging_key(aws):
    event, _ = _stage_upload(aws, user_id=USER_A)
    stolen = json.loads(event["body"])

    with pytest.raises(ValidationError, match="^Invalid S3 object key$"):
        confirm_upload(
            with_auth({"body": json.dumps(stolen)}, user_id="user-b-sub")
        )


def test_same_file_id_can_exist_for_two_users(aws, monkeypatch):
    shared_id = "same-file-id"
    monkeypatch.setattr(files.uuid, "uuid4", lambda: shared_id)

    first_event, _ = _stage_upload(aws, user_id=USER_A)
    second_event, _ = _stage_upload(aws, user_id="user-b-sub")

    first = json.loads(confirm_upload(first_event)["body"])["file"]
    second = json.loads(confirm_upload(second_event)["body"])["file"]

    assert first["fileId"] == second["fileId"] == shared_id
    assert first["userId"] == USER_A
    assert second["userId"] == "user-b-sub"


def test_confirm_rejects_actual_size_mismatch(aws):
    event, _ = _stage_upload(aws, content=b"different length", size=12)

    with pytest.raises(
        ValidationError, match="^Uploaded file size does not match$"
    ):
        confirm_upload(event)


def test_confirm_replay_without_restaging_raises_conflict(aws):
    event, _ = _stage_upload(aws)
    confirm_upload(event)

    with pytest.raises(ConflictError, match="^File already confirmed$"):
        confirm_upload(event)


def test_confirm_precheck_uses_consistent_dynamodb_read(aws, monkeypatch):
    event, requested = _stage_upload(aws)
    table = Mock(wraps=aws["table"])
    monkeypatch.setattr(files, "_table", lambda: table)

    confirm_upload(event)

    table.get_item.assert_called_once_with(
        Key={"userId": USER_A, "fileId": requested["fileId"]},
        ConsistentRead=True,
    )


def test_confirm_rejects_missing_fields(aws):
    with pytest.raises(ValidationError, match="^Missing required field"):
        confirm_upload(
            with_auth({"body": json.dumps({"fileId": "some-id"})})
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("fileId", 123), ("s3Key", ["staging", "key"])],
)
def test_confirm_requires_string_file_id_and_s3_key(aws, field, value):
    event, _ = _stage_upload(aws)
    body = json.loads(event["body"])
    body[field] = value

    with pytest.raises(
        ValidationError, match="^fileId and s3Key must be strings$"
    ):
        confirm_upload(with_auth({"body": json.dumps(body)}))


def test_confirm_uses_s3_content_type_for_metadata(aws):
    event, requested = _stage_upload(aws)
    aws["s3"].copy_object(
        Bucket="cloudvault-test-bucket",
        Key=requested["s3Key"],
        CopySource={
            "Bucket": "cloudvault-test-bucket",
            "Key": requested["s3Key"],
        },
        ContentType="image/png",
        MetadataDirective="REPLACE",
    )

    item = json.loads(confirm_upload(event)["body"])["file"]

    assert item["contentType"] == "image/png"
