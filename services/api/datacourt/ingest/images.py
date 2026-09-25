"""Image decoding, validation, perceptual hashing, thumbnails.

Decoder safety: Pillow's decompression-bomb guard is set to the configured pixel limit
and raised to an error; truncated images are rejected (not silently loaded).

HEIC/HEIF (iPhone photos, many Android phones and cameras) is decoded by libheif through
pillow-heif, a worker-only dependency like the rest of this module. Every format is normalised
the same way before anything is measured: the rotation stored in the file applied (the EXIF
orientation, or the HEIF container's transform, which libheif applies while decoding), alpha
composited on white, 8-bit RGB. The original bytes are never modified: they stay the stored
object and the evidence; thumbnails and browser copies are separate, derived objects.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
import pillow_heif
from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

from datacourt import algorithms
from datacourt.config import get_settings
from datacourt.ingest.hashes import hex_to_uint64  # noqa: F401 - re-exported for callers
from datacourt.ingest.layout import HEIF_EXTENSIONS

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = get_settings().max_image_pixels
warnings.simplefilter("error", Image.DecompressionBombWarning)
# Only the primary image is analysed, so thumbnails, depth maps and auxiliary images (HDR gain
# maps, mattes) are not read. libheif's own security limits stay on.
pillow_heif.register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)
HEIF_DECODER = f"libheif {pillow_heif.libheif_version()} (pillow-heif {pillow_heif.__version__})"

# Recorded formats: Pillow's names, except that HEVC-coded HEIF is recorded as HEIC.
FORMAT_EXTENSIONS = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
    "BMP": {".bmp"},
    "TIFF": {".tif", ".tiff"},
    "GIF": {".gif"},
    "MPO": {".jpg", ".jpeg"},
    "HEIC": HEIF_EXTENSIONS,
    "HEIF": HEIF_EXTENSIONS,
}
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "TIFF", "GIF", "MPO", "HEIF"}  # as Pillow names them
BROWSER_COPY_FORMATS = {"HEIC", "HEIF"}  # most browsers cannot display these: a WebP copy is stored
BROWSER_COPY_SIDE = 1600
PREVIEW_SIDE = 512  # the decoded size the hashes are computed from
HASH_VERSION = algorithms.IMAGE_HASH
PIXEL_HASH_VERSION = algorithms.PIXEL_HASH


class InvalidImage(Exception):
    pass


@dataclass
class ImageInfo:
    sha256: str
    width: int
    height: int
    format: str
    mode: str
    byte_size: int
    phash: str
    phash_flip: str
    dhash: str
    attributes: dict
    ext: str = ""
    content_type: str = "application/octet-stream"
    # Decoded pixels kept for the derived objects, so ingestion decodes each file once.
    preview: np.ndarray | None = field(default=None, repr=False)  # longest side <= PREVIEW_SIDE
    normalized: Image.Image | None = field(default=None, repr=False)  # full size; browser-copy formats


def _normalized(im: Image.Image) -> Image.Image:
    """The picture as displayed: stored rotation applied, alpha composited on white, 8-bit RGB."""
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    return im.convert("RGB")


def _fit(rgb: Image.Image, max_side: int | None) -> np.ndarray:
    if max_side and max(rgb.size) > max_side:
        rgb = rgb.copy()
        rgb.thumbnail((max_side, max_side), Image.Resampling.BILINEAR)
    return np.asarray(rgb, dtype=np.uint8)


def decode_rgb(data: bytes, max_side: int | None = None) -> np.ndarray:
    """Decodes to an RGB uint8 array (stored rotation applied, alpha composited on white)."""
    with Image.open(io.BytesIO(data)) as im:
        return _fit(_normalized(im), max_side)


def pixel_sha256(rgb: Image.Image) -> str:
    """SHA-256 of the normalised picture: equal for files that decode to identical pixels in any
    format (a HEIC and a lossless PNG made from it, say), unlike the SHA-256 of the file."""
    h = hashlib.sha256(f"{rgb.width}x{rgb.height}:".encode())
    h.update(rgb.tobytes())
    return h.hexdigest()


def _bits_to_hex(bits: np.ndarray) -> str:
    value = 0
    for b in bits.flatten():
        value = (value << 1) | int(b)
    return f"{value:016x}"


def phash(gray: np.ndarray) -> str:
    """64-bit DCT perceptual hash (32x32 -> low 8x8, median threshold, DC excluded from median)."""
    small = cv2.resize(gray.astype(np.float32), (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(small)
    low = dct[:8, :8]
    med = np.median(low.flatten()[1:])
    return _bits_to_hex(low > med)


def dhash(gray: np.ndarray) -> str:
    small = cv2.resize(gray.astype(np.float32), (9, 8), interpolation=cv2.INTER_AREA)
    return _bits_to_hex(small[:, 1:] > small[:, :-1])


def hamming_hex(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _heif_details(im: ImageFile.ImageFile, data: bytes) -> dict:
    """Provenance of a HEIF file: what it was and how it was decoded (no pixel data)."""
    brand = data[8:12].decode("ascii", "replace") if data[4:8] == b"ftyp" else None
    out: dict = {
        "brand": brand,
        "mimetype": im.get_format_mimetype(),
        "bit_depth": im.info.get("bit_depth"),
        "chroma": im.info.get("chroma"),
        # Applied by the decoder from the container's transform; the file's EXIF value is kept here.
        "exif_orientation": im.info.get("original_orientation"),
        "color_profile": _icc_description(im.info.get("icc_profile")),
        "decoder": HEIF_DECODER,
    }
    return {k: v for k, v in out.items() if v is not None}


def _icc_description(icc: bytes | None) -> str | None:
    if not icc:
        return None
    try:
        from PIL import ImageCms

        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip()[:60] or None
    except Exception:  # noqa: BLE001 - an unreadable profile is provenance we cannot read, not an error
        return "unreadable ICC profile"


def _recorded_format(pil_format: str, mimetype: str | None) -> str:
    if pil_format == "HEIF":
        return "HEIC" if (mimetype or "").startswith("image/heic") else "HEIF"
    return pil_format


def inspect_image(data: bytes, filename: str) -> ImageInfo:
    sha = hashlib.sha256(data).hexdigest()
    pil_format = ""
    heif: dict | None = None
    try:
        with Image.open(io.BytesIO(data)) as im:
            pil_format = (im.format or "").upper()
            if pil_format not in ALLOWED_FORMATS:
                raise InvalidImage(f"unsupported image format {pil_format or 'unknown'}")
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            mode = im.mode
            n_frames = getattr(im, "n_frames", 1)
            exif = im.getexif() if hasattr(im, "getexif") else {}
            orientation = int(exif.get(0x0112, 1)) if exif else 1
            camera = str(exif.get(0x0110, "")).strip()[:60] if exif else ""
            if pil_format == "HEIF":
                heif = _heif_details(im, data)
            im.load()
            # The single full decode of this file: hashes, thumbnail and browser copy derive from it.
            full = _normalized(im)
    except InvalidImage:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        if pil_format == "HEIF" and isinstance(exc, ValueError) and str(exc).strip():
            # libheif's message says what is wrong ("Unexpected end of file", ...) and holds no data.
            raise InvalidImage(f"corrupt HEIC/HEIF image: {str(exc).strip().splitlines()[0][:160]}") from exc
        raise InvalidImage(f"unreadable or corrupt image ({type(exc).__name__})") from exc
    width, height = full.size  # as displayed: after the stored rotation, like everything measured
    if width < 1 or height < 1:
        raise InvalidImage("image has zero size")

    fmt = _recorded_format(pil_format, (heif or {}).get("mimetype"))
    preview = _fit(full, PREVIEW_SIDE)
    gray = cv2.cvtColor(preview, cv2.COLOR_RGB2GRAY)
    name_ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    attributes: dict[str, Any] = {
        "n_frames": int(n_frames),
        "exif_orientation": orientation,
        "camera_model": camera or None,
        "extension_mismatch": bool(name_ext) and name_ext not in FORMAT_EXTENSIONS.get(fmt, {name_ext}),
        "hash_version": HASH_VERSION,
        "pixel_sha256": pixel_sha256(full),
        "pixel_hash_version": PIXEL_HASH_VERSION,
    }
    if heif is not None:
        attributes["heif"] = heif
    if fmt in BROWSER_COPY_FORMATS:
        attributes["browser_copy"] = "webp"
    ext = EXT_FOR_FORMAT[fmt]
    return ImageInfo(
        sha256=sha,
        width=int(width),
        height=int(height),
        format=fmt,
        mode=mode,
        byte_size=len(data),
        phash=phash(gray),
        phash_flip=phash(np.ascontiguousarray(gray[:, ::-1])),
        dhash=dhash(gray),
        attributes=attributes,
        ext=ext,
        content_type=CONTENT_TYPES[ext],
        preview=preview,
        normalized=full if fmt in BROWSER_COPY_FORMATS else None,
    )


def make_thumbnail(data: bytes, size: int, info: ImageInfo | None = None) -> bytes:
    """WebP thumbnail. Reuses the decoded preview of `info` when it is large enough."""
    if info is not None and info.preview is not None and 2 * size <= PREVIEW_SIDE:
        rgb = info.preview
    else:
        rgb = decode_rgb(data, max_side=size * 2)
    im = Image.fromarray(rgb)
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="WEBP", quality=80, method=4)
    return out.getvalue()


def make_browser_copy(data: bytes, info: ImageInfo | None = None) -> bytes:
    """A WebP of the picture as displayed, for formats browsers cannot show (HEIC/HEIF). The
    original stays the stored object; this copy is only for viewing."""
    if info is not None and info.normalized is not None:
        im = info.normalized.copy()
    else:
        im = Image.fromarray(decode_rgb(data))
    im.thumbnail((BROWSER_COPY_SIDE, BROWSER_COPY_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="WEBP", quality=85, method=4)
    return out.getvalue()


EXT_FOR_FORMAT = {
    "JPEG": ".jpg",
    "MPO": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tif",
    "GIF": ".gif",
    "HEIC": ".heic",
    "HEIF": ".heif",
}
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
