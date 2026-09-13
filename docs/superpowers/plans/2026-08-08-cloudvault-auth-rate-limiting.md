# CloudVault Authentication and Rate Limiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add public Cognito signup, managed login, strict per-user file isolation, and simple shared API Gateway throttling to CloudVault.

**Architecture:** Cognito issues OAuth access tokens to the React SPA, API Gateway validates them with a JWT authorizer, and Lambda derives ownership exclusively from the verified `sub` claim. DynamoDB uses `(userId, fileId)`, S3 keys include `userId`, and API Gateway applies best-effort stage throttling at 5 requests per second with a burst of 10.

**Tech Stack:** React 19, Vite 8, AWS Amplify Auth v6, Python 3.13 Lambda, API Gateway HTTP API v2, Cognito User Pools, DynamoDB, S3, Terraform AWS provider v5, pytest, moto.

**Spec:** `docs/superpowers/specs/2026-08-08-cloudvault-auth-rate-limiting-design.md`

---

## Global constraints

- Work only in the existing `feature/cloudvault-v1` worktree.
- Do not recreate AWS infrastructure until the automated backend, frontend, and Terraform checks pass.
- The next apply creates a fresh composite-key DynamoDB table. Existing metadata is not migrated.
- Trust only `event.requestContext.authorizer.jwt.claims.sub` as the user ID.
- Never accept ownership from a request body, query string, path parameter, email address, or filename.
- Use the Cognito access token, not the ID token, in the API `Authorization` header.
- Require `authorization_scopes = ["openid"]` on all five file routes.
- Use exact local callback and logout URLs: `http://localhost:5173/`.
- Keep file bytes direct between the browser and S3.
- Keep throttling API-wide and best-effort; do not build a DynamoDB rate limiter.
- Return `404` for cross-user file access.
- Never log tokens, presigned grants, passwords, verification codes, or file contents.
- Run `terraform destroy` after the manual AWS verification.

---

### Task 1: Authentication-context helper

**Files:**
- Create: `backend/app/auth.py`
- Create: `backend/tests/test_auth.py`
- Modify: `backend/app/handler.py`
- Modify: `backend/tests/test_handler.py`

**Interfaces:**
- Produces `AuthenticationError` and `authenticated_user_id(event) -> str`.
- Handler maps direct invocations with missing authentication context to HTTP `401`.

- [ ] **Step 1: Write the failing authentication-helper tests**

Create `backend/tests/test_auth.py`:

```python
import pytest

from auth import AuthenticationError, authenticated_user_id


def authenticated_event(sub="user-a-sub"):
    return {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {"sub": sub, "email": "user@example.com"}
                }
            }
        }
    }


def test_authenticated_user_id_returns_verified_sub():
    assert authenticated_user_id(authenticated_event()) == "user-a-sub"


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"requestContext": {}},
        {"requestContext": {"authorizer": {}}},
        {"requestContext": {"authorizer": {"jwt": {}}}},
        {"requestContext": {"authorizer": {"jwt": {"claims": {}}}}},
        authenticated_event(sub=""),
        authenticated_event(sub=123),
    ],
)
def test_authenticated_user_id_rejects_missing_or_invalid_sub(event):
    with pytest.raises(AuthenticationError, match="^Authentication required$"):
        authenticated_user_id(event)


def test_authenticated_user_id_ignores_untrusted_user_id_fields():
    event = authenticated_event()
    event["body"] = '{"userId":"attacker"}'
    event["queryStringParameters"] = {"userId": "attacker"}

    assert authenticated_user_id(event) == "user-a-sub"
```

- [ ] **Step 2: Run the helper tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_auth.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'auth'`.

- [ ] **Step 3: Implement the authentication helper**

Create `backend/app/auth.py`:

```python
"""Authentication context extracted from API Gateway's verified JWT."""


class AuthenticationError(Exception):
    """The Lambda invocation has no usable authenticated subject."""


def authenticated_user_id(event: dict) -> str:
    if not isinstance(event, dict):
        raise AuthenticationError("Authentication required")

    try:
        user_id = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        raise AuthenticationError("Authentication required") from None

    if not isinstance(user_id, str) or not user_id:
        raise AuthenticationError("Authentication required")

    return user_id
```

- [ ] **Step 4: Run the helper tests to verify GREEN**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_auth.py -q
```

Expected: all authentication-helper tests pass.

- [ ] **Step 5: Write the failing handler mapping test**

Append to `backend/tests/test_handler.py`:

```python
from auth import AuthenticationError


def test_authentication_error_returns_401(monkeypatch):
    def unauthenticated(_event):
        raise AuthenticationError("Authentication required")

    monkeypatch.setattr(files, "create_upload_url", unauthenticated)

    result = handler.lambda_handler(
        {"routeKey": "POST /files/upload-url"}, None
    )

    assert result["statusCode"] == 401
    assert body(result) == {"message": "Authentication required"}
    assert_cors(result)
```

- [ ] **Step 6: Run the handler test to verify RED**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_handler.py::test_authentication_error_returns_401 -q
```

Expected: the result is `500`, not `401`.

- [ ] **Step 7: Map authentication errors in the handler**

In `backend/app/handler.py`, add the import:

```python
from auth import AuthenticationError
```

Add this exception branch immediately before `except ValidationError`:

```python
    except AuthenticationError as exc:
        return response(401, {"message": str(exc)})
