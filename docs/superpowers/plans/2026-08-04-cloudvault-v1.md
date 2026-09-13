# CloudVault v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a serverless file-management app (upload, list, download, delete) where file bytes travel directly between the browser and S3, with Lambda issuing time-boxed presigned grants and recording metadata in DynamoDB.

**Architecture:** API Gateway HTTP API → single Python Lambda → S3 + DynamoDB, all provisioned by Terraform. Uploads use a presigned POST policy carrying a `content-length-range` condition, landing in a `staging/` prefix that a lifecycle rule sweeps daily; a confirm call verifies the object with `head_object` and server-side-copies it to `uploads/` before writing metadata. Backend logic is split into four small modules so pure validation is testable without AWS mocks.

**Tech Stack:** Python 3.13, boto3, pytest + moto 5, Terraform ≥ 1.8 (AWS provider v5), React 18 + Vite, AWS region `ap-south-1`.

**Spec:** `docs/superpowers/specs/2026-08-04-cloudvault-design.md`

## Global Constraints

- Region is `ap-south-1` everywhere. Never hardcode an account ID.
- `MAX_FILE_SIZE` = `10 * 1024 * 1024` (10485760 bytes). Enforced in Lambda *and* in the S3 POST policy.
- Allowed content types, exactly: `application/pdf`, `image/png`, `image/jpeg`, `text/plain`.
- Presigned grants expire in 300 seconds (`PRESIGNED_URL_EXPIRY`, both upload policy and download URL).
- S3 prefixes: unconfirmed objects live under `staging/`, confirmed under `uploads/`. Never blanket-expire `uploads/`.
- DynamoDB items have exactly these attributes: `fileId`, `fileName`, `s3Key`, `contentType`, `size`, `uploadedAt`. There is **no** `status` field.
- The stored `s3Key` is always the `uploads/` key, never the `staging/` key.
- `contentType` and `size` written to DynamoDB come from `head_object`, never from the client's claim.
- Status codes: validation → 400, missing item → 404, replayed confirm → **409**, AWS failure → 500. A duplicate confirm must never return 500.
- Never log presigned URLs, POST policies, or file contents. Log `fileId` and operation only.
- Lambda modules import each other as **top-level** modules (`from validation import ...`), because Terraform zips the *contents* of `backend/app/` to the archive root.
- boto3 clients are constructed lazily inside functions, never at module import, so moto can intercept them.
- v1 has **no authentication**. Run `terraform destroy` at the end of every working session.

---

### Task 1: Backend scaffolding and the validation module

Sets up the Python project and delivers the pure-function validation layer. No AWS involved.

**Files:**
- Create: `.gitignore`
- Create: `backend/requirements-dev.txt`
- Create: `pytest.ini`
- Create: `backend/app/validation.py`
- Test: `backend/tests/test_validation.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `MAX_FILE_SIZE: int`, `ALLOWED_CONTENT_TYPES: frozenset[str]`
  - `class ValidationError(Exception)`
  - `parse_body(event: dict) -> dict`
  - `sanitize_filename(filename: str) -> str`
  - `validate_content_type(content_type: str) -> str`
  - `validate_size(size: int) -> int`
  - `require_fields(body: dict, fields: list[str]) -> None`

- [ ] **Step 1: Create `.gitignore`**

```gitignore
# Terraform
.terraform/
*.tfstate
*.tfstate.*
terraform.tfvars
crash.log

# Lambda packages
*.zip

# Frontend
frontend/node_modules/
frontend/dist/
frontend/.env

# Python
__pycache__/
*.pyc
.venv/
.pytest_cache/

# OS and editor
.DS_Store
.vscode/
```

`.terraform.lock.hcl` is intentionally NOT ignored — it gets committed.

- [ ] **Step 2: Create `backend/requirements-dev.txt`**

```text
boto3>=1.34
moto[s3,dynamodb]>=5.0
pytest>=8.0
```

- [ ] **Step 3: Create `pytest.ini` at the repo root**

```ini
[pytest]
pythonpath = backend/app
testpaths = backend/tests
```

`pythonpath` is what lets tests do `from validation import ...` exactly the way the Lambda will.

- [ ] **Step 4: Create the virtualenv and install**

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r backend/requirements-dev.txt
```

- [ ] **Step 5: Write the failing tests**

Create `backend/tests/test_validation.py`:

```python
import base64
import json

import pytest

from validation import (
    MAX_FILE_SIZE,
    ValidationError,
    parse_body,
    require_fields,
    sanitize_filename,
    validate_content_type,
    validate_size,
)


def test_parse_body_reads_plain_json():
    assert parse_body({"body": '{"a": 1}'}) == {"a": 1}


def test_parse_body_returns_empty_dict_when_absent():
    assert parse_body({}) == {}
    assert parse_body({"body": ""}) == {}


def test_parse_body_decodes_base64():
    encoded = base64.b64encode(json.dumps({"a": 1}).encode()).decode()
    event = {"body": encoded, "isBase64Encoded": True}
    assert parse_body(event) == {"a": 1}


def test_parse_body_rejects_malformed_json():
    with pytest.raises(ValidationError):
        parse_body({"body": "{not json"})


def test_parse_body_rejects_non_object_json():
    with pytest.raises(ValidationError):
        parse_body({"body": "[1, 2, 3]"})


def test_sanitize_filename_strips_directory_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"


def test_sanitize_filename_replaces_unsafe_characters():
    assert sanitize_filename("my report (final).pdf") == "my_report__final_.pdf"


def test_sanitize_filename_keeps_safe_characters():
    assert sanitize_filename("resume-v2.final.pdf") == "resume-v2.final.pdf"


def test_sanitize_filename_rejects_name_that_empties_out():
    with pytest.raises(ValidationError):
        sanitize_filename("..")
    with pytest.raises(ValidationError):
        sanitize_filename("   ")


def test_sanitize_filename_rejects_non_string():
    with pytest.raises(ValidationError):
        sanitize_filename(None)


def test_validate_content_type_accepts_allowed():
    assert validate_content_type("application/pdf") == "application/pdf"


def test_validate_content_type_rejects_others():
    with pytest.raises(ValidationError):
        validate_content_type("application/x-msdownload")


def test_validate_size_accepts_valid():
    assert validate_size(1024) == 1024
    assert validate_size(MAX_FILE_SIZE) == MAX_FILE_SIZE


@pytest.mark.parametrize("bad", [0, -1, "1024", 1.5, None, True, MAX_FILE_SIZE + 1])
def test_validate_size_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        validate_size(bad)


def test_require_fields_passes_when_present():
    require_fields({"a": 1, "b": 2}, ["a", "b"])


def test_require_fields_names_the_missing_ones():
    with pytest.raises(ValidationError) as excinfo:
        require_fields({"a": 1}, ["a", "b", "c"])

    assert "b" in str(excinfo.value)
    assert "c" in str(excinfo.value)
```

Note `True` in the rejection list: `isinstance(True, int)` is `True` in Python, so booleans must be excluded explicitly.

- [ ] **Step 6: Run the tests to verify they fail**

Run: `.venv/bin/pytest -v`
Expected: collection error — `ModuleNotFoundError: No module named 'validation'`

- [ ] **Step 7: Write `backend/app/validation.py`**

```python
"""Pure input validation. No AWS, no I/O — everything here is unit-testable."""

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
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
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
    if content_type not in ALLOWED_CONTENT_TYPES:
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
```

`lstrip(".")` is what turns `".."` into `""` so it gets rejected, and it also prevents a leading-dot hidden file.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: PASS, 22 tests

- [ ] **Step 9: Commit**

```bash
git add .gitignore pytest.ini backend/requirements-dev.txt backend/app/validation.py backend/tests/test_validation.py
git commit -m "feat(backend): add input validation module with tests"
```

---

### Task 2: Response formatting module

**Files:**
- Create: `backend/app/responses.py`
- Test: `backend/tests/test_responses.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `response(status_code: int, body: dict | list) -> dict` — API Gateway v2 proxy response
  - `decimal_serializer(value) -> int | float`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_responses.py`:

