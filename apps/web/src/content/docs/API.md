# CI & API

DataCourt exposes a JSON REST API under `/api/v1`. The web app uses the same API. The interactive OpenAPI schema is served at `/api/v1/docs` (Swagger UI) and `/api/v1/openapi.json`.

## Authentication

| Client | Mechanism |
|---|---|
| Browser | `dc_session` HttpOnly cookie from `POST /api/v1/auth/login`. State-changing requests must send `x-datacourt-csrf: 1`. |
| Automation / CI | `Authorization: Bearer dct_…` API token, created by a workspace admin under **Settings → API tokens** |

Token scopes:

| Scope | Allows |
|---|---|
| `contracts:read` | `GET /ci/contract` |
| `audits:read` | read endpoints for the token's workspace |
| `audits:write` | start audits (acts with the admin role inside the token's workspace) |

Tokens only reach data in the workspace that created them. Objects in other workspaces return 404.

## Data-contract gate for CI

The typical flow: a pipeline uploads or registers a dataset version, waits for its audit, then fails the build if the version breaks the project's data contract or the preflight gate is BLOCKED.

### With the `datacourt-ci` command

`datacourt-ci` is a single file, `services/api/datacourt/ci_client.py`, and uses only the Python standard library. Copy that file into your pipeline, or install the package, which also installs the full backend dependencies:

```bash
export DATACOURT_TOKEN=dct_...            # secret; scope contracts:read
python ci_client.py --api https://<your-app>/api/v1 --dataset-version <dataset-version-id>
# or, after `pip install ./services/api`:
datacourt-ci --api https://<your-app>/api/v1 --dataset-version <dataset-version-id>
```

Example output:

```
DataCourt data contract: DataCourt default contract v0
Preflight: BLOCKED  ·  Dataset Debt: HIGH

  [FAIL] Exact duplicate evaluation samples across splits: 12 <= 0
  [FAIL] High/critical leakage findings: 3 <= 0
  [PASS] Smallest class size: 55 >= 30
  [PASS] Unresolved STRONG_REVIEW / POSSIBLE_RELABEL cases: 18 <= 20
  [PASS] Largest / smallest class ratio: 3.1 <= 10
  [PASS] Required splits present: ["test", "train", "val"] includes ["train"]
  [FAIL] Preflight blocking checks: exact_eval_leakage

FAILED
```

Exit codes: `0` passed · `1` contract failed or preflight BLOCKED · `2` usage, network or API error. Add `--json` for the raw response.

### GitHub Actions example

With `ci_client.py` copied into your repository as `tools/datacourt_ci.py`:

```yaml
- name: DataCourt data contract
  env:
    DATACOURT_TOKEN: ${{ secrets.DATACOURT_TOKEN }}
  run: |
    python tools/datacourt_ci.py --api "${{ vars.DATACOURT_API_URL }}" --dataset-version "${{ vars.DATASET_VERSION_ID }}"
```

### Raw HTTP

```bash
curl -fsS -H "Authorization: Bearer $DATACOURT_TOKEN" \
  "https://<your-app>/api/v1/ci/contract?dataset_version_id=<id>"
```

```json
{
  "passed": false,
  "contract": { "name": "…", "version": 3, "passed": false, "results": [
    { "metric": "exact_cross_split_duplicates", "title": "…", "op": "<=", "expected": 0, "actual": 12, "passed": false }
  ]},
  "preflight": { "status": "BLOCKED", "effective_status": "BLOCKED", "blocking": ["exact_eval_leakage"] },
  "debt": "HIGH",
  "dataset_version_id": "…"
}
```

`passed` is true only when every contract rule passes **and** the effective preflight status (after any admin override) is not BLOCKED.

## Contract rules

Contracts are edited per project (**Project → Contract & CI**) or with `PUT /projects/{id}/contract`:

```json
{ "name": "Release gate", "rules": [
  { "metric": "exact_cross_split_duplicates", "op": "<=", "value": 0 },
  { "metric": "leakage_findings_high", "op": "<=", "value": 0 },
  { "metric": "min_images_per_class", "op": ">=", "value": 50 },
  { "metric": "quality_issue_rate", "op": "<=", "value": 0.03 },
  { "metric": "debt_level", "op": "<=", "value": "MODERATE" },
  { "metric": "required_splits", "op": "includes", "value": ["train", "test"] }
]}
```

Metrics: `exact_cross_split_duplicates`, `leakage_eval_samples`, `leakage_findings_high`, `min_images_per_class`, `class_imbalance_ratio`, `unresolved_strong_review_cases`, `unresolved_high_priority_cases`, `quality_issue_rate`, `duplicate_rate`, `shortcut_max_cramers_v`, `unreadable_fraction`, `debt_level` (LOW < MODERATE < HIGH < CRITICAL), `required_splits`, `preflight_status` (READY < READY_WITH_WARNINGS < BLOCKED). `GET /contract-metrics` lists them with descriptions.

Operators: `<=`, `<`, `>=`, `>`, `==`, `includes`. Every contract change creates a new contract version.

## Main endpoints

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `POST /auth/demo`, `GET /auth/me`, `DELETE /auth/account` |
| Workspaces | `POST /orgs`, `GET/PATCH/DELETE /orgs/{id}`, members, tokens, `GET /orgs/{id}/ledger`, `/ledger/verify`, `/security-log`, `/metrics` |
| Projects | `GET/POST /orgs/{id}/projects`, `GET /projects/{id}`, `GET/PUT /projects/{id}/audit-config`, `PUT /projects/{id}/contract` |
| Datasets | `POST /projects/{id}/datasets`, `POST /datasets/{id}/versions` (returns an upload plan: one presigned PUT, or presigned multipart part URLs), `POST /versions/{id}/upload/parts` (fresh part URLs), `GET /versions/{id}/upload` (received parts), `POST /versions/{id}/upload/abort`, `POST /versions/{id}/finalize-upload`, `POST /versions/{id}/retry-ingest` (ingest a failed version again from the archive it stored), `GET /versions/{id}`, `GET /versions/{id}/samples`, `GET /samples/{id}` |
| Audits | `POST /audits`, `GET /audits/{id}`, `/progress`, `/cancel`, `/retry`, `GET /versions/{id}/audit-estimate` |
| Findings | `GET /versions/{id}/{overview, quality, duplicates, leakage, labels, rare-or-wrong, shortcuts, coverage, cartography, influence, failures, privacy, debt, preflight, dna, compare}` |
| Court & review | `GET /versions/{id}/cases`, `GET /cases/{id}`, `POST /cases/{id}/decision`, `POST /decisions/{id}/undo`, `POST /cases/{id}/explain`, `GET /cases/{id}/counterfactuals`, `POST /review-budget`, review sessions |
| Experiments | `POST /what-if`, `GET /what-if/{id}`, `GET /what-if/action-types`, `GET /failures/{id}/blame-map` |
| Outputs | `POST /exports`, `GET /exports/{id}/download`, `POST /reports`, `GET /reports/{id}` |
| System | `GET /health`, `GET /algorithms`, `GET /ci/contract`, `GET /jobs/{id}` (execution status of a background job) |

`GET /samples/{id}` returns `image_url`, a short-lived link to a picture browsers can display: the original, or for HEIC/HEIF (which most browsers cannot show) its WebP copy, marked by `attributes.browser_copy`. `original_url` downloads the original file, unchanged, under its own name.

Long-running operations (ingestion, audits, What-if runs, exports, reports, contamination checks) return immediately with a `queued` status and run on a worker (GitHub Actions in the hosted deployment). Poll the resource until it is `completed` or `failed`; while it is pending, its `job` / `execution` block reports where it stands (`waiting`: `starting_worker`, `retry_scheduled`, `dispatch_failed`), the worker run's URL, attempts and errors.

## Errors and limits

- Errors are JSON `{"detail": "…"}` with a conventional status code:
  - 400: invalid confirmation;
  - 401: unauthenticated;
  - 403: role or scope;
  - 404: missing or other tenant;
  - 409: state conflict;
  - 410: expired archive;
  - 422: validation;
  - 429: rate limited.
- Every response carries `X-Request-ID`. Include it when reporting a problem.
- Rate limits: 20 authentication requests per minute, 120 CI requests per minute and 600 other requests per minute, per client.
