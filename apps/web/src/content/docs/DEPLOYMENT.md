# Deployment

DataCourt's production deployment runs entirely on free tiers, with no paid resource and no credit card:

| Component | Service | Role |
|---|---|---|
| Web app and API | **Vercel Hobby**, one project with two services (root [`vercel.json`](../vercel.json)) | `apps/web` (Next.js UI) and `services/api` (FastAPI: auth, tenancy, review, dataset uploads, job orchestration and status). The API runs no ML. |
| Batch worker | **GitHub Actions**, standard hosted runners ([`datacourt-worker.yml`](../.github/workflows/datacourt-worker.yml)) | Ingestion, audits, What-if runs, exports, reports, version diffs, contamination checks and storage purges |
| Database | **Neon** Postgres (free plan) | Source of truth: users, projects, audits, findings, review state, metrics, and the job queue with its status |
| Object storage | **Backblaze B2** (free tier, private bucket) | Source ZIPs, sample images, thumbnails, audit artifacts, exports and reports |
| Explanations (optional) | Google Gemini API | Only when `GEMINI_API_KEY` is set on Vercel |

## How a dataset becomes an audit

```
Browser ──(1) create version ──► Vercel API ──► Neon: dataset_version (awaiting_upload)
Browser ──(2) presigned PUTs ───────────────────► Backblaze B2 (private bucket; the archive never passes through Vercel)
Browser ──(3) finalize ────────► Vercel API ──► Neon: version uploaded + ingest job queued
                                    │ after commit: workflow_dispatch (server-side GitHub token)
                                    ▼
                              GitHub Actions run: migrate → drain the queue
                                    │ ingest ZIP → audit → findings/cases   ◄──► Neon (state, progress)
                                    │                                        ◄──► B2 (objects, artifacts)
Browser ◄─(4) polls job status ─ Vercel API ◄── Neon (progress, stage, run link, errors)
```

1. `POST /datasets/{id}/versions` creates the version and returns an **upload plan**. Archives up to 64 MiB use one presigned PUT. Larger archives use a presigned **multipart** upload in 16 MiB parts, sent 3 at a time and retried individually. Presigned URLs sign the declared size, so they cannot store a larger object.
2. `POST /versions/{id}/finalize-upload` completes the upload from the bucket's own list of received parts (the CORS rules need not expose `ETag`). It checks the size and ZIP signature, then queues ingestion.
3. After the transaction commits, the API starts the worker workflow. It calls `workflow_dispatch` with a fine-grained GitHub token that exists only in Vercel's environment.
4. The run applies pending migrations, then drains the queue: ingestion, then the audit it queues, then anything else waiting. Progress, stage, attempts, errors and the run URL are written to Neon. The UI polls the API every few seconds and shows the state: *starting a worker*, *running on GitHub Actions (view run)*, *retrying*, *failed* or *cancelling*.

Reliability features:

| Concern | Mechanism |
|---|---|
| Job identity & idempotency | UUID job ids; idempotency keys (`ingest:{version}`, `audit:{id}`); claims use `FOR UPDATE SKIP LOCKED`, so a job runs on one worker at a time |
| Concurrency | `WORKER_MAX_PARALLEL` slots (default 2). Each slot is a GitHub Actions concurrency group: one running run, at most one pending. The API picks the least busy slot. |
| Lost dispatches | Recorded on the job (`dispatch_error`). Status polling re-dispatches queued jobs no runner picked up, with backoff; a schedule every 6 hours is the safety net. |
| Retries | Transient failures are retried with exponential backoff, up to 3 attempts in total; completed audit stages are kept. Permanent errors (such as an invalid archive) fail immediately. |
| Timeouts | Every job has a time limit scaled to its size (15 min – 3 h). A run only claims jobs that fit in its remaining budget (5 h of the 5.5 h job limit), and hands leftover work to a successor run. |
| Worker loss | Runs heartbeat every few seconds; a stale heartbeat (runner cancelled or lost) re-queues the job. |
| Cancellation | Cooperative: queued jobs are cancelled at once; running jobs stop at the next checkpoint. |
| Cleanup | Deleting data purges every stored version of every object (bounded inline, then a purge job). A maintenance run removes unfinished multipart uploads and applies retention policies. |
| Public logs | This repository is public, so run logs are public. Workers log ids, job types, counts and timings only. |

## Free-tier limits to know