```python
import json
from decimal import Decimal

import pytest

from responses import decimal_serializer, response


def test_response_shape():
    result = response(200, {"message": "ok"})

    assert result["statusCode"] == 200
    assert result["headers"]["Content-Type"] == "application/json"
    assert result["headers"]["Access-Control-Allow-Origin"] == "*"
    assert json.loads(result["body"]) == {"message": "ok"}


def test_response_serializes_whole_decimals_as_int():
    result = response(200, {"size": Decimal("245781")})
    body = json.loads(result["body"])

    assert body["size"] == 245781
    assert isinstance(body["size"], int)


def test_response_serializes_fractional_decimals_as_float():
    result = response(200, {"ratio": Decimal("1.5")})

    assert json.loads(result["body"])["ratio"] == 1.5


def test_response_handles_list_bodies():
    result = response(200, [{"a": 1}])

    assert json.loads(result["body"]) == [{"a": 1}]


def test_decimal_serializer_rejects_unknown_types():
    with pytest.raises(TypeError):
        decimal_serializer(object())


def test_headers_are_not_shared_between_responses():
    first = response(200, {})
    first["headers"]["X-Test"] = "mutated"

    assert "X-Test" not in response(200, {})["headers"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_responses.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'responses'`

- [ ] **Step 3: Write `backend/app/responses.py`**

```python
"""API Gateway v2 proxy response formatting."""

import json
from decimal import Decimal

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def decimal_serializer(value):
    """DynamoDB returns numbers as Decimal, which json cannot encode."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)

    raise TypeError(f"Cannot serialize type: {type(value).__name__}")


def response(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "headers": dict(_BASE_HEADERS),
        "body": json.dumps(body, default=decimal_serializer),
    }
```

`dict(_BASE_HEADERS)` copies deliberately — returning the shared dict would let one caller mutate every later response.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest -v`
Expected: PASS, 28 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/responses.py backend/tests/test_responses.py
git commit -m "feat(backend): add response formatting with Decimal support"
```

---

### Task 3: Upload URL route

First task touching AWS. Delivers the moto fixture used by every remaining backend task.

**Files:**
- Create: `backend/tests/conftest.py`
- Create: `backend/app/files.py`
- Test: `backend/tests/test_files_upload_url.py`

**Interfaces:**
- Consumes: `validation` and `responses` from Tasks 1–2.
- Produces:
  - `STAGING_PREFIX = "staging/"`, `UPLOADS_PREFIX = "uploads/"`
  - `class NotFoundError(Exception)`, `class ConflictError(Exception)`
  - `create_upload_url(event: dict) -> dict`
  - internal helpers `_s3()`, `_table()`, `_bucket()`, `_expiry()`
  - pytest fixtures `aws_env` (autouse) and `aws` yielding `{"s3": client, "table": Table}`

- [ ] **Step 1: Write the shared test fixture**

Create `backend/tests/conftest.py`:

```python
import boto3
import pytest
from moto import mock_aws

REGION = "ap-south-1"
BUCKET = "cloudvault-test-bucket"
TABLE = "cloudvault-test-files"


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
            KeySchema=[{"AttributeName": "fileId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "fileId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        yield {"s3": s3, "table": table}
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_files_upload_url.py`:

```python
import json

import pytest

from files import STAGING_PREFIX, create_upload_url
from validation import MAX_FILE_SIZE, ValidationError


def event_for(**overrides):
    body = {
        "fileName": "resume.pdf",
        "contentType": "application/pdf",
        "size": 1024,
    }
    body.update(overrides)

    return {"body": json.dumps(body)}


def test_returns_policy_for_valid_request(aws):
    result = create_upload_url(event_for())
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["fileName"] == "resume.pdf"
    assert body["s3Key"].startswith(f"{STAGING_PREFIX}{body['fileId']}-")
    assert body["s3Key"].endswith("-resume.pdf")
    assert body["expiresIn"] == 300
    assert body["uploadUrl"]
    assert "policy" in body["fields"]
    assert body["fields"]["Content-Type"] == "application/pdf"


def test_key_targets_staging_not_uploads(aws):
    body = json.loads(create_upload_url(event_for())["body"])

    assert body["s3Key"].startswith("staging/")
    assert "uploads/" not in body["s3Key"]


def test_each_request_gets_a_unique_file_id(aws):
    first = json.loads(create_upload_url(event_for())["body"])["fileId"]
    second = json.loads(create_upload_url(event_for())["body"])["fileId"]

    assert first != second


def test_filename_is_sanitized_in_response_and_key(aws):
    body = json.loads(create_upload_url(event_for(fileName="../../etc/pa ss.pdf"))["body"])

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
        create_upload_url({"body": json.dumps({"fileName": "a.pdf"})})
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_files_upload_url.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'files'`

- [ ] **Step 4: Write `backend/app/files.py`**

```python
"""File operations. The only module that talks to AWS."""

import logging
import os
import uuid

import boto3

from responses import response
from validation import (
    MAX_FILE_SIZE,
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
    return boto3.client("s3")


def _table():
    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def _bucket() -> str:
    return os.environ["BUCKET_NAME"]


def _expiry() -> int:
    return int(os.environ.get("PRESIGNED_URL_EXPIRY", "300"))


def create_upload_url(event: dict) -> dict:
    body = parse_body(event)
    require_fields(body, ["fileName", "contentType", "size"])

    file_name = sanitize_filename(body["fileName"])
    content_type = validate_content_type(body["contentType"])
    validate_size(body["size"])

    file_id = str(uuid.uuid4())
    staging_key = f"{STAGING_PREFIX}{file_id}-{file_name}"
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
```

The two `Conditions` entries are the whole point: S3 rejects a mismatched content type or an oversized body itself, at upload time, before the bytes are accepted.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest -v`
Expected: PASS, 35 tests

- [ ] **Step 6: Commit**

```bash
git add backend/tests/conftest.py backend/app/files.py backend/tests/test_files_upload_url.py
git commit -m "feat(backend): issue presigned POST policies for uploads"
```

---

### Task 4: Confirm upload route

The most intricate route: verify, promote from `staging/` to `uploads/`, write metadata.

**Files:**
- Modify: `backend/app/files.py` (append)
- Test: `backend/tests/test_files_confirm.py`

**Interfaces:**
- Consumes: everything from Task 3.
- Produces: `confirm_upload(event: dict) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_files_confirm.py`:

```python
import json

import pytest
from botocore.exceptions import ClientError

from conftest import BUCKET
from files import ConflictError, confirm_upload, create_upload_url
from validation import ValidationError

CONTENT = b"Hello World!"


def staged_upload(aws, file_name="sample.txt", content=CONTENT):
    """Run the real upload-url route, then put the object where the policy points."""
    issued = json.loads(
        create_upload_url(
            {
                "body": json.dumps(
                    {
                        "fileName": file_name,
                        "contentType": "text/plain",
                        "size": len(content),
                    }
                )
            }
        )["body"]
    )

    aws["s3"].put_object(
        Bucket=BUCKET,
        Key=issued["s3Key"],
        Body=content,
        ContentType="text/plain",
    )

    return issued


def confirm_event(issued, content=CONTENT, **overrides):
    body = {
        "fileId": issued["fileId"],
        "fileName": issued["fileName"],
        "s3Key": issued["s3Key"],
        "contentType": "text/plain",
        "size": len(content),
    }
    body.update(overrides)

    return {"body": json.dumps(body)}


def test_confirm_returns_201_with_the_item(aws):
    issued = staged_upload(aws)

    result = confirm_upload(confirm_event(issued))
    body = json.loads(result["body"])

    assert result["statusCode"] == 201
    assert body["message"] == "Upload confirmed"
    assert body["file"]["fileId"] == issued["fileId"]
    assert body["file"]["fileName"] == "sample.txt"
    assert body["file"]["size"] == len(CONTENT)
    assert body["file"]["contentType"] == "text/plain"
    assert "status" not in body["file"]


def test_confirm_stores_the_uploads_key_not_the_staging_key(aws):
    issued = staged_upload(aws)

    body = json.loads(confirm_upload(confirm_event(issued))["body"])

    assert body["file"]["s3Key"] == f"uploads/{issued['fileId']}-sample.txt"