```

- [ ] **Step 8: Run focused and full backend tests**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_auth.py backend/tests/test_handler.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add backend/app/auth.py backend/app/handler.py \
  backend/tests/test_auth.py backend/tests/test_handler.py
git commit -m "feat(auth): extract authenticated Cognito subject"
```

---

### Task 2: Shared authenticated-event test fixtures

**Files:**
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_files_upload_url.py`

**Interfaces:**
- Produces `USER_A`, `USER_B`, and `with_auth(event=None, user_id=USER_A)` for all route tests.
- Upload policy keys become user-prefixed before the DynamoDB schema changes.

- [ ] **Step 1: Add authenticated-event test helpers**

Append to `backend/tests/conftest.py`:

```python
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
```

- [ ] **Step 2: Write failing per-user upload-key tests**

In `backend/tests/test_files_upload_url.py`, import the helpers:

```python
from conftest import USER_A, USER_B, with_auth
```

Change `event_for` to return an authenticated event:

```python
def event_for(*, user_id=USER_A, **overrides):
    body = {
        "fileName": "resume.pdf",
        "contentType": "application/pdf",
        "size": 1024,
    }
    body.update(overrides)
    return with_auth({"body": json.dumps(body)}, user_id=user_id)
```

Add:

```python
def test_key_is_scoped_to_authenticated_user(aws):
    body = json.loads(create_upload_url(event_for(user_id=USER_A))["body"])

    assert body["s3Key"].startswith(f"staging/{USER_A}/{body['fileId']}-")


def test_same_filename_for_two_users_has_separate_prefixes(aws):
    first = json.loads(create_upload_url(event_for(user_id=USER_A))["body"])
    second = json.loads(create_upload_url(event_for(user_id=USER_B))["body"])

    assert first["s3Key"].startswith(f"staging/{USER_A}/")
    assert second["s3Key"].startswith(f"staging/{USER_B}/")
```

Update existing staging-key assertions in this test file from:

```python
f"{STAGING_PREFIX}{body['fileId']}-"
```

to:

```python
f"{STAGING_PREFIX}{USER_A}/{body['fileId']}-"
```

- [ ] **Step 3: Run upload tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_files_upload_url.py -q
```

Expected: failures show keys begin with `staging/{fileId}-` instead of the user prefix.

- [ ] **Step 4: Scope upload policies to the authenticated user**

In `backend/app/files.py`, add:

```python
from auth import authenticated_user_id
```

At the start of `create_upload_url`, before parsing the body, add:

```python
    user_id = authenticated_user_id(event)
```

Change the staging key to:

```python
    staging_key = f"{STAGING_PREFIX}{user_id}/{file_id}-{file_name}"
```

- [ ] **Step 5: Update the confirm-test upload request helper to authenticate**

In `backend/tests/test_files_confirm.py`, import:

```python
from conftest import USER_A, with_auth
```

Change `_upload_request` to:

```python
def _upload_request(*, user_id=USER_A, **overrides):
    body = {
        "fileName": "resume.pdf",
        "contentType": "application/pdf",
        "size": 12,
    }
    body.update(overrides)
    return with_auth({"body": json.dumps(body)}, user_id=user_id)
```

Change `_stage_upload` so its returned confirm event carries the same user:

```python
def _stage_upload(aws, *, content=b"hello world!", user_id=USER_A, **overrides):
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
```

- [ ] **Step 6: Run the upload and confirm suites**

Run:

```bash
.venv/bin/python -m pytest \
  backend/tests/test_files_upload_url.py \
  backend/tests/test_files_confirm.py -q
```

Expected: both suites pass; confirm still uses the old DynamoDB key temporarily.

- [ ] **Step 7: Commit**

```bash
git add backend/app/files.py backend/tests/conftest.py \
  backend/tests/test_files_upload_url.py backend/tests/test_files_confirm.py
git commit -m "feat(storage): scope staging uploads to Cognito users"
```

---

### Task 3: Composite DynamoDB schema and confirmed-file ownership

**Files:**
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_files_confirm.py`
- Modify: `backend/app/files.py`

**Interfaces:**
- DynamoDB keys become `(userId, fileId)`.
- Confirmed S3 keys become `uploads/{userId}/{fileId}-{name}`.

- [ ] **Step 1: Change the Moto table fixture to the composite key**

Replace the DynamoDB creation block in `backend/tests/conftest.py` with:

```python
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
```

- [ ] **Step 2: Update confirm expectations and add isolation tests**

In `backend/tests/test_files_confirm.py`, update expected items to include:

```python
"userId": USER_A,
```

Update expected upload keys to:

```python
f"uploads/{USER_A}/{requested['fileId']}-resume.pdf"
```

Update direct table reads to:

```python
aws["table"].get_item(
    Key={"userId": USER_A, "fileId": requested["fileId"]}
)
```

Update the consistent precheck assertion to:

```python
table.get_item.assert_called_once_with(
    Key={"userId": USER_A, "fileId": requested["fileId"]},
    ConsistentRead=True,
)
```

Wrap every confirm event assembled directly inside a test with `with_auth(...)`, including
the missing-object case and every test that mutates an `_stage_upload` event body. For
example, change:

```python
confirm_upload({"body": json.dumps(body)})
```

to:

```python
confirm_upload(with_auth({"body": json.dumps(body)}))
```

Likewise, pass `with_auth({"body": ...})` in the missing-fields test so validation—not
authentication—remains the behavior under test.

Add:

```python
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
```

- [ ] **Step 3: Run confirm tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_files_confirm.py -q
```

Expected: failures show missing `userId`, incomplete DynamoDB keys, and non-user-prefixed
destination keys.

