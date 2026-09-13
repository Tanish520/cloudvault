import json
from unittest.mock import Mock, call
from urllib.parse import unquote

import pytest
from botocore.exceptions import ClientError

import files
from conftest import BUCKET, USER_A, USER_B, with_auth
from files import NotFoundError, create_download_url, delete_file
from validation import ValidationError


def stored_file(aws, file_id="abc123", user_id=USER_A):
    key = f"uploads/{user_id}/{file_id}-report.pdf"
    aws["s3"].put_object(Bucket=BUCKET, Key=key, Body=b"pdf-bytes")
    aws["table"].put_item(
        Item={
            "userId": user_id,
            "fileId": file_id,
            "fileName": "report.pdf",
            "s3Key": key,
            "contentType": "application/pdf",
            "size": 9,
            "uploadedAt": "2026-08-01T00:00:00+00:00",
        }
    )
    return key


def event_for(file_id, user_id=USER_A):
    return with_auth(
        {"pathParameters": {"fileId": file_id}}, user_id=user_id
    )


def test_file_lookup_uses_a_consistent_read(aws, monkeypatch):
    stored_file(aws)
    table = Mock(wraps=aws["table"])
    monkeypatch.setattr(files, "_table", lambda: table)

    files._get_item_or_404(USER_A, "abc123")

    table.get_item.assert_called_once_with(
        Key={"userId": USER_A, "fileId": "abc123"}, ConsistentRead=True
    )


def test_download_returns_a_presigned_url_with_configured_expiry(aws, monkeypatch):
    stored_file(aws)
    monkeypatch.setenv("PRESIGNED_URL_EXPIRY", "120")

    result = create_download_url(event_for("abc123"))
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["expiresIn"] == 120
    assert "X-Amz-Signature" in body["downloadUrl"]


def test_download_url_forces_the_original_filename(aws):
    stored_file(aws)

    url = json.loads(create_download_url(event_for("abc123"))["body"])[
        "downloadUrl"
    ]

    assert 'attachment; filename="report.pdf"' in unquote(url)


def test_download_unknown_file_raises_not_found(aws):
    with pytest.raises(NotFoundError, match="^File not found$"):
        create_download_url(event_for("does-not-exist"))


@pytest.mark.parametrize(
    "event", [with_auth({"pathParameters": {}}), with_auth({})]
)
def test_download_requires_a_file_id(aws, event):
    with pytest.raises(ValidationError, match="^fileId is required$"):
        create_download_url(event)


def test_delete_removes_object_and_row(aws):
    key = stored_file(aws)

    result = delete_file(event_for("abc123"))

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {
        "message": "File deleted successfully"
    }
    assert "Item" not in aws["table"].get_item(
        Key={"userId": USER_A, "fileId": "abc123"}
    )

    with pytest.raises(ClientError):
        aws["s3"].head_object(Bucket=BUCKET, Key=key)


def test_delete_unknown_file_raises_not_found(aws):
    with pytest.raises(NotFoundError, match="^File not found$"):
        delete_file(event_for("does-not-exist"))


def test_delete_requires_a_file_id_when_path_parameters_are_none(aws):
    with pytest.raises(ValidationError, match="^fileId is required$"):
        delete_file(with_auth({"pathParameters": None}))


def test_download_cannot_read_another_users_file(aws):
    stored_file(aws, user_id=USER_A)

    with pytest.raises(NotFoundError, match="^File not found$"):
        create_download_url(event_for("abc123", user_id=USER_B))


def test_delete_cannot_remove_another_users_file(aws):
    key = stored_file(aws, user_id=USER_A)

    with pytest.raises(NotFoundError, match="^File not found$"):
        delete_file(event_for("abc123", user_id=USER_B))

    assert aws["s3"].head_object(Bucket=BUCKET, Key=key)["ContentLength"] == 9
    assert "Item" in aws["table"].get_item(
        Key={"userId": USER_A, "fileId": "abc123"}
    )


