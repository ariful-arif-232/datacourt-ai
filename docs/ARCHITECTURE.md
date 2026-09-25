# Architecture

DataCourt is a monorepo. One Vercel project serves the web app and a lightweight API; heavy work runs as batch jobs on GitHub Actions; Neon and Backblaze B2 hold all state.

```
┌──────────────────────┐  HTTPS, one origin   ┌──────────────────────────────────────────────┐
│  Browser             ├─────────────────────►│  Vercel project (root vercel.json)           │
│  - React 19 UI       │                      │   /(.*)     → web: Next.js 16 (apps/web)     │
│  - cookie dc_session │                      │   /api/(.*) → api: FastAPI (services/api)    │
└───┬──────────────────┘                      │     auth, tenancy, review, uploads, status,  │
    │ presigned PUT (uploads, multipart)      │     job orchestration — no ML on Vercel      │
    │ presigned GET (images, exports,         └───────┬──────────────┬───────────────────────┘
    │ reports); thumbnails via /api/v1/media          │ SQL          │ workflow_dispatch
    ▼                                                 │ (pooled)     │ (server-side token)
┌──────────────────────┐                              ▼              ▼
│  Backblaze B2        │   S3 API   ┌──────────────────────┐   ┌──────────────────────────────────┐
│  private bucket:     │◄───────────┤  Neon PostgreSQL     │◄──┤  GitHub Actions: DataCourt worker │
│  archives, images,   │◄───────────┤  63 tables, job      │   │  standard hosted runners         │
│  thumbnails,         │   S3 API   │  queue + status,     │   │  migrate → drain queue: ingest,  │
│  artifacts, exports  │            │  evidence ledger     │   │  audit, what-if, export, report  │
└──────────────────────┘            └──────────────────────┘   └──────────────────────────────────┘
```

Gemini (optional) is called by the API for plain-language wording only. Locally, the same code runs with the filesystem as object storage and an in-process worker thread.

## Components

### Web app (`apps/web`)

- Next.js App Router, React 19, TypeScript, Tailwind CSS 4, SWR, cmdk (command palette) and lucide icons.
- The browser only calls `/api/*` on its own origin: on Vercel the project routes it to the API service, and elsewhere `next.config.ts` proxies it to `API_ORIGIN`. Session cookies are first-party and API calls need no CORS preflight.
- Pages:
  - `/`: landing page.
  - `/docs/*`: rendered from `docs/*.md`.
  - `/login`, `/register`.
  - `/app/*`: authenticated workspace, with a per-dataset-version sidebar of 22 views.
- Charts are hand-written SVG and canvas components (`components/charts.tsx`). They use a validated categorical palette with legends and table fallbacks. There is no chart library dependency.
- Evidence types are shown by `KindTag`: measured, heuristic, model prediction, model estimate, LLM or human.

### API (`services/api`, Python package `datacourt`)

| Module | Responsibility |
|---|---|
| `main.py` | App factory, request-ID and security-header middleware, safe error handlers |
| `api/routers/*` | 97 operations on 89 paths under `/api/v1` (auth, orgs, datasets, audits, findings, court, review, experiments, system) |
| `tenancy.py` | `Principal`; every lookup is scoped to the caller's organisations and returns 404 across tenants |
| `ingest/` | ZIP safety, layout detection, image decoding (Pillow; HEIC/HEIF through libheif), normalisation, hashing, thumbnails, browser copies, provenance |
| `pipeline/` | Versioned config, `AuditContext` (artifacts), stage functions, runner |
| `ml/` | Algorithms: quality, embeddings, kNN, duplicates, leakage, baseline, influence, labels, shortcuts, coverage, privacy, jury, governance, DNA |
| `services/` | Review, budget, blame, counterfactuals, explanations, version diff, contamination, retention, bootstrap |
| `whatif/lab.py` | What-if experiments |
| `exporting.py`, `reports/` | Clean export and HTML/JSON audit report |
| `jobs.py`, `worker.py` | Postgres job queue and worker (poll forever, or drain within a time budget) |
| `execution.py` | Waking workers: GitHub Actions workflow dispatch after commit, slot selection, re-dispatch of lost dispatches |
| `ledger.py` | Hash-chained evidence ledger and security audit log |
| `storage.py` | `LocalObjectStore` (HMAC-signed URLs), `S3ObjectStore` (Backblaze B2 or any S3 API: presigned single and multipart uploads, ranged reads, version-aware deletes) and the worker's write-through `CachingObjectStore` |
| `sample_objects.py` | Reads sample images from the worker cache or the source ZIP (one download) before falling back to per-object GETs |
| `storage_check.py` | `datacourt-storage-check`: end-to-end verification of the configured bucket |
| `ingest_check.py` | `datacourt-ingest-check`: the real ingestion and a FAST audit on a mixed JPG/HEIC dataset, with checks |
| `storage_cors.py` | `datacourt-storage-cors`: sets the bucket's CORS rule for browser uploads (`PutBucketCors`, or B2's `b2_update_bucket` when the bucket has Native API rules) and verifies it with real preflight requests |
| `registry.py` | Public registry of algorithm versions |
| `benchmark/` | Synthetic benchmark generator, evaluator and runner |
| `demo.py`, `ci_client.py` | Demo workspace seeder; `datacourt-ci` command |

