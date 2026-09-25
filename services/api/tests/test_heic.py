"""HEIC/HEIF ingestion: decoding, normalisation, provenance, corrupt files, thumbnails, hashes,
duplicates across encodings, and a mixed JPG/HEIC dataset through the API and the worker.

The HEIC files are real: encoded here by libheif (x265) through pillow-heif, decoded by libheif's
HEVC decoder, exactly as the worker does. No third-party images are committed.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import cv2
import numpy as np
import pillow_heif
import pytest
from PIL import Image
from sqlalchemy import func, select

from datacourt.db import models as m
from datacourt.db.session import new_session
from datacourt.ingest import images, layout
from datacourt.ml import duplicates as dup
from datacourt.ml.embeddings import DescriptorBackend
from datacourt.ml.knn import knn
from datacourt.pipeline.config import DEFAULT_CONFIG, backend_thresholds
from datacourt.storage import browser_copy_key, browser_copy_key_for, sample_object_key
from tests.conftest import register
from tests.test_api_flow import _upload, drain

API = "/api/v1"
ROTATE_CW = 6  # EXIF orientation: rotate 90° clockwise to display


def _photo(seed: int, w: int = 192, h: int = 144) -> np.ndarray:
    """Smooth, structured content (not noise), so lossy codecs keep its detail like a photo's."""
    rng = np.random.default_rng(seed)
    base = cv2.GaussianBlur(rng.normal(128, 50, (h, w, 3)).astype(np.float32), (0, 0), 2.5)
    cv2.circle(base, (w // 3 + seed % 11, h // 2), h // 4, (30.0, 70.0, 210.0), -1)
    cv2.rectangle(base, (w // 2, h // 5), (w - 12, h // 2), (220.0, 200.0, 40.0), -1)
    return np.clip(base, 0, 255).astype(np.uint8)


def _encode(
    rgb: np.ndarray, fmt: str, *, orientation: int | None = None, model: str | None = None, **kw
) -> bytes:
    im = Image.fromarray(rgb)
    if orientation or model:
        exif = Image.Exif()
        if orientation:
            exif[0x0112] = orientation
        if model:
            exif[0x0110] = model
        kw["exif"] = exif.tobytes()
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _heic(rgb: np.ndarray, **kw) -> bytes:
    return _encode(rgb, "HEIF", quality=90, **kw)


def _jpeg(rgb: np.ndarray, **kw) -> bytes:
    return _encode(rgb, "JPEG", quality=92, **kw)


def _png(rgb: np.ndarray) -> bytes:
    return _encode(rgb, "PNG")


def _diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


# ---------------------------------------------------------------------------
# Layout, decoding and provenance
# ---------------------------------------------------------------------------


def test_heic_and_heif_extensions_are_images():
    lay = layout.detect_layout(
        [
            "photos/train/cat/IMG_0001.HEIC",
            "photos/train/cat/IMG_0002.heic",
            "photos/train/dog/IMG_0003.HEIF",
            "photos/train/dog/DSC_0004.hif",
            "photos/val/cat/IMG_0005.jpg",
            "photos/val/dog/IMG_0006.heic",
            "photos/train/cat/notes.txt",
        ]
    )
    assert lay.kind == "split_first" and lay.classes == ["cat", "dog"]
    labelled = {a.path.rsplit("/", 1)[-1]: a.label for a in lay.assignments if a.label}
    assert labelled == {
        "IMG_0001.HEIC": "cat",
        "IMG_0002.heic": "cat",
        "IMG_0003.HEIF": "dog",
        "DSC_0004.hif": "dog",
        "IMG_0005.jpg": "cat",
        "IMG_0006.heic": "dog",
    }


def test_heic_is_decoded_upright_with_its_provenance():
    src = _photo(1)
    data = _heic(src, orientation=ROTATE_CW, model="iPhone 15 Pro")
    info = images.inspect_image(data, "IMG_0001.HEIC")
    assert (info.format, info.ext, info.content_type) == ("HEIC", ".heic", "image/heic")
    assert (info.width, info.height) == (144, 192)  # as displayed: the container's rotation applied
    a = info.attributes
    assert a["camera_model"] == "iPhone 15 Pro" and a["extension_mismatch"] is False
    # The pixels are already upright, so no loader rotates them again: no EXIF rotation to warn about.
    assert a["exif_orientation"] == 1 and a["heif"]["exif_orientation"] == ROTATE_CW
    assert a["heif"]["brand"] == "heic" and a["heif"]["mimetype"] == "image/heic"
    assert a["heif"]["bit_depth"] == 8 and a["heif"]["decoder"].startswith("libheif ")
    assert a["browser_copy"] == "webp" and len(a["pixel_sha256"]) == 64
    assert a["pixel_hash_version"] == "pixel-sha256-rgb8-v1"

    rgb = images.decode_rgb(data)
    upright = cv2.rotate(src, cv2.ROTATE_90_CLOCKWISE)
    assert rgb.shape == upright.shape and _diff(rgb, upright) < 6
    assert _diff(rgb, cv2.rotate(src, cv2.ROTATE_90_COUNTERCLOCKWISE)) > 20


def test_heic_and_the_same_photo_as_rotated_jpeg_are_measured_alike():
    src = _photo(2)
    heic = images.inspect_image(_heic(src, orientation=ROTATE_CW), "a.heic")
    jpeg = images.inspect_image(_jpeg(src, orientation=ROTATE_CW), "a.jpg")
    # Same displayed size and near-identical perceptual hashes, so duplicate checks agree.
    assert (heic.width, heic.height) == (jpeg.width, jpeg.height) == (144, 192)
    assert images.hamming_hex(heic.phash, jpeg.phash) <= 4
    assert _diff(heic.preview, jpeg.preview) < 6
    # A JPEG's EXIF flag still matters: loaders that ignore it see the photo rotated.
    assert jpeg.attributes["exif_orientation"] == ROTATE_CW and "heif" not in jpeg.attributes


def test_10_bit_and_transparent_heif_become_8_bit_rgb():
    rng = np.random.default_rng(3)
    deep = rng.integers(0, 65535, (48, 64, 3), dtype=np.uint16)
    heif10 = pillow_heif.from_bytes(mode="RGB;16", size=(64, 48), data=deep.astype("<u2").tobytes())
    buf = io.BytesIO()
    heif10.save(buf, quality=90)
    info = images.inspect_image(buf.getvalue(), "hdr.heic")
    assert info.attributes["heif"]["bit_depth"] == 10 and info.mode == "RGB"
    assert info.preview is not None and info.preview.dtype == np.uint8 and info.preview.shape == (48, 64, 3)

    rgba = np.zeros((40, 60, 4), dtype=np.uint8)
    rgba[:, :30] = (200, 30, 30, 255)  # left half opaque red, right half transparent
    buf = io.BytesIO()
    Image.fromarray(rgba).save(buf, format="HEIF", quality=95)
    info = images.inspect_image(buf.getvalue(), "cutout.heic")
    assert info.mode == "RGBA" and info.preview is not None
    assert info.preview[:, 40:].min() >= 245  # composited on white, like PNGs with alpha
    assert info.preview[:, :20, 0].mean() > 150 and info.preview[:, :20, 1].mean() < 80


def test_corrupt_heic_files_are_rejected_with_a_reason():
    data = _heic(_photo(4, 256, 192))
    mdat = data.find(b"mdat")
    no_payload = data[: mdat + 8] + bytes(len(data) - mdat - 8)
    for bad in (b"", data[:24], data[: len(data) // 2], no_payload):
        with pytest.raises(images.InvalidImage):
            images.inspect_image(bad, "IMG_0200.HEIC")
    with pytest.raises(images.InvalidImage, match="corrupt HEIC/HEIF image: "):
        images.inspect_image(no_payload, "IMG_0200.HEIC")
    # Apps sometimes save a JPEG under a .heic name: accepted, recorded as JPEG, flagged.
    info = images.inspect_image(_jpeg(_photo(5)), "IMG_0300.heic")
    assert info.format == "JPEG" and info.attributes["extension_mismatch"] is True


# ---------------------------------------------------------------------------
# Thumbnails, browser copies and hashes
# ---------------------------------------------------------------------------


def test_thumbnail_and_browser_copy_come_from_the_single_decode(monkeypatch):
    big = cv2.resize(_photo(6), (2400, 1800), interpolation=cv2.INTER_CUBIC)
    data = _heic(big, orientation=ROTATE_CW)
    info = images.inspect_image(data, "IMG_0400.HEIC")
    assert info.normalized is not None and info.normalized.size == (1800, 2400)

    def no_second_decode(*_a, **_k):
        raise AssertionError("decoded again")

    with monkeypatch.context() as mp:
        mp.setattr(images, "decode_rgb", no_second_decode)
        thumb = images.make_thumbnail(data, 256, info)
        copy = images.make_browser_copy(data, info)
    with Image.open(io.BytesIO(thumb)) as t:
        assert t.format == "WEBP" and t.size == (192, 256)  # upright, longest side 256
    with Image.open(io.BytesIO(copy)) as c:
        assert c.format == "WEBP" and c.size == (1200, 1600)  # upright, longest side 1600
        assert _diff(np.asarray(c.convert("RGB").resize((180, 240))), images.decode_rgb(data, 240)) < 8
    # Reusing the decode changes nothing: the thumbnail equals one made from the bytes.
    assert images.make_thumbnail(data, 256) == thumb


@pytest.mark.parametrize("kind", ["jpeg", "rotated-jpeg", "png-alpha", "heic"])
def test_perceptual_hashes_are_those_of_the_512px_decode(kind):
    """`phash-dct32-v1` is unchanged by the single-decode path: same values as before."""
    src = _photo(7, 900, 600)
    if kind == "jpeg":
        data = _jpeg(src)
    elif kind == "rotated-jpeg":
        data = _jpeg(src, orientation=ROTATE_CW)
    elif kind == "png-alpha":
        rgba = np.dstack([src, np.full(src.shape[:2], 180, np.uint8)])
        buf = io.BytesIO()
        Image.fromarray(rgba).save(buf, format="PNG")
        data = buf.getvalue()
    else:
        data = _heic(src)
    info = images.inspect_image(data, f"x.{kind}")
    gray = cv2.cvtColor(images.decode_rgb(data, max_side=512), cv2.COLOR_RGB2GRAY)
    assert info.phash == images.phash(gray) and info.dhash == images.dhash(gray)
    again = images.inspect_image(data, f"x.{kind}")
    assert (again.sha256, again.phash, again.attributes["pixel_sha256"]) == (
        info.sha256,
        info.phash,
        info.attributes["pixel_sha256"],
    )


def test_pixel_hash_identifies_the_same_picture_in_other_formats():
    heic = _heic(_photo(8))
    decoded = images.decode_rgb(heic)
    lossless = {"png": _png(decoded), "tiff": _encode(decoded, "TIFF"), "bmp": _encode(decoded, "BMP")}
    infos = {k: images.inspect_image(v, f"export.{k}") for k, v in {"heic": heic, **lossless}.items()}
    assert len({i.sha256 for i in infos.values()}) == 4  # four different files...
    assert len({i.attributes["pixel_sha256"] for i in infos.values()}) == 1  # ...one picture
    jpeg = images.inspect_image(_jpeg(decoded), "export.jpg")  # lossy: a near duplicate, not exact
    assert jpeg.attributes["pixel_sha256"] != infos["heic"].attributes["pixel_sha256"]
    assert images.hamming_hex(jpeg.phash, infos["heic"].phash) <= 4


def test_browser_copy_key_is_derived_from_the_sample_key():
    import uuid

    o, d, v = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sha = hashlib.sha256(b"x").hexdigest()
    assert browser_copy_key_for(sample_object_key(o, d, v, sha, ".heic"), sha) == browser_copy_key(
        o, d, v, sha
    )
    with pytest.raises(ValueError):
        browser_copy_key_for("orgs/x/other/file.heic", sha)


# ---------------------------------------------------------------------------
# Duplicates across encodings (dup-families-v2)
# ---------------------------------------------------------------------------


def _find(datas: list[bytes], with_pixels: bool = True):
    infos = [images.inspect_image(dd, f"{i}.bin") for i, dd in enumerate(datas)]
    rgbs = [images.decode_rgb(dd) for dd in datas]
    emb = DescriptorBackend().embed(rgbs)
    sims, idx = knn(emb, emb, 4, exclude_self=True)
    return dup.find_duplicates(
        sha=[i.sha256 for i in infos],
        ph=images.hex_to_uint64([i.phash for i in infos]),
        ph_flip=images.hex_to_uint64([i.phash_flip for i in infos]),
        width=np.array([i.width for i in infos]),
        height=np.array([i.height for i in infos]),
        brightness=np.array([float(r.mean()) for r in rgbs]),
        saturation=np.array([float(cv2.cvtColor(r, cv2.COLOR_RGB2HSV)[..., 1].mean()) for r in rgbs]),
        emb=emb,
        struct=np.stack([dup.structure_thumb(cv2.cvtColor(r, cv2.COLOR_RGB2GRAY)) for r in rgbs]),
        knn_sims=sims,
        knn_idx=idx,
        cfg=backend_thresholds(DEFAULT_CONFIG, "dc-descriptor"),
        pixel=[i.attributes["pixel_sha256"] for i in infos] if with_pixels else None,
    )


def test_heic_and_its_conversions_form_one_duplicate_family():
    heic = _heic(_photo(9), orientation=ROTATE_CW)
    decoded = images.decode_rgb(heic)
    datas = [heic, _png(decoded), _jpeg(decoded), _jpeg(_photo(31)), _heic(_photo(47))]
    edges, fams = _find(datas)
    by_pair = {(e.a, e.b): e for e in edges}
    assert by_pair[(0, 1)].relation == "exact"
    assert (
        by_pair[(0, 1)].evidence["rule"] == "identical decoded pixels (same picture, different file encoding)"
    )
    assert (0, 2) in by_pair and by_pair[(0, 2)].relation != "exact"  # lossy JPEG: near duplicate
    assert not any(3 in (e.a, e.b) or 4 in (e.a, e.b) for e in edges)  # other photos stay apart
    assert [sorted(f.members) for f in fams] == [[0, 1, 2]]
    # Without the pixel hash (v1), the lossless copy was only a near duplicate.
    edges_v1, _ = _find(datas, with_pixels=False)
    assert {(e.a, e.b): e for e in edges_v1}[(0, 1)].relation != "exact"


# ---------------------------------------------------------------------------
# A mixed JPG/HEIC dataset through the API and the worker
# ---------------------------------------------------------------------------


def _mixed_dataset() -> tuple[bytes, dict[str, bytes]]:
    files: dict[str, bytes] = {}
    for i in range(8):
        files[f"iphone/train/cat/IMG_{i:04d}.HEIC"] = _heic(
            _photo(100 + i), orientation=ROTATE_CW if i % 2 else None
        )
        files[f"iphone/train/dog/IMG_{i + 50:04d}.JPG"] = _jpeg(_photo(200 + i))
    for i in range(3):
        files[f"iphone/val/cat/IMG_{i + 20:04d}.heic"] = _heic(_photo(300 + i))
        files[f"iphone/val/dog/IMG_{i + 70:04d}.jpg"] = _jpeg(_photo(400 + i))
    files["iphone/val/dog/IMG_0080.HEIF"] = _heic(_photo(500))
    # The same photo twice: once byte-identical, once exported losslessly into the validation split.
    files["iphone/train/cat/IMG_0000 copy.HEIC"] = files["iphone/train/cat/IMG_0000.HEIC"]
    files["iphone/val/cat/IMG_0001 export.png"] = _png(
        images.decode_rgb(files["iphone/train/cat/IMG_0001.HEIC"])
    )
    whole = _heic(_photo(600, 256, 192))
    files["iphone/train/cat/IMG_0099.HEIC"] = whole[: len(whole) // 2]  # cut off: rejected
    files["iphone/train/dog/IMG_0098.heic"] = _jpeg(_photo(700))  # a JPEG named .heic
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue(), files


def test_mixed_jpg_heic_dataset_is_ingested_and_audited(client):
    archive, files = _mixed_dataset()
    me = register(client, "iphone-owner@example.com", "Phone Owner")
    proj = client.post(
        f"{API}/orgs/{me['organizations'][0]['id']}/projects", json={"name": "Phone photos"}
    ).json()
    vid = _upload(client, proj["id"], "phone-photos", archive, audit="fast")
    drain()

    v = client.get(f"{API}/versions/{vid}").json()
    assert v["status"] == "ready", v.get("error")
    assert v["source_sha256"] == hashlib.sha256(archive).hexdigest()  # the upload itself is untouched
    st = v["stats"]
    assert st["formats"] == {"HEIC": 13, "JPEG": 12, "PNG": 1}
    assert st["files"]["rejected"] == 1 and v["audits"][0]["status"] == "completed"
    with new_session() as s:
        reasons = s.scalars(
            select(m.DatasetFile.reason).where(
                m.DatasetFile.dataset_version_id == vid, m.DatasetFile.status == "rejected"
            )
        ).all()
    assert len(reasons) == 1 and "corrupt" in reasons[0]

    items = client.get(f"{API}/versions/{vid}/samples", params={"limit": 100}).json()["items"]
    by_path = {x["path"]: x for x in items}
    rotated = client.get(f"{API}/samples/{by_path['iphone/train/cat/IMG_0001.HEIC']['id']}").json()
    assert rotated["format"] == "HEIC" and (rotated["width"], rotated["height"]) == (144, 192)
    assert rotated["attributes"]["heif"]["exif_orientation"] == ROTATE_CW
    view = client.get(rotated["image_url"])  # what the browser shows: a WebP copy, upright
    assert view.status_code == 200 and view.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(view.content)) as im:
        assert im.size == (144, 192)
    original = client.get(rotated["original_url"])  # the stored original, byte for byte
    assert original.content == files["iphone/train/cat/IMG_0001.HEIC"]
    assert "IMG_0001.HEIC" in original.headers["content-disposition"]
    thumb = client.get(rotated["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/webp"
    jpg = client.get(f"{API}/samples/{by_path['iphone/train/dog/IMG_0050.JPG']['id']}").json()
    assert "browser_copy" not in jpg["attributes"]  # browsers show JPEG itself
    assert client.get(jpg["image_url"]).content == files["iphone/train/dog/IMG_0050.JPG"]
    renamed = by_path["iphone/train/dog/IMG_0098.heic"]
    assert renamed["format"] == "JPEG"

    fams = client.get(f"{API}/versions/{vid}/duplicates").json()["families"]
    groups = {frozenset(x["sample"]["path"] for x in f["members"]): f for f in fams}
    copy = groups[frozenset({"iphone/train/cat/IMG_0000.HEIC", "iphone/train/cat/IMG_0000 copy.HEIC"})]
    assert copy["kind"] == "exact"
    export = groups[frozenset({"iphone/train/cat/IMG_0001.HEIC", "iphone/val/cat/IMG_0001 export.png"})]
    assert export["kind"] == "exact" and export["crosses_splits"] is True
    leaks = client.get(f"{API}/versions/{vid}/leakage").json()["findings"]
    assert any(
        {x["path"] for x in f.get("samples", [])}
        >= {"iphone/train/cat/IMG_0001.HEIC", "iphone/val/cat/IMG_0001 export.png"}
        for f in leaks
    )


def test_a_version_that_failed_on_heic_can_be_ingested_again(client, monkeypatch):
    """The production failure: HEIC-only archive, extensions not recognised, "no class folders
    with images were found". After the fix the stored archive is ingested again without a new upload."""
    files = {
        f"train/{c}/IMG_{i:04d}.HEIC": _heic(_photo(800 + 10 * k + i))
        for k, c in enumerate(("a", "b"))
        for i in range(4)
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    me = register(client, "retry-owner@example.com", "Retry Owner")
    proj = client.post(f"{API}/orgs/{me['organizations'][0]['id']}/projects", json={"name": "Retry"}).json()
    vid = _upload(client, proj["id"], "heic-only", buf.getvalue(), audit="fast")
    with monkeypatch.context() as mp:
        mp.setattr(
            layout, "IMAGE_EXTENSIONS", {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
        )
        drain()
    v = client.get(f"{API}/versions/{vid}").json()
    assert v["status"] == "failed" and "no class folders with images were found" in v["error"]

    r = client.post(f"{API}/versions/{vid}/retry-ingest")
    assert r.status_code == 200, r.text
    assert r.json()["version"]["status"] == "uploaded"
    assert client.post(f"{API}/versions/{vid}/retry-ingest").status_code == 409  # already queued
    drain()
    v = client.get(f"{API}/versions/{vid}").json()
    assert v["status"] == "ready" and v["stats"]["formats"] == {"HEIC": 8}
    assert v["audits"] and v["audits"][0]["profile"] == "fast"  # the audit chosen at upload
    assert client.post(f"{API}/versions/{vid}/retry-ingest").status_code == 409  # not failed


def test_ingest_check_command_passes_and_leaves_nothing_behind(capsys):
    """`datacourt-ingest-check` (run on GitHub Actions workers) on its generated JPG/HEIC set."""
    from datacourt import ingest_check

    with new_session() as s:
        orgs_before = s.scalar(select(func.count()).select_from(m.Organization))
    assert ingest_check.main([]) == 0
    out = capsys.readouterr().out
    assert "All checks passed." in out and "[FAIL]" not in out
    assert "browser-viewable copy for every HEIC/HEIF sample" in out
    with new_session() as s:  # the workspace it created was deleted with its objects
        assert s.scalar(select(func.count()).select_from(m.Organization)) == orgs_before
