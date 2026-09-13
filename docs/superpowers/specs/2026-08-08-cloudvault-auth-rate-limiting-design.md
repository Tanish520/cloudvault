# CloudVault Authentication, User Isolation, and Rate Limiting — Design

**Date:** 2026-08-08  
**Status:** Proposed for final review

## 1. Purpose

Upgrade CloudVault from an unauthenticated shared-file demo into a multi-user application
where anyone can register, but each signed-in user can access only their own files.

Keep the design small:

- Amazon Cognito managed login handles signup, email verification, login, logout, password
  reset, and token issuance.
- API Gateway's JWT authorizer authenticates requests before Lambda runs.
- Lambda derives ownership only from the verified Cognito `sub` claim.
- DynamoDB and S3 partition file data by that user ID.
- API Gateway applies best-effort, API-wide throttling.

## 2. Scope

### In scope

- Public email/password signup with required email verification.
- Cognito managed login using OAuth authorization code with PKCE.
- Login and logout controls in the React application.
- JWT authorization on all five file routes.
- User-specific DynamoDB keys and S3 prefixes.
- User-isolated upload, confirm, list, download, and delete operations.
- Shared HTTP API throttling at 5 requests per second with a burst of 10.
- Automated unit and integration tests for ownership isolation.
- A documented manual two-user test against a temporary AWS deployment.

### Out of scope

- Social or enterprise identity providers.
- Custom login and signup forms.
- Custom OAuth resource-server scopes.
- Per-user rate limits or quotas.
- CAPTCHA, AWS WAF, Cognito threat protection, or advanced fraud controls.
- Migration of existing DynamoDB metadata.
- A hosted production frontend or production callback URL.

## 3. Architecture

```text
                    ┌──────────────────────────────┐
                    │ Cognito managed login       │
                    │ Signup, verification, login │
                    │ password reset, logout      │
                    └──────────────┬───────────────┘
                                   │ JWT access token
                                   ▼
┌──────────────┐   Authorization: Bearer <JWT>   ┌─────────────────────┐
│ React + Vite │ ───────────────────────────────► │ API Gateway HTTP API│
│ localhost    │                                  │ JWT + throttling    │
└──────┬───────┘                                  └──────────┬──────────┘
       │ direct presigned transfer                           │ verified claims
       ▼                                                     ▼
┌────────────────────┐                             ┌─────────────────────┐
│ S3                 │ ◄────────────────────────── │ Python Lambda       │
│ {userId}/file keys │                             │ ownership from sub  │
└────────────────────┘                             └──────────┬──────────┘
                                                            │
                                                            ▼
                                                 ┌─────────────────────┐
                                                 │ DynamoDB            │
                                                 │ PK userId           │
                                                 │ SK fileId           │
                                                 └─────────────────────┘
```

Authentication and ownership authorization remain separate:

```text
API Gateway JWT authorizer  → proves the caller is signed in
Lambda + verified JWT sub   → limits the caller to their own records and objects
```

The browser never supplies a trusted `userId`.

## 4. Cognito design

Terraform creates:

- One Cognito user pool.
- Email-based signup and sign-in.
- Self-registration enabled.
- Automatic email verification required before login.
- A strong password policy.
- One public SPA app client with `generate_secret = false`.
- Authorization-code OAuth flow.
- OAuth scopes `openid`, `email`, and `profile`.
- One Cognito managed-login domain.

Local redirect URLs must match exactly, including the trailing slash:

```text
Callback URL: http://localhost:5173/
Logout URL:   http://localhost:5173/
```

Production URLs may be added later as additional callback and logout URLs.

Terraform outputs the public frontend configuration:

- User-pool ID.
- App-client ID.
- Cognito domain.
- AWS region.
- API URL.

No Cognito client secret exists or enters the frontend.

## 5. API Gateway authorization

Create one `JWT` authorizer with:

```text
Identity source: $request.header.Authorization
Issuer:          https://cognito-idp.{region}.amazonaws.com/{userPoolId}
Audience:        Cognito app-client ID
```

Every file route must contain all three settings:

```hcl
authorization_type   = "JWT"
authorizer_id        = aws_apigatewayv2_authorizer.cognito.id
authorization_scopes = ["openid"]
```

