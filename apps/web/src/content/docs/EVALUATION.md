# Evaluation

DataCourt ships its own benchmark so every detector can be scored against known answers. The results below come from one full run, committed as [`docs/benchmark/results.json`](benchmark/results.json). They are reported as produced, without selecting favourable seeds or metrics.

> **What this shows:** the methods behave as designed on controlled data with known problems.
> **What it does not show:** performance on your real dataset. Synthetic shapes are much easier to separate than natural images, and injected errors are cleaner than real annotation mistakes.

## Reproduce

```bash
cd services/api
pip install -e ".[dev]"
export DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/datacourt_bench
alembic upgrade head
datacourt-benchmark --profile deep --seed 7 --out benchmark-results
```

The run uses the real ingestion pipeline, job queue, worker and audit pipeline. Nothing is mocked.

## The benchmark: DataCourt Synthetic Shapes

`datacourt/benchmark/generator.py` renders a seeded five-class image dataset (cross, disc, square, star, triangle) with varied backgrounds, lighting, positions, scales and textures. It then injects known issues and records the ground truth:

| Injected issue | Count (seed 7) | Details |
|---|---|---|
| Label errors | 40 | train/test images saved under the wrong class folder |
| Exact duplicates | 20 | byte-identical copies inside train |
| Near duplicates | 42 | resized, cropped, flipped and brightness-changed copies inside train |
| Cross-split exact | 12 | test images byte-identical to train images |
| Cross-split variants | 10 | validation images that are transformed train images |
| Quality defects | 28 | heavy blur, tiny resolution, severe underexposure |
| Rare-but-valid | 8 | a valid but visually unusual sub-type of one class |
| Class imbalance | 1 class | one class has far fewer images |
| Shortcut | 1 | 85% of training "star" images on a blue border/background |
| Condition gap | 1 | "triangle" never appears in low light; other classes often do |

1,121 files in total; 1,119 samples after ingestion.

## Setup

- Profile: `deep` (5 folds, perturbation tests, influence).
- Embedding backend: `dc-descriptor-v1`, the CPU default. DINOv2 was unavailable in the build environment.
- Baseline macro-F1 on the uploaded evaluation split: 0.775.
- Pipeline time: 47.8 s; end-to-end wall-clock including generation and ingestion: 62.6 s. Measured on a shared cloud container (CPU only).

## Results

### Duplicate detection

| Metric | Value |
|---|---|
| Pair precision | **1.000** (82 / 82) |
| Pair recall | **0.976** (82 / 84) |
| Recall by injected relation | exact 1.0 · resized 1.0 · flipped 1.0 · photometric 1.0 · **cropped 0.8** |
| Edge precision by predicted relation | exact, resized, photometric, flipped and possible crop all 1.0 |

The two missed pairs are crops, the hardest relation. Crop verification needs enough keypoints in the overlap.

### Leakage detection

| Metric | Value |
|---|---|
| Cross-split pair precision / recall | **1.000 / 1.000** (22 / 22) |
| Leaked evaluation samples, precision / recall | **1.000 / 1.000** (22 / 22) |

### Label-error triage

Ranking all samples by the label suspicion score:

| Metric | Value |
|---|---|
| Average precision | **0.744** (random ranking: 0.036) |
| Precision@40 / Recall@40 | 0.70 / 0.70 |
| Precision@80 / Recall@80 | 0.45 / 0.90 |
| POSSIBLE_RELABEL verdicts | 20, of which 14 were real errors (precision 0.70). The suggested target class was correct for 13. |

How the 40 label errors were judged:

| Verdict | Label errors |
|---|---|
| POSSIBLE_RELABEL | 14 |
| STRONG_REVIEW | 15 |
| REVIEW | 6 |
| LIKELY_RARE | **5** |

Every label error received a case. However, 5 of 40 (12.5%) were classed as *likely rare*, which lowers their review priority. This is the cost of protecting rare examples (next section).

### Rare or wrong

| Metric | Value |
|---|---|
| Rare-but-valid samples | 8 |
| **False removal recommendation rate** | **0.0** (no rare-valid sample received POSSIBLE_REMOVE or POSSIBLE_RELABEL) |
| Verdicts for rare-valid samples | 6 without a case, 1 REVIEW, 1 LIKELY_RARE |

### Review effort (time saved)

How many reviews it takes to find a given share of the injected label errors, reviewing in DataCourt's order versus a random order (expected value):

