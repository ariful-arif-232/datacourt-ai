# Methodology

DataCourt turns raw image-classification data into **evidence**, evidence into **cases**, and cases into **human decisions** whose effect can be **tested** before it is exported. This page describes every stage, the formulas, the default thresholds and what each output does *not* mean.

Every threshold lives in one versioned configuration (`audit-config-v1`, `services/api/datacourt/pipeline/config.py`). An audit stores the fully resolved configuration, its SHA-256 and the version of every algorithm it ran, so any finding can be traced to the exact rules that produced it. Projects can override individual values. Unknown keys are rejected.

## Evidence types

Each value in the product carries one of six labels. The UI shows them as tags.

| Type | Meaning | Examples |
|---|---|---|
| Measured | A direct measurement or deterministic fact | SHA-256, image statistics, split overlap |
| Heuristic | A versioned rule applied to measurements | quality rules, jury verdicts, debt levels |
| Model prediction | Output of the baseline classifier | out-of-fold probabilities, confusion matrix |
| Model estimate | An approximation of a counterfactual quantity | TracIn influence, what-if effects |
| LLM explanation | Optional wording of existing evidence | case summaries (Gemini, if configured) |
| Human decision | A reviewer's decision | keep / relabel / remove / escalate |

Only human decisions change data, and only through a clean export that creates a new version. Originals are immutable.

## Pipeline

Stages run in this order for each audit (`fast` or `deep` profile):

`PROFILING → QUALITY_ANALYSIS → EMBEDDING → DUPLICATE_ANALYSIS → LEAKAGE_ANALYSIS → BASELINE_TRAINING → TRAINING_DYNAMICS → INFLUENCE_ANALYSIS → LABEL_FORENSICS → SHORTCUT_ANALYSIS → COVERAGE_ANALYSIS → COURT_CASE_GENERATION → DEBT_CALCULATION → PREFLIGHT`

The deep profile uses 5 folds instead of 3 and adds perturbation tests, a larger coverage map and influence over more failures. Stage timings are stored with every run.

## Decoding and hashes