def test_confirm_moves_the_object_from_staging_to_uploads(aws):
    issued = staged_upload(aws)

    confirm_upload(confirm_event(issued))

    assert aws["s3"].get_object(
        Bucket=BUCKET, Key=f"uploads/{issued['fileId']}-sample.txt"
    )["Body"].read() == CONTENT

    with pytest.raises(ClientError):
        aws["s3"].head_object(Bucket=BUCKET, Key=issued["s3Key"])


def test_confirm_writes_the_row_to_dynamodb(aws):
    issued = staged_upload(aws)

    confirm_upload(confirm_event(issued))
    item = aws["table"].get_item(Key={"fileId": issued["fileId"]})["Item"]

    assert item["fileName"] == "sample.txt"
    assert item["s3Key"].startswith("uploads/")
    assert item["uploadedAt"].endswith("+00:00")


def test_confirm_rejects_when_nothing_was_uploaded(aws):
    issued = json.loads(
        create_upload_url(
            {
                "body": json.dumps(
                    {"fileName": "ghost.txt", "contentType": "text/plain", "size": 5}
                )
            }
        )["body"]
    )

    with pytest.raises(ValidationError):
        confirm_upload(confirm_event(issued, content=b"ghost"))


def test_confirm_rejects_a_key_belonging_to_another_file(aws):
    victim = staged_upload(aws)
    attacker = staged_upload(aws, file_name="attack.txt")

    event = confirm_event(attacker, s3Key=victim["s3Key"])

    with pytest.raises(ValidationError):
        confirm_upload(event)


def test_confirm_rejects_a_key_outside_staging(aws):
    issued = staged_upload(aws)
    event = confirm_event(issued, s3Key=f"uploads/{issued['fileId']}-sample.txt")

    with pytest.raises(ValidationError):
        confirm_upload(event)


def test_confirm_rejects_size_mismatch(aws):
    issued = staged_upload(aws)

    with pytest.raises(ValidationError):
        confirm_upload(confirm_event(issued, size=999))


def test_confirm_replayed_raises_conflict(aws):
    issued = staged_upload(aws)
    confirm_upload(confirm_event(issued))

    # Re-stage the object because the first confirm consumed it.
    aws["s3"].put_object(
        Bucket=BUCKET, Key=issued["s3Key"], Body=CONTENT, ContentType="text/plain"
    )

    with pytest.raises(ConflictError):
        confirm_upload(confirm_event(issued))


def test_confirm_requires_all_fields(aws):
    with pytest.raises(ValidationError):
        confirm_upload({"body": json.dumps({"fileId": "abc"})})
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_files_confirm.py -v`
Expected: FAIL — `ImportError: cannot import name 'confirm_upload'`

- [ ] **Step 3: Append to `backend/app/files.py`**

Add the import of `ClientError` and `datetime` at the top of the file:

```python
from datetime import datetime, timezone

