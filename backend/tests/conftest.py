import sys
from pathlib import Path

import boto3
import pytest

# pytest prepends backend/app to sys.path, where the application's responses.py
# would otherwise shadow Moto's third-party ``responses`` dependency.
_APP_PATH = str(Path(__file__).parents[1] / "app")
_APP_PATH_INDEX = sys.path.index(_APP_PATH)
sys.path.pop(_APP_PATH_INDEX)
try:
    from moto import mock_aws
finally:
    sys.path.insert(_APP_PATH_INDEX, _APP_PATH)
    sys.modules.pop("responses", None)

REGION = "ap-south-1"
BUCKET = "cloudvault-test-bucket"
TABLE = "cloudvault-test-files"
RATE_LIMIT_TABLE = "cloudvault-test-rate-limits"
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"


def with_auth(event=None, *, user_id=USER_A):
    authenticated = dict(event or {})
    authenticated["requestContext"] = {
        "authorizer": {
            "jwt": {
                "claims": {
                    "sub": user_id,
                    "email": f"{user_id}@example.com",
                }
            }
        }
    }
    return authenticated


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Fake credentials so a bug can never reach real AWS, plus Lambda's env vars."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("BUCKET_NAME", BUCKET)
    monkeypatch.setenv("TABLE_NAME", TABLE)
    monkeypatch.setenv("PRESIGNED_URL_EXPIRY", "300")
    monkeypatch.setenv("RATE_LIMIT_TABLE_NAME", RATE_LIMIT_TABLE)
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "30")


@pytest.fixture
def aws(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "fileId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "fileId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        rate_limit_table = dynamodb.create_table(
            TableName=RATE_LIMIT_TABLE,
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "window", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "window", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield {"s3": s3, "table": table, "rate_limit_table": rate_limit_table}
