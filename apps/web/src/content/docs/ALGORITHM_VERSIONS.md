# Algorithm versions

Every score in DataCourt comes from a named, versioned algorithm. Each audit stores:

- `algorithm_versions`: the version of every stage that ran;
- `config` and `config_hash`: the fully resolved configuration and its SHA-256;
- the embedding backend identifier, including its model revision.

The same registry is served live at `GET /api/v1/algorithms` and shown on the **Models & algorithms** page. It is defined in `services/api/datacourt/registry.py`, which imports each version constant from its implementing module, so the registry cannot drift from the code.

## Current versions

| Area | Version | Output type | Summary |
|---|---|---|---|
| Configuration | `audit-config-v1` | measured | Versioned default thresholds; project overrides are validated and stored with each audit |
| Ingestion | `phash-dct32-v1` | measured | SHA-256 identity; 64-bit DCT perceptual hash and dHash |
| Ingestion | `pixel-sha256-rgb8-v1` | measured | SHA-256 of the decoded, normalised pixels: the same picture in any file format |
| Quality | `attributes-v1` | measured | Per-image brightness, contrast, normalised sharpness, entropy, edges, colour, border, aspect |
| Quality | `quality-rules-v1` | heuristic | Absolute and dataset-relative rules for potential blur, exposure, contrast, near-empty, resolution, aspect, extension mismatch |
| Embeddings | `dc-descriptor-v1` | measured | 2,588-d CPU descriptor (default) |
| Embeddings | `dinov2-small` (`facebook/dinov2-small@<revision>`) | measured | Optional self-supervised ViT-S/14 |
| Duplicates | `dup-families-v2` | measured | Candidates from hashes and kNN, structural verification, families, lineage |
| Leakage | `leakage-v1` | measured | Cross-split families and adaptive similar-subject clusters |
| Baseline | `baseline-logreg-v2` | model prediction | Class-balanced logistic head; OOF probabilities; temperature scaling |
| Training dynamics | `cartography-v1` | model estimate | Confidence, variability, correctness, forgetting |
| Influence | `tracin-linear-v1` | model estimate | TracIn on the linear head |
| Labels | `label-forensics-v1` | heuristic | Reliability-weighted witness evidence score |
| Labels | `rare-or-wrong-v1` | heuristic | Competing hypothesis scores |
| Shortcuts | `shortcut-detective-v1` | measured | Cramér's V, lift, cue-only accuracy, perturbation tests |
| Coverage | `coverage-v1` | measured | Map, regions, gaps, collection planner |
| Privacy | `privacy-scan-v1` | heuristic | Haar face and MSER text-region detectors |
| Court | `jury-v1` | heuristic | Reason codes, ordered rules R1–R10, uncertainty, priority |
| Review | `review-budget-v1` | heuristic | Greedy budgeted selection with diminishing returns |
| Review | `counterfactual-proxy-v1` | model estimate | First-order action effect estimates from the TracIn basis |
| Experiments | `what-if-v1` | model estimate | Paired retraining with training and evaluation bootstraps |
| Governance | `debt-v1` | heuristic | Eight debt dimensions with published formulas |
| Governance | `preflight-v1` | heuristic | Blocking and warning rules |
| Governance | `contract-v1` | measured | Declarative contract rules over 15 metrics |
| Versions | `dna-v1` | measured | Descriptive profile and JS-divergence drift |
| Versions | `version-diff-v1` | measured | SHA-matched version comparison |
| Versions | `contamination-v2` | measured | Exact and verified near-duplicate overlap with a reference version |
| Export | `export-v1` | measured | Deterministic clean export with manifest and checksums |
| Retention | `retention-v1` | measured | Time-based purge of redundant archives |

## Versioning policy

- **Patch-level changes** (bug fixes that do not change outputs on valid input) keep the version string.
- **Any change to outputs**, including thresholds, formulas, features or ordering, bumps the version: `jury-v1` → `jury-v2`. The previous version is documented here with the reason for the change.
- Old audits keep their stored versions and configuration. Comparing a new audit with an old one shows both versions, so differences caused by algorithm upgrades are not mistaken for data changes.
- Default configuration changes bump `audit-config-vN`.

## History

| Version | Replaced by | Reason |
|---|---|---|
| `dup-families-v1` | `dup-families-v2` | Files with identical decoded pixels in different formats (a HEIC and a lossless PNG of it) were near duplicates; v2 makes them exact duplicates. Families otherwise unchanged. |
| `contamination-v1` | `contamination-v2` | Exact overlap also matches identical decoded pixels, as in `dup-families-v2`. |
| `baseline-logreg-v1` (SGD for everything) | `baseline-logreg-v2` | SGD optimiser noise made What-if deltas unstable between seeds. v2 uses converged L-BFGS for evidence, metrics and experiments, and keeps SGD only where the training trajectory is itself the evidence (dynamics, TracIn). |