from botocore.exceptions import ClientError
```

Then append:

```python
def confirm_upload(event: dict) -> dict:
    body = parse_body(event)
    require_fields(body, ["fileId", "fileName", "s3Key", "contentType", "size"])

    file_id = body["fileId"]
    staging_key = body["s3Key"]

    if not isinstance(file_id, str) or not isinstance(staging_key, str):
        raise ValidationError("fileId and s3Key must be strings")

    file_name = sanitize_filename(body["fileName"])
    declared_size = validate_size(body["size"])
    validate_content_type(body["contentType"])

    # Binding the key to the fileId stops one upload being confirmed as another.
    if not staging_key.startswith(f"{STAGING_PREFIX}{file_id}-"):
        raise ValidationError("Invalid S3 object key")

    s3 = _s3()
    bucket = _bucket()

    try:
        head = s3.head_object(Bucket=bucket, Key=staging_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in S3_NOT_FOUND_CODES:
            raise ValidationError("File was not uploaded to S3") from exc
        raise

    actual_size = head["ContentLength"]

    if actual_size != declared_size:
        raise ValidationError("Uploaded file size does not match")

    destination_key = f"{UPLOADS_PREFIX}{file_id}-{file_name}"

    # Server-side copy: the bytes move inside S3 and never enter Lambda.
    s3.copy_object(
        Bucket=bucket,
        Key=destination_key,
        CopySource={"Bucket": bucket, "Key": staging_key},
    )
    s3.delete_object(Bucket=bucket, Key=staging_key)

    item = {
        "fileId": file_id,
        "fileName": file_name,
        "s3Key": destination_key,
        "contentType": head.get("ContentType", "application/octet-stream"),
        "size": actual_size,
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(fileId)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ConflictError("File already confirmed") from exc
        raise

    logger.info("Confirmed upload fileId=%s", file_id)

    return response(201, {"message": "Upload confirmed", "file": item})
```

Also add `ValidationError` to the existing `from validation import (...)` block.

Note the ordering: verify first, then move, then write metadata. A failure at any stage leaves the object in `staging/`, where the lifecycle rule sweeps it within a day.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest -v`
Expected: PASS, 45 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/files.py backend/tests/test_files_confirm.py
git commit -m "feat(backend): verify and promote staged uploads on confirm"
```

---

### Task 5: List files route

**Files:**
- Modify: `backend/app/files.py` (append)
- Test: `backend/tests/test_files_list.py`

**Interfaces:**
- Consumes: Task 4.
- Produces: `list_files() -> dict` — note it takes **no** arguments.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_files_list.py`:

```python
import json

from files import list_files


def put_row(aws, file_id, uploaded_at):
    aws["table"].put_item(
        Item={
            "fileId": file_id,
            "fileName": f"{file_id}.txt",
            "s3Key": f"uploads/{file_id}-{file_id}.txt",
            "contentType": "text/plain",
            "size": 12,
            "uploadedAt": uploaded_at,
        }
    )


def test_empty_table_returns_empty_list(aws):
    result = list_files()

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"files": []}


def test_returns_newest_first(aws):
    put_row(aws, "old", "2026-01-01T00:00:00+00:00")
    put_row(aws, "new", "2026-08-01T00:00:00+00:00")
    put_row(aws, "middle", "2026-04-01T00:00:00+00:00")

    files = json.loads(list_files()["body"])["files"]

    assert [f["fileId"] for f in files] == ["new", "middle", "old"]


def test_size_serializes_as_int_not_decimal(aws):
    put_row(aws, "a", "2026-01-01T00:00:00+00:00")

    size = json.loads(list_files()["body"])["files"][0]["size"]

    assert size == 12
    assert isinstance(size, int)


def test_paginates_across_scan_pages(aws, monkeypatch):
    """Rows this small never fill a 1 MB scan page, so force two pages explicitly."""
    pages = [
        {
            "Items": [{"fileId": "a", "uploadedAt": "2026-01-01T00:00:00+00:00"}],
            "LastEvaluatedKey": {"fileId": "a"},
        },
        {"Items": [{"fileId": "b", "uploadedAt": "2026-02-01T00:00:00+00:00"}]},
    ]
    calls = []

    class FakeTable:
        def scan(self, **kwargs):
            calls.append(kwargs)
            return pages[len(calls) - 1]

    monkeypatch.setattr("files._table", lambda: FakeTable())

    files = json.loads(list_files()["body"])["files"]

    assert [f["fileId"] for f in files] == ["b", "a"]
    assert calls[1]["ExclusiveStartKey"] == {"fileId": "a"}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_files_list.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_files'`

- [ ] **Step 3: Append to `backend/app/files.py`**

```python
def list_files() -> dict:
    table = _table()
    items = []
    scan_arguments = {}

    while True:
        result = table.scan(**scan_arguments)
        items.extend(result.get("Items", []))

        last_key = result.get("LastEvaluatedKey")

        if not last_key:
            break

        scan_arguments["ExclusiveStartKey"] = last_key

    # Sorting in Lambda is fine at this scale; v2's userId partition key
    # replaces the scan with a Query.
    items.sort(key=lambda item: item.get("uploadedAt", ""), reverse=True)

    return response(200, {"files": items})
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest -v`
Expected: PASS, 49 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/files.py backend/tests/test_files_list.py
git commit -m "feat(backend): list files newest-first with scan pagination"
```

---

### Task 6: Download and delete routes

**Files:**
- Modify: `backend/app/files.py` (append)
- Test: `backend/tests/test_files_download_delete.py`

**Interfaces:**
- Consumes: Task 5.
- Produces:
  - `create_download_url(event: dict) -> dict`
  - `delete_file(event: dict) -> dict`
  - helpers `_path_file_id(event) -> str`, `_get_item_or_404(file_id) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_files_download_delete.py`:

```python
import json
from urllib.parse import unquote

import pytest
from botocore.exceptions import ClientError

from conftest import BUCKET
from files import NotFoundError, create_download_url, delete_file
from validation import ValidationError


def stored_file(aws, file_id="abc123"):
    key = f"uploads/{file_id}-report.pdf"
    aws["s3"].put_object(Bucket=BUCKET, Key=key, Body=b"pdf-bytes")
    aws["table"].put_item(
        Item={
            "fileId": file_id,
            "fileName": "report.pdf",
            "s3Key": key,
            "contentType": "application/pdf",
            "size": 9,
            "uploadedAt": "2026-08-01T00:00:00+00:00",
        }
    )
    return key


def event_for(file_id):
    return {"pathParameters": {"fileId": file_id}}


def test_download_returns_a_presigned_url(aws):
    stored_file(aws)

    result = create_download_url(event_for("abc123"))
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["expiresIn"] == 300
    assert "X-Amz-Signature" in body["downloadUrl"]


def test_download_url_forces_the_original_filename(aws):
    stored_file(aws)

    url = json.loads(create_download_url(event_for("abc123"))["body"])["downloadUrl"]

    assert 'attachment; filename="report.pdf"' in unquote(url)


def test_download_unknown_file_raises_not_found(aws):
    with pytest.raises(NotFoundError):
        create_download_url(event_for("does-not-exist"))


def test_download_requires_a_file_id(aws):
    with pytest.raises(ValidationError):
        create_download_url({"pathParameters": {}})

    with pytest.raises(ValidationError):
        create_download_url({})


def test_delete_removes_object_and_row(aws):
    key = stored_file(aws)

    result = delete_file(event_for("abc123"))

    assert result["statusCode"] == 200
    assert "Item" not in aws["table"].get_item(Key={"fileId": "abc123"})

    with pytest.raises(ClientError):
        aws["s3"].head_object(Bucket=BUCKET, Key=key)


def test_delete_unknown_file_raises_not_found(aws):
    with pytest.raises(NotFoundError):
        delete_file(event_for("does-not-exist"))


def test_delete_requires_a_file_id(aws):
    with pytest.raises(ValidationError):
        delete_file({"pathParameters": None})
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_files_download_delete.py -v`
Expected: FAIL — `ImportError: cannot import name 'create_download_url'`

- [ ] **Step 3: Append to `backend/app/files.py`**

```python
def _path_file_id(event: dict) -> str:
    parameters = event.get("pathParameters") or {}
    file_id = parameters.get("fileId")

    if not file_id:
        raise ValidationError("fileId is required")

    return file_id


def _get_item_or_404(file_id: str) -> dict:
    item = _table().get_item(Key={"fileId": file_id}).get("Item")

    if not item:
        raise NotFoundError("File not found")

    return item


def create_download_url(event: dict) -> dict:
    file_id = _path_file_id(event)
    item = _get_item_or_404(file_id)
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
    file_id = _path_file_id(event)
    item = _get_item_or_404(file_id)

    # Object first: a failure after this leaves a visible, retryable row.
    # The reverse order would orphan an object that nothing ever sweeps.
    _s3().delete_object(Bucket=_bucket(), Key=item["s3Key"])

    try:
        _table().delete_item(
            Key={"fileId": file_id},
            ConditionExpression="attribute_exists(fileId)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise NotFoundError("File not found") from exc
        raise

    logger.info("Deleted fileId=%s", file_id)

    return response(200, {"message": "File deleted successfully"})
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest -v`
Expected: PASS, 56 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/files.py backend/tests/test_files_download_delete.py
git commit -m "feat(backend): add download URL and delete routes"
```

---

### Task 7: Handler routing and error mapping

**Files:**
- Create: `backend/app/handler.py`
- Test: `backend/tests/test_handler.py`

**Interfaces:**
- Consumes: `files`, `responses`, `validation`.
- Produces: `lambda_handler(event, context) -> dict` — the Lambda entry point, `handler.lambda_handler`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_handler.py`:

```python
import json

import pytest
from botocore.exceptions import ClientError

import files
from handler import lambda_handler
from validation import ValidationError


def test_unknown_route_returns_404(aws):
    result = lambda_handler({"routeKey": "GET /nope"}, None)

    assert result["statusCode"] == 404
    assert json.loads(result["body"])["message"] == "Route not found"


def test_missing_route_key_returns_404(aws):
    assert lambda_handler({}, None)["statusCode"] == 404


def test_routes_list_files(aws):
    result = lambda_handler({"routeKey": "GET /files"}, None)

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"files": []}


def test_routes_upload_url(aws):
    event = {
        "routeKey": "POST /files/upload-url",
        "body": json.dumps(
            {"fileName": "a.pdf", "contentType": "application/pdf", "size": 10}
        ),
    }

    assert lambda_handler(event, None)["statusCode"] == 200


def test_validation_error_maps_to_400(aws):
    event = {
        "routeKey": "POST /files/upload-url",
        "body": json.dumps({"fileName": "a.exe", "contentType": "bad/type", "size": 10}),
    }
    result = lambda_handler(event, None)

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["message"] == "Unsupported file type"


def test_not_found_error_maps_to_404(aws):
    event = {
        "routeKey": "DELETE /files/{fileId}",
        "pathParameters": {"fileId": "ghost"},
    }

    assert lambda_handler(event, None)["statusCode"] == 404


def test_conflict_error_maps_to_409(aws, monkeypatch):
    def boom(event):
        raise files.ConflictError("File already confirmed")

    monkeypatch.setattr(files, "confirm_upload", boom)
    result = lambda_handler({"routeKey": "POST /files/confirm", "body": "{}"}, None)

    assert result["statusCode"] == 409
    assert json.loads(result["body"])["message"] == "File already confirmed"


def test_client_error_maps_to_500_without_leaking_details(aws, monkeypatch):
    def boom():
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "secret internals"}},
            "Scan",
        )

    monkeypatch.setattr(files, "list_files", boom)
    result = lambda_handler({"routeKey": "GET /files"}, None)

    assert result["statusCode"] == 500
    assert json.loads(result["body"])["message"] == "AWS operation failed"
    assert "secret internals" not in result["body"]


def test_unexpected_error_maps_to_500(aws, monkeypatch):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(files, "list_files", boom)
    result = lambda_handler({"routeKey": "GET /files"}, None)

    assert result["statusCode"] == 500
    assert json.loads(result["body"])["message"] == "Internal server error"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest backend/tests/test_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'handler'`

- [ ] **Step 3: Write `backend/app/handler.py`**

```python
"""Lambda entry point: route dispatch and exception-to-status mapping."""

import logging

from botocore.exceptions import ClientError

import files
from responses import response
from validation import ValidationError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Looked up through the `files` module (not imported directly) so tests can
# monkeypatch individual routes.
ROUTES = {
    "POST /files/upload-url": lambda event: files.create_upload_url(event),
    "POST /files/confirm": lambda event: files.confirm_upload(event),
    "GET /files": lambda event: files.list_files(),
    "GET /files/{fileId}/download": lambda event: files.create_download_url(event),
    "DELETE /files/{fileId}": lambda event: files.delete_file(event),
}


