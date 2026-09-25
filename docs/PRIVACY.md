# Privacy

DataCourt processes datasets that may contain personal data: photos of people, readable documents, licence plates. This page describes what is processed, where, for how long, and which controls exist. It describes the software's behaviour. It is not a data processing agreement or legal advice.

## What DataCourt stores

| Data | Where | Why |
|---|---|---|
| Account: email, name, password hash (scrypt) | PostgreSQL | sign-in |
| Session and API-token hashes, user agent | PostgreSQL | authentication, security |
| Uploaded archive (ZIP) | object storage (`orgs/{org}/datasets/…/source/original.zip`) | ingestion; can be purged by the retention policy once ingested |
| Individual images and thumbnails | object storage (`orgs/{org}/datasets/…`) | analysis, review, export |
| Derived features: embeddings, hashes, statistics, model outputs | object storage artifacts and PostgreSQL | findings, experiments, reports |
| Review decisions and notes | PostgreSQL | human review, export, audit trail |
| Evidence ledger and security log | PostgreSQL | integrity and accountability |
| Exports and reports | object storage | download |

DataCourt does not use customer data to train shared models. Every model fitted during an audit, such as the baseline classifier or the influence checkpoints, is scoped to that audit and stored in that workspace.

## Third parties

| Service | Receives | When |
|---|---|---|
| Hosting (Vercel) | request traffic; the API runs here | always |
| Batch processing (GitHub Actions hosted runners) | images, archives and derived data while a job runs. Runners are ephemeral virtual machines, discarded after each run. Logs hold ids, job types and timings only. | when jobs run (production) |
| Database (Neon) | all relational data above | always |
| Object storage (Backblaze B2, private bucket) | archives, images, artifacts, exports, reports | always (production) |
| Google Gemini API | the **structured evidence** of a case: reason codes, witness findings, scores, the current label and split. **Never image pixels, file paths or reviewer notes.** | only if `GEMINI_API_KEY` is configured and a user asks for an explanation |
| Hugging Face | nothing about your data; only a model download | only if the DINOv2 backend is enabled |

With no Gemini key, no dataset information leaves the DataCourt services. Explanations then use deterministic templates.

## Privacy scan

The audit can flag **potential** faces (OpenCV Haar frontal-face detection) and text-like regions (MSER heuristics, no OCR). It is **detection only**: DataCourt never performs face recognition, identity matching, biometric template extraction or text transcription. Flags appear on the Privacy page and as neutral case evidence, so teams can decide whether consent, blurring or removal is needed. The scan can be switched off per workspace for datasets that legitimately contain people.

## Retention and deletion

- **Archive retention** (workspace setting, off by default): when set, a periodic maintenance job permanently deletes uploaded archives (after ingestion), export archives and report files older than the configured number of days. Dataset versions, findings, decisions and the ledger are kept until you delete them. Each purge is recorded in the security log.
- **Deleting data** (a dataset, a version, a workspace or an account) removes the database rows at once. It also permanently deletes every stored version of the related objects: images, thumbnails, archives, audit artifacts, exports and reports. Large deletions finish in a background job created in the same transaction. Thumbnails previously served to members can remain in CDN caches for up to a day.
- **Workspace deletion** (owners, with name confirmation) deletes every project, dataset version, image, artifact, export and report, together with the related database rows.
- **Account deletion** (with email confirmation) deletes every workspace where the user was the only owner, including its data, and signs them out everywhere. If they have review decisions or notes in workspaces that still exist, the account is **pseudonymised** instead of deleted: email and name are replaced, the password is destroyed, the account is deactivated and all memberships are removed. The decisions stay attributed to "Deleted user", so other teams' review state and audit trails are not silently changed. Otherwise the account row is deleted.
- Demo visitors are ephemeral viewer accounts in the demo workspace. They hold no personal data beyond a random identifier.

## Data minimisation

- Logs never contain image content, file contents, passwords or tokens. Paths and IDs appear only where needed for debugging.
- LLM prompts contain evidence summaries, not images, and are only sent on demand.
- Provenance metadata (`provenance.csv`) is optional. Only a fixed set of columns is stored, each up to 500 characters: source, license, collector, collection method, capture date and consent note.

## Your responsibilities as an operator or customer

- Make sure you have a lawful basis to process the images you upload, and that your agreements with data subjects cover dataset quality auditing.
- Keep the object storage bucket private, and choose hosting regions that fit your requirements.
- Use the retention setting and deletion tools to meet your own retention obligations.
- DataCourt reports are technical audit aids. They are not legal, regulatory or compliance certifications.