- [ ] **Step 4: Implement user-owned confirmation**

In `confirm_upload`, derive the user first:

```python
    user_id = authenticated_user_id(event)
```

Require the exact staging key:

```python
    if staging_key != f"{STAGING_PREFIX}{user_id}/{file_id}-{file_name}":
        raise ValidationError("Invalid S3 object key")
```

Use the composite key for the replay check:

```python
    item_key = {"userId": user_id, "fileId": file_id}
    table = _table()
    if "Item" in table.get_item(Key=item_key, ConsistentRead=True):
        raise ConflictError("File already confirmed")
```

Use the user prefix for the destination:

```python
    destination_key = f"{UPLOADS_PREFIX}{user_id}/{file_id}-{file_name}"
```

Add `userId` to the item:

```python
    item = {
        "userId": user_id,
        "fileId": file_id,
        "fileName": file_name,
        "s3Key": destination_key,
        "contentType": uploaded.get("ContentType", "application/octet-stream"),
        "size": actual_size,
        "uploadedAt": datetime.now(timezone.utc).isoformat(),
    }
```

Change the conditional write to:

```python
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(userId) AND attribute_not_exists(fileId)"
            ),
        )
```

- [ ] **Step 5: Run confirm tests to verify GREEN**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_files_confirm.py -q
```

Expected: all confirm tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/files.py backend/tests/conftest.py \
  backend/tests/test_files_confirm.py
git commit -m "feat(storage): store confirmed files by user"
```

---

### Task 4: User-partitioned listing

**Files:**
- Modify: `backend/app/files.py`
- Modify: `backend/app/handler.py`
- Modify: `backend/tests/test_files_list.py`
- Modify: `backend/tests/test_handler.py`

**Interfaces:**
- `list_files(event)` queries one `userId` partition.

- [ ] **Step 1: Write failing user-isolated list tests**

Replace `backend/tests/test_files_list.py` with:

```python
import json
from decimal import Decimal
from unittest.mock import Mock, call

import files
from boto3.dynamodb.conditions import Key
from conftest import USER_A, USER_B, with_auth
from files import list_files


def _body(result):
    return json.loads(result["body"])


def _item(user_id, file_id, uploaded_at, size=None):
    item = {
        "userId": user_id,
        "fileId": file_id,
        "uploadedAt": uploaded_at,
    }
    if size is not None:
        item["size"] = size
    return item


def test_list_files_returns_empty_list_for_authenticated_user(aws):
    assert _body(list_files(with_auth())) == {"files": []}


def test_list_files_returns_only_authenticated_users_items(aws):
    own = _item(USER_A, "own", "2026-02-01T00:00:00+00:00")
    other = _item(USER_B, "other", "2026-03-01T00:00:00+00:00")
    aws["table"].put_item(Item=own)
    aws["table"].put_item(Item=other)

    assert _body(list_files(with_auth(user_id=USER_A)))["files"] == [own]


def test_list_files_returns_newest_first_and_serializes_decimal(aws):
    older = _item(
        USER_A, "older", "2026-01-01T00:00:00+00:00", Decimal("42")
    )
    newer = _item(
        USER_A, "newer", "2026-02-01T00:00:00+00:00", Decimal("1.5")
    )
    aws["table"].put_item(Item=older)
    aws["table"].put_item(Item=newer)

    listed = _body(list_files(with_auth()))["files"]

    assert [item["fileId"] for item in listed] == ["newer", "older"]
    assert listed[0]["size"] == 1.5
    assert listed[1]["size"] == 42


def test_list_files_paginates_query_with_the_same_user_key(monkeypatch):
    page_key = {"userId": USER_A, "fileId": "page-one-last"}
    table = Mock()
    table.query.side_effect = [
        {
            "Items": [_item(USER_A, "oldest", "2026-01-01T00:00:00+00:00")],
            "LastEvaluatedKey": page_key,
        },
        {
            "Items": [_item(USER_A, "newest", "2026-03-01T00:00:00+00:00")]
        },
    ]
    monkeypatch.setattr(files, "_table", Mock(return_value=table))

    result = _body(list_files(with_auth()))["files"]

    assert [item["fileId"] for item in result] == ["newest", "oldest"]
    assert table.query.call_args_list == [
        call(KeyConditionExpression=Key("userId").eq(USER_A)),
        call(
            KeyConditionExpression=Key("userId").eq(USER_A),
            ExclusiveStartKey=page_key,
        ),
    ]
    table.scan.assert_not_called()
```

- [ ] **Step 2: Run list tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_files_list.py -q
```

Expected: `list_files` rejects the event argument and still calls `scan`.

- [ ] **Step 3: Implement partition query**

Add to `backend/app/files.py`:

```python
from boto3.dynamodb.conditions import Key
```

Replace `list_files` with:

```python
def list_files(event: dict) -> dict:
    user_id = authenticated_user_id(event)
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
```

Change the handler route to pass the event:

```python
"GET /files": lambda event: files.list_files(event),
```

- [ ] **Step 4: Update handler dispatch test**

Replace `test_list_route_calls_list_files_without_arguments` with:

```python
def test_list_route_passes_event(monkeypatch):
    event = {"routeKey": "GET /files", "marker": "list"}
    calls = []

    def fake_list_files(received):
        calls.append(received)
        return response(200, {"files": []})

    monkeypatch.setattr(files, "list_files", fake_list_files)

    result = handler.lambda_handler(event, None)

    assert calls == [event]
    assert result["statusCode"] == 200