def lambda_handler(event, context):
    route_key = event.get("routeKey")
    route = ROUTES.get(route_key)

    if route is None:
        return response(404, {"message": "Route not found"})

    try:
        return route(event)

    except ValidationError as exc:
        return response(400, {"message": str(exc)})

    except files.NotFoundError as exc:
        return response(404, {"message": str(exc)})

    except files.ConflictError as exc:
        return response(409, {"message": str(exc)})

    except ClientError:
        logger.exception("AWS operation failed route=%s", route_key)
        return response(500, {"message": "AWS operation failed"})

    except Exception:
        logger.exception("Unexpected error route=%s", route_key)
        return response(500, {"message": "Internal server error"})
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: PASS, 65 tests

- [ ] **Step 5: Commit**

```bash
git add backend/app/handler.py backend/tests/test_handler.py
git commit -m "feat(backend): add route dispatch and error status mapping"
```

---

### Task 8: Terraform storage layer

**Files:**
- Create: `terraform/provider.tf`, `terraform/variables.tf`, `terraform/terraform.tfvars`, `terraform/s3.tf`, `terraform/dynamodb.tf`

**Interfaces:**
- Consumes: nothing.
- Produces: `aws_s3_bucket.files`, `aws_dynamodb_table.files`, variables `aws_region`, `project_name`, `frontend_origin`.

- [ ] **Step 1: Write `terraform/provider.tf`**

```hcl
terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }

    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "Terraform"
    }
  }
}
```

- [ ] **Step 2: Write `terraform/variables.tf`**

```hcl
variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Name prefix applied to every resource"
  type        = string
  default     = "cloudvault"
}

variable "frontend_origin" {
  description = "Single origin allowed by API Gateway and S3 CORS"
  type        = string
  default     = "http://localhost:5173"
}

variable "presigned_url_expiry" {
  description = "Lifetime in seconds of presigned upload policies and download URLs"
  type        = number
  default     = 300
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the backend Lambda"
  type        = number
  default     = 7
}
```

- [ ] **Step 3: Write `terraform/terraform.tfvars`**

```hcl
aws_region      = "ap-south-1"
project_name    = "cloudvault"
frontend_origin = "http://localhost:5173"
```

This file is gitignored — it's listed here so the values are reproducible.

- [ ] **Step 4: Write `terraform/s3.tf`**

```hcl
resource "aws_s3_bucket" "files" {
  bucket_prefix = "${var.project_name}-files-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "files" {
  bucket = aws_s3_bucket.files.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "files" {
  bucket = aws_s3_bucket.files.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Presigned POST, so the browser issues POST — not PUT.
resource "aws_s3_bucket_cors_configuration" "files" {
  bucket = aws_s3_bucket.files.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["POST"]
    allowed_origins = [var.frontend_origin]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# Sweeps uploads that were never confirmed. Scoped to staging/ only —
# a rule covering uploads/ would delete confirmed files a day after upload.
resource "aws_s3_bucket_lifecycle_configuration" "staging_cleanup" {
  bucket = aws_s3_bucket.files.id

  rule {
    id     = "expire-unconfirmed-uploads"
    status = "Enabled"

    filter {
      prefix = "staging/"
    }

    expiration {
      days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
```

- [ ] **Step 5: Write `terraform/dynamodb.tf`**

```hcl
resource "aws_dynamodb_table" "files" {
  name         = "${var.project_name}-files"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "fileId"

  attribute {
    name = "fileId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }
}
```

- [ ] **Step 6: Validate**