### Job queue and execution

Long-running work never happens inside a request. Ingestion, audits, What-if runs, exports, reports, version diffs, contamination checks and storage purges are jobs in the `jobs` table:

- **Claiming** uses `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)`, ordered by priority and age, so any number of workers can run safely.
- **Idempotency keys** stop duplicate enqueues. For example, a version is ingested only once.
- **Time limits.** Every job has `timeout_seconds`, scaled to its size (15 min – 3 h). A worker with a bounded lifetime claims only jobs that fit in its remaining budget. The limit is enforced cooperatively at checkpoints, and a timeout fails the job without a retry.
- **Heartbeats and progress** are flushed by a background thread, every 2 s while progress changes and every 10 s otherwise, so handlers never wait on the database. A reaper re-queues jobs whose heartbeat is older than 180 s.
- **Retries** use exponential backoff. `PermanentJobError` (bad input) and timeouts never retry. Failures are classified (`permanent`, `transient`, `resource_exhausted`, `internal`, `cancelled`, `timeout`, `worker_lost`) for the admin metrics.
- **Cancellation** is cooperative: stages call `check_cancelled()`, which the heartbeat thread arms when the job's `cancel_requested` flag is set.

How workers are started is chosen by `EXECUTION_BACKEND` (`execution.py`):

| Backend | Used for | Behaviour |
|---|---|---|
| `github_actions` | production | After the enqueuing transaction commits, the API dispatches the `DataCourt worker` workflow with the job id and a worker slot. Each slot is a concurrency group (one running run, at most one pending), so at most `WORKER_MAX_PARALLEL` runs execute at once. A run migrates the database, drains the queue, waits briefly for follow-up jobs, and exits. If its time budget ends with work left, it dispatches its successor with its own `GITHUB_TOKEN`. Dispatch outcomes are stored on the job; status polls re-dispatch jobs that no runner picked up. |
| `external` | a dedicated or GPU worker | A long-running `datacourt-worker` polls the queue; the API dispatches nothing. |
| `embedded` | local development | A worker thread inside the API process. |
| `none` | tests, and inside workers | Never dispatch. |

Workers on public GitHub runners write public logs, so they run with `PUBLIC_LOGS=true`: only allow-listed fields (ids, job types, counts, timings) are logged, exception messages are dropped, and tracebacks are reduced to code locations.

### Audit pipeline