def test_delete_removes_s3_object_before_conditional_row_delete(monkeypatch):
    operations = Mock()
    s3 = Mock()
    table = Mock()
    s3.delete_object.side_effect = lambda **kwargs: operations("s3", kwargs)
    table.delete_item.side_effect = lambda **kwargs: operations("ddb", kwargs)
    monkeypatch.setattr(
        files,
        "_get_item_or_404",
        lambda user_id, file_id: {
            "userId": user_id,
            "fileId": file_id,
            "s3Key": f"uploads/{user_id}/{file_id}-report.pdf",
            "uploadedAt": "2026-08-01T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(files, "_s3", lambda: s3)
    monkeypatch.setattr(files, "_table", lambda: table)
    monkeypatch.setattr(files, "_bucket", lambda: BUCKET)
    monkeypatch.setattr(files, "check_rate_limit", lambda user_id: None)

    delete_file(event_for("abc123"))

    assert operations.call_args_list == [
        call(
            "s3",
            {
                "Bucket": BUCKET,
                "Key": f"uploads/{USER_A}/abc123-report.pdf",
            },
        ),
        call(
            "ddb",
            {
                "Key": {"userId": USER_A, "fileId": "abc123"},
                "ConditionExpression": (
                    "attribute_exists(fileId) AND uploadedAt = :uploaded_at"
                ),
                "ExpressionAttributeValues": {
                    ":uploaded_at": "2026-08-01T00:00:00+00:00"
                },
            },
        ),
    ]


def test_delete_does_not_remove_a_replacement_row(aws, monkeypatch):
    old_key = stored_file(aws)
    replacement = {
        "userId": USER_A,
        "fileId": "abc123",
        "fileName": "replacement.pdf",
        "s3Key": f"uploads/{USER_A}/abc123-replacement.pdf",
        "contentType": "application/pdf",
        "size": 11,
        "uploadedAt": "2026-08-02T00:00:00+00:00",
    }
    s3 = Mock(wraps=aws["s3"])

    def replace_row_then_delete_object(**kwargs):
        aws["table"].put_item(Item=replacement)
        return aws["s3"].delete_object(**kwargs)

    s3.delete_object.side_effect = replace_row_then_delete_object
    monkeypatch.setattr(files, "_s3", lambda: s3)

    with pytest.raises(NotFoundError, match="^File not found$"):
        delete_file(event_for("abc123"))

    assert aws["table"].get_item(
        Key={"userId": USER_A, "fileId": "abc123"}
    )["Item"] == replacement
    with pytest.raises(ClientError):
        aws["s3"].head_object(Bucket=BUCKET, Key=old_key)


def test_delete_maps_conditional_failure_to_not_found(aws, monkeypatch):
    stored_file(aws)
    table = Mock(wraps=aws["table"])
    table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}},
        "DeleteItem",
    )
    monkeypatch.setattr(files, "_table", lambda: table)

    with pytest.raises(NotFoundError, match="^File not found$"):
        delete_file(event_for("abc123"))


def test_delete_reraises_other_dynamodb_client_errors(aws, monkeypatch):
    stored_file(aws)
    original = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
        "DeleteItem",
    )
    table = Mock(wraps=aws["table"])
    table.delete_item.side_effect = original
    monkeypatch.setattr(files, "_table", lambda: table)

    with pytest.raises(ClientError) as excinfo:
        delete_file(event_for("abc123"))

    assert excinfo.value is original
    assert aws["table"].get_item(
        Key={"userId": USER_A, "fileId": "abc123"}
    )["Item"]


def test_s3_delete_failure_prevents_row_delete(aws, monkeypatch):
    stored_file(aws)
    s3_error = ClientError(
        {"Error": {"Code": "ServiceUnavailable"}},
        "DeleteObject",
    )
    s3 = Mock()
    s3.delete_object.side_effect = s3_error
    table = Mock(wraps=aws["table"])
    monkeypatch.setattr(files, "_s3", lambda: s3)
    monkeypatch.setattr(files, "_table", lambda: table)

    with pytest.raises(ClientError) as excinfo:
        delete_file(event_for("abc123"))

    assert excinfo.value is s3_error
    table.delete_item.assert_not_called()
    assert aws["table"].get_item(
        Key={"userId": USER_A, "fileId": "abc123"}
    )["Item"]
