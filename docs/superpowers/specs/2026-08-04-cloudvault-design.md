# CloudVault v1 — Design

**Date:** 2026-08-04
**Status:** Approved

## 1. Purpose

CloudVault is a serverless file-management application: upload, list, download, delete.

The deliverable is a **portfolio artifact** — a public GitHub repository with a README,
architecture diagram, and screenshots, reproducible by anyone who runs `terraform apply`.
It is not a permanently-hosted service.

Success means every item in §12 is true.

## 2. Scope

### In scope (v1)

- Five HTTP endpoints backed by a single Python Lambda.
- Direct browser-to-S3 transfer via presigned POST (upload) and presigned GET (download).
- File metadata in DynamoDB.
- Full infrastructure in Terraform.
- Unit tests with `pytest` + `moto`.
- CloudWatch log group with retention, and a Lambda error alarm.
- React frontend running locally, screenshotted for documentation.

### Out of scope (v1)

- **Authentication.** Deferred to v2 (§13). Every route is public in v1.
- Hosted frontend (no Amplify, no CloudFront).
- GitHub Actions CI.
- Remote Terraform state.
- Cognito, SQS, Textract, Step Functions, microservices.

### Operational constraint

Because v1 has no authentication, a deployed API allows anyone who discovers the URL to
upload, list, and delete files. Infrastructure is therefore **destroyed at the end of each
working session** (`terraform destroy`) and never left running unattended.

## 3. Technology

| Layer | Choice |
|---|---|
| Frontend | React + Vite (local dev server only) |
| Backend | Python 3.13 Lambda |
| API | API Gateway HTTP API (v2, payload format 2.0) |
| File storage | Amazon S3 |
| Metadata | DynamoDB (on-demand) |
| Infrastructure | Terraform (local state) |
| Tests | pytest + moto |
| Region | `ap-south-1` |

Repository: a new standalone git repository at `~/Desktop/cloudvault`.

## 4. Architecture

File bytes never pass through Lambda. Lambda holds the only AWS credentials and issues
time-boxed, single-object grants to the browser.

```
Browser ──1. POST /files/upload-url ──────► API GW ──► Lambda ──► signs POST policy
Browser ──2. multipart POST ──────────────────────────► S3      (bytes land in staging/)
Browser ──3. POST /files/confirm ─────────► API GW ──► Lambda ──► head → copy → delete
                                                              └──► DynamoDB put
Browser ──4. GET /files ──────────────────► API GW ──► Lambda ──► DynamoDB scan
Browser ──5. GET /files/{id}/download ────► API GW ──► Lambda ──► signs GET URL
Browser ──6. GET presigned URL ───────────────────────► S3      (bytes served from here)
```

### Backend module layout

Split from the single-file approach so that pure logic is testable without AWS mocks:

```
backend/
├── app/
│   ├── handler.py      # lambda_handler: route dispatch, exception → status mapping
│   ├── files.py        # the five operations; the only module that calls boto3
│   ├── validation.py   # parse_body, sanitize_filename, size/type checks (pure)
│   └── responses.py    # response(), Decimal serializer (pure)
└── tests/
```

Terraform packages `source_dir = backend/app`. The function reads three environment
variables: `BUCKET_NAME`, `TABLE_NAME`, and `PRESIGNED_URL_EXPIRY` (default `"300"`,
used as `ExpiresIn` for both the upload POST policy and the download URL). Timeout 10s,
memory 256 MB.

### Terraform layout

One file per concern: `provider.tf`, `variables.tf`, `s3.tf`, `dynamodb.tf`, `iam.tf`,
`lambda.tf`, `api_gateway.tf`, `monitoring.tf`, `outputs.tf`, `terraform.tfvars`.

## 5. Storage design

### S3 prefixes

Two prefixes distinguish unconfirmed from confirmed objects:

```
staging/{fileId}-{sanitizedName}   ← presigned POST target; lifecycle expires after 1 day
uploads/{fileId}-{sanitizedName}   ← confirmed files; no expiry
```

`confirm` performs a server-side `copy_object` from `staging/` to `uploads/`, then
`delete_object` on the staging key. The copy happens inside S3, so bytes still never reach
Lambda; the cost is one extra PUT and one extra DELETE request per upload.

This design makes "unconfirmed" a physical property of object location, so the lifecycle
sweep can never delete a confirmed file. A single-prefix lifecycle rule would expire real
files one day after upload.

