"""Visual quality rules (rule set `quality-rules-v1`).

Each finding stores the measured value, the threshold, the comparator and the rule
version. Language is "potential quality issue" unless the check is deterministic
(e.g. extension/format mismatch). No single metric is treated as proof of a bad image;
severity escalates only when absolute and dataset-relative evidence agree.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from datacourt import algorithms
from datacourt.enums import Severity
from datacourt.ml.attributes import robust_z

RULE_VERSION = algorithms.QUALITY_RULES


@dataclass
class Finding:
    sample_index: int
    finding_type: str
    severity: Severity
    measured_value: float | None
    threshold: float | None
    comparator: str
    deterministic: bool
    description: str


def evaluate_quality(
    attrs: list[dict],
    image_meta: list[dict],
    cfg: dict,
) -> list[Finding]:
    """attrs: per-sample attribute dicts; image_meta: {width,height,mode,attributes} per sample."""
    n = len(attrs)
    if n == 0:
        return []
    q = cfg
    findings: list[Finding] = []

    def arr(key: str) -> np.ndarray:
        return np.array([float(a[key]) for a in attrs], dtype=np.float64)

    blur = arr("blur_laplacian_var")
    sharp = np.array(
        [
            float(a.get("sharpness_norm", a["blur_laplacian_var"] / (float(a["contrast"]) ** 2 + 1)))
            for a in attrs
        ]
    )
    blur_z = robust_z(np.log(sharp + 1e-6))
    brightness = arr("brightness")
    contrast = arr("contrast")
    contrast_z = robust_z(contrast)
    bright_z = robust_z(brightness)
    entropy = arr("entropy_bits")
    edge = arr("edge_density")
    min_side = arr("min_side")
    aspect = arr("aspect_ratio")
    log_aspect_z = robust_z(np.log(np.clip(aspect, 1e-3, None)))
    med_side = float(np.median(min_side))

    for i in range(n):
        # Resolution
        if min_side[i] < q["min_resolution_abs"]:
            findings.append(
                Finding(
                    i,
                    "low_resolution",
                    Severity.HIGH,
                    min_side[i],
                    q["min_resolution_abs"],
                    "<",
                    True,
                    f"Shortest side is {int(min_side[i])}px, below the absolute minimum of {q['min_resolution_abs']}px.",
                )
            )
        elif min_side[i] < q["min_resolution_rel"] * med_side:
            thr = q["min_resolution_rel"] * med_side
            findings.append(
                Finding(
                    i,
                    "low_resolution",
                    Severity.MEDIUM,
                    min_side[i],
                    round(thr, 1),
                    "<",
                    True,
                    f"Shortest side is {int(min_side[i])}px, less than {q['min_resolution_rel']:.0%} of the dataset median ({int(med_side)}px).",
                )
            )
        # Blur: contrast-normalized sharpness must be a strong dataset outlier AND raw sharpness low.
        if blur[i] < q["blur_abs"] and blur_z[i] < q["blur_rel_z"]:
            sev = Severity.HIGH if blur_z[i] < q["blur_rel_z"] * 1.5 else Severity.MEDIUM
            findings.append(
                Finding(
                    i,
                    "potential_blur",
                    sev,
                    round(blur[i], 2),
                    q["blur_abs"],
                    "<",
                    False,
                    f"Laplacian variance {blur[i]:.1f} (< {q['blur_abs']}) and contrast-normalized sharpness "
                    f"{abs(blur_z[i]):.1f} robust SDs below the dataset (sharpness proxy).",
                )
            )
        # Exposure: absolute threshold AND dataset-relative outlier.
        if brightness[i] < q["dark_abs"] and bright_z[i] < -q["exposure_rel_z"]:
            sev = Severity.HIGH if brightness[i] < q["dark_abs"] / 2 else Severity.MEDIUM
            findings.append(
                Finding(
                    i,
                    "potential_underexposure",
                    sev,
                    round(brightness[i], 2),
                    q["dark_abs"],
                    "<",
                    False,
                    f"Mean luminance {brightness[i]:.1f}/255 is very dark and {abs(bright_z[i]):.1f} robust SDs below the dataset.",
                )
            )
        elif brightness[i] > q["bright_abs"] and bright_z[i] > q["exposure_rel_z"]:
            sev = Severity.HIGH if brightness[i] > (255 + q["bright_abs"]) / 2 else Severity.MEDIUM
            findings.append(
                Finding(
                    i,
                    "potential_overexposure",
                    sev,
                    round(brightness[i], 2),
                    q["bright_abs"],
                    ">",
                    False,
                    f"Mean luminance {brightness[i]:.1f}/255 suggests a washed-out or overexposed image.",
                )
            )
        if contrast[i] < q["low_contrast_abs"] and contrast_z[i] < -q["contrast_rel_z"]:
            sev = Severity.HIGH if contrast[i] < q["low_contrast_abs"] / 2.5 else Severity.MEDIUM
            findings.append(
                Finding(
                    i,
                    "low_contrast",
                    sev,
                    round(contrast[i], 2),
                    q["low_contrast_abs"],
                    "<",
                    False,
                    f"Luminance standard deviation {contrast[i]:.1f} indicates very low contrast relative to the dataset.",
                )
            )
        if (
            entropy[i] < q["low_info_entropy"]
            and edge[i] < q["low_info_edge_density"]
            and contrast[i] < q["near_empty_contrast_abs"]
        ):
            findings.append(
                Finding(
                    i,
                    "near_empty",
                    Severity.HIGH,
                    round(entropy[i], 3),
                    q["low_info_entropy"],
                    "<",
                    False,
                    f"Histogram entropy {entropy[i]:.2f} bits, edge density {edge[i]:.4f} and contrast {contrast[i]:.1f}: the image carries very little visual information.",
                )
            )
        if abs(log_aspect_z[i]) > q["aspect_rel_z"]:
            findings.append(
                Finding(
                    i,
                    "unusual_aspect_ratio",
                    Severity.LOW,
                    round(aspect[i], 3),
                    q["aspect_rel_z"],
                    "|z|>",
                    False,
                    f"Aspect ratio {aspect[i]:.2f} is {abs(log_aspect_z[i]):.1f} robust SDs from the dataset norm; it may be distorted when resized.",
                )
            )
        meta = image_meta[i]
        a = meta.get("attributes") or {}
        if a.get("extension_mismatch"):
            findings.append(
                Finding(
                    i,
                    "extension_mismatch",
                    Severity.LOW,
                    None,
                    None,
                    "=",
                    True,
                    f"File extension does not match its actual {meta.get('format')} encoding.",
                )
            )
        if int(a.get("n_frames") or 1) > 1:
            findings.append(
                Finding(
                    i,
                    "multi_frame_image",
                    Severity.LOW,
                    float(a.get("n_frames")),
                    1,
                    ">",
                    True,
                    "Animated/multi-frame file; most loaders will only use the first frame.",
                )
            )
        if meta.get("mode") in {"CMYK", "I;16", "I", "F", "YCbCr", "LAB"}:
            findings.append(
                Finding(
                    i,
                    "unusual_color_mode",
                    Severity.INFO,
                    None,
                    None,
                    "=",
                    True,
                    f"Stored in {meta.get('mode')} mode; confirm your loader converts it consistently.",
                )
            )
        if int(a.get("exif_orientation") or 1) != 1:
            findings.append(
                Finding(
                    i,
                    "exif_rotation",
                    Severity.INFO,
                    float(a.get("exif_orientation")),
                    1,
                    "!=",
                    True,
                    "EXIF orientation flag is set; loaders that ignore EXIF will see a rotated image.",
                )
            )

    # Escalate when several independent medium concerns co-occur on one sample.
    by_sample: dict[int, list[Finding]] = {}
    for f in findings:
        by_sample.setdefault(f.sample_index, []).append(f)
    for i, fs in by_sample.items():
        mediums = [f for f in fs if f.severity == Severity.MEDIUM]
        if len(mediums) >= 2:
            findings.append(
                Finding(
                    i,
                    "multiple_quality_concerns",
                    Severity.HIGH,
                    float(len(mediums)),
                    2,
                    ">=",
                    False,
                    "Several independent quality measurements are outside normal ranges: "
                    + ", ".join(sorted({f.finding_type for f in mediums}))
                    + ".",
                )
            )
    return findings


SEVERITY_SCORE = {Severity.INFO: 0.0, Severity.LOW: 0.3, Severity.MEDIUM: 0.6, Severity.HIGH: 0.9}
