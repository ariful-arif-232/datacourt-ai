import io

import cv2
import numpy as np
import pytest
from PIL import Image

from datacourt.enums import LeakageKind, Relation, Risk, Verdict
from datacourt.ingest import images
from datacourt.ml import duplicates as dup
from datacourt.ml import governance as gov
from datacourt.ml import jury
from datacourt.ml import leakage as lk
from datacourt.ml.baseline import TrainConfig, out_of_fold_logits, softmax, train_softmax
from datacourt.ml.embeddings import DescriptorBackend
from datacourt.ml.influence import tracin
from datacourt.pipeline.config import DEFAULT_CONFIG, ConfigError, backend_thresholds, resolve_config


def _img(seed: int, size: int = 128) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.normal(128, 50, (size, size, 3)).astype(np.float32), (0, 0), 2)
    cv2.circle(base, (size // 2 + seed % 7, size // 2), size // 4, (20.0, 60.0, 200.0), -1)
    return np.clip(base, 0, 255).astype(np.uint8)


def _jpeg(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def test_inspect_image_and_hashes_deterministic():
    data = _jpeg(_img(1))
    a, b = images.inspect_image(data, "x.jpg"), images.inspect_image(data, "x.jpg")
    assert a.sha256 == b.sha256 and a.phash == b.phash and a.width == 128
    flipped = images.inspect_image(_jpeg(_img(1)[:, ::-1].copy()), "f.jpg")
    assert images.hamming_hex(a.phash, flipped.phash_flip) <= 6


def test_corrupt_and_mismatched_images():
    with pytest.raises(images.InvalidImage):
        images.inspect_image(b"\xff\xd8\xff\xe0 garbage", "bad.jpg")
    buf = io.BytesIO()
    Image.fromarray(_img(2)).save(buf, format="PNG")
    info = images.inspect_image(buf.getvalue(), "actually_png.jpg")
    assert info.format == "PNG" and info.attributes["extension_mismatch"] is True


def _dup_inputs(imgs: list[np.ndarray]):
    datas = [_jpeg(x) for x in imgs]
    infos = [images.inspect_image(d, f"{i}.jpg") for i, d in enumerate(datas)]
    rgbs = [images.decode_rgb(d) for d in datas]
    emb = DescriptorBackend().embed(rgbs)
    struct = np.stack([dup.structure_thumb(cv2.cvtColor(r, cv2.COLOR_RGB2GRAY)) for r in rgbs])
    from datacourt.ml.knn import knn

    sims, idx = knn(emb, emb, 4, exclude_self=True)
    return dict(
        sha=[i.sha256 for i in infos],
        ph=images.hex_to_uint64([i.phash for i in infos]),
        ph_flip=images.hex_to_uint64([i.phash_flip for i in infos]),
        width=np.array([i.width for i in infos]),
        height=np.array([i.height for i in infos]),
        brightness=np.array([float(r.mean()) for r in rgbs]),
        saturation=np.array([float(cv2.cvtColor(r, cv2.COLOR_RGB2HSV)[..., 1].mean()) for r in rgbs]),
        emb=emb,
        struct=struct,
        knn_sims=sims,
        knn_idx=idx,
        cfg=backend_thresholds(DEFAULT_CONFIG, "dc-descriptor"),
    )


def test_duplicate_families_detect_variants_not_strangers():
    a = _img(10, 160)
    variants = [
        a,
        cv2.resize(a, (100, 100), interpolation=cv2.INTER_AREA),
        a[:, ::-1].copy(),
        np.clip(a.astype(np.float32) * 1.3 + 15, 0, 255).astype(np.uint8),
    ]
    strangers = [_img(s, 160) for s in (20, 30, 40)]
    edges, fams = dup.find_duplicates(**_dup_inputs(variants + strangers))
    assert len(fams) == 1
    assert sorted(fams[0].members) == [0, 1, 2, 3]
    rels = {e.relation for e in edges}
    assert Relation.RESIZED in rels and Relation.FLIPPED in rels and Relation.PHOTOMETRIC in rels
    assert fams[0].root == 0  # highest resolution is the source-like member


def test_exact_duplicates_and_cross_split_leakage():
    a, b = _img(1, 140), _img(2, 140)
    ins = _dup_inputs([a, a, b, cv2.resize(b, (90, 90), interpolation=cv2.INTER_AREA)])
    edges, fams = dup.find_duplicates(**ins)
    assert {e.relation for e in edges} == {Relation.EXACT, Relation.RESIZED}
    leaks = lk.family_leaks(fams, ["train", "test", "train", "val"], ["x", "x", "y", "y"])
    kinds = {x.kind: x.risk for x in leaks}
    assert kinds[LeakageKind.EXACT_CROSS_SPLIT] == Risk.CRITICAL
    assert kinds[LeakageKind.TRANSFORMED_CROSS_SPLIT] == Risk.MEDIUM


def test_config_validation_and_hash_stability():
    cfg, h = resolve_config({"quality": {"blur_abs": 99}})
    assert cfg["quality"]["blur_abs"] == 99 and resolve_config({"quality": {"blur_abs": 99}})[1] == h
    with pytest.raises(ConfigError):
        resolve_config({"quality": {"nonexistent": 1}})
    with pytest.raises(ConfigError):
        resolve_config({"quality": {"blur_abs": "high"}})


def test_trainer_is_deterministic_and_oof_is_out_of_sample():
    rng = np.random.default_rng(0)
    X = np.concatenate([rng.normal(0, 1, (60, 8)), rng.normal(2.5, 1, (60, 8))])
    y = np.array([0] * 60 + [1] * 60)
    cfg = TrainConfig(epochs=8, seed=3)
    a, b = train_softmax(X, y, 2, cfg), train_softmax(X, y, 2, cfg)
    assert np.allclose(a.W, b.W)
    logits, folds = out_of_fold_logits(X, y, 2, cfg, 3)
    assert (folds >= 0).all() and (softmax(logits).argmax(1) == y).mean() > 0.85


def test_tracin_self_influence_flags_flipped_label():
    rng = np.random.default_rng(1)
    X = np.concatenate([rng.normal(0, 0.5, (50, 4)), rng.normal(3, 0.5, (50, 4))])
    y = np.array([0] * 50 + [1] * 50)
    y[5] = 1  # label error deep inside class 0
    res = train_softmax(X, y, 2, TrainConfig(epochs=10, seed=2), record_checkpoints=True)
    self_inf, _ = tracin(res, X, y, X[:2], y[:2])
    assert int(np.argmax(self_inf)) == 5


def _ci(**kw) -> jury.CaseInputs:
    base = dict(
        label="cat", split="train", k=10, reliability={"neighbor": 0.8, "centroid": 0.8, "model": 0.8}
    )
    base.update(kw)
    return jury.CaseInputs(**base)


JCFG = DEFAULT_CONFIG["jury"]


def test_jury_consistent_mislabel_suggests_relabel():
    ci = _ci(
        p_given=0.05,
        pred_label="dog",
        p_pred=0.9,
        neighbor_same=0.1,
        neighbor_major_label="dog",
        neighbor_major_share=0.9,
        centroid_ratio=0.7,
        nearest_other_label="dog",
        density_pct=0.5,
        suspicion=0.8,
    )
    d = jury.deliberate(ci, JCFG, {"model", "dynamics", "influence"})
    assert d.verdict == Verdict.POSSIBLE_RELABEL and d.target_label == "dog"
    assert "CONSISTENT_ALTERNATIVE_LABEL" in d.reason_codes
    assert any(t["matched"] for t in d.rule_trace)


def test_jury_leakage_and_redundant_copy_and_rare():
    leak = jury.deliberate(
        _ci(leakage={"risk": "critical", "kind": "exact_cross_split", "splits": ["test", "train"]}),
        JCFG,
        set(),
    )
    assert leak.verdict == Verdict.LEAKAGE_ACTION_NEEDED and str(leak.uncertainty) == "low"
    copy = jury.deliberate(
        _ci(
            family={
                "number": 1,
                "size": 2,
                "kind": "exact",
                "is_root": False,
                "relation": "exact",
                "label_conflict": False,
                "splits": ["train"],
                "labels": ["cat"],
            }
        ),
        JCFG,
        set(),
    )
    assert copy.verdict == Verdict.POSSIBLE_REMOVE
    rare = jury.deliberate(
        _ci(
            p_given=0.3,
            pred_label="dog",
            p_pred=0.45,
            neighbor_same=0.5,
            neighbor_major_share=0.2,
            centroid_ratio=0.4,
            density_pct=0.01,
            hypothesis="rare_valid",
        ),
        JCFG,
        set(),
    )
    assert rare.verdict == Verdict.LIKELY_RARE


def test_jury_ignores_unreliable_neighbor_witness():
    ci = _ci(
        p_given=0.5,
        pred_label="cat",
        p_pred=0.5,
        neighbor_same=0.05,
        neighbor_major_label="dog",
        neighbor_major_share=0.95,
        centroid_ratio=0.3,
        reliability={"neighbor": 0.1, "centroid": 0.8, "model": 0.8},
    )
    d = jury.deliberate(ci, JCFG, set())
    assert "NEIGHBOR_WITNESS_WEAK" in d.reason_codes and "HIGH_NEIGHBOR_DISAGREEMENT" not in d.reason_codes


def _facts(**kw) -> dict:
    f = dict(
        sample_count=1000,
        unresolved_label_strong=0,
        unresolved_label_review=0,
        eval_count=200,
        eval_samples_leaking=0,
        exact_eval_leaks=0,
        leakage_by_risk={},
        quality_samples_medium_plus=0,
        quality_by_type={},
        gaps_high=0,
        gaps_medium=0,
        imbalance_ratio=1.5,
        shortcut_max_strength=0.1,
        shortcut_strong=0,
        cue_only_accuracy=None,
        cue_chance=None,
        redundant_samples=0,
        duplicate_families=0,
        exact_families=0,
        unresolved_high_priority=0,
        disputed_cases=0,
        decided_cases=0,
        provenance_coverage=1.0,
        unreadable_fraction=0.0,
        class_count=3,
        min_class_count=300,
        classes_missing_from_train=[],
        classes_missing_from_eval=[],
        unresolved_cases=0,
        leakage_high_count=0,
        quality_rate=0.0,
        duplicate_rate=0.0,
        splits_present=["test", "train"],
    )
    f.update(kw)
    return f


def test_debt_formulas_and_preflight_rules():
    clean = gov.compute_debt(_facts())
    assert clean["overall"] == "LOW"
    dirty = gov.compute_debt(_facts(exact_eval_leaks=2, eval_samples_leaking=2))
    assert dirty["dimensions"]["leakage"]["level"] in ("HIGH", "CRITICAL") and dirty["overall"] in (
        "HIGH",
        "CRITICAL",
    )
    assert all("formula" in d for d in dirty["dimensions"].values())
    cfg = DEFAULT_CONFIG["preflight"]
    assert gov.preflight(_facts(), cfg)["status"] == "READY"
    assert gov.preflight(_facts(exact_eval_leaks=1, eval_samples_leaking=1), cfg)["status"] == "BLOCKED"
    assert gov.preflight(_facts(imbalance_ratio=25), cfg)["status"] == "READY_WITH_WARNINGS"


def test_contract_evaluation():
    facts = _facts(exact_eval_leaks=1, min_class_count=12)
    out = gov.evaluate_contract(gov.DEFAULT_CONTRACT, facts)
    failed = {r["metric"] for r in out["results"] if not r["passed"]}
    assert not out["passed"] and {"exact_cross_split_duplicates", "min_images_per_class"} <= failed
    assert gov.validate_rules([{"metric": "nope", "op": "<=", "value": 1}])