Each file is decoded once at ingestion. JPEG, PNG, WebP, BMP, TIFF and GIF are decoded by Pillow; HEIC/HEIF by libheif (through pillow-heif). All formats are then normalised the same way: the rotation stored in the file applied (the EXIF orientation, or the HEIF container's transform, which libheif applies while decoding), transparency composited on white, 8-bit RGB. A HEIC and the same photo saved as a rotated JPEG are therefore measured alike.

- `phash-dct32-v1`: SHA-256 of the file, and a 64-bit DCT perceptual hash plus dHash of the normalised picture reduced to 512 px (also of its mirror image).
- `pixel-sha256-rgb8-v1`: SHA-256 of the normalised picture's pixels at full size. It is equal for files that hold the same pixels in different formats, such as a HEIC and a lossless PNG exported from it, where the file SHA-256 differs.

## Profiling and quality

`attributes-v1` measures each image on a downscaled copy so values are comparable across resolutions. It records brightness, contrast (grey-level standard deviation), sharpness (variance of the Laplacian, normalised to a 256 px short side), entropy, edge density, saturation, border colour and brightness, aspect ratio, format and resolution.

`quality-rules-v1` interprets those measurements. Findings are described as *potential* issues. Blur, exposure and contrast findings need an absolute threshold **and** a dataset-relative check to agree. The relative check is a robust z-score (median and MAD) across the dataset, so a uniformly dark dataset does not flag every image. Severity is high when the z-score is 1.5 times beyond its threshold.

| Finding | Rule (defaults) |
|---|---|
| Potential blur | normalised sharpness < 150 **and** robust z of log-sharpness < −3.0 |
| Potential under- / over-exposure | mean brightness < 25 / > 235 **and** robust \|z\| > 2.5 in the same direction |
| Low contrast | contrast < 8 **and** robust z < −2.5 |
| Near-empty | entropy < 2.0 bits **and** edge density < 0.002 **and** contrast < 3 |
| Low resolution | short side < 32 px, or < 25% of the dataset's median short side |
| Unusual aspect ratio | robust \|z\| of log aspect ratio > 4.0 |
| Extension mismatch | file extension disagrees with the decoded format (deterministic) |

Quality alone never removes an image. A dark photo can be a valid low-light example.

## Embeddings

Similarity, neighbour witnesses, the baseline classifier and the coverage map all run on frozen image embeddings.

- **`dc-descriptor-v1`** (default) is a deterministic CPU descriptor with 2,588 dimensions. It concatenates an HSV colour histogram, a Lab spatial layout, coarse and fine HOG, an LBP texture histogram and a tiny thumbnail, with each block L2-normalised. It needs no GPU or download, but its semantics are weaker than self-supervised models.
- **`dinov2-small`** (optional) is the facebook/dinov2-small ViT-S/14, using the CLS token concatenated with the mean patch token. Pin the revision with `DINOV2_REVISION`. If the model cannot be loaded, the audit falls back to the descriptor and records a warning.

Nearest neighbours use exact inner-product search (FAISS `IndexFlatIP`) on L2-normalised vectors, with k = 10 by default.

## Duplicates

`dup-families-v2` finds exact and transformed copies and groups them into families.

**Candidates** come from four sources: identical SHA-256; identical decoded pixels (`pixel-sha256-rgb8-v1`); perceptual-hash (64-bit DCT pHash) Hamming distance ≤ 12, including against the mirrored image; and embedding kNN with cosine ≥ 0.90 (descriptor) or 0.85 (DINOv2). Embedding similarity only proposes candidates and never creates a duplicate on its own.

**Verification** needs structural evidence. Both images are reduced to 64 px greyscale thumbnails, and the *detail layer* (thumbnail minus its Gaussian blur, σ = 1.5) is compared by normalised cross-correlation (NCC):

| Relation | Rule |
|---|---|
| exact | identical SHA-256, or identical decoded pixels: the same picture in another file format (deterministic) |
| resized | detail NCC ≥ 0.92, same aspect ratio (±3%), different dimensions |
| photometric | detail NCC ≥ 0.92, brightness/saturation change ≥ 12 |
| flipped | mirrored detail NCC ≥ 0.92 |
| near | detail NCC ≥ 0.92 without a more specific relation |
| possible crop | ORB keypoints (images upscaled to 320 px) with RANSAC homography from the larger to the smaller image: ≥ 12 inliers, inlier ratio ≥ 0.6, overlap ≥ 40%, and NCC ≥ 0.80 of the difference-of-Gaussians band (σ 0.7–3.0) over the warped overlap |

Relations other than `exact` are heuristic descriptions of how two files relate.

**Families** are the connected components of verified pairs (union-find). Each family gets a lineage tree: a maximum spanning tree by similarity (Prim), rooted at the largest-area member as the most "source-like" file. The tree shows likely derivation, not proven provenance.

## Leakage

`leakage-v1` reports duplicate families that span training and evaluation splits:

| Cross-split relation | Touches test | Otherwise |
|---|---|---|
| exact | critical | high |
| transformed (resized, flipped, cropped, photometric) | high | medium |
| near | medium | medium |

Families with conflicting labels are flagged. **Similar-subject clusters** are pairs that are visually close across splits but not verified duplicates. Their threshold adapts to the dataset: the maximum of the backend floor (0.97 descriptor, 0.90 DINOv2) and the 99.5th percentile of nearest-neighbour similarity outside duplicate families. They are reported as low risk, and identity is never asserted.

## Baseline classifier

`baseline-logreg-v2` is one model family: a class-balanced multinomial logistic head on standardised embeddings. It is solved in two ways:

- **Converged L-BFGS** (L2, C = 0.001) produces stratified K-fold out-of-fold (OOF) probabilities, evaluation metrics and What-if experiments. It is deterministic, so differences reflect data changes rather than optimiser noise.
- **Seeded SGD** (momentum 0.9, 20 epochs, cosine schedule) is used where the training trajectory is the evidence: training dynamics and TracIn checkpoints.

Every training sample is scored by a model that never saw it. OOF logits are temperature-scaled, and expected calibration error (ECE) is reported before and after, so users can judge how far to trust the probabilities. If ECE > 0.12, cases get a `MODEL_POORLY_CALIBRATED` code.

## Training dynamics and influence

Training dynamics (`cartography-v1`) follow dataset cartography (Swayamdipta et al., 2020) and forgetting events (Toneva et al., 2019). Over SGD epochs they track *confidence* (the mean probability of the given label), *variability* (its standard deviation), *correctness* (the share of epochs where the argmax equals the label) and *forgetting* (correct → incorrect transitions). Each sample gets the first matching category:

1. consistently hard: confidence < 0.3 and correctness < 0.3
2. forgotten: ≥ 2 forgetting events
3. ambiguous: variability ≥ 0.2
4. easy: confidence ≥ 0.6 and correctness ≥ 0.8
5. unstable: anything else

Influence (`tracin-linear-v1`) follows TracIn (Pruthi et al., 2020) on the linear head. The per-sample gradient is (p − y) ⊗ [x, 1], so

`TracIn(z, z′) = Σ_checkpoints η · ((p(z) − y(z)) · (p(z′) − y(z′))) · (x·x′ + 1)`

The formula is exact for the head's checkpoints but ignores the frozen embedding network, so it is an **approximation** and is labelled as a model estimate. Negative values mean training on *z* increased the loss on *z′*. Those samples appear as "harmful" in the Blame Map and Failure Replay.

## Label forensics

`label-forensics-v1` combines independent **witnesses** into an evidence score in [0, 1]. The score is not a probability that the label is wrong.

| Witness | Signal *s* | Default weight |
|---|---|---|
| Baseline model | 1 − calibrated OOF probability of the given label | 0.35 |
| Neighbours | 1 − similarity-weighted share of the 10 nearest neighbours with the same label (duplicates of the sample excluded) | 0.30 |
| Class centroid | clip((r − 0.4) / 0.3, 0, 1), where r = d_own / (d_own + d_nearest_other) | 0.15 |
| Training dynamics | hard 1.0, forgotten 0.7, ambiguous 0.5, unstable 0.4, easy 0 | 0.10 |
| Duplicate conflict | 1 if a duplicate carries a different label | 0.10 |

**Witness reliability.** A witness is only as good as it proves to be on *this* dataset. Each witness's own label prediction is scored by chance-corrected balanced accuracy, (bacc − 1/C) / (1 − 1/C), clipped to [0, 1]. Configured weights are multiplied by reliability, with a floor of 0.05. The suspicion score is the weighted mean of the available signals. Witnesses with reliability below 0.35 don't produce jury reason codes (`NEIGHBOR_WITNESS_WEAK`, `EMBEDDING_WITNESS_WEAK`).

Default actions:

| Condition | Action |
|---|---|
| rare-like (below) | likely rare |
| suspicion ≥ 0.55 | review |
| suspicion ≥ 0.38 | low-priority review |
| otherwise | no action |

A sample is **rare-like** when the model disagrees (s ≥ 0.5), fewer than half of its neighbours share one other label, it sits in the sparsest 8% of the local-density distribution, it is still closer to its own class centroid (r < 0.55), and its image quality is acceptable.

## Rare or wrong

`rare-or-wrong-v1` scores competing hypotheses for each suspicious sample:

| Hypothesis | Evidence |
|---|---|
| Likely mislabeled | reliability-weighted mean of the other-class model probability, the dominant other-label neighbour share and the centroid signal; +0.15 when model, neighbours and centroid point to the same other class |
| Rare but valid | isolation, closeness to its own centroid, no dominant other label, acceptable quality; damped ×0.6 when the model is confident in another class |
| Poor quality | worst quality-finding severity |
| Domain shifted | isolation × how far capture attributes deviate from the class (robust z beyond 2) |
| Ambiguous | small top-1/top-2 probability gap, boosted when neighbours are split and training dynamics are ambiguous |

A hypothesis wins only if it scores ≥ 0.5 **and** leads the runner-up by ≥ 0.08. Otherwise the sample is *uncertain*. Rare-valid samples get a "protect from removal" recommendation, and What-if actions can preserve them explicitly.

## Shortcuts

`shortcut-detective-v1` looks for non-content cues that predict the label: border and background colour and brightness, greyscale, frames, overlay proxies, resolution, aspect ratio, file format and camera model. Association is not causation, so it only reports measurable proxies:

1. Bias-corrected **Cramér's V** between each cue and the label on the training split (moderate ≥ 0.3, strong ≥ 0.5). Per-cell **lift** and support are reported for findings with ≥ 8 samples, lift ≥ 1.8 and ≥ 50% of a class.
2. Whether the association also holds in evaluation. If it does, evaluation will not catch the shortcut.
3. **Cue-only predictability**: cross-validated balanced accuracy of a classifier that sees only the cues, compared with chance.
4. Deep profile only: baseline accuracy on **border-only** images (content masked) and **content-only** images (border masked), on up to 400 evaluation images.

## Coverage and the Active Collection Planner

`coverage-v1` builds a 2-D map with t-SNE (fixed seed) on up to 2,500 samples (fast) or 6,000 (deep). Remaining samples are placed at the similarity-weighted mean of their mapped neighbours, and PCA is the fallback. K-means regions on the embeddings give per-region class and split composition, purity and density.

The planner reports these gaps:

- under-represented classes (fewer than max(30, 50% of the median class size) images)
- weak class modes (regions holding < 8% of a class)
- boundary zones (region purity < 0.6)
- evaluation blind spots (regions with ≥ 15 training samples but no evaluation samples)
- sparse regions
- condition gaps, where a capture condition such as low light makes up ≥ 12% of other classes but ≤ 3% of this one

Each gap becomes a collection recommendation with a transparent priority score and a heuristic quantity range. It says what to *collect*. It never generates data.

## Privacy scan

`privacy-scan-v1` flags *potential* faces (OpenCV Haar frontal-face detector, minimum face size 6% of the shorter image side) and text-like regions (≥ 6 MSER blobs arranged like characters, no OCR). It detects content only and performs no recognition or identification. It can be switched off per workspace.

## Court cases and the jury

A case is opened for any sample with a quality, duplicate, leakage, label, rare, shortcut or privacy finding. `jury-v1` first converts witness evidence into **reason codes**, using default thresholds from `config.jury`:

| Code | Condition |
|---|---|
| `HIGH_MODEL_DISAGREEMENT` | model prefers another class and p(given) < 0.2 |
| `MODEL_DISAGREEMENT` | model prefers another class and p(given) < 0.4 |
| `MODEL_AGREES` | p(given) ≥ 0.6 |
| `HIGH_NEIGHBOR_DISAGREEMENT` | same-label neighbour share < 0.35 and one other label ≥ 50% (reliable witness only) |
| `NEIGHBORS_AGREE` | same-label neighbour share ≥ 0.6 (reliable witness only) |
| `OUTSIDE_CLASS_CLUSTER` / `INSIDE_CLASS_CLUSTER` | centroid ratio ≥ 0.55 / < 0.45 (reliable witness only) |
| `CONSISTENT_ALTERNATIVE_LABEL` | model, neighbour majority and nearest centroid all name the same other class |
| `LOW_DENSITY_REGION` | local density percentile ≤ 5% |
| `SEVERE_QUALITY_ISSUE` / `QUALITY_CONCERN` | worst quality finding is high / medium |
| `EXACT_DUPLICATE_COPY`, `NEAR_DUPLICATE_MEMBER`, `DUPLICATE_LABEL_CONFLICT` | family membership |
| `CROSS_SPLIT_LEAKAGE_HIGH`, `CROSS_SPLIT_LEAKAGE`, `SIMILAR_SUBJECT_ACROSS_SPLITS` | leakage risk high/critical, medium, low |
| `HARD_TO_LEARN`, `FORGOTTEN_DURING_TRAINING`, `AMBIGUOUS_TRAINING`, … | training dynamics category |
| `HIGH_SELF_INFLUENCE`, `HARMS_EVAL_FAILURES` | self-influence ≥ 97th percentile; harmful to ≥ 2 evaluation failures |
| `RARE_HYPOTHESIS`, `MISLABEL_HYPOTHESIS`, … | Rare-or-Wrong outcome |

The rules are then evaluated **in order**. The first match sets the verdict, and the full trace is stored.

| Rule | Condition | Verdict |
|---|---|---|
| R1 | high/critical or medium cross-split leakage | LEAKAGE_ACTION_NEEDED |
| R2 | exact duplicate copy without label conflict | POSSIBLE_REMOVE |
| R3 | duplicate label conflict | STRONG_REVIEW |
| R4 | severe quality issue and (model disagreement or hard to learn), unless the rare hypothesis holds | POSSIBLE_REMOVE |
| R5 | high model disagreement + high neighbour disagreement + outside class cluster + consistent alternative label, no severe quality issue | POSSIBLE_RELABEL (target = predicted class) |
| R6 | rare hypothesis without a consistent alternative label | LIKELY_RARE |
| R7 | at least two of: model disagreement, high neighbour disagreement, outside class cluster | STRONG_REVIEW |
| R8 | model disagrees but neighbours agree and the sample is inside its class cluster | UNCERTAIN |
| R9 | any single label, quality, leakage, influence, hypothesis or near-duplicate signal | REVIEW |
| R10 | otherwise | KEEP |

The case view separates evidence into **prosecution** (supports a problem), **defence** (supports keeping the sample as is, including the rare-valid hypothesis) and neutral context.

**Uncertainty** is:

- *low* for deterministic evidence (exact copies, exact leakage);
- *high* when prosecution and defence weights are both substantial and within 40% of each other;
- *medium* for UNCERTAIN / LIKELY_RARE verdicts, poorly calibrated models, or when dynamics and influence were not run;
- otherwise *low* or *medium*, depending on how one-sided the evidence is.

**Priority** combines three components:

`priority = 0.45 × severity(verdict) + 0.40 × strength + 0.15 × impact`

- **Severity** by verdict: LEAKAGE 0.9, RELABEL 0.85, STRONG 0.8, REMOVE 0.6, REVIEW 0.5, UNCERTAIN 0.45, RARE 0.35, KEEP 0.1.
- **Strength** is the maximum of the label suspicion, the leakage risk (low 0.3, medium 0.6, high 0.85, critical 1.0) and 0.8 × the worst quality severity.
- **Impact** is the maximum of the influence term (0.5 × self-influence percentile + 0.25 × harmed failures, up to 2), 0.6 × strength for evaluation samples, and 0.15 × family size for non-root family members.

No LLM takes part in verdicts. Explanations use deterministic templates, or optionally Gemini with the evidence as input. Any number in an LLM explanation that is not in the evidence is flagged as unverified.

## Human review

Reviewers record KEEP, RELABEL (with a target class), REMOVE, UNSURE or ESCALATE, with an optional note. A case is *decided* once all decisive reviews agree. When reviewers disagree, or anyone escalates, it becomes *disputed*, and an admin can adjudicate it to *resolved*. Export applies only the latest adjudication or a consensus. Undo is supported, and every decision, undo and adjudication is written to the evidence ledger. Agreement statistics report pairwise agreement and pooled Cohen's κ over cases with two or more reviewers.

## Review Budget

`review-budget-v1` chooses cases under a budget of items or minutes. Estimated minutes per case are 0.75, plus 0.5 for relabel, strong-review and uncertain verdicts, plus 0.2 per family member (up to 2) for duplicate and leakage cases. Each case gets a value under the chosen objective (balanced, label errors, leakage, model impact or quality):

`value = w_risk·priority + w_impact·impact + w_unc·u·0.3 + Σ category weights × strength`

- *u* is 0.2, 0.5 or 1.0 for low, medium or high uncertainty.
- The category terms add strength for leakage, label and quality cases, plus a small term for cases in the evaluation split.
- LIKELY_RARE cases are down-weighted ×0.6, because they mostly need confirmation rather than action.

Selection is greedy by marginal value per minute, with diminishing returns: a second case from the same duplicate family counts ×0.5, and each additional case from the same class counts ×0.85 (up to 10 cases). Greedy selection on a monotone submodular objective has the classical (1 − 1/e) guarantee.

**Coverage** is the share of total case value in the queue. It describes how the ranking spreads review effort. It does not promise any change to model accuracy.

## Blame Map and Failure Replay

For each evaluation error (up to 2,000), the Blame Map lists the training samples with the most negative TracIn influence and links them to open cases. **Failure Replay** reruns the What-if experiment for a set of proposed fixes and reports whether that specific failure flips. **Counterfactual estimates** (`counterfactual-proxy-v1`) rank alternative actions for a training sample, such as removing it or relabelling it to each other class. They use a first-order estimate of the change in total evaluation loss, computed from the TracIn basis without retraining: removing *i* gives Δ ≈ influence(i, y), and relabelling to *y′* gives Δ ≈ influence(i, y) − influence(i, y′). They rank options. A What-if run verifies them.

## What-if Lab

`what-if-v1` never mutates data. A change set combines these actions: remove samples, relabel samples, remove duplicate copies, exclude quality failures, rebalance, preserve rare examples, move leakage out of evaluation, apply review decisions, or apply unreviewed jury suggestions. It is resolved into training samples removed, samples relabelled and evaluation samples dropped. Both variants are trained with the converged baseline on the audit's frozen embeddings and evaluated on three sets:

- **original_eval**: the evaluation split as uploaded.
- **preserved_holdout**: evaluation samples no action touched and not involved in leakage. This is the fairest comparison.
- **cleaned_eval**: leaked copies removed and proposed relabels applied. It depends on those relabels being right.

The **point estimate** is the difference between the two converged fits. **Training noise** is estimated with a paired Poisson bootstrap: each original sample gets the same random weight in both variants, and a result is marked `within_noise` when |Δ| ≤ 2 × std(Δ). A **paired evaluation bootstrap** (1,000 resamples) gives a 95% CI for the macro-F1 difference on the preserved holdout. Results are labelled "Experimental result on this evaluation setup".

## Dataset Debt

`debt-v1` has eight dimensions, each with a published formula and thresholds for moderate, high and critical:

| Dimension | Formula | Moderate / high / critical |
|---|---|---|
| Label | (unresolved STRONG_REVIEW & POSSIBLE_RELABEL + 0.5 × unresolved REVIEW label cases) / samples | 1% / 3% / 8% |
| Leakage | evaluation samples in medium+ cross-split families / evaluation samples (HIGH floor if any exact evaluation duplicate is unresolved) | 0.5% / 2% / 5% |
| Quality | samples with a medium/high quality finding / samples | 2% / 5% / 12% |
| Coverage | 2 × high gaps + medium gaps + imbalance points (3 if ratio ≥ 10, 1 if ≥ 4) | 2 / 5 / 9 points |
| Shortcut | maximum Cramér's V between a cue and the label | 0.3 / 0.5 / 0.7 |
| Duplication | Σ(family size − 1) / samples | 2% / 5% / 15% |
| Review | unresolved high-priority + disputed cases / samples | 1% / 3% / 8% |
| Provenance | share of samples without source/license metadata (tracked, not scored) | 10% / 50% / 90% |

The overall level is CRITICAL if any scored dimension is critical; HIGH if any is high or at least three are moderate; MODERATE if any is moderate; otherwise LOW. Live debt reflects review progress, and snapshots are kept per version for trends.

## Preflight gate and data contracts

`preflight-v1` returns READY, READY_WITH_WARNINGS or BLOCKED.

It **blocks** when:

- more than 20% of files are unreadable;
- fewer than 2 classes exist;
- a class appears in evaluation but not in training;
- any exact duplicate crosses into evaluation (configurable);
- more than 10% of samples have unresolved high-priority cases.

It **warns** about:

- missing evaluation classes or no evaluation split;
- near-duplicate leakage;
- class imbalance above 10×;
- classes with fewer than 30 images;
- strong shortcuts;
- high-priority coverage gaps;
- open cases;
- provenance unknown for more than 50% of samples.

Admins can override the status with a written reason, and the override is recorded in the ledger.

`contract-v1` data contracts are lists of `{metric, op, value}` rules over 15 metrics, with operators `<=`, `<`, `>=`, `>`, `==` and `includes`. The default contract requires:

- no exact cross-split duplicates;
- no high leakage findings;
- at least 30 images per class;
- at most 20 unresolved strong-review cases;
- an imbalance ratio of at most 10;
- a `train` split.

CI reads the result from `GET /api/v1/ci/contract`. See [API.md](API.md).

## Versions, DNA and contamination

- **Version diff** (`version-diff-v1`) matches files by SHA-256. It reports added and removed files, label and split changes, leakage fixed or new, label cases resolved or new, duplicate families resolved, shortcut changes, and debt and metric trends.
- **Dataset DNA** (`dna-v1`) is a descriptive profile of class mix, capture conditions, resolution and format mix, embedding dispersion and per-class centroids, plus the issue profile. Drift between versions uses Jensen–Shannon divergence (bits) for distributions and 10 × (1 − cosine) for centroid shifts. A signal is moderate at ≥ 0.05 and strong at ≥ 0.15. DNA is not a cryptographic identity; the manifest SHA-256 identifies exact contents.
- **Contamination checks** (`contamination-v2`) compare a version with another version in the same workspace, such as your benchmark. They report exact overlap (identical SHA-256 or identical decoded pixels) plus near duplicates verified by kNN and detail NCC. The verdict is clean, minor overlap (≤ 1%) or contaminated.

## Evidence ledger

Audit completion, case verdicts, human decisions, undo and adjudication, preflight overrides, What-if runs, exports and reports are each appended to a per-workspace ledger. Every entry stores the SHA-256 of its canonical-JSON payload and `entry_hash = SHA-256(prev_hash ‖ event_type ‖ entity ‖ payload_sha256 ‖ created_at)`. A Postgres advisory lock serialises appends per workspace. `GET /orgs/{id}/ledger/verify` recomputes the chain and reports the first broken entry.

The ledger is tamper-*evident* for data in the database. It is not a signature by an external authority.

## Known limitations

- The default descriptor backend is weaker than self-supervised models on natural images. Label-witness reliability is measured and reported for that reason, and DINOv2 is recommended where it can run.
- Out-of-fold predictions come from a linear head on frozen embeddings, not from the model you will actually train.
- Influence ignores the frozen feature extractor.
- What-if results measure the baseline, not your production model.
- Crop detection is recall-limited for small crops and heavy re-compression.
- Similar-subject clusters cannot confirm identity.
- The privacy scan misses non-frontal faces and does not read text.
- Shortcut detection only covers the listed cues. Semantic shortcuts, such as objects that co-occur in the scene, are out of scope.
- Benchmark numbers come from synthetic data with known answers. See [EVALUATION.md](EVALUATION.md).