```

Update the unexpected-error test's fake list function to accept `_event`.

- [ ] **Step 5: Run focused and full tests**

Run:

```bash
.venv/bin/python -m pytest \
  backend/tests/test_files_list.py backend/tests/test_handler.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/files.py backend/app/handler.py \
  backend/tests/test_files_list.py backend/tests/test_handler.py
git commit -m "feat(storage): query files by authenticated user"
```

---

### Task 5: User-isolated download and delete

**Files:**
- Modify: `backend/app/files.py`
- Modify: `backend/tests/test_files_download_delete.py`

**Interfaces:**
- File lookup and deletion always use `(userId, fileId)`.
- Cross-user lookup returns `NotFoundError`.

- [ ] **Step 1: Convert download/delete test helpers to composite keys**

In `backend/tests/test_files_download_delete.py`, import:

```python
from conftest import BUCKET, USER_A, USER_B, with_auth
```

Replace `stored_file` and `event_for` with:

```python
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
```

Update all direct DynamoDB keys in this file to:

```python
{"userId": USER_A, "fileId": "abc123"}
```

Wrap the raw missing-path events with `with_auth(...)` so those tests continue to exercise
file-ID validation after authentication is introduced:

```python
@pytest.mark.parametrize(
    "event",
    [with_auth({"pathParameters": {}}), with_auth({})],
)
```

Use `with_auth({"pathParameters": None})` in the equivalent delete test.

- [ ] **Step 2: Add cross-user isolation tests**

Add:

```python
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
```

Update the consistent-read assertion to:

```python
table.get_item.assert_called_once_with(
    Key={"userId": USER_A, "fileId": "abc123"}, ConsistentRead=True
)
```

Update delete-call assertions to expect:

```python
"Key": {"userId": USER_A, "fileId": "abc123"}
```

Update mocks of `_get_item_or_404` to accept `(user_id, file_id)`, and include `userId` in
mocked and replacement items. For example:

```python
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
```

- [ ] **Step 3: Run download/delete tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_files_download_delete.py -q
```

Expected: lookup uses incomplete keys and cross-user tests fail.

- [ ] **Step 4: Implement composite-key lookup and delete**

Replace `_get_item_or_404` with:

```python
def _get_item_or_404(user_id: str, file_id: str) -> dict:
    item = _table().get_item(
        Key={"userId": user_id, "fileId": file_id},
        ConsistentRead=True,
    ).get("Item")

    if not item:
        raise NotFoundError("File not found")

    return item
```

At the start of both `create_download_url` and `delete_file`, derive the user:

```python
    user_id = authenticated_user_id(event)
```

Pass it to lookup:

```python
    item = _get_item_or_404(user_id, file_id)
```

Change the delete key to:

```python
Key={"userId": user_id, "fileId": file_id},
```

- [ ] **Step 5: Run focused and full tests**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_files_download_delete.py -q
.venv/bin/python -m pytest -q
```

Expected: all tests pass, including both cross-user tests.

- [ ] **Step 6: Commit**

```bash
git add backend/app/files.py backend/tests/test_files_download_delete.py
git commit -m "feat(storage): isolate downloads and deletes by user"
```

---

### Task 6: Terraform composite table and IAM permissions

**Files:**
- Modify: `terraform/dynamodb.tf`
- Modify: `terraform/iam.tf`

- [ ] **Step 1: Change the DynamoDB key schema**

Replace `terraform/dynamodb.tf` with:

```hcl
resource "aws_dynamodb_table" "files" {
  name         = "${var.project_name}-files"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "userId"
  range_key    = "fileId"

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "fileId"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }
}
```

- [ ] **Step 2: Replace scan permission with query permission**

In `terraform/iam.tf`, change:

```hcl
"dynamodb:Scan"
```

to:

```hcl
"dynamodb:Query"
```

- [ ] **Step 3: Format and validate Terraform**

Run:

```bash
terraform -chdir=terraform fmt -recursive
terraform -chdir=terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Confirm the AWS stack remains absent**

Run:

```bash
terraform -chdir=terraform state list
```

Expected: no resources. Do not run `terraform apply` yet.

- [ ] **Step 5: Commit**

```bash
git add terraform/dynamodb.tf terraform/iam.tf
git commit -m "feat(infra): partition file metadata by user"
```

---

### Task 7: Cognito infrastructure and public configuration

**Files:**
- Create: `terraform/cognito.tf`
- Modify: `terraform/variables.tf`
- Modify: `terraform/outputs.tf`

- [ ] **Step 1: Add exact frontend redirect variables**

Append to `terraform/variables.tf`:

```hcl
variable "frontend_callback_url" {
  description = "Exact OAuth callback URL for the local frontend"
  type        = string
  default     = "http://localhost:5173/"
}

variable "frontend_logout_url" {
  description = "Exact OAuth logout URL for the local frontend"
  type        = string
  default     = "http://localhost:5173/"
}
```

- [ ] **Step 2: Create Cognito resources**

Create `terraform/cognito.tf`:

```hcl
data "aws_caller_identity" "current" {}

resource "aws_cognito_user_pool" "users" {
  name = "${var.project_name}-users"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  username_configuration {
    case_sensitive = false
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "frontend" {
  name         = "${var.project_name}-frontend"
  user_pool_id = aws_cognito_user_pool.users.id

  generate_secret                      = false
  prevent_user_existence_errors        = "ENABLED"
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = [var.frontend_callback_url]
  logout_urls                          = [var.frontend_logout_url]

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "frontend" {
  domain       = "${var.project_name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.users.id
}
```

