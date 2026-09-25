import io
import stat
import zipfile

import pytest

from datacourt.enums import Split
from datacourt.ingest.layout import detect_layout
from datacourt.ingest.zip_safety import UnsafeArchive, normalize_entry_name, scan_archive

LIMITS = {"max_files": 1000, "max_total_bytes": 50 * 1024 * 1024, "max_entry_bytes": 10 * 1024 * 1024}


def _zip(entries: dict[str, bytes], symlink: str | None = None) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    buf.seek(0)
    return zipfile.ZipFile(buf)


@pytest.mark.parametrize(
    "name",
    ["../evil.jpg", "a/../../evil.jpg", "/etc/passwd", "C:/windows/x.jpg", "a\\..\\..\\x.jpg", "x\x00.jpg"],
)
def test_unsafe_names_rejected(name):
    assert normalize_entry_name(name) is None


def test_safe_names_normalized():
    assert normalize_entry_name("./data/train/cat/1.jpg") == "data/train/cat/1.jpg"
    assert normalize_entry_name("data\\train\\cat\\1.jpg") == "data/train/cat/1.jpg"


def test_traversal_archive_is_rejected():
    zf = _zip({"train/cat/1.jpg": b"x", "../../evil.sh": b"x"})
    with pytest.raises(UnsafeArchive):
        scan_archive(zf, **LIMITS)


def test_symlink_archive_is_rejected():
    zf = _zip({"train/cat/1.jpg": b"x"}, symlink="train/cat/link.jpg")
    with pytest.raises(UnsafeArchive):
        scan_archive(zf, **LIMITS)


def test_zip_bomb_ratio_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("train/cat/bomb.jpg", b"\0" * (20 * 1024 * 1024))
    buf.seek(0)
    with pytest.raises(UnsafeArchive):
        scan_archive(zipfile.ZipFile(buf), **{**LIMITS, "max_entry_bytes": 50 * 1024 * 1024})


def test_too_many_files_rejected():
    zf = _zip({f"train/cat/{i}.jpg": b"x" for i in range(20)})
    with pytest.raises(UnsafeArchive):
        scan_archive(zf, **{**LIMITS, "max_files": 10})


def test_system_files_ignored():
    zf = _zip({"__MACOSX/train/._a.jpg": b"x", "train/cat/.DS_Store": b"x", "train/cat/a.jpg": b"x"})
    entries = {e.name: e.status for e in scan_archive(zf, **LIMITS)}
    assert entries["train/cat/a.jpg"] == "candidate"
    assert entries["train/cat/.DS_Store"] == "ignored"
    assert entries["__MACOSX/train/._a.jpg"] == "ignored"


def test_layout_split_first_with_root_and_aliases():
    paths = [
        "dataset/train/cat/1.jpg",
        "dataset/train/dog/2.jpg",
        "dataset/valid/cat/3.jpg",
        "dataset/testing/dog/4.png",
        "dataset/README.md",
    ]
    lay = detect_layout(paths)
    assert lay.kind == "split_first"
    assert lay.root_prefix == "dataset/"
    assert lay.classes == ["cat", "dog"]
    got = {a.path: (a.split, a.label) for a in lay.assignments}
    assert got["dataset/valid/cat/3.jpg"] == (Split.VAL, "cat")
    assert got["dataset/testing/dog/4.png"] == (Split.TEST, "dog")
    assert got["dataset/README.md"] == (None, None)


def test_layout_class_only_and_class_first():
    lay = detect_layout(["cat/1.jpg", "dog/2.jpg", "dog/sub/3.jpg"])
    assert lay.kind == "class_only"
    assert {a.split for a in lay.assignments if a.label} == {Split.UNSPLIT}
    lay2 = detect_layout(["cat/train/1.jpg", "cat/val/2.jpg", "dog/train/3.jpg", "dog/dev/4.jpg"])
    assert lay2.kind == "class_first"
    assert {(a.label, a.split) for a in lay2.assignments} == {
        ("cat", Split.TRAIN),
        ("cat", Split.VAL),
        ("dog", Split.TRAIN),
        ("dog", Split.VAL),
    }


def test_layout_file_without_class_folder_is_ignored():
    lay = detect_layout(["train/cat/1.jpg", "train/dog/2.jpg", "train/orphan.jpg"])
    orphan = [a for a in lay.assignments if a.path == "train/orphan.jpg"][0]
    assert orphan.label is None and orphan.reason