Bucket configuration: private, Block Public Access fully enabled, SSE-S3 (AES256),
`force_destroy = true` (acceptable for a teardown-friendly portfolio project).

CORS allows `POST` (presigned POST, not PUT) from `var.frontend_origin` only.

### DynamoDB

Table `cloudvault-files`, partition key `fileId` (String), `PAY_PER_REQUEST`. One item per
**confirmed** file:

```json
{
  "fileId": "550e8400-e29b-41d4-a716-446655440000",
  "fileName": "resume.pdf",
  "s3Key": "uploads/550e8400-e29b-41d4-a716-446655440000-resume.pdf",
  "contentType": "application/pdf",
  "size": 245781,
  "uploadedAt": "2026-08-04T10:30:00+00:00"
}
```

No `status` field: a row exists only after S3 has confirmed the object, so the value would
be permanently `"UPLOADED"`.

`size` returns from DynamoDB as `Decimal` and requires a custom JSON serializer.

## 6. API contract

Base URL is the HTTP API's `$default` stage endpoint. All responses are JSON with
`Access-Control-Allow-Origin` set.

### `POST /files/upload-url`

Request: `{ "fileName": string, "contentType": string, "size": int }`

Response `200`:

```json
{
  "fileId": "550e8400-...",
  "fileName": "resume.pdf",
  "s3Key": "staging/550e8400-...-resume.pdf",
  "uploadUrl": "https://cloudvault-files-xxxx.s3.ap-south-1.amazonaws.com/",
  "fields": { "key": "...", "Content-Type": "...", "policy": "...", "x-amz-signature": "..." },
  "expiresIn": 300
}
```

Generated with `generate_presigned_post`, `ExpiresIn=300`, and:

```python
Conditions = [
    {"Content-Type": content_type},
    ["content-length-range", 1, 10485760],
]
```

S3 itself rejects an oversized or wrong-typed body at upload time. `fileName` in the
response is the sanitized name; the client must use it, not its own.

### Browser upload (step 2, no API involvement)

Build a `FormData`, append every entry of `fields`, then append the file **last** — S3
ignores any field following the file. A successful presigned POST returns **HTTP 204 with
an empty body**, not 200.

### `POST /files/confirm`

Request: `{ fileId, fileName, s3Key, contentType, size }`

The request carries the **staging** key. Rejects any `s3Key` not starting with
`staging/{fileId}-`, so one file's upload cannot be confirmed against another's metadata.
Then: `head_object` → compare actual size against declared → `copy_object` to `uploads/`
→ `delete_object` on staging → `put_item` with
`ConditionExpression="attribute_not_exists(fileId)"`.

The stored row's `s3Key` is the **destination** key (`uploads/{fileId}-{name}`), not the
staging key from the request. `contentType` and `size` are taken from the `head_object`
result, not from the client's claim — the client's values are used only for validation and
the size comparison.

Response `201`: `{ "message": "Upload confirmed", "file": { ...item } }`

### `GET /files`

Paginated `scan` following `LastEvaluatedKey`, sorted by `uploadedAt` descending in Lambda.

Response `200`: `{ "files": [ ...items ] }`

Scan is acceptable at portfolio scale and is documented as a known limitation; v2's
`userId` partition key replaces it with `Query`.

### `GET /files/{fileId}/download`

Presigned GET, `ExpiresIn=300`, with
`ResponseContentDisposition: attachment; filename="{fileName}"`.

Response `200`: `{ "downloadUrl": string, "expiresIn": 300 }`

### `DELETE /files/{fileId}`

Deletes the S3 object first, then the DynamoDB row. If the row delete fails, the result is
a row pointing at a missing object — visible, and fixed by retrying. The reverse order
would leave an orphan in `uploads/` that nothing sweeps.

Response `200`: `{ "message": "File deleted successfully" }`

### Validation rules

- `MAX_FILE_SIZE` = 10 MB (10485760 bytes), enforced both in Lambda and in the POST policy.
- Allowed content types: `application/pdf`, `image/png`, `image/jpeg`, `text/plain`.
- Filenames: `os.path.basename`, then `[^A-Za-z0-9._-]` → `_`; empty result is rejected.

## 7. Error handling

`handler.py` maps exceptions to status codes; route functions raise rather than format.

