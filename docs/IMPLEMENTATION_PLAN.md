# DataCourt AI — Implementation Plan

## Starting state (inspected 2026-09-24)

- Repository was empty (no commits, no files). Nothing to reuse.
- Sandbox: 4 CPU, 15 GB RAM, no GPU, Python 3.11, Node 22, PostgreSQL 16 (local).
- Network policy: PyPI and npm reachable; **HuggingFace and download.pytorch.org are blocked**,
  so pretrained DINOv2/SigLIP weights cannot be fetched inside this sandbox.
- Connected services:
  - Neon: reachable (free tier). A dedicated `datacourt-ai` project can be created.
  - Vercel: personal Hobby account (no team).
  - Railway: authenticated, new account without workspace (trial/plan status must be confirmed before creating billable services).
  - Cloudflare R2: **not enabled on the account** (API error 10042). Requires the account owner to enable R2 in the dashboard.

## Architecture decisions

| Concern | Decision | Why |
| --- | --- | --- |
| Repo | Monorepo: `apps/web` (Next.js), `services/api` (FastAPI + worker), `docs/` | One CI, one version history |
| DB | PostgreSQL (Neon in prod, local PG in dev/CI), SQLAlchemy 2 + Alembic | JSONB, `FOR UPDATE SKIP LOCKED` queue |
| Queue | Postgres `jobs` table, safe claiming, heartbeat, stale-job reaper | No extra infra |
| Storage | `ObjectStore` interface: `LocalObjectStore` (HMAC-signed URLs) and `S3ObjectStore` (R2/any S3, presigned URLs) | R2 not enabled yet; swap by env |
| Embeddings | `EmbeddingBackend` interface: `dinov2-small` (torch, optional extra) and `dc-descriptor-v1` (deterministic CPU descriptor) | Weights blocked in sandbox; free-tier CPU hosts |
| Similarity | FAISS `IndexFlatIP` (exact cosine) with NumPy fallback | Reproducible, no approximate-index drift |
| Baseline model | Multinomial logistic head on frozen embeddings; K-fold out-of-fold probabilities; seeded SGD run for training dynamics | Cheap, deterministic, enables exact TracIn for the head |
| Influence | TracIn over SGD checkpoints for the linear head (exact for that head, approximation for the full system) | Honest, computable on CPU |
| Verdicts | Deterministic, versioned jury rules producing reason codes | LLM never decides |
| LLM | Gemini (optional) receives structured evidence only; template fallback | Correctness path is LLM-free |
| Auth | Email + scrypt password hashes, opaque DB-backed session tokens in HttpOnly cookies; API tokens for CI | No third-party dependency |
| Frontend → API | Next.js rewrites `/api/*` to the API origin, so cookies are first-party | Avoids cross-site cookie issues |

## Update 2026-09-25: free production architecture

The owner replaced the two open production blockers. They chose no paid hosting (so no Railway or Render) and Backblaze B2 instead of Cloudflare R2:

| Concern | Decision | Why |
| --- | --- | --- |
| Web + API hosting | One Vercel Hobby project with two services: Next.js (`apps/web`) and FastAPI (`services/api`), routed by the root `vercel.json` | Free; one origin keeps cookies first-party; the API installs only its core dependencies (no ML stack) |
| Heavy processing | GitHub Actions (`datacourt-worker.yml`) as a batch worker, dispatched by the API after a job commits; slots as concurrency groups, time budgets, re-dispatch and continuation runs | Free standard runners for this public repository; the Postgres queue stays the source of truth |
| Execution portability | `EXECUTION_BACKEND` = `github_actions` / `external` / `embedded` / `none` | A dedicated or GPU worker can replace Actions without code changes |
| Storage | Backblaze B2 through the existing `S3ObjectStore`: presigned single and multipart uploads, ranged reads, version-aware deletes, worker write-through cache, thumbnails via a CDN-cacheable media route | B2 is free without a card, but caps daily downloads and listings; the design keeps request counts low |
| Database | Neon (unchanged); migration `0002` adds job execution state and multipart upload state | |

## Phases (executed in order, each ends with tests + commit)

1. Foundation: config/env validation, DB models + migrations, auth, orgs/projects, storage, jobs, CI.
2. Ingestion: upload → immutable ZIP → safe extraction → layout detection → image validation → hashes → thumbnails → manifest.
3. Core evidence: quality engine, embeddings, kNN, duplicate families + lineage, leakage.
4. Model evidence: baseline, predictions, training dynamics, label forensics, rare-vs-wrong, influence.
5. Advanced intelligence: shortcut detective, coverage map, collection planner, Dataset DNA, drift.
6. Court workflow: cases, witnesses, jury, Gemini/template explanations, human review, consensus.
7. Decision optimization: review budget, blame map, failure replay, counterfactual proxies.
8. Experimental verification: what-if lab with multi-seed before/after and preserved holdout.
9. Governance: debt, preflight, data contracts + CI API, evidence ledger (hash-chained), provenance, privacy scan, contamination check.
10. Export/publishing: clean export (new version), audit report, docs, benchmark, deployment.