| Target recall | DataCourt order | Random order | Reduction |
|---|---|---|---|
| 50% | 26 reviews | ≈ 560 | **95.4%** |
| 80% | 55 reviews | ≈ 895 | **93.9%** |

### Review Budget coverage

There are 152 ground-truth issues: label errors, quality defects and the redundant copy of every duplicate pair. 62 of them count as high-impact: all 40 label errors plus the 22 evaluation images leaked from training.

| Budget | Objective | Precision | Issue coverage | High-impact coverage | Est. minutes |
|---|---|---|---|---|---|
| 25 | balanced | 0.48 | 0.079 | 0.129 | 21.5 |
| 25 | label errors | 0.52 | 0.086 | 0.097 | 20.1 |
| 25 | leakage | 0.56 | 0.092 | 0.226 | 27.9 |
| 50 | balanced | 0.48 | 0.158 | 0.258 | 44.6 |
| 100 | balanced | 0.41 | 0.270 | 0.532 | 91.6 |
| 100 | leakage | 0.41 | 0.270 | 0.532 | 94.4 |

Budget queues spread effort across families and classes by design (diminishing returns). As a result, their precision against *label errors only* is lower than the pure suspicion ranking above.

### Quality detection

| Metric | Value |
|---|---|
| Recall of injected defects | **0.964** (27 / 28): blur 0.83, low resolution 1.0, underexposure 1.0 |
| Precision | **0.45** (27 / 60) |

The 33 "false positives" are mostly potential underexposure (28). The generator deliberately renders dark low-light images as valid data, and the quality rules correctly describe them as dark. Precision counts them as errors anyway. This is also why quality findings are only "potential" issues and never trigger removal on their own.

### Shortcut detection

The injected "star on blue border" shortcut was detected: bias-corrected Cramér's V = 0.32 (moderate), with no other shortcut findings.

### Coverage

- The injected "triangle × low light" condition gap was detected (medium priority).
- The class imbalance was detected.
- 16 gaps were reported in total. The others are boundary zones and weak modes, and are not scored against ground truth.

### Model metrics before and after guided cleanup (What-if Lab)

Three experiments on the same audit, compared on the **preserved holdout** (296–300 evaluation samples untouched by any action):

| Experiment | Changes | Macro-F1 before → after | Δ | Training-bootstrap std | Within noise | Paired eval 95% CI |
|---|---|---|---|---|---|---|
| Guided review of the top 80 (a simulated reviewer fixes only the real errors), plus duplicate copies removed and leakage moved out of evaluation | 36 relabelled, 73 removed, 22 dropped from eval | 0.781 → 0.812 | **+0.031** | 0.030 | yes | [+0.002, +0.061] |
| Unreviewed jury suggestions applied blindly, with rare examples protected | 20 relabelled, 27 removed, 22 dropped from eval | 0.780 → 0.777 | −0.004 | 0.024 | yes | [−0.035, +0.030] |
| Random removal control (36 random training samples) | 36 removed | 0.771 → 0.765 | −0.006 | 0.017 | yes | [−0.019, +0.006] |

Reading these honestly:

- Human-verified fixes gave the largest gain. The paired evaluation CI excludes zero, but the change is still **within training-data noise** (|Δ| ≤ 2 std). On this benchmark it is suggestive, not conclusive.
- Applying the jury's suggestions **without** human review did not help. This supports the design rule that suggestions go to reviewers and are not applied automatically.
- Random removal did not help, as expected.

## False-positive summary

| Area | False-positive measure |
|---|---|
| Duplicates | 0 false pairs out of 82 predicted |
| Leakage | 0 false pairs out of 22 predicted |
| Relabel suggestions | 6 of 20 POSSIBLE_RELABEL were not label errors |
| Removal of rare-valid data | 0 of 8 |
| Quality | 33 of 60 medium/high findings outside the injected set (see above) |

## Limitations

- **Synthetic data.** Procedural shapes are easier than natural images, and injected label errors are uniform random swaps. Real errors cluster on ambiguous classes.
- **One seed, one run.** The numbers are for seed 7. The Rare-or-Wrong result is based on only 8 rare samples. Run other seeds with `--seed`.
- **Descriptor backend.** Results use `dc-descriptor-v1`. With DINOv2, witness reliability and label triage are expected to change, but that has not been measured here.
- **Baseline, not your model.** What-if deltas are for the linear baseline on frozen embeddings.
- **Review simulation.** The guided experiment assumes a reviewer who is always right about the samples they see.
- **Unscored outputs.** Influence, the Blame Map and Dataset DNA drift have no ground truth in this benchmark.