| Condition | Status |
|---|---|
| Malformed JSON body | 400 |
| Missing or invalid field; bad content type; size ≤ 0 or > 10 MB | 400 |
| `s3Key` not matching `staging/{fileId}-` | 400 |
| Actual S3 size ≠ declared size | 400 |
| `head_object` → `404` / `NoSuchKey` / `NotFound` | 400 (never uploaded, or swept) |
| `get_item` returns no item | 404 |
| `put_item` fails `attribute_not_exists(fileId)` (replayed confirm) | **409** |
| Any other `ClientError` | 500, logged via `logger.exception` |
| Unhandled exception | 500, logged via `logger.exception` |
| Unknown `routeKey` | 404 |

The 409 is a deliberate fix: a duplicate confirm is a client error, not a server error.

Logs record `fileId` and operation only — never presigned URLs, never file contents.

## 8. Security decisions

To be reproduced in the README:

- S3 bucket is private with Block Public Access fully enabled.
- Objects are encrypted at rest (SSE-S3, AES256).
- The browser never receives AWS credentials — only presigned grants for one object.
- Presigned URLs and POST policies expire after 5 minutes.
- Lambda's IAM policy is scoped to `staging/*` and `uploads/*` in one bucket and to one
  DynamoDB table, with only the five actions it uses.
- Content type and size are enforced by S3 at upload time via POST policy conditions, and
  re-verified server-side at confirm.
- `s3Key` ownership is verified against `fileId` before any metadata write.
- Filenames are sanitized against path traversal before use.
- **Authentication is deliberately out of scope in v1** — stated plainly, with the v2 plan.

## 9. IAM

Lambda execution role:

- `AWSLambdaBasicExecutionRole` (managed) for CloudWatch Logs.
- Inline policy:
  - `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `{bucket}/staging/*` and
    `{bucket}/uploads/*`.
  - `dynamodb:PutItem`, `GetItem`, `DeleteItem`, `Scan` on the table ARN.

## 10. Monitoring

- `aws_cloudwatch_log_group` `/aws/lambda/cloudvault-backend`, `retention_in_days = 7`,
  managed by Terraform so `destroy` removes it. (Without this, Lambda auto-creates the
  group with never-expire retention and it survives teardown.)
- `aws_cloudwatch_metric_alarm` on `AWS/Lambda` `Errors`, sum ≥ 1 over 300s,
  `treat_missing_data = "notBreaching"`.

## 11. Testing

`pytest` + `moto`. No test touches real AWS.

**Pure unit tests** (`validation.py`, `responses.py`, no mocks):

- filename sanitization: `../../etc/passwd`, unicode, names empty after sanitizing
- size rejection: zero, negative, non-integer, over 10 MB
- content-type rejection
- `Decimal` → int/float serialization
- base64-encoded request bodies
- malformed JSON

**Integration tests** (`files.py` under moto):

- happy path for all five routes
- confirm when no object exists in S3
- confirm with a mismatched `s3Key`
- confirm with actual size ≠ declared size
- confirm replayed → 409
- confirm moves the object from `staging/` to `uploads/`
- download and delete with an unknown `fileId` → 404
- list paginating across a `LastEvaluatedKey`

The backend is written **test-first**: tests for a route, then the route.

## 12. Definition of done

- `terraform apply` creates all backend resources from a clean state.
- `pytest` passes.
- A supported file uploads from the browser directly to S3.
- An oversized or wrong-typed upload is rejected by S3 itself.
- DynamoDB receives a metadata row only after S3 confirms the object.
- The dashboard lists uploaded files newest-first.
- Download works via a temporary URL and preserves the original filename.
- Delete removes both the S3 object and the DynamoDB row.
- The bucket is private; no AWS credentials exist in the frontend.
- Lambda logs and the error alarm are visible in CloudWatch.
- `terraform destroy` removes all resources including the log group.
- README covers overview, architecture diagram, upload flow, AWS services, local setup,
  Terraform deployment, API reference, security decisions, screenshots, known limitations,
  future improvements, and cleanup.

## 13. Future work (not v1)

- **v2 — authentication.** Cognito user pool + HTTP API JWT authorizer. DynamoDB becomes
  `userId` (PK) + `fileId` (SK), replacing `Scan` with `Query` and giving per-user
  isolation. Unblocks hosting the frontend publicly.
- **v3 — event-driven confirm.** Replace `POST /files/confirm` with an S3 `ObjectCreated`
  trigger. Removes a round trip and survives the browser closing mid-flow, at the cost of
  eventual consistency in the file list and carrying the original filename through S3
  object metadata.
- Remote Terraform state (S3 backend + DynamoDB lock table).
- GitHub Actions CI: `terraform fmt -check`, `terraform validate`, `pytest`.
- Multipart upload for files above 10 MB.
