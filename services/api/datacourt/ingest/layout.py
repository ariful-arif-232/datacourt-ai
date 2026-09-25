"""Dataset layout detection for image classification archives.

Supported layouts (after stripping a single common root folder such as `dataset/`):

    <split>/<class>/<file>      split-first (train/val/test and aliases)
    <class>/<split>/<file>      class-first with split subfolders
    <class>/<file>              class folders without explicit split -> `unsplit`

Nested folders below the class folder are allowed; the class is the first folder
under the split. Files that cannot be assigned a class are ignored with a reason.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from datacourt.enums import Split

SPLIT_ALIASES: dict[str, Split] = {
    "train": Split.TRAIN,
    "training": Split.TRAIN,
    "trn": Split.TRAIN,
    "val": Split.VAL,
    "valid": Split.VAL,
    "validation": Split.VAL,
    "dev": Split.VAL,
    "development": Split.VAL,
    "test": Split.TEST,
    "testing": Split.TEST,
    "tst": Split.TEST,
    "holdout": Split.TEST,
}

# HEIC is Apple's name for HEVC-coded HEIF (iPhone photos); .hif is used by some cameras.
HEIF_EXTENSIONS = frozenset({".heic", ".heif", ".hif"})
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", *HEIF_EXTENSIONS}
METADATA_FILES = {
    "provenance.csv",
    "datacourt.json",
    "readme.md",
    "readme.txt",
    "license",
    "license.txt",
    "labels.csv",
}


@dataclass
class Assignment:
    path: str
    split: Split | None
    label: str | None
    reason: str | None = None


@dataclass
class Layout:
    kind: str  # split_first | class_first | class_only
    root_prefix: str
    split_folders: dict[str, str] = field(default_factory=dict)  # split -> source folder name
    classes: list[str] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "kind": self.kind,
            "root_prefix": self.root_prefix,
            "split_folders": self.split_folders,
            "classes": self.classes,
            "warnings": self.warnings,
        }


def _ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _common_root(paths: list[str]) -> str:
    """A single top folder shared by all paths that is not itself a split or class-level marker."""
    firsts = {p.split("/", 1)[0] for p in paths if "/" in p}
    if len(firsts) == 1 and all("/" in p for p in paths):
        root = next(iter(firsts))
        if root.casefold() not in SPLIT_ALIASES:
            # Only strip if what's below still has folders (i.e. root is a wrapper).
            below = [p.split("/", 1)[1] for p in paths]
            if sum("/" in b for b in below) >= max(1, len(below) // 2):
                return root + "/"
    return ""


def detect_layout(paths: list[str]) -> Layout:
    image_paths = [p for p in paths if _ext(p) in IMAGE_EXTENSIONS]
    other = [p for p in paths if _ext(p) not in IMAGE_EXTENSIONS]
    prefix = _common_root(image_paths) if image_paths else ""
    rel = [p[len(prefix) :] if p.startswith(prefix) else p for p in image_paths]

    first_parts = Counter(r.split("/", 1)[0].casefold() for r in rel if "/" in r)
    second_parts = Counter(r.split("/")[1].casefold() for r in rel if r.count("/") >= 2)
    split_first_hits = sum(c for k, c in first_parts.items() if k in SPLIT_ALIASES)
    class_first_hits = sum(c for k, c in second_parts.items() if k in SPLIT_ALIASES)

    if split_first_hits >= 0.8 * max(1, len(rel)):
        kind = "split_first"
    elif class_first_hits >= 0.8 * max(1, len(rel)):
        kind = "class_first"
    else:
        kind = "class_only"

    layout = Layout(kind=kind, root_prefix=prefix)
    split_sources: dict[Split, set[str]] = {}
    classes: set[str] = set()

    for original, r in zip(image_paths, rel, strict=True):
        parts = r.split("/")
        split: Split | None = None
        label: str | None = None
        if kind == "split_first":
            if len(parts) >= 3 and parts[0].casefold() in SPLIT_ALIASES:
                split = SPLIT_ALIASES[parts[0].casefold()]
                split_sources.setdefault(split, set()).add(parts[0])
                label = parts[1]
            elif len(parts) == 2 and parts[0].casefold() in SPLIT_ALIASES:
                layout.assignments.append(
                    Assignment(original, None, None, "image directly inside split folder (no class folder)")
                )
                continue
        elif kind == "class_first":
            if len(parts) >= 3 and parts[1].casefold() in SPLIT_ALIASES:
                split = SPLIT_ALIASES[parts[1].casefold()]
                split_sources.setdefault(split, set()).add(parts[1])
                label = parts[0]
        else:
            if len(parts) >= 2:
                split = Split.UNSPLIT
                label = parts[0]
        if label is None or split is None:
            layout.assignments.append(Assignment(original, None, None, "could not determine class folder"))
            continue
        label = label.strip()
        if not label or label.startswith("."):
            layout.assignments.append(Assignment(original, None, None, "invalid class folder name"))
            continue
        classes.add(label)
        layout.assignments.append(Assignment(original, split, label))

    for p in other:
        base = p.rsplit("/", 1)[-1].casefold()
        reason = "metadata file" if base in METADATA_FILES else "unsupported file type"
        layout.assignments.append(Assignment(p, None, None, reason))

    layout.classes = sorted(classes)
    layout.split_folders = {str(k): sorted(v)[0] for k, v in split_sources.items()}
    for split_name, sources in split_sources.items():
        if len(sources) > 1:
            layout.warnings.append(f"multiple folders map to split '{split_name}': {sorted(sources)}")
    if kind == "class_only" and first_parts and any(k in SPLIT_ALIASES for k in first_parts):
        layout.warnings.append(
            "some top-level folders look like splits but most do not; treated as class folders"
        )
    if len(layout.classes) < 2:
        layout.warnings.append("fewer than two classes detected")
    return layout