- **Vercel Hobby** is for non-commercial use. The API functions are light: they never process images.
- **GitHub Actions**: standard runners are free with unlimited minutes for **public** repositories. A private repository on GitHub Free gets 2,000 minutes a month; at the $0 default spending limit, runs simply stop when the quota is used. GitHub pauses scheduled workflows in public repositories after 60 days without activity; dispatches from the API are not affected.
- **Neon free plan**: 0.5 GB storage per branch, and the compute suspends when idle.
- **Backblaze B2 without a payment method** has daily caps: 10 GB storage, 1 GB of downloads, and 2,500 Class B (downloads, HEAD) and 2,500 Class C (listing) transactions. Past a cap, requests fail until the daily reset, and nothing is charged. DataCourt keeps well within them:
  - writes are unconditional, so no existence check precedes an upload;
  - workers cache everything they write, and read samples from a single download of the source ZIP;
  - thumbnails are served through a CDN-cacheable URL;
  - uploads are validated with one ranged read.

  Heavy browsing of new thumbnails, or re-auditing large datasets many times a day, can still reach a cap; the UI then reports that storage is temporarily unavailable.

## 1. Database (Neon)

The `datacourt-ai` project (region AWS Singapore, `aws-ap-southeast-1`) already holds the schema in its `datacourt` database, at migration revision `0002`. Later migrations are applied automatically by the *Database migrations* workflow and by every worker run.

In the Neon console, open **Connect**, select the `datacourt` database (role `datacourt`), and copy two connection strings:

- the **pooled** string (host contains `-pooler`), for Vercel;
- the **direct** string (pooling off), for GitHub Actions. Migrations and long-running workers need a direct session.

Paste them as-is: `postgresql://…?sslmode=require` is accepted, and the driver is added automatically. Nothing needs to be enabled in Neon.

## 2. Object storage (Backblaze B2)

The bucket `datacourt-ai` must stay **private**.

1. **Endpoint.** On the *Buckets* page, the bucket shows its endpoint, for example `s3.us-west-004.backblazeb2.com`. Then:
   - `S3_ENDPOINT_URL` is `https://` followed by that host;
   - `S3_REGION` is the middle part of the host, for example `us-west-004`.
2. **Application key.** Use your bucket-scoped key with *Read and Write*. Its **keyID** is `S3_ACCESS_KEY_ID` and its **applicationKey** is `S3_SECRET_ACCESS_KEY`. B2 shows the application key only once.
3. **CORS for browser uploads.** Browsers upload archives straight to the bucket with presigned `PUT` requests, so the bucket must answer their CORS preflight for the app's origin. The web console's CORS presets allow downloads only, and B2 then refuses the upload preflight with `403`. Set the rule with the **Bucket CORS** workflow instead: **Actions → Bucket CORS → Run workflow**, with origins `https://datacourt-ai.vercel.app`. It uses the same repository secrets as the worker and sets this rule with the S3-compatible `PutBucketCors` call:

   | field | value |
   |---|---|
   | origins | `https://datacourt-ai.vercel.app` (exact; no wildcard) |
   | methods | `GET`, `PUT`, `HEAD` |
   | request headers | `content-type` |
   | exposed headers | `ETag` |
   | max age | 3600 seconds |

   - It becomes the only rule for that origin, so it replaces a download-only preset for the same origin. Rules for other origins are kept.
   - B2 refuses `PutBucketCors` while the bucket has rules set through its Native API, and the web console's presets are such rules. The workflow then writes the same rule through the Native API's `b2_update_bucket`, with the same key. The rule's operations are the S3 ones: `s3_get`, `s3_head` and `s3_put`. It sends only the bucket's id and CORS rules, and confirms the bucket type is still `allPrivate`.
   - It changes nothing else: never the bucket's ACL, policy, type or objects. The bucket stays private and every upload still needs a presigned URL.
   - It then sends the preflight requests browsers send: a single-file upload with `Content-Type`, a multipart part upload, and downloads. It fails unless the bucket accepts them, and it checks that another origin is refused.
   - Tick **dry run** to see the current and planned rules without changing anything.
   - The rule lists exactly the origins you give. If you add a custom domain later, run it with both, separated by a space.
   - To run it locally, set the `S3_*` variables and run `datacourt-storage-cors --origin https://datacourt-ai.vercel.app`.

   Both ways of writing the rule need the key's `writeBuckets` capability. If the run says the key lacks it, B2 does not let this key change bucket settings, and the bucket is left unchanged. Set the rule once from your own computer with the [B2 command-line tool](https://www.backblaze.com/docs/cloud-storage-command-line-tools) and your master application key. The tool prompts for the key, so it never lands in the repository, a secret or a chat:

   ```bash
   b2 account authorize
   b2 bucket update --cors-rules '[{"corsRuleName":"datacourt-browser-uploads","allowedOrigins":["https://datacourt-ai.vercel.app"],"allowedOperations":["s3_get","s3_head","s3_put"],"allowedHeaders":["content-type"],"exposeHeaders":["etag"],"maxAgeSeconds":3600}]' datacourt-ai allPrivate
   ```

   Then run the Bucket CORS workflow with **dry run** ticked. It verifies the preflights without writing.