`pipeline/runner.py` runs the stages listed in [METHODOLOGY.md](METHODOLOGY.md#pipeline). Each stage:

- reads and writes typed artifacts through `AuditContext`: NumPy `.npz` and JSON files in object storage under `orgs/{org}/audits/{audit}/`;
- persists its findings to Postgres;
- records its timing and algorithm version in `audit_pipeline_steps`.

Artifacts let later features reuse embeddings, OOF probabilities, TracIn bases and structural thumbnails without recomputing them. What-if runs, counterfactuals, contamination checks and the Blame Map all depend on this.

### Data model (summary)

63 tables with UUID primary keys, created by the Alembic migration `0001_initial_schema`; `0002_execution_and_uploads` adds execution bookkeeping on `jobs` (time limit, dispatch state, runner URL) and multipart upload state on `dataset_versions`:

| Group | Tables (examples) |
|---|---|
| Identity & tenancy | `users`, `auth_sessions`, `organizations`, `organization_members`, `api_tokens`, `security_audit_log` |
| Projects & data | `projects`, `audit_configs`, `data_contracts`, `datasets`, `dataset_versions`, `dataset_files`, `classes`, `splits`, `samples` |
| Audits | `audit_runs`, `audit_pipeline_steps`, `algorithm_versions`, `system_model_versions`, `sample_quality_metrics`, `quality_findings`, `embeddings_metadata`, `similarity_edges`, `duplicate_families`, `duplicate_family_members`, `leakage_findings`, `split_integrity_metrics`, `baseline_model_runs`, `model_predictions`, `training_dynamics`, `label_findings`, `rare_wrong_findings`, `shortcut_findings`, `coverage_clusters`, `coverage_gaps`, `collection_tasks`, `influence_findings`, `failure_events`, `failure_data_links`, `privacy_findings` |
| Court & review | `court_cases`, `case_evidence`, `case_verdicts`, `case_explanations`, `review_sessions`, `review_queue_items`, `human_decisions`, `reviewer_notes`, `review_budget_runs` |
| Governance | `dataset_debt_snapshots`, `preflight_results`, `contract_evaluations`, `dataset_dna_profiles`, `dataset_version_diffs`, `contamination_checks`, `evidence_ledger` |
| Experiments & outputs | `what_if_runs`, `what_if_actions`, `what_if_results`, `exports`, `audit_reports` |
| Infrastructure | `jobs`, `job_events` |

Every tenant-owned row carries `org_id`. Object keys are always built as `orgs/{org_id}/…` by the key helpers in `storage.py`, and the storage layer rejects keys containing traversal segments, backslashes, NUL bytes or a leading slash.

### Upload flow

1. `POST /projects/{id}/datasets` creates a dataset. `POST /datasets/{id}/versions` creates a version in `awaiting_upload` and returns an upload plan:
   - archives up to 64 MiB get one presigned PUT;
   - larger archives get a presigned multipart upload, with one URL per 16 MiB part.

   URLs are valid for 6 hours and sign the declared size. In development, the plan is an HMAC-signed URL served by the API.
2. The browser uploads directly to storage, three parts at a time, with retries per part and fresh part URLs after expiry (`POST /versions/{id}/upload/parts`). The archive never passes through Vercel. `GET /versions/{id}/upload` lists received parts; `POST /versions/{id}/upload/abort` discards an upload.
3. `POST /versions/{id}/finalize-upload` checks what arrived, then enqueues ingestion:
   - multipart: completes the upload from the bucket's own part list;
   - single PUT: one ranged read returns both the size and the ZIP signature.

   Ingestion can automatically enqueue a fast or deep audit.

### Object storage access

- Keys are content-addressed where possible. Workers write without a prior existence check, and keep a local write-through cache for the duration of a run. An audit after ingestion on the same runner therefore downloads nothing. A later job downloads the source ZIP once instead of one request per image.
- Thumbnails are served by `GET /api/v1/media/{token}`. The token is a signed capability whose URL is stable for a week, so a CDN can cache it (for at most a day). Full-size images, exports and reports use short-lived presigned URLs to the private bucket.
- Deletions remove every stored version of every object under the deleted prefix. B2 keeps old versions, and a plain delete would only hide them. The purge runs inline within a time budget; a durable `purge_prefix` job, created in the same transaction as the deletion, finishes anything left.

### Explanations

`services/explain.py` builds explanations from the case's stored evidence. The default is a deterministic template. When `GEMINI_API_KEY` is set, Gemini is called over REST with the evidence JSON and strict instructions. The response is checked for numbers that are not in the evidence, and any found are flagged as unverified. Gemini errors fall back to the template. Gemini explanations are cached per case and evidence hash, so an unchanged case is never sent twice.

## Scaling notes

- **Embedding and kNN.** Exact FAISS inner-product search is used up to hundreds of thousands of vectors. For larger datasets, swap in an IVF index behind `ml/knn.py`.
- **pHash candidates** use block-wise brute force up to 60,000 images. Above that, candidates come from SHA-256 and embedding kNN only.
- **t-SNE** runs on a capped sample. The remaining points are placed by nearest mapped neighbours.
- **Workers.** Raise `WORKER_MAX_PARALLEL` for more concurrent GitHub Actions runs, or add dedicated `datacourt-worker` processes (`EXECUTION_BACKEND=external`); the queue needs no other coordination. A GPU worker for DINOv2 runs the same way.
- **Database.** Heavy per-sample tables (`samples`, `label_findings`, `case_evidence`) are indexed on `(audit_run_id, …)` / `(dataset_version_id, …)`, and list endpoints are paginated.

## Repository layout

```
apps/web/                 Next.js app
  src/app/                routes (landing, docs, auth, /app workspace)
  src/components/         UI kit, charts, shell, lineage graph, sample modal
  src/lib/                API client, session and dataset contexts, glossary
  src/content/docs/       synced copies of docs/*.md for the /docs pages
services/api/             FastAPI service and worker (package `datacourt`)
  datacourt/              application code
  tests/                  pytest suite (real PostgreSQL, moto S3, fake GitHub API)
  Dockerfile              dedicated worker / self-hosted image
docs/                     documentation (this folder) and benchmark results
vercel.json               Vercel services: web (apps/web) and api (services/api), routing
.github/workflows/        ci.yml (lint, types, tests, build, secret scan),
                          datacourt-worker.yml (batch worker), db-migrate.yml,
                          storage-check.yml, storage-cors.yml, ingest-check.yml
```
