# Novelty and positioning

## The problem

Teams training image classifiers know their data has problems: mislabels, duplicates, leakage between splits, blurry or corrupt images, spurious cues, missing conditions. Finding *candidates* is increasingly easy. The hard parts come after:

1. **Is this finding real?** Model disagreement, visual similarity and outlier scores all produce false positives.
2. **Does it matter?** A mislabel in the test set distorts every reported metric. One in a well-covered training class may barely matter.
3. **What should a person look at first?** Review time is the scarce resource, not compute.
4. **Did fixing it help?** Cleaning data without measuring the effect is guesswork, and it can hurt, for example by deleting rare but valid examples.
5. **Can we show what we did?** Datasets change across versions and teams need an auditable record.

Most tools answer *"what looks wrong?"*. DataCourt is built to answer *"what looks wrong, why does it matter, what should a human do first, and can we show that fixing it helps?"*

## Known categories of dataset-quality tools

These are categories, not endorsements or comparisons of specific products:

| Category | Typical capability | Typical gap DataCourt targets |
|---|---|---|
| Label-error detection libraries (e.g. confident-learning methods) | rank likely mislabels from model predictions | one signal, no competing hypotheses, no review workflow or verification |
| Dataset exploration and curation apps | visualise embeddings, browse samples, find duplicates | exploration-first; decisions and their impact are left to the user |
| Image-quality linters | blur, exposure, duplicate and odd-size checks | per-image flags without model impact or prioritisation |
| Annotation and active-learning platforms | labeling workflows, choose samples to label next | optimised for labeling throughput, not for evidence about existing labels |
| Data validation and observability tools | schema, statistics and drift checks, often tabular | little image-specific forensics (leakage families, visual shortcuts) |
| Data-attribution research (influence functions, TracIn, datamodels) | estimate which training data drives predictions | research code, rarely connected to a review and export workflow |

## What DataCourt does differently

The individual techniques are established, and DataCourt uses published methods where they exist: perceptual hashing, kNN, logistic probes, confident-learning-style disagreement, dataset cartography, TracIn, Cramér's V, t-SNE and bootstrap confidence intervals. The differentiation is the **integrated evidence-to-decision system** built around them:

1. **Evidence-based sample trials.** Every suspicious sample becomes a *case* with typed evidence (measured, heuristic, model prediction, model estimate, LLM, human), not a score in a table.
2. **Prosecution vs defence.** Evidence *for keeping* a sample, including the rare-but-valid hypothesis, is gathered as deliberately as evidence against it.
3. **A deterministic jury.** Ordered, versioned rules turn evidence into verdicts, with reason codes and a full rule trace. No LLM decides anything, and witnesses are weighted by their measured reliability on *this* dataset.
4. **Rare-vs-wrong preservation.** Competing hypotheses (mislabeled, rare valid, poor quality, domain shifted, ambiguous) protect unusual but valid data from deletion. On the benchmark, no rare-valid sample received a removal or relabel recommendation.
5. **A human Review Budget.** Given minutes or items, the queue maximises expected value per minute, with diminishing returns per duplicate family and class.
6. **Model-to-Data Blame Map.** Evaluation failures are traced to the training samples that most increased their loss (TracIn on the baseline head) and linked to open cases.
7. **Failure Replay.** Re-run a specific failure after proposed fixes to see whether it flips.
8. **What-if experimental verification.** Proposed changes are tested before export on a *preserved holdout*, with training-noise and evaluation bootstraps. The product says when an effect is within noise.
9. **Dataset Debt across versions.** Eight debt dimensions with published formulas, snapshotted per version and reduced by review progress.
10. **Active Data Collection Planner.** Coverage gaps become concrete *collection* requests, not only deletions.
11. **Dataset DNA and drift.** A versioned descriptive fingerprint and drift signals between versions.
12. **Data-contract CI and preflight gate.** Machine-readable contracts and a READY / WARN / BLOCKED gate that pipelines can enforce.
13. **Evidence ledger.** A hash-chained record of audits, verdicts, decisions, overrides, experiments and exports.

## Foundational vs differentiating

| Foundational (expected of any serious tool) | Differentiating workflow |
|---|---|
| Hash and embedding duplicate detection | Duplicate *families* with lineage trees, driving leakage severity and case evidence |
| Label-error ranking from model disagreement | Reliability-weighted witnesses, Rare-or-Wrong hypotheses and jury verdicts with traces |
| Quality checks | Quality as evidence in cases, never an automatic deletion |
| Embedding maps | Coverage gaps turned into a collection plan |
| Train/test overlap checks | Leakage-aware *preserved holdout* for fair before/after measurement |
| Review UI | Budget-optimised queues, consensus and adjudication, clean export from human decisions only |
| Reports | Ledger-backed audit report with algorithm versions, config hash and limitations |

## Claims we do not make

- We do not claim DataCourt is the first or only tool to combine these ideas. We have not independently verified that.
- Benchmark results come from synthetic data (see [EVALUATION.md](EVALUATION.md)), and gains on real datasets will vary.
- Influence estimates, What-if effects and hypothesis scores are **estimates** and are labelled as such.
- Reports are technical audit aids, not compliance certifications.

## References

- Northcutt, Jiang & Chuang (2021). *Confident Learning: Estimating Uncertainty in Dataset Labels.* JAIR.
- Swayamdipta et al. (2020). *Dataset Cartography: Mapping and Diagnosing Datasets with Training Dynamics.* EMNLP.
- Toneva et al. (2019). *An Empirical Study of Example Forgetting during Deep Neural Network Learning.* ICLR.
- Pruthi et al. (2020). *Estimating Training Data Influence by Tracing Gradient Descent (TracIn).* NeurIPS.
- Koh & Liang (2017). *Understanding Black-box Predictions via Influence Functions.* ICML.
- Oquab et al. (2023). *DINOv2: Learning Robust Visual Features without Supervision.*
- Guo et al. (2017). *On Calibration of Modern Neural Networks.* ICML (temperature scaling).
- Bergsma (2013). *A bias-correction for Cramér's V and Tschuprow's T.* J. Korean Stat. Soc.
- Nemhauser, Wolsey & Fisher (1978). *An analysis of approximations for maximizing submodular set functions.*