Protected routes:

- `POST /files/upload-url`
- `POST /files/confirm`
- `GET /files`
- `GET /files/{fileId}/download`
- `DELETE /files/{fileId}`

Requiring `openid` ensures the supplied access token has a matching `scope` claim and
prevents accidentally treating a Cognito ID token as the API access token. A custom
`cloudvault/files` scope would be clearer semantically but would require an additional
Cognito resource server, so it is deliberately deferred.

API Gateway handles browser `OPTIONS` preflight requests through its HTTP API CORS
configuration. JWT authorization is not attached to preflight requests. CORS must allow:

```hcl
allow_headers = ["authorization", "content-type"]
```

## 6. User ownership

Lambda reads the stable Cognito subject from the verified request context:

```python
event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
```

Email is display information only and is never used as an ownership key because an email
address can change.

Add a focused authentication helper that returns the `sub` or raises an authentication
error when Lambda is invoked without valid authorizer context. API Gateway normally rejects
such requests before invocation; the helper makes direct Lambda invocation fail closed.

The API never accepts or trusts a request-body or query-string `userId`.

## 7. DynamoDB model

Replace the existing primary-key schema:

```text
Old: PK fileId

New: PK userId
     SK fileId
```

An item becomes:

```json
{
  "userId": "cognito-sub",
  "fileId": "550e8400-e29b-41d4-a716-446655440000",
  "fileName": "report.pdf",
  "s3Key": "uploads/cognito-sub/550e8400-...-report.pdf",
  "contentType": "application/pdf",
  "size": 245781,
  "uploadedAt": "2026-08-08T10:30:00+00:00"
}
```

Operation mapping:

| Operation | DynamoDB access |
|---|---|
| Confirm | Conditional `PutItem` with `(userId, fileId)` |
| List | `Query` by `userId`, then sort newest-first |
| Download | Consistent `GetItem` by `(userId, fileId)` |
| Delete | Conditional `DeleteItem` by `(userId, fileId)` |

Changing a DynamoDB primary key requires table replacement. This version recreates the
development metadata table; existing test metadata is not migrated. The AWS stack is
currently destroyed, so the next deployment creates the new schema from a clean state.

## 8. S3 layout and upload flow

S3 keys become:

```text
staging/{userId}/{fileId}-{sanitizedName}
uploads/{userId}/{fileId}-{sanitizedName}
```

The upload flow remains direct-to-S3:

1. The authenticated browser requests an upload policy.
2. Lambda derives `userId` from the JWT and signs one exact staging key.
3. The browser posts file bytes directly to S3.
4. The browser confirms using `fileId`, sanitized filename, and staging key.
5. Lambda derives `userId` again and requires the exact expected staging key.
6. Lambda verifies the object, copies it to the user's `uploads/` prefix, deletes staging,
   and conditionally writes the user's DynamoDB item.

The bucket remains private. Lambda retains bucket-wide access to the two application
prefixes because it is the trusted server-side principal; Lambda code enforces user
ownership using verified claims.

## 9. Frontend authentication

Use AWS Amplify Auth rather than implementing OAuth, PKCE, refresh tokens, and token storage
manually.

The application has two top-level states:

```text
Signed out → CloudVault introduction + “Sign in or create account”
Signed in  → Existing file UI + user email + “Sign out”
```

The sign-in button redirects to Cognito managed login. Cognito handles signup, verification,
login, forgot password, and the callback. Amplify restores the session after redirect and
refreshes tokens when possible.

Before every API request, the service layer obtains the current access token and sends:

```http
Authorization: Bearer <access-token>
```

The S3 multipart POST does not receive the Cognito JWT. Its only authorization remains the
presigned POST policy.

## 10. Rate limiting

Configure the API Gateway `$default` stage with:

```hcl
default_route_settings {
  throttling_rate_limit  = 5
  throttling_burst_limit = 10
}
```

This is best-effort API-wide throttling shared by all authenticated users. It protects the
API and Lambda from accidental loops and basic request floods, but it is not a per-user quota
or a guaranteed cost ceiling.

