# Security

This document describes DataCourt's security model, the controls that are implemented, and what remains the operator's responsibility. To report a vulnerability, open a private security advisory on the GitHub repository. Please do not open a public issue.

## Threat model (summary)

| Asset | Threats considered |
|---|---|
| Customer datasets and derived artifacts | cross-tenant access, leaked URLs, path traversal, malicious archives |
| Accounts and sessions | credential stuffing, session theft, CSRF |
| Review decisions and audit history | silent tampering, repudiation |
| Service availability | oversized uploads, ZIP bombs, decompression bombs, request floods |
| Secrets | leakage via logs, errors, client bundles or the repository |

Out of scope: compromise of the hosting providers (Vercel, GitHub, Neon, Backblaze) and of the operator's own accounts.

## Tenancy and authorisation

- Every tenant-owned row carries `org_id`. Handlers never load objects by ID alone. `tenancy.py` resolves each object through the caller's organisation memberships and returns **404** (not 403) for objects in other workspaces, so IDs cannot be probed.
- Roles:
  - **owner**: everything, including deleting the workspace.
  - **admin**: settings, members, tokens, exports, preflight overrides, adjudication.
  - **reviewer**: run audits and experiments, record decisions.
  - **viewer**: read-only.
- Role checks are server-side dependencies. The UI only hides controls.
- Demo visitors are ephemeral read-only viewers of the demo workspace. A `not_demo` dependency rejects every state-changing endpoint for demo users.
- The test suite includes cross-tenant access tests: another user's version, cases, exports, reports and ledger return 404.

## Authentication

- Passwords must be 10–200 characters. They are hashed with **scrypt** (N = 2¹⁴, r = 8, p = 1, 16-byte salt) and compared in constant time. Unknown emails are checked against a dummy hash so response timing does not reveal whether an account exists.
- Sessions are random 256-bit tokens. **Only their SHA-256 is stored.** They are sent as the `dc_session` cookie with `HttpOnly`, `SameSite=Lax`, and `Secure` in production, and expire after 14 days by default. Logout deletes the server-side session.
- **CSRF**: cookie-authenticated state changes must send the custom header `x-datacourt-csrf: 1`, which a cross-site form cannot set. The `Origin` header, when present, must match the app or an allowed origin.
- **API tokens** (`dct_…`) are for automation. They are scoped (`contracts:read`, `audits:read`, `audits:write`), shown once, stored as SHA-256 hashes, revocable, and record when they were last used.
- **Rate limits** (per client IP hash, in process):
  - authentication: 20 per minute;
  - demo login: 10 per minute;
  - CI endpoint: 120 per minute;
  - default: 600 per minute.

## Uploads and archive safety

Archives are treated as hostile (`ingest/zip_safety.py`):

- Uploads go directly to object storage through short-lived presigned PUT URLs (single or multipart). Each URL signs the declared size, so it cannot store a larger object. Size is checked again when the upload is finalized (default limit 2 GiB), along with the `PK` magic bytes for single uploads; ingestion validates every archive.
- Entry names are rejected for absolute paths, `..` traversal, drive letters, backslash tricks, NUL bytes, over-long paths and symlinks. Nothing is ever extracted to a path built from an entry name.
- Limits apply to the number of entries (200,000), the total uncompressed size (8 GiB), per-file size (64 MiB) and per-entry compression ratio (> 250:1 is rejected above 1 MiB). Reads are capped at the declared size to defeat lying headers.
- Images are decoded with Pillow's decompression-bomb guard (64 MP). HEIC/HEIF is decoded by libheif (pillow-heif) with its own security limits on, and only the primary image is read (no thumbnails, depth maps or auxiliary images). Decoders run only in the worker, never in the API. The extension is checked against the decoded format, and undecodable files are recorded with a reason instead of failing the job.
- Files that are not images, such as scripts or executables, are ignored and never executed or served.

## Storage

- All object keys are under `orgs/{org_id}/…`. The storage layer rejects keys containing `..`, backslashes or leading slashes.
- Buckets are private. The browser only receives **short-lived signed URLs**:
  - 15 minutes by default for reads;
  - 10 minutes for exports and reports;
  - 6 hours for upload PUTs and multipart parts (large uploads take time; URLs for missing parts can be refreshed).
- These are Backblaze B2 (S3 API) presigned URLs in production, or HMAC-SHA-256 signed tokens served by the API in local development.
- Thumbnails are the exception. They are served through `/api/v1/media/{token}`, where the token is an HMAC-signed capability:
  - it names one thumbnail key;
  - it is valid for one to two weeks;
  - its URL is stable, so a CDN may cache the response for at most a day.

  Only members receive these URLs. A deleted dataset's thumbnails can remain in CDN caches for up to a day after the objects are gone.