- [ ] **Step 3: Add Cognito outputs**

Append to `terraform/outputs.tf`:

```hcl
output "cognito_user_pool_id" {
  description = "Cognito user pool for CloudVault users"
  value       = aws_cognito_user_pool.users.id
}

output "cognito_user_pool_client_id" {
  description = "Public Cognito SPA app-client ID"
  value       = aws_cognito_user_pool_client.frontend.id
}

output "cognito_domain" {
  description = "Cognito managed-login domain without a URL scheme"
  value = format(
    "%s.auth.%s.amazoncognito.com",
    aws_cognito_user_pool_domain.frontend.domain,
    var.aws_region,
  )
}

output "aws_region" {
  description = "AWS region used by CloudVault"
  value       = var.aws_region
}
```

- [ ] **Step 4: Format, validate, and inspect outputs**

Run:

```bash
terraform -chdir=terraform fmt -recursive
terraform -chdir=terraform validate
terraform -chdir=terraform providers
```

Expected: valid configuration using only the existing AWS and archive providers.

- [ ] **Step 5: Commit**

```bash
git add terraform/cognito.tf terraform/variables.tf terraform/outputs.tf
git commit -m "feat(auth): provision Cognito managed login"
```

---

### Task 8: JWT route protection and shared throttling

**Files:**
- Modify: `terraform/api_gateway.tf`

- [ ] **Step 1: Add the JWT authorizer**

Add after the HTTP API resource:

```hcl
resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.http.id
  name             = "${var.project_name}-cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.frontend.id]
    issuer   = "https://${aws_cognito_user_pool.users.endpoint}"
  }
}
```

- [ ] **Step 2: Protect all five routes**

Replace the route resource with:

```hcl
resource "aws_apigatewayv2_route" "routes" {
  for_each = local.routes

  api_id               = aws_apigatewayv2_api.http.id
  route_key            = each.value
  target               = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.cognito.id
  authorization_scopes = ["openid"]
}
```

- [ ] **Step 3: Allow the authorization header in CORS**

Change the HTTP API CORS headers to:

```hcl
allow_headers = ["authorization", "content-type"]
```

- [ ] **Step 4: Add shared stage throttling**

Add inside `aws_apigatewayv2_stage.default`:

```hcl
  default_route_settings {
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
```

- [ ] **Step 5: Add a static Terraform contract test**

Create `backend/tests/test_terraform_auth_contract.py`:

```python
from pathlib import Path


ROOT = Path(__file__).parents[2]
API_GATEWAY = (ROOT / "terraform" / "api_gateway.tf").read_text()
COGNITO = (ROOT / "terraform" / "cognito.tf").read_text()


def test_all_dynamic_file_routes_require_the_jwt_authorizer():
    assert 'authorization_type   = "JWT"' in API_GATEWAY
    assert "authorizer_id" in API_GATEWAY
    assert 'authorization_scopes = ["openid"]' in API_GATEWAY
    assert 'for_each = local.routes' in API_GATEWAY


def test_cors_allows_bearer_token_header():
    assert 'allow_headers = ["authorization", "content-type"]' in API_GATEWAY


def test_shared_throttle_is_small_and_explicit():
    assert "throttling_rate_limit  = 5" in API_GATEWAY
    assert "throttling_burst_limit = 10" in API_GATEWAY


def test_spa_client_has_no_secret_and_uses_code_flow():
    assert "generate_secret" in COGNITO
    assert "generate_secret                      = false" in COGNITO
    assert 'allowed_oauth_flows                  = ["code"]' in COGNITO
```

- [ ] **Step 6: Run contract tests and Terraform validation**

Run:

```bash
.venv/bin/python -m pytest backend/tests/test_terraform_auth_contract.py -q
terraform -chdir=terraform fmt -recursive
terraform -chdir=terraform validate
```

Expected: contract tests pass and Terraform is valid.

- [ ] **Step 7: Commit**

```bash
git add terraform/api_gateway.tf backend/tests/test_terraform_auth_contract.py
git commit -m "feat(auth): protect API routes and add throttling"
```

---

### Task 9: Amplify Auth configuration

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `frontend/src/auth/config.js`
- Modify: `frontend/src/main.jsx`
- Modify: `frontend/.env.example`

- [ ] **Step 1: Install Amplify**

Run:

```bash
cd frontend
npm install aws-amplify
```

Expected: `aws-amplify` appears in dependencies and npm reports zero known vulnerabilities.

- [ ] **Step 2: Create public Auth configuration**

Create `frontend/src/auth/config.js`:

```javascript
import { Amplify } from "aws-amplify";

Amplify.configure({
  Auth: {
    Cognito: {
      userPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID,
      userPoolClientId: import.meta.env.VITE_COGNITO_USER_POOL_CLIENT_ID,
      loginWith: {
        oauth: {
          domain: import.meta.env.VITE_COGNITO_DOMAIN,
          scopes: ["openid", "email", "profile"],
          redirectSignIn: ["http://localhost:5173/"],
          redirectSignOut: ["http://localhost:5173/"],
          responseType: "code",
        },
      },
    },
  },
});
```

- [ ] **Step 3: Load Auth configuration before React renders**

Add this as the first local import in `frontend/src/main.jsx`:

```javascript
import "./auth/config";
```

- [ ] **Step 4: Document the required frontend environment**

Create `frontend/.env.example`:

```dotenv
VITE_API_URL=https://example.execute-api.ap-south-1.amazonaws.com
VITE_COGNITO_USER_POOL_ID=ap-south-1_example
VITE_COGNITO_USER_POOL_CLIENT_ID=exampleclientid
VITE_COGNITO_DOMAIN=cloudvault-123456789012.auth.ap-south-1.amazoncognito.com
```

Do not commit the deployment-specific `frontend/.env`.

- [ ] **Step 5: Run frontend verification**

Run:

```bash
cd frontend
npm run lint
npm run build
```

Expected: lint and Vite build pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/package.json frontend/package-lock.json \
  frontend/src/auth/config.js frontend/src/main.jsx frontend/.env.example
git commit -m "feat(auth): configure Cognito OAuth in React"
```

---

### Task 10: Access-token API client and rate-limit errors

**Files:**
- Modify: `frontend/src/services/api.js`

- [ ] **Step 1: Add the authenticated API request helper**

At the top of `frontend/src/services/api.js`, add:

```javascript
import { fetchAuthSession } from "aws-amplify/auth";
```

Replace the existing `const API_URL` declaration with an exported declaration, add a
small token accessor for authenticated diagnostics, and use it from the request helper:

```javascript
export const API_URL = import.meta.env.VITE_API_URL;

export async function getAccessToken() {
  const session = await fetchAuthSession();
  const token = session.tokens?.accessToken?.toString();

  if (!token) {
    throw new Error("Sign in to continue.");
  }

  return token;
}