4. **Lifecycle (recommended).** Set the bucket's lifecycle to *Keep only the last version of the file*. This removes old versions left by overwrites. User-requested deletions already remove every version explicitly.

Verify with the **Object storage check** workflow (section 3). It exercises writes, reads, ranged reads, listing, single and multipart presigned uploads, the signed size limit, presigned downloads with a file name, artifact round-trips, versioned deletes and the CORS preflights for single-file and multipart uploads. It then removes its test objects.

## 3. Worker (GitHub Actions)

In the repository, open **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `DATABASE_URL` | Neon **direct** connection string |
| `S3_ENDPOINT_URL` | `https://s3.<region>.backblazeb2.com` |
| `S3_REGION` | `<region>`, e.g. `us-west-004` |
| `S3_BUCKET` | `datacourt-ai` |
| `S3_ACCESS_KEY_ID` | B2 keyID |
| `S3_SECRET_ACCESS_KEY` | B2 applicationKey |

The worker needs no other credential. It uses the run's own `GITHUB_TOKEN`, with the `actions: write` permission the workflow requests, only to start a successor run when its time budget runs out.

The repository has five workflows besides CI:

- **DataCourt worker** (`datacourt-worker.yml`). The API dispatches it. It also runs every 6 hours to run maintenance and pick up anything left. You can start it manually with the task `drain`, `maintenance` or `seed-demo`.
- **Database migrations** (`db-migrate.yml`). It runs when migrations change on the deployed branch, and on demand.
- **Object storage check** (`storage-check.yml`). Run it on demand after configuring B2.
- **Bucket CORS** (`storage-cors.yml`). Run it on demand to set the bucket's CORS rule for browser uploads (section 2).
- **Ingestion check** (`ingest-check.yml`). Run it on demand to see ingestion work on a GitHub-hosted runner, installed exactly like the worker, against a throwaway database (no secrets, no production data). It ingests and audits a mixed JPG/HEIC dataset, including real phone photos from a public HEIC test corpus, and checks decoding, originals, thumbnails, browser copies and duplicates across formats.

HEIC/HEIF decoding comes with the worker's Python dependencies (the `ml` extra: pillow-heif, whose wheels include libheif), so runners need no system packages. The Vercel API does not install it.

## 4. Web app and API (Vercel)

The project `datacourt-ai` builds from the **repository root**. The root `vercel.json` defines two services:

- `web`: the Next.js app in `apps/web`;
- `api`: FastAPI in `services/api`, entrypoint `datacourt.main:app`. Only its core dependencies are installed; the ML stack is the worker-only `ml` extra.

`/api/*` is routed to the API and everything else to the web app, all on one origin, so session cookies stay first-party. Functions run in `sin1` (Singapore), next to the Neon database.

Project settings: *Framework Preset* **Services**, *Root Directory* empty (the repository root). Set these **Environment Variables** for **Production**, and also for **Preview** if you want preview deployments to work:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Neon **pooled** connection string |
| `AUTH_SECRET` | random, ≥ 32 characters |
| `SIGNED_URL_SECRET` | a different random value, ≥ 32 characters |
| `ENVIRONMENT` | `production` |
| `COOKIE_SECURE` | `true` |
| `PUBLIC_APP_URL` | `https://datacourt-ai.vercel.app` |
| `ALLOWED_ORIGINS` | `https://datacourt-ai.vercel.app` |
| `STORAGE_BACKEND` | `s3` |
| `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | as for GitHub Actions |
| `EXECUTION_BACKEND` | `github_actions` |
| `DATACOURT_GITHUB_REPO` | `ariful-arif-232/datacourt-ai` |
| `DATACOURT_GITHUB_TOKEN` | fine-grained personal access token (below) |
| `DATACOURT_GITHUB_REF` | optional: the branch whose worker workflow runs; defaults to the deployed branch |
| `GEMINI_API_KEY` | optional |

Generate each secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"` or `openssl rand -base64 48`. Remove `API_ORIGIN` if it is still set; the API is now part of the same deployment.