- Deletions remove every stored version of every object. B2 keeps previous versions, and a plain delete would only hide the file.
- Downloads use `Content-Disposition: attachment` where appropriate. Report HTML is served from storage, not from the app origin.

## Transport and headers

- Production enforces HTTPS. The API sets `Strict-Transport-Security` when `COOKIE_SECURE=true`.
- Both the web app and the API send `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` and `X-Frame-Options: DENY`. The web app also sends a restrictive `Permissions-Policy`.
- CORS is limited to the configured app origins, but in production the browser talks to the API on the same origin (Vercel routes `/api/*` to the API service). The bucket's CORS rule allows only the exact app origin, and only for the presigned requests browsers make (`GET`, `PUT`, `HEAD` with `Content-Type`). The *Bucket CORS* workflow sets that rule and nothing else, and checks that other origins are refused.

## Integrity and auditability

- **Evidence ledger**: an append-only hash chain per workspace (see [METHODOLOGY.md](METHODOLOGY.md#evidence-ledger)) for audits, verdicts, decisions, overrides, experiments and exports. `GET /orgs/{id}/ledger/verify` recomputes the chain.
- **Security audit log**: logins, failed logins, registrations, member and role changes, token creation and revocation, settings changes, retention purges and deletions.
- Originals are immutable. Exports create new versions and never modify the source.
- No AI output changes data. Destructive changes need a human decision, and preflight overrides need an admin with a written reason.

## Secrets

- Secrets come only from environment variables (`services/api/.env.example`, `apps/web/.env.example`). There are no defaults in production: startup fails if `AUTH_SECRET` or `SIGNED_URL_SECRET` is missing or shorter than 32 characters, if `COOKIE_SECURE` is false, or if S3 storage is selected without credentials.
- Structured JSON logs redact fields named like `password`, `token`, `secret`, `authorization`, `cookie`, `api_key` and `database_url`. Client error responses never include stack traces or internal messages. Unhandled errors return a generic message with the request ID.
- The web app has no secrets in its client bundle. Server-side settings (`API_ORIGIN` when used, and the API's variables) never reach the browser.
- **GitHub credentials.** The API starts worker runs with a fine-grained token scoped to this repository with *Actions: read and write* only (`DATACOURT_GITHUB_TOKEN`). The token lives in Vercel's server environment and is sent only to `api.github.com`. It is never logged, stored in the database, included in error messages (dispatch errors record the HTTP status only) or sent to browsers.
- **Worker runs** use repository secrets for the database and storage. They are triggered only by `workflow_dispatch` (repository writers or that token) and by the schedule, never by pull requests, so forks cannot obtain secrets. Workflow inputs are passed through environment variables and validated before use, never interpolated into shell code.
- **Public run logs.** The repository is public, so worker logs are public. Workers run with `PUBLIC_LOGS=true`:
  - only allow-listed fields are written (ids, job types, counts, timings);
  - exception messages, which can quote file names from datasets, are dropped;
  - tracebacks are reduced to code locations;
  - third-party log messages are withheld.

  Step summaries list job ids, types and durations only.
- `.gitignore` excludes `.env*` (except examples), keys and data folders. CI runs a secret scan (gitleaks) on every push.

## Dependencies

- The Python dependencies are mainstream, maintained packages. OpenCV is pinned to `<5` for API stability.
- Frontend dependencies are locked by `package-lock.json`.
- CI runs lint, type checks, tests and the production build on every push.

## Operator responsibilities

- Generate strong, unique `AUTH_SECRET` and `SIGNED_URL_SECRET` values, and rotate them if they leak. Rotating `AUTH_SECRET` does not invalidate sessions, which are stored hashed. Rotating `SIGNED_URL_SECRET` invalidates outstanding local signed URLs.
- Keep the B2 bucket private and the application key restricted to that bucket. Allow CORS only for the app's origin.
- Keep the GitHub token scoped to this repository with Actions permission only, and rotate it before it expires.
- Restrict database network access and use the provider's TLS connection strings.
- Review the security audit log and API tokens periodically.

## Known limitations

- The rate limiter is in-process. With several API replicas, each enforces its own limits, so use an edge rate limiter for strict global limits.
- There is no MFA or SSO yet (see the roadmap).
- The ledger makes tampering *evident* but is stored in the same database. For stronger guarantees, export the chain head hash periodically to an external system.