async function authenticatedFetch(path, options = {}) {
  const token = await getAccessToken();

  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);

  return fetch(`${API_URL}${path}`, { ...options, headers });
}
```

- [ ] **Step 2: Add explicit 401 and 429 response messages**

Replace `parseResponse` with:

```javascript
async function parseResponse(response) {
  const body = await response.json().catch(() => ({}));

  if (response.status === 401) {
    throw new Error("Your session has expired. Sign in again.");
  }

  if (response.status === 429) {
    throw new Error("Too many requests. Please wait a moment and try again.");
  }

  if (!response.ok) {
    throw new Error(body.message || `Request failed (${response.status})`);
  }

  return body;
}
```

- [ ] **Step 3: Route API Gateway calls through authenticatedFetch**

Use these exact implementations:

```javascript
export async function requestUploadUrl(file) {
  const response = await authenticatedFetch("/files/upload-url", {
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

export async function confirmUpload(uploadData, file) {
  const response = await authenticatedFetch("/files/confirm", {
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
  return parseResponse(await authenticatedFetch("/files"));
}

export async function getDownloadUrl(fileId) {
  return parseResponse(
    await authenticatedFetch(
      `/files/${encodeURIComponent(fileId)}/download`,
    ),
  );
}

export async function deleteFile(fileId) {
  return parseResponse(
    await authenticatedFetch(`/files/${encodeURIComponent(fileId)}`, {
      method: "DELETE",
    }),
  );
}
```

Leave `uploadFileToS3` unchanged so the Cognito JWT is never sent to S3.

- [ ] **Step 4: Run lint and build**

Run:

```bash
cd frontend
npm run lint
npm run build
```

Expected: lint and build pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/services/api.js
git commit -m "feat(auth): attach access tokens to API requests"
```

---

### Task 11: Signed-in and signed-out application states

**Files:**
- Create: `frontend/src/components/AuthPanel.jsx`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/App.css`

- [ ] **Step 1: Create the authentication controls**

Create `frontend/src/components/AuthPanel.jsx`:

```jsx
import { signInWithRedirect, signOut } from "aws-amplify/auth";

export default function AuthPanel({ user, onAuthChanged }) {
  async function handleSignOut() {
    await signOut();
    await onAuthChanged();
  }

  if (!user) {
    return (
      <section className="panel auth-panel">
        <h2>Private file storage</h2>
        <p>Create an account or sign in to access your files.</p>
        <button type="button" onClick={() => signInWithRedirect()}>
          Sign in or create account
        </button>
      </section>
    );
  }

  return (
    <div className="account-row">
      <span>{user.email}</span>
      <button type="button" className="secondary" onClick={handleSignOut}>
        Sign out
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Replace App with session-aware rendering**

Replace `frontend/src/App.jsx` with:

```jsx
import { useCallback, useEffect, useState } from "react";
import { fetchUserAttributes, getCurrentUser } from "aws-amplify/auth";
import { Hub } from "aws-amplify/utils";
import AuthPanel from "./components/AuthPanel";
import FileList from "./components/FileList";
import FileUpload from "./components/FileUpload";
import { getFiles } from "./services/api";
import "./App.css";

export default function App() {
  const [user, setUser] = useState(null);
  const [checkingAuth, setCheckingAuth] = useState(true);
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const loadSession = useCallback(async () => {
    setCheckingAuth(true);
    try {
      await getCurrentUser();
      const attributes = await fetchUserAttributes();
      setUser({ email: attributes.email ?? "Signed-in user" });
    } catch {
      setUser(null);
      setFiles([]);
    } finally {
      setCheckingAuth(false);
    }
  }, []);

  const loadFiles = useCallback(async () => {
    if (!user) return;
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
  }, [user]);

  useEffect(() => {
    loadSession();
    return Hub.listen("auth", ({ payload }) => {
      if (["signedIn", "signedOut"].includes(payload.event)) {
        loadSession();
      }
    });
  }, [loadSession]);

  useEffect(() => {
    if (user) loadFiles();
  }, [user, loadFiles]);

  return (
    <main>
      <header>
        <div>
          <h1>CloudVault</h1>
          <p>Private serverless file storage on AWS.</p>
        </div>
        {user && <AuthPanel user={user} onAuthChanged={loadSession} />}
      </header>

      {checkingAuth ? (
        <p className="status">Checking your session...</p>
      ) : user ? (
        <>
          <FileUpload onUploaded={loadFiles} />
          {error && <p className="error" role="alert">{error}</p>}
          <FileList files={files} loading={loading} onChanged={loadFiles} />
        </>
      ) : (
        <AuthPanel user={null} onAuthChanged={loadSession} />
      )}
    </main>
  );
}
```

- [ ] **Step 3: Add authentication styles**

Append to `frontend/src/App.css`:

```css
header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}

.auth-panel p {
  margin-bottom: 1rem;
}

.account-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

button.secondary {
  background: #475569;
}

@media (max-width: 40rem) {
  header,
  .account-row {
    align-items: stretch;
    flex-direction: column;
  }
}
```

- [ ] **Step 4: Run frontend verification**

Run:

```bash
cd frontend
npm run lint
npm run build
```

Expected: lint and production build pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AuthPanel.jsx frontend/src/App.jsx frontend/src/App.css
git commit -m "feat(auth): add managed-login application states"
```

---

### Task 12: Full local verification and deployment configuration

**Files:**
- Modify: `frontend/.env` (ignored; do not commit)

- [ ] **Step 1: Run every automated check before AWS deployment**

Run:

```bash
.venv/bin/python -m pytest -q
cd frontend && npm run lint && npm run build && cd ..
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
git diff --check
```

Expected: all backend tests pass, frontend lint/build passes, Terraform is valid, and Git
reports no whitespace errors.

- [ ] **Step 2: Review the Terraform creation plan**

Run:

```bash
aws sts get-caller-identity
terraform -chdir=terraform plan -out=/tmp/cloudvault-auth.tfplan
```

Expected:

- The active identity is the intended AWS account.
- The plan contains only CloudVault additions because Terraform state is empty.
- Cognito, JWT authorizer, composite DynamoDB table, and stage throttle are present.
- No Cognito client secret is present.
- There are no destroys of unrelated infrastructure.

- [ ] **Step 3: Apply the reviewed plan**

Run:

```bash
terraform -chdir=terraform apply /tmp/cloudvault-auth.tfplan
```

Expected: apply completes without errors.

- [ ] **Step 4: Create the ignored frontend environment file**

Read the four deployment values from the repository root:

```bash
terraform -chdir=terraform output -raw api_url
terraform -chdir=terraform output -raw cognito_user_pool_id
terraform -chdir=terraform output -raw cognito_user_pool_client_id
terraform -chdir=terraform output -raw cognito_domain
```

Use `apply_patch` to create `frontend/.env` with the four exact printed values assigned to
`VITE_API_URL`, `VITE_COGNITO_USER_POOL_ID`,
`VITE_COGNITO_USER_POOL_CLIENT_ID`, and `VITE_COGNITO_DOMAIN`, respectively. Do not
commit this file.

Expected: `frontend/.env` contains the four public deployment values, contains no client
secret, and remains ignored by Git.

- [ ] **Step 5: Verify the deployed authorization boundary**

Run:

```bash
cv_api_url=$(terraform -chdir=terraform output -raw api_url)
curl --silent --output /dev/null --write-out '%{http_code}\n' "$cv_api_url/files"
```

Expected: `401`.

- [ ] **Step 6: Verify Terraform has no drift**

Run:

```bash
terraform -chdir=terraform plan -detailed-exitcode
```

Expected: exit code `0` and `No changes`.

---

### Task 13: Manual two-user isolation and throttling verification

**Files:**
- Create: `docs/authentication-smoke-test.md`

- [ ] **Step 1: Start the frontend**

Run:

```bash
cd frontend
npm run dev
```

Expected: Vite serves `http://localhost:5173/`.

- [ ] **Step 2: Verify public signup and User A isolation**

In a browser:

1. Select **Sign in or create account**.
2. Create User A with a real test email.
3. Enter the email verification code.
4. Confirm Cognito redirects exactly to `http://localhost:5173/`.
5. Upload `user-a.txt` containing `owned by user a`.
6. Confirm it appears after a hard refresh.
7. Record its `fileId` from the authenticated `GET /files` response in browser developer
   tools.

Expected: User A sees only `user-a.txt`.

- [ ] **Step 3: Verify User B cannot access User A**

1. Sign out User A.
2. Create and verify User B with a second real test email.
3. Confirm User B's list is empty.
4. In developer tools, call User A's download and delete paths using User B's access token.
5. Upload `user-b.txt` containing `owned by user b`.

Expected: User A's download and delete paths both return `404`; User B sees only
`user-b.txt`.

- [ ] **Step 4: Recheck User A**

1. Sign out User B and sign in as User A.
2. Confirm `user-a.txt` still exists.
3. Confirm `user-b.txt` is absent.
4. Download and delete `user-a.txt`.

Expected: User A can operate on their own file and cannot see User B's file.

- [ ] **Step 5: Verify best-effort throttling**

From the authenticated browser console, reuse the access token and send 50 concurrent
requests to `GET /files`.

```javascript
const api = await import("/src/services/api.js");
const accessToken = await api.getAccessToken();
const statuses = await Promise.all(
  Array.from({ length: 50 }, () =>
    fetch(`${api.API_URL}/files`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    }).then((response) => response.status),
  ),
);
console.log(statuses);
```

Expected: the list contains at least one `429`. Do not assert an exact count.

- [ ] **Step 6: Cross-check AWS ownership records**

Run:

```bash
cv_bucket=$(terraform -chdir=terraform output -raw bucket_name)
cv_table=$(terraform -chdir=terraform output -raw dynamodb_table_name)
aws s3 ls "s3://$cv_bucket/uploads/" --recursive
aws s3 ls "s3://$cv_bucket/staging/" --recursive
aws dynamodb scan --table-name "$cv_table" --region ap-south-1
```

Expected: keys are grouped under Cognito subject prefixes, staging is empty, and DynamoDB
items contain matching `userId` partition keys.

- [ ] **Step 7: Document the manual result**

Create `docs/authentication-smoke-test.md` with:

```markdown
# Authentication smoke test

**Date:** 2026-08-08
**Region:** ap-south-1

- [x] Anonymous API request returned 401.
- [x] Public signup required email verification.
- [x] User A listed only User A's file.
- [x] User B listed only User B's file.
- [x] Cross-user download returned 404.
- [x] Cross-user delete returned 404.
- [x] S3 keys used Cognito subject prefixes.
- [x] DynamoDB items used `(userId, fileId)` keys.
- [x] Sustained excessive requests produced at least one 429.
- [x] Test users and test files were deleted.
```

- [ ] **Step 8: Delete both temporary Cognito users**

Resolve usernames from the two verified email addresses and delete exactly those test
accounts:

```bash
cv_pool_id=$(terraform -chdir=terraform output -raw cognito_user_pool_id)
read "cv_user_a_email?User A test email: "
read "cv_user_b_email?User B test email: "
cv_user_a=$(aws cognito-idp list-users \
  --user-pool-id "$cv_pool_id" --region ap-south-1 \
  --filter "email = \"$cv_user_a_email\"" \
  --query 'Users[0].Username' --output text)
cv_user_b=$(aws cognito-idp list-users \
  --user-pool-id "$cv_pool_id" --region ap-south-1 \
  --filter "email = \"$cv_user_b_email\"" \
  --query 'Users[0].Username' --output text)
test "$cv_user_a" != "None" && test -n "$cv_user_a"
test "$cv_user_b" != "None" && test -n "$cv_user_b"
test "$cv_user_a" != "$cv_user_b"
aws cognito-idp admin-delete-user \
  --user-pool-id "$cv_pool_id" --username "$cv_user_a" \
  --region ap-south-1
aws cognito-idp admin-delete-user \
  --user-pool-id "$cv_pool_id" --username "$cv_user_b" \
  --region ap-south-1
```

Before executing the two delete commands, inspect both resolved username values and
confirm each result belongs to the intended test email. Never delete users by an
unverified guess.

- [ ] **Step 9: Commit the verification record**

```bash
git add docs/authentication-smoke-test.md
git commit -m "docs: record two-user authentication smoke test"
```

---

### Task 14: Final verification and teardown

**Files:** none

- [ ] **Step 1: Run the complete automated verification suite**

Run:

```bash
.venv/bin/python -m pytest -q
cd frontend && npm run lint && npm run build && cd ..
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform validate
git diff --check
git status --short --branch
```

Expected: all tests and builds pass, Terraform is valid, and the worktree is clean.

- [ ] **Step 2: Create and review the destroy plan**

Run:

```bash
terraform -chdir=terraform plan -destroy -out=/tmp/cloudvault-auth-destroy.tfplan
```

Expected: only Terraform-managed CloudVault resources are marked for deletion.

- [ ] **Step 3: Destroy the temporary stack**

Run:

```bash
terraform -chdir=terraform apply /tmp/cloudvault-auth-destroy.tfplan
```

Expected: all CloudVault resources are destroyed.

- [ ] **Step 4: Verify no CloudVault resources remain**

Run:

```bash
terraform -chdir=terraform state list
aws s3api list-buckets \
  --query "Buckets[?starts_with(Name, 'cloudvault-files-')].Name" --output json
aws dynamodb list-tables --region ap-south-1 \
  --query "TableNames[?starts_with(@, 'cloudvault')]" --output json
aws lambda list-functions --region ap-south-1 \
  --query "Functions[?starts_with(FunctionName, 'cloudvault')].FunctionName" --output json
aws apigatewayv2 get-apis --region ap-south-1 \
  --query "Items[?starts_with(Name, 'cloudvault')].Name" --output json
aws cognito-idp list-user-pools --max-results 60 --region ap-south-1 \
  --query "UserPools[?starts_with(Name, 'cloudvault')].Name" --output json
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/cloudvault \
  --region ap-south-1 --query 'logGroups[].logGroupName' --output json
aws cloudwatch describe-alarms --alarm-name-prefix cloudvault \
  --region ap-south-1 --query 'MetricAlarms[].AlarmName' --output json
```

Expected: Terraform state and every returned AWS list are empty.

---

## Execution notes

- The existing `frontend/.env` points to a destroyed API and must be regenerated only after
  the new Terraform apply.
- Route throttling is intentionally shared. A noisy authenticated user can temporarily
  consume the API-wide allowance; per-user quotas remain future work.
- Cognito's hosted pages, email delivery, and verification are manually tested because
  automating real email and browser login would add disproportionate complexity.
- The manual test must use access tokens. If an ID token succeeds against a protected route,
  the `openid` route scope or frontend token selection is wrong.
- The S3 lifecycle rule remains scoped to `staging/`, which includes every user's staging
  subdirectory.
