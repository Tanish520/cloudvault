import base64
import json

import pytest

from conftest import USER_A, USER_B, with_auth
from files import STAGING_PREFIX, create_upload_url
from rate_limit import RateLimitError
from validation import MAX_FILE_SIZE, ValidationError


def event_for(*, user_id=USER_A, **overrides):
    body = {
        "fileName": "resume.pdf",
        "contentType": "application/pdf",
        "size": 1024,
    }
    body.update(overrides)

    return with_auth({"body": json.dumps(body)}, user_id=user_id)


def test_returns_policy_for_valid_request(aws):
    result = create_upload_url(event_for())
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["fileName"] == "resume.pdf"
    assert body["s3Key"].startswith(
        f"{STAGING_PREFIX}{USER_A}/{body['fileId']}-"
    )
    assert body["s3Key"].endswith("-resume.pdf")
    assert body["expiresIn"] == 300
    assert body["uploadUrl"]
    assert "policy" in body["fields"]
    assert body["fields"]["Content-Type"] == "application/pdf"


def test_upload_url_uses_the_active_regional_s3_endpoint(aws):
    body = json.loads(create_upload_url(event_for())["body"])

    assert body["uploadUrl"].startswith("https://s3.ap-south-1.amazonaws.com/")


def test_policy_signs_exact_upload_constraints(aws):
    body = json.loads(create_upload_url(event_for())["body"])
    policy = json.loads(base64.b64decode(body["fields"]["policy"]))

    assert body["fields"]["key"] == body["s3Key"]
    assert {"key": body["s3Key"]} in policy["conditions"]
    assert {"Content-Type": "application/pdf"} in policy["conditions"]
    assert ["content-length-range", 1, MAX_FILE_SIZE] in policy["conditions"]


def test_key_targets_staging_not_uploads(aws):
    body = json.loads(create_upload_url(event_for())["body"])

    assert body["s3Key"].startswith("staging/")
    assert "uploads/" not in body["s3Key"]


def test_key_is_scoped_to_authenticated_user(aws):
    body = json.loads(create_upload_url(event_for(user_id=USER_A))["body"])

    assert body["s3Key"].startswith(f"staging/{USER_A}/{body['fileId']}-")


def test_same_filename_for_two_users_has_separate_prefixes(aws):
    first = json.loads(create_upload_url(event_for(user_id=USER_A))["body"])
    second = json.loads(create_upload_url(event_for(user_id=USER_B))["body"])

    assert first["s3Key"].startswith(f"staging/{USER_A}/")
    assert second["s3Key"].startswith(f"staging/{USER_B}/")


def test_each_request_gets_a_unique_file_id(aws):
    first = json.loads(create_upload_url(event_for())["body"])["fileId"]
    second = json.loads(create_upload_url(event_for())["body"])["fileId"]

    assert first != second


def test_filename_is_sanitized_in_response_and_key(aws):
    body = json.loads(
        create_upload_url(event_for(fileName="../../etc/pa ss.pdf"))["body"]
    )

    assert body["fileName"] == "pa_ss.pdf"
    assert ".." not in body["s3Key"]


def test_rejects_unsupported_content_type(aws):
    with pytest.raises(ValidationError):
        create_upload_url(event_for(contentType="application/x-msdownload"))


def test_rejects_oversized_request(aws):
    with pytest.raises(ValidationError):
        create_upload_url(event_for(size=MAX_FILE_SIZE + 1))


def test_rejects_missing_fields(aws):
    with pytest.raises(ValidationError):
        create_upload_url(
            with_auth({"body": json.dumps({"fileName": "a.pdf"})})
        )


def test_exceeding_the_rate_limit_is_rejected(aws, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "2")

    create_upload_url(event_for())
    create_upload_url(event_for())

    with pytest.raises(RateLimitError):
        create_upload_url(event_for())
