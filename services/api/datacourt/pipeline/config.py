"""Versioned audit configuration. Every threshold used by the pipeline lives here.

A run stores the fully-resolved config and its SHA-256, so any finding can be traced
back to the exact thresholds that produced it. Projects may override values; overrides
are deep-merged and validated against the default schema (unknown keys are rejected).
"""

from __future__ import annotations

import copy
from typing import Any

from datacourt import algorithms
from datacourt.security import sha256_json

CONFIG_VERSION = algorithms.AUDIT_CONFIG

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "seed": 1234,
    "profiling": {"decode_max_side": 512, "workers": 4},
    "quality": {
        "min_resolution_abs": 32,
        "min_resolution_rel": 0.25,
        "blur_abs": 150.0,
        "blur_rel_z": -3.0,
        "dark_abs": 25.0,
        "bright_abs": 235.0,
        "exposure_rel_z": 2.5,
        "low_contrast_abs": 8.0,
        "contrast_rel_z": 2.5,
        "low_info_entropy": 2.0,
        "low_info_edge_density": 0.002,
        "near_empty_contrast_abs": 3.0,
        "aspect_rel_z": 4.0,
    },
    "privacy": {"enabled": True, "face_min_size_frac": 0.06, "text_min_regions": 6},
    "embedding": {"batch_size": 64},
    "knn": {"k": 10},
    "duplicates": {
        # cosine thresholds per embedding backend
        "by_backend": {
            "dc-descriptor": {
                "candidate_min_cosine": 0.9,
                "crop_min_cosine": 0.93,
                "near_duplicate_cosine": 0.97,
                "similar_subject_cosine": 0.97,
            },
            "dinov2-small": {
                "candidate_min_cosine": 0.85,
                "crop_min_cosine": 0.88,
                "near_duplicate_cosine": 0.95,
                "similar_subject_cosine": 0.9,
            },
        },
        "phash_candidate_max": 12,
        "struct_min_detail_ncc": 0.92,
        "crop_min_inliers": 12,
        "crop_min_inlier_ratio": 0.6,
        "crop_min_overlap_ncc": 0.8,
        "crop_min_overlap_fraction": 0.4,
        "photometric_min_delta": 12.0,
        "phash_bruteforce_max_n": 60000,
    },
    "leakage": {"max_similar_subject_clusters": 50, "similar_subject_quantile": 0.995},
    "baseline": {
        "C": 0.001,
        "folds_fast": 3,
        "folds_deep": 5,
        "epochs": 20,
        "batch_size": 64,
        "lr": 0.05,
        "momentum": 0.9,
        "weight_decay": 0.0001,
        "class_balanced": True,
        "checkpoint_every": 2,
    },
    "dynamics": {
        "hard_confidence": 0.3,
        "hard_correctness": 0.3,
        "forgotten_min_events": 2,
        "ambiguous_variability": 0.2,
        "easy_confidence": 0.6,
        "easy_correctness": 0.8,
    },
    "influence": {"top_k_links": 8, "max_failures": 2000},
    "labels": {
        "weights": {
            "model": 0.35,
            "neighbor": 0.3,
            "centroid": 0.15,
            "dynamics": 0.1,
            "duplicate_conflict": 0.1,
        },
        "review_threshold": 0.55,
        "low_priority_threshold": 0.38,
        "rare_model_disagreement_min": 0.5,
        "rare_max_other_neighbor_share": 0.5,
        "rare_max_density_pct": 0.08,
        "rare_max_centroid_ratio": 0.55,
        "store_min_suspicion": 0.2,
        "hypothesis_min_score": 0.5,
        "hypothesis_min_gap": 0.08,
    },
    "shortcuts": {
        "moderate_cramers_v": 0.3,
        "strong_cramers_v": 0.5,
        "min_support": 8,
        "min_lift": 1.8,
        "min_class_share": 0.5,
        "perturbation_max_samples": 400,
    },
    "coverage": {
        "tsne_max_fast": 2500,
        "tsne_max_deep": 6000,
        "density_k": 5,
        "sparse_cluster_quantile": 0.2,
        "sparse_min_fraction": 0.01,
        "min_per_class": 30,
        "underrepresented_rel": 0.5,
        "weak_mode_share": 0.08,
        "boundary_max_purity": 0.6,
        "blind_spot_min_train": 15,
        "condition_other_min": 0.12,
        "condition_class_max": 0.03,
        "max_gaps": 40,
    },
    "jury": {
        "model_high_disagreement": 0.2,
        "model_disagreement": 0.4,
        "model_agreement": 0.6,
        "uncalibrated_ece": 0.12,
        "neighbor_disagreement": 0.35,
        "neighbor_agreement": 0.6,
        "outside_cluster_ratio": 0.55,
        "inside_cluster_ratio": 0.45,
        "low_density_pct": 0.05,
        "high_self_influence_pct": 0.97,
        "harms_failures_min": 2,
        "min_witness_reliability": 0.35,
        "max_cases": 5000,
    },
    "debt": {"formula_version": "debt-v1"},
    "preflight": {
        "rules_version": "preflight-v1",
        "block_on_exact_eval_leakage": True,
        "block_max_unreadable_fraction": 0.2,
        "block_min_classes": 2,
        "block_unresolved_critical_fraction": 0.1,
        "warn_imbalance_ratio": 10.0,
        "warn_min_per_class": 30,
        "warn_unresolved_review": 1,
        "warn_provenance_unknown_fraction": 0.5,
    },
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, override: dict, path: str = "") -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        where = f"{path}.{k}" if path else k
        if k not in base:
            raise ConfigError(f"unknown config key: {where}")
        if isinstance(base[k], dict):
            if not isinstance(v, dict):
                raise ConfigError(f"config key {where} must be an object")
            out[k] = _merge(base[k], v, where)
        else:
            if isinstance(base[k], bool) and not isinstance(v, bool):
                raise ConfigError(f"config key {where} must be a boolean")
            if (
                isinstance(base[k], (int, float))
                and not isinstance(base[k], bool)
                and not isinstance(v, (int, float))
            ):
                raise ConfigError(f"config key {where} must be a number")
            out[k] = v
    return out


def resolve_config(overrides: dict | None = None) -> tuple[dict, str]:
    cfg = _merge(DEFAULT_CONFIG, overrides or {})
    cfg["config_version"] = CONFIG_VERSION
    return cfg, sha256_json(cfg)


def backend_thresholds(cfg: dict, backend_name: str) -> dict:
    d = cfg["duplicates"]
    per = d["by_backend"].get(backend_name) or d["by_backend"]["dc-descriptor"]
    return {**{k: v for k, v in d.items() if k != "by_backend"}, **per}