**GitHub token for dispatching workers.** Create it under GitHub **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**:

- *Resource owner*: your account.
- *Repository access*: **Only select repositories** → `datacourt-ai`.
- *Repository permissions*: **Actions: Read and write** (Metadata: read-only is added automatically).
- *Expiration*: your choice. Rotate the token before it expires.

The token stays in Vercel's server environment. It is sent only to the GitHub API, and is never logged, stored or exposed to browsers.

On Vercel the API always applies the production rules, even if `ENVIRONMENT` is missing, so it never falls back to development defaults. Until the settings are complete, it answers `503` on `/api/v1/health` with the names of the missing settings (never their values), and the sign-in page explains that the API is not configured yet.

## 5. Demo workspace

In **Actions → DataCourt worker → Run workflow**, choose the task `seed-demo`. It generates the synthetic benchmark, runs the real ingestion and a deep audit, and reviews part of the queue as a demo curator. Visitors then use **Explore the read-only demo**. Set `DEMO_ENABLED=false` on Vercel to hide it.

## 6. Verify

1. **Actions → Object storage check → Run workflow**, with origin `https://datacourt-ai.vercel.app`. All checks should pass. If only the CORS check fails, run **Bucket CORS** (section 2) first.
2. `https://datacourt-ai.vercel.app/api/v1/health` should return `"ok": true, "storage": "s3", "execution": "github_actions", "database": "ok"`.
3. Register, create a project and upload a ZIP. The version page should show *Starting a worker on GitHub Actions*, then *Running on GitHub Actions · view run*, then the finished audit.

## Adding a dedicated worker or GPU later

The execution layer is portable. Any machine with the same `DATABASE_URL` and `S3_*` settings can run the worker:

```bash
pip install "./services/api[ml,deep]"      # deep: DINOv2 (torch + transformers)
DATACOURT_ROLE=worker ENVIRONMENT=production datacourt-worker          # polls forever
```

Then set `EXECUTION_BACKEND=external` on Vercel, so the API stops dispatching GitHub Actions runs. The `Dockerfile` in `services/api` builds this worker image. Jobs are claimed with `SKIP LOCKED`, so GitHub Actions runs and dedicated workers can also run side by side.

## Local development

```bash
createdb datacourt                  # any local PostgreSQL

cd services/api
python -m venv .venv && . .venv/bin/activate
pip install -e ".[ml,dev,server]"   # add ,deep for DINOv2
cp .env.example .env                # local storage, embedded worker
alembic upgrade head
RUN_EMBEDDED_WORKER=true uvicorn datacourt.main:app --reload --port 8000

cd apps/web
cp .env.example .env.local          # API_ORIGIN=http://127.0.0.1:8000
npm install && npm run dev          # http://localhost:3000
```

Tests:

- **API.** Run `pytest` in `services/api`. It needs PostgreSQL; set `TEST_DATABASE_URL` if yours is not `postgresql+psycopg://postgres@127.0.0.1:5432/datacourt_test`. The suite includes:
  - the S3 storage layer against a local versioned S3 server (`moto`);
  - a production-shaped end-to-end run: the API dispatches to a fake GitHub API, which starts real worker processes.
- **Web.** Run `npm test`, `npm run lint`, `npm run typecheck` and `npm run build` in `apps/web`.

## Operations

- **Logs.** The API writes structured JSON with `request_id` and `job_id`. Each GitHub Actions run links from the job's status in the UI, and its step summary lists job ids, types and timings.
- **Admin metrics** (Settings → Security & operations): audits, durations, queue length and failures by class, per workspace.
- **Backups.** Neon point-in-time restore (the free plan keeps 6 hours of history). Objects can be re-created from the source archives only while retention keeps them.
- **Rotating credentials.** Replace the value in Vercel or GitHub. Redeploy on Vercel for the API to use it; workers pick up new secrets on their next run.