Throttling covers requests to API Gateway only. It does not throttle file bytes sent directly
to S3 after an upload policy has been issued. Existing file-size and policy-expiry controls
continue to constrain individual uploads.

Sustained excess requests may receive `429 Too Many Requests`. The frontend displays:

```text
Too many requests. Please wait a moment and try again.
```

## 11. Error handling

| Condition | Result |
|---|---|
| Missing, invalid, or expired JWT | API Gateway `401` |
| Missing `sub` during direct Lambda invocation | Lambda `401` |
| Valid user requesting another user's file | Lambda `404` |
| API throttle exceeded | API Gateway `429` |
| Unverified signup | Cognito blocks login |
| Refreshable expired access token | Amplify refreshes it |
| Expired refresh session | Frontend returns to signed-out state |
| Existing validation error | Lambda `400` |
| Replayed confirmation for the same user/file | Lambda `409` |
| AWS operation failure | Generic Lambda `500` |

Cross-user access returns `404`, not `403`, so the API does not reveal whether another
user's file ID exists.

## 12. Testing

### Automated backend tests

- Extract `sub` from valid API Gateway JWT claims.
- Reject missing, malformed, or empty authentication context.
- Ignore any client-provided `userId`.
- Include the authenticated user in staging and upload keys.
- Confirm metadata contains `userId` from the JWT.
- Query only the authenticated user's DynamoDB partition.
- Prevent User A from listing User B's files.
- Return `404` when User A downloads or deletes User B's file.
- Permit the same `fileId` in two different user partitions.
- Preserve existing filename, size, content-type, replay, and error behavior.

The Moto DynamoDB fixture uses the new partition and sort keys.

### Frontend and infrastructure checks

- Frontend lint and production build pass.
- Terraform formatting and validation pass.
- All five routes reference the JWT authorizer and `openid` scope.
- CORS allows `authorization` and `content-type`.
- The Cognito SPA app client has no secret.
- Stage throttling is configured at rate 5 and burst 10.

### Manual real-AWS verification

Use two temporary email-verified users:

1. Confirm anonymous API calls return `401`.
2. User A uploads and lists a file.
3. User B signs in and cannot list User A's file.
4. User B receives `404` when using User A's file ID for download or delete.
5. User B uploads a separate file visible only to User B.
6. Sustained excess API requests eventually produce at least one `429`.
7. Delete both users and destroy the Terraform stack.

The test does not automate email verification or managed-login browser interaction, and it
does not assert an exact number of `429` responses because API Gateway throttling is
best-effort.

## 13. Implementation order

1. **Data isolation:** composite DynamoDB key, per-user S3 keys, `Query`, and isolation tests.
2. **Authentication infrastructure:** Cognito, exact redirect URLs, JWT authorizer, scopes,
   route protection, and CORS.
3. **Frontend authentication:** Amplify configuration, login/logout UI, session restoration,
   and access-token attachment.
4. **Protection and verification:** shared throttling, `401`/`429` handling, full automated
   verification, and the documented two-user smoke test.

## 14. Known limitations

- Anyone with an email address can register after verification.
- Email verification is not CAPTCHA or bot protection.
- API throttling is shared, best-effort, and not per user.
- A user can request multiple upload policies within the shared throttle.
- S3 transfer traffic does not pass through API Gateway throttling.
- The frontend remains local-only.
- Existing metadata is recreated rather than migrated.

CAPTCHA, WAF, per-user quotas, a custom OAuth scope, hosted frontend callbacks, and advanced
Cognito threat protection are future improvements.

## 15. Definition of done

- Public signup, email verification, login, logout, and password reset work through Cognito
  managed login.
- Anonymous calls to every file route return `401`.
- All authenticated API calls use Cognito access tokens.
- User A cannot list, download, confirm, or delete User B's files.
- DynamoDB uses `userId` plus `fileId` and listing uses `Query`.
- S3 staging and upload keys contain the authenticated Cognito subject.
- Direct browser-to-S3 upload and presigned download still work.
- Sustained excessive API requests eventually receive `429`.
- Backend tests, frontend lint/build, and Terraform validation pass.
- The two-user manual AWS verification is documented and completed.
- The temporary stack and users are destroyed after verification.