```bash
cd terraform
terraform fmt -recursive
terraform init
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 7: Commit**

```bash
git add terraform/provider.tf terraform/variables.tf terraform/s3.tf terraform/dynamodb.tf terraform/.terraform.lock.hcl
git commit -m "feat(infra): add S3 bucket with staging lifecycle and DynamoDB table"
```

---

### Task 9: Terraform IAM and Lambda

**Files:**
- Create: `terraform/iam.tf`, `terraform/lambda.tf`

**Interfaces:**
- Consumes: `aws_s3_bucket.files`, `aws_dynamodb_table.files` from Task 8; `backend/app/` from Tasks 1–7.
- Produces: `aws_lambda_function.backend`, `aws_iam_role.lambda`, `aws_cloudwatch_log_group.lambda`.

- [ ] **Step 1: Write `terraform/iam.tf`**

```hcl
resource "aws_iam_role" "lambda" {
  name_prefix = "${var.project_name}-lambda-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "lambda_access" {
  name_prefix = "${var.project_name}-lambda-access-"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "FileObjectAccess"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject"
        ]
        Resource = [
          "${aws_s3_bucket.files.arn}/staging/*",
          "${aws_s3_bucket.files.arn}/uploads/*"
        ]
      },
      {
        Sid    = "FileMetadataAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan"
        ]
        Resource = aws_dynamodb_table.files.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_access" {
  role       = aws_iam_role.lambda.name
  policy_arn = aws_iam_policy.lambda_access.arn
}
```

`s3:GetObject` on `staging/*` is required — `copy_object` reads the source.

- [ ] **Step 2: Write `terraform/lambda.tf`**

```hcl
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../backend/app"
  output_path = "${path.module}/lambda_function.zip"
  excludes    = ["__pycache__"]
}

# Declared explicitly so retention is bounded and `terraform destroy` removes it.
# Without this, Lambda auto-creates the group with never-expire retention and it
# survives teardown.
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
      BUCKET_NAME          = aws_s3_bucket.files.id
      TABLE_NAME           = aws_dynamodb_table.files.name
      PRESIGNED_URL_EXPIRY = tostring(var.presigned_url_expiry)
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_logs,
    aws_cloudwatch_log_group.lambda,
  ]
}
```

`handler` is `handler.lambda_handler` — `source_dir` puts the module at the zip root.

- [ ] **Step 3: Validate**

```bash
cd terraform && terraform fmt -recursive && terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Commit**

```bash
git add terraform/iam.tf terraform/lambda.tf
git commit -m "feat(infra): add scoped IAM role, Lambda function, and log group"
```

---

### Task 10: Terraform API Gateway, monitoring, outputs, and first apply

**Files:**
- Create: `terraform/api_gateway.tf`, `terraform/monitoring.tf`, `terraform/outputs.tf`

**Interfaces:**
- Consumes: `aws_lambda_function.backend` from Task 9.
- Produces: outputs `api_url`, `bucket_name`, `dynamodb_table_name`, `lambda_function_name`.

- [ ] **Step 1: Write `terraform/api_gateway.tf`**

```hcl
resource "aws_apigatewayv2_api" "http" {
  name          = "${var.project_name}-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = [var.frontend_origin]
    allow_methods = ["GET", "POST", "DELETE", "OPTIONS"]
    allow_headers = ["content-type"]
    max_age       = 300
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id = aws_apigatewayv2_api.http.id

  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.backend.invoke_arn
  payload_format_version = "2.0"
}

locals {
  routes = toset([
    "POST /files/upload-url",
    "POST /files/confirm",
    "GET /files",
    "GET /files/{fileId}/download",
    "DELETE /files/{fileId}",
  ])
}

resource "aws_apigatewayv2_route" "routes" {
  for_each = local.routes

  api_id    = aws_apigatewayv2_api.http.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowApiGatewayInvocation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
```

The `routeKey` strings here must match `ROUTES` in `handler.py` character for character.

- [ ] **Step 2: Write `terraform/monitoring.tf`**

```hcl
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name        = "${var.project_name}-lambda-errors"
  alarm_description = "CloudVault backend Lambda reported one or more errors"

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  statistic   = "Sum"

  dimensions = {
    FunctionName = aws_lambda_function.backend.function_name
  }

  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
}
```

- [ ] **Step 3: Write `terraform/outputs.tf`**

```hcl
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

output "lambda_function_name" {
  description = "Backend Lambda function"
  value       = aws_lambda_function.backend.function_name
}
```

- [ ] **Step 4: Confirm AWS identity is not the root account**

```bash
aws sts get-caller-identity
```

Expected: an IAM user or role ARN. If the `Arn` ends in `:root`, stop and switch credentials.

- [ ] **Step 5: Plan and apply**

```bash
cd terraform
terraform fmt -recursive
terraform validate
terraform plan
terraform apply
```

Expected: ~15 resources created, no errors.

- [ ] **Step 6: Capture the API URL**

```bash
export API_URL="$(terraform output -raw api_url)"
echo "$API_URL"
```

- [ ] **Step 7: Commit**

```bash
git add terraform/api_gateway.tf terraform/monitoring.tf terraform/outputs.tf
git commit -m "feat(infra): add HTTP API routes, error alarm, and outputs"
```

---

### Task 11: End-to-end verification against real AWS

No code — this proves the deployed stack actually works before any React is written. Run from the repo root with `API_URL` exported.

**Files:** none (throwaway `sample.txt` in the scratch shell).

- [ ] **Step 1: Confirm the list route is reachable and empty**

```bash
curl -s "$API_URL/files"
```

Expected: `{"files": []}`

- [ ] **Step 2: Create a sample file and note its exact byte count**

```bash
printf "Hello World!" > sample.txt
wc -c < sample.txt
```

Expected: `12`

- [ ] **Step 3: Request an upload policy**

```bash
curl -s -X POST "$API_URL/files/upload-url" \
  -H "Content-Type: application/json" \
  -d '{"fileName": "sample.txt", "contentType": "text/plain", "size": 12}' \
  | tee upload.json
```

Expected: JSON with `fileId`, `s3Key` starting `staging/`, `uploadUrl`, and a `fields` object.

- [ ] **Step 4: Upload directly to S3 using the policy**

```bash
UPLOAD_URL=$(python3 -c "import json;print(json.load(open('upload.json'))['uploadUrl'])")
FIELD_ARGS=$(python3 -c "
import json
fields = json.load(open('upload.json'))['fields']
print(' '.join(f'-F {k}={v!r}' for k, v in fields.items()))
")

eval curl -s -w '%{http_code}' -X POST "$UPLOAD_URL" $FIELD_ARGS -F file=@sample.txt
```

Expected: `204` with an empty body. Presigned POST returns 204, not 200.

- [ ] **Step 5: Verify the object landed in `staging/`**

```bash
aws s3 ls "s3://$(cd terraform && terraform output -raw bucket_name)/staging/"
```

Expected: one object.

- [ ] **Step 6: Confirm the upload**

```bash
FILE_ID=$(python3 -c "import json;print(json.load(open('upload.json'))['fileId'])")
S3_KEY=$(python3 -c "import json;print(json.load(open('upload.json'))['s3Key'])")

curl -s -X POST "$API_URL/files/confirm" \
  -H "Content-Type: application/json" \
  -d "{\"fileId\":\"$FILE_ID\",\"fileName\":\"sample.txt\",\"s3Key\":\"$S3_KEY\",\"contentType\":\"text/plain\",\"size\":12}"
```

Expected: `201` with `"message": "Upload confirmed"` and `file.s3Key` starting `uploads/`.

- [ ] **Step 7: Verify the object moved**

```bash
BUCKET=$(cd terraform && terraform output -raw bucket_name)
aws s3 ls "s3://$BUCKET/staging/"
aws s3 ls "s3://$BUCKET/uploads/"
```

Expected: `staging/` empty, `uploads/` holds the object.

- [ ] **Step 8: Verify the 409 on a replayed confirm**

Re-run the exact command from Step 6.

Expected: HTTP 409, `{"message": "File already confirmed"}`. If this returns 500, `handler.py`'s exception ordering is wrong.

- [ ] **Step 9: List, download, delete**

```bash
curl -s "$API_URL/files"
curl -s "$API_URL/files/$FILE_ID/download"
DOWNLOAD_URL=$(curl -s "$API_URL/files/$FILE_ID/download" | python3 -c "import sys,json;print(json.load(sys.stdin)['downloadUrl'])")
curl -s "$DOWNLOAD_URL"
curl -s -X DELETE "$API_URL/files/$FILE_ID"
curl -s "$API_URL/files"
```

Expected: the file appears in the list; the download URL returns `Hello World!`; delete returns `File deleted successfully`; the final list is `{"files": []}`.

- [ ] **Step 10: Verify the error paths**

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$API_URL/files/no-such-id/download"
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "$API_URL/files/no-such-id"
curl -s -X POST "$API_URL/files/upload-url" -H "Content-Type: application/json" \
  -d '{"fileName": "virus.exe", "contentType": "application/x-msdownload", "size": 10}'
curl -s -X POST "$API_URL/files/upload-url" -H "Content-Type: application/json" \
  -d '{"fileName": "big.pdf", "contentType": "application/pdf", "size": 20000000}'
```

Expected: `404`, `404`, then `Unsupported file type` and `Maximum file size is 10 MB`.

- [ ] **Step 11: Verify S3 rejects an oversized body itself**

Generate a file larger than the policy's 10 MB ceiling and try to upload it:

```bash
python3 -c "open('big.txt','w').write('x' * 11000000)"
curl -s -X POST "$API_URL/files/upload-url" -H "Content-Type: application/json" \
  -d '{"fileName": "ok.txt", "contentType": "text/plain", "size": 500}' > big-upload.json
UPLOAD_URL=$(python3 -c "import json;print(json.load(open('big-upload.json'))['uploadUrl'])")
FIELD_ARGS=$(python3 -c "
import json
fields = json.load(open('big-upload.json'))['fields']
print(' '.join(f'-F {k}={v!r}' for k, v in fields.items()))
")
eval curl -s -X POST "$UPLOAD_URL" $FIELD_ARGS -F file=@big.txt
```

Expected: S3 returns `EntityTooLarge`. This is the guide's original hole, proven closed at the source.

- [ ] **Step 12: Check the logs**

```bash
aws logs tail "/aws/lambda/cloudvault-backend" --since 30m --region ap-south-1
```

Expected: `Issued upload policy fileId=...`, `Confirmed upload fileId=...`, `Deleted fileId=...`. Confirm no presigned URL or file content appears anywhere in the output.

- [ ] **Step 13: Clean up scratch files**

```bash
rm -f sample.txt big.txt upload.json big-upload.json
```

---

### Task 12: Frontend scaffold and API service layer

**Files:**
- Create: `frontend/` (via Vite), `frontend/.env`, `frontend/src/services/api.js`
- Delete: `frontend/src/assets/react.svg`

**Interfaces:**
- Consumes: the deployed API from Task 10.
- Produces:
  - `requestUploadUrl(file) -> Promise<{fileId, fileName, s3Key, uploadUrl, fields, expiresIn}>`
  - `uploadFileToS3(uploadData, file) -> Promise<void>`
  - `confirmUpload(uploadData, file) -> Promise<object>`
  - `getFiles() -> Promise<{files: object[]}>`
  - `getDownloadUrl(fileId) -> Promise<{downloadUrl, expiresIn}>`
  - `deleteFile(fileId) -> Promise<object>`

- [ ] **Step 1: Scaffold**

```bash
npm create vite@latest frontend -- --template react
cd frontend && npm install
```

- [ ] **Step 2: Create `frontend/.env`**

```env
VITE_API_URL=PASTE_THE_TERRAFORM_api_url_OUTPUT_HERE
```

Fill it with the real value:

```bash
echo "VITE_API_URL=$(cd ../terraform && terraform output -raw api_url)" > .env
```

This file is gitignored — it holds a deployment-specific URL.

- [ ] **Step 3: Write `frontend/src/services/api.js`**

```javascript
const API_URL = import.meta.env.VITE_API_URL;

async function parseResponse(response) {
  const body = await response.json().catch(() => ({}));

  if (!response.ok) {
    throw new Error(body.message || `Request failed (${response.status})`);
  }

  return body;
}

export async function requestUploadUrl(file) {
  const response = await fetch(`${API_URL}/files/upload-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      fileName: file.name,
      contentType: file.type,
      size: file.size,
    }),
  });

  return parseResponse(response);
}

export async function uploadFileToS3(uploadData, file) {
  const form = new FormData();

  Object.entries(uploadData.fields).forEach(([key, value]) => {
    form.append(key, value);
  });

  // The file must be appended last — S3 ignores any field that follows it.
  form.append("file", file);

  // No Content-Type header: the browser must set the multipart boundary itself.
  const response = await fetch(uploadData.uploadUrl, {
    method: "POST",
    body: form,
  });

  // A successful presigned POST returns 204 with an empty body.
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      detail.includes("EntityTooLarge")
        ? "File is larger than the upload policy allows"
        : `S3 upload failed (${response.status})`,
    );
  }
}

export async function confirmUpload(uploadData, file) {
  const response = await fetch(`${API_URL}/files/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      fileId: uploadData.fileId,
      fileName: uploadData.fileName,
      s3Key: uploadData.s3Key,
      contentType: file.type,
      size: file.size,
    }),
  });

  return parseResponse(response);
}

export async function getFiles() {
  return parseResponse(await fetch(`${API_URL}/files`));
}

export async function getDownloadUrl(fileId) {
  const response = await fetch(
    `${API_URL}/files/${encodeURIComponent(fileId)}/download`,
  );

  return parseResponse(response);
}

export async function deleteFile(fileId) {
  const response = await fetch(
    `${API_URL}/files/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  );

  return parseResponse(response);
}
```

`confirmUpload` sends `uploadData.s3Key` — the **staging** key — because that is what the backend validates against.

- [ ] **Step 4: Verify the dev server boots and can reach the API**

```bash
npm run dev
```

Open `http://localhost:5173`, then in the browser console:

```javascript
await (await import("/src/services/api.js")).getFiles()
```

Expected: `{files: []}`. A CORS error here means `frontend_origin` doesn't match `http://localhost:5173`.

- [ ] **Step 5: Commit**

```bash
cd .. && rm -f frontend/src/assets/react.svg
git add frontend/ ':!frontend/node_modules' ':!frontend/.env'
git commit -m "feat(frontend): scaffold Vite app and add API service layer"
```

---

### Task 13: Upload component

**Files:**
- Create: `frontend/src/components/FileUpload.jsx`

**Interfaces:**
- Consumes: `requestUploadUrl`, `uploadFileToS3`, `confirmUpload` from Task 12.
- Produces: `<FileUpload onUploaded={() => Promise<void>} />`

- [ ] **Step 1: Write `frontend/src/components/FileUpload.jsx`**

```jsx
import { useState } from "react";
import {
  confirmUpload,
  requestUploadUrl,
  uploadFileToS3,
} from "../services/api";

const MAX_FILE_SIZE = 10 * 1024 * 1024;

const ALLOWED_TYPES = [
  "application/pdf",
  "image/png",
  "image/jpeg",
  "text/plain",
];

export default function FileUpload({ onUploaded }) {
  const [file, setFile] = useState(null);
  const [status, setStatus] = useState("");
  const [uploading, setUploading] = useState(false);

  function chooseFile(event) {
    setFile(event.target.files?.[0] ?? null);
    setStatus("");
  }

  async function handleUpload() {
    if (!file) {
      setStatus("Choose a file first.");
      return;
    }

    // Client-side checks are a courtesy — S3 and Lambda enforce the real limits.
    if (!ALLOWED_TYPES.includes(file.type)) {
      setStatus("Unsupported file type. Allowed: PDF, PNG, JPEG, TXT.");
      return;
    }

    if (file.size > MAX_FILE_SIZE) {
      setStatus("Maximum file size is 10 MB.");
      return;
    }

    setUploading(true);

    try {
      setStatus("Requesting upload permission...");
      const uploadData = await requestUploadUrl(file);

      setStatus("Uploading to S3...");
      await uploadFileToS3(uploadData, file);

      setStatus("Saving file information...");
      await confirmUpload(uploadData, file);

      setStatus(`Uploaded ${uploadData.fileName}.`);
      setFile(null);
      await onUploaded();
    } catch (error) {
      setStatus(error.message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <section className="panel">
      <h2>Upload a file</h2>

      <div className="upload-row">
        <input
          type="file"
          accept=".pdf,.png,.jpg,.jpeg,.txt"
          disabled={uploading}
          onChange={chooseFile}
        />

        <button type="button" disabled={!file || uploading} onClick={handleUpload}>
          {uploading ? "Uploading..." : "Upload"}
        </button>
      </div>

      {status && <p className="status">{status}</p>}
    </section>
  );
}
```

The `value`-less file input means selecting the same file twice in a row still fires `onChange` only if the browser considers it a change; clearing `file` state after success is what resets the button.

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/FileUpload.jsx
git commit -m "feat(frontend): add upload component with three-step flow"
```

---

### Task 14: File list component

**Files:**
- Create: `frontend/src/components/FileList.jsx`

**Interfaces:**
- Consumes: `getDownloadUrl`, `deleteFile` from Task 12.
- Produces: `<FileList files={object[]} loading={boolean} onChanged={() => Promise<void>} />`

- [ ] **Step 1: Write `frontend/src/components/FileList.jsx`**

```jsx
import { useState } from "react";
import { deleteFile, getDownloadUrl } from "../services/api";

function formatBytes(bytes) {
  if (!bytes) {
    return "0 B";
  }

  const units = ["B", "KB", "MB", "GB"];
  const index = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / 1024 ** index;

  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

export default function FileList({ files, loading, onChanged }) {
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");

  async function handleDownload(fileId) {
    setBusyId(fileId);
    setError("");

    try {
      const { downloadUrl } = await getDownloadUrl(fileId);
      window.location.assign(downloadUrl);
    } catch (downloadError) {
      setError(downloadError.message);
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(fileId, fileName) {
    if (!window.confirm(`Delete ${fileName}? This cannot be undone.`)) {
      return;
    }

    setBusyId(fileId);
    setError("");

    try {
      await deleteFile(fileId);
      await onChanged();
    } catch (deleteError) {
      setError(deleteError.message);
    } finally {
      setBusyId(null);
    }
  }

  if (loading) {
    return <p className="status">Loading files...</p>;
  }

  if (files.length === 0) {
    return <p className="status">No files uploaded yet.</p>;
  }

  return (
    <section className="panel">
      <h2>Your files</h2>

      {error && <p className="error" role="alert">{error}</p>}

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Size</th>
            <th>Uploaded</th>
            <th>Actions</th>
          </tr>
        </thead>

        <tbody>
          {files.map((file) => (
            <tr key={file.fileId}>
              <td>{file.fileName}</td>
              <td>{formatBytes(Number(file.size))}</td>
              <td>{new Date(file.uploadedAt).toLocaleString()}</td>
              <td className="actions">
                <button
                  type="button"
                  disabled={busyId === file.fileId}
                  onClick={() => handleDownload(file.fileId)}
                >
                  Download
                </button>

                <button
                  type="button"
                  className="danger"
                  disabled={busyId === file.fileId}
                  onClick={() => handleDelete(file.fileId, file.fileName)}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/FileList.jsx
git commit -m "feat(frontend): add file list with download and delete"
```

---

### Task 15: Wire the app, style it, and verify end-to-end in the browser

**Files:**
- Modify: `frontend/src/App.jsx` (replace Vite's default entirely)
- Modify: `frontend/src/App.css` (replace entirely)
- Modify: `frontend/src/index.css` (replace entirely)
- Create: `docs/screenshots/`

- [ ] **Step 1: Replace `frontend/src/App.jsx`**

```jsx
import { useCallback, useEffect, useState } from "react";
import FileList from "./components/FileList";
import FileUpload from "./components/FileUpload";
import { getFiles } from "./services/api";
import "./App.css";

export default function App() {
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadFiles = useCallback(async () => {
    setLoading(true);
    setError("");

    try {
      const result = await getFiles();
      setFiles(result.files ?? []);
    } catch (loadError) {
      setError(loadError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadFiles();
  }, [loadFiles]);

  return (
    <main>
      <header>
        <h1>CloudVault</h1>
        <p>Serverless file storage on AWS. Files transfer directly to S3 — never through the API.</p>
      </header>

      <FileUpload onUploaded={loadFiles} />

      {error && <p className="error" role="alert">{error}</p>}

      <FileList files={files} loading={loading} onChanged={loadFiles} />
    </main>
  );
}
```

- [ ] **Step 2: Replace `frontend/src/index.css`**

```css
:root {
  color-scheme: light dark;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.5;
}

body {
  margin: 0;
  background: #f5f6f8;
  color: #1c1e21;
}

@media (prefers-color-scheme: dark) {
  body {
    background: #16181c;
    color: #e8eaed;
  }
}
```

- [ ] **Step 3: Replace `frontend/src/App.css`**

```css
main {
  max-width: 60rem;
  margin: 0 auto;
  padding: 2rem 1rem 4rem;
}

header h1 {
  margin: 0 0 0.25rem;
  font-size: 2rem;
}

header p {
  margin: 0 0 2rem;
  opacity: 0.7;
}

.panel {
  background: light-dark(#ffffff, #22252a);
  border: 1px solid light-dark(#e3e5e8, #32363c);
  border-radius: 0.75rem;
  padding: 1.25rem 1.5rem;
  margin-bottom: 1.5rem;
}

.panel h2 {
  margin-top: 0;
  font-size: 1.1rem;
}

.upload-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: center;
}

button {
  border: 0;
  border-radius: 0.5rem;
  padding: 0.5rem 1rem;
  font: inherit;
  font-weight: 500;
  background: #2563eb;
  color: #ffffff;
  cursor: pointer;
}

button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

button.danger {
  background: #dc2626;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  text-align: left;
  padding: 0.6rem 0.5rem;
  border-bottom: 1px solid light-dark(#eceef1, #32363c);
}

th {
  font-size: 0.8rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  opacity: 0.6;
}

.actions {
  display: flex;
  gap: 0.5rem;
}

.status {
  margin: 0.75rem 0 0;
  opacity: 0.8;
}

.error {
  color: #dc2626;
  font-weight: 500;
}

@media (max-width: 40rem) {
  table,
  thead,
  tbody,
  tr,
  td {
    display: block;
  }

  thead {
    display: none;
  }

  tr {
    border-bottom: 1px solid light-dark(#eceef1, #32363c);
    padding: 0.5rem 0;
  }

  td {
    border: 0;
    padding: 0.15rem 0;
  }
}
```

- [ ] **Step 4: Run the app and walk the full flow**

```bash
cd frontend && npm run dev
```

At `http://localhost:5173`, verify each of these:

1. The list loads empty with "No files uploaded yet."
2. A PDF uploads and appears in the list immediately.
3. A PNG or JPEG uploads.
4. A `.txt` file uploads.
5. Choosing an unsupported type (e.g. a `.zip`) shows "Unsupported file type."
6. A file over 10 MB shows "Maximum file size is 10 MB."
7. Download returns the original file with its original filename.
8. Delete prompts for confirmation, then removes the row.
9. A hard refresh after upload still shows the file.
10. A hard refresh after delete still shows it gone.

- [ ] **Step 5: Cross-check S3 and DynamoDB directly**

```bash
BUCKET=$(cd ../terraform && terraform output -raw bucket_name)
aws s3 ls "s3://$BUCKET/uploads/"
aws s3 ls "s3://$BUCKET/staging/"
aws dynamodb scan --table-name cloudvault-files --region ap-south-1 --max-items 10
```

Expected: one object under `uploads/` per listed file, `staging/` empty, and matching DynamoDB rows.

- [ ] **Step 6: Capture screenshots**

Save to `docs/screenshots/`: `empty-state.png`, `uploading.png`, `file-list.png`, `validation-error.png`, `delete-confirm.png`.

- [ ] **Step 7: Commit**

```bash
cd .. && git add frontend/src docs/screenshots
git commit -m "feat(frontend): wire app shell, add styling, capture screenshots"
```

---

### Task 16: Architecture diagram and README

**Files:**
- Create: `docs/architecture.png`
- Create: `README.md`
- Create: `LICENSE`

- [ ] **Step 1: Produce the architecture diagram**

Draw in Excalidraw or diagrams.net and export to `docs/architecture.png`. It must show: Browser, API Gateway HTTP API, Lambda, DynamoDB, S3 with both `staging/` and `uploads/` prefixes, CloudWatch. Draw the browser→S3 arrows in a distinct colour from the API arrows — that contrast is the single most important thing on the diagram.

- [ ] **Step 2: Add an MIT `LICENSE`**

```bash
curl -s https://raw.githubusercontent.com/licenses/license-templates/master/templates/mit.txt \
  | sed "s/{{ year }}/2026/; s/{{ organization }}/Tanish Gupta/" > LICENSE
```

- [ ] **Step 3: Write `README.md`**

Required sections, in order:

1. **Title and one-line description.**
2. **Architecture** — embed `docs/architecture.png`.
3. **Why the file never passes through Lambda** — presigned POST up, presigned GET down; API Gateway's payload limit and Lambda memory never constrain file size; Lambda holds the only credentials.
4. **Upload flow** — the four steps, noting that S3 enforces type and size via POST policy conditions, and that `staging/`→`uploads/` promotion happens only after `head_object` verification.
5. **AWS services used** — table of service and role.
6. **API reference** — the five endpoints with request/response examples, copied from the spec.
7. **Local setup** — prerequisites, `aws configure`, `pip install -r backend/requirements-dev.txt`, `pytest`.
8. **Deployment** — `terraform init/plan/apply`, then `echo "VITE_API_URL=$(terraform output -raw api_url)" > frontend/.env`, then `npm install && npm run dev`.
9. **Testing** — `.venv/bin/pytest`, what the suite covers.
10. **Security decisions** — reproduce §8 of the spec verbatim, including the explicit statement that authentication is out of scope for v1.
11. **Known limitations** — no auth; `Scan` instead of `Query`; single-part uploads capped at 10 MB; local Terraform state.
12. **Future improvements** — v2 Cognito with `userId` partition key, v3 S3-event-driven confirm, remote state, CI.
13. **Screenshots.**
14. **Cleanup** — `terraform destroy`.

- [ ] **Step 4: Verify every command in the README actually runs**

Work through the Local setup and Deployment sections literally, top to bottom, in a fresh shell. Fix anything that doesn't work as written.

- [ ] **Step 5: Commit**

```bash
git add README.md LICENSE docs/architecture.png
git commit -m "docs: add README, architecture diagram, and license"
```

---

### Task 17: Teardown verification

Proves the infrastructure is genuinely reproducible and leaves nothing behind — including the log group, which is the thing that normally survives.

- [ ] **Step 1: Destroy**

```bash
cd terraform && terraform destroy
```

Expected: all resources destroyed. `force_destroy = true` lets the bucket go even with objects in it.

- [ ] **Step 2: Verify nothing remains**

```bash
aws s3 ls | grep cloudvault || echo "no buckets"
aws dynamodb list-tables --region ap-south-1 | grep cloudvault || echo "no tables"
aws lambda list-functions --region ap-south-1 | grep cloudvault || echo "no functions"
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/cloudvault --region ap-south-1
aws cloudwatch describe-alarms --alarm-name-prefix cloudvault --region ap-south-1
```

Expected: no buckets, no tables, no functions, an empty `logGroups` array, an empty `MetricAlarms` array.

The empty `logGroups` array is the specific payoff of declaring `aws_cloudwatch_log_group` in Terraform.

- [ ] **Step 3: Verify a clean rebuild**

```bash
terraform apply
curl -s "$(terraform output -raw api_url)/files"
terraform destroy
```

Expected: `{"files": []}` from a stack built entirely from source. This is the claim the README makes — verify it rather than assume it.

- [ ] **Step 4: Push**

```bash
cd .. && git log --oneline
gh repo create cloudvault --public --source=. --push
```

---

## Notes for the implementer

- **Run `terraform destroy` at the end of every session.** v1 has no authentication; a live API is an open bucket.
- If a CORS error appears in the browser, check two separate places: `aws_apigatewayv2_api.http.cors_configuration` for API calls, and `aws_s3_bucket_cors_configuration.files` for the direct upload. They fail independently and produce identical-looking console errors.
- If `terraform apply` says the Lambda code didn't change after you edited a module, confirm the edit landed inside `backend/app/` — `archive_file` only watches that directory.
- Presigned POST returns **204**, presigned PUT returns 200. Anything treating 200 as the success condition will report a false failure.
