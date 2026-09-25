"""Safe ZIP inspection.

The archive is never extracted with `extractall`. Every entry is validated and read
individually into memory-bounded buffers or content-addressed objects. Rejects:
absolute paths, drive letters, `..` traversal, symlinks, encrypted entries, excessive
entry counts, zip bombs (total size and per-entry compression ratio), and oversize entries.
"""

from __future__ import annotations

import posixpath
import stat
import unicodedata
import zipfile
from dataclasses import dataclass

IGNORED_BASENAMES = {".ds_store", "thumbs.db", "desktop.ini"}
IGNORED_DIR_PARTS = {"__macosx", ".git", ".svn", "@eadir"}
MAX_PATH_LENGTH = 1000
MAX_COMPRESSION_RATIO = 250  # per entry, only enforced above 1 MiB uncompressed


class UnsafeArchive(Exception):
    """The archive as a whole is unsafe; nothing should be ingested."""


@dataclass(frozen=True)
class ZipEntry:
    name: str  # normalized POSIX relative path
    info: zipfile.ZipInfo
    size: int
    status: str  # "candidate" | "ignored" | "rejected"
    reason: str | None = None


def normalize_entry_name(raw: str) -> str | None:
    """Returns a safe normalized relative path or None if the name is unsafe."""
    name = unicodedata.normalize("NFC", raw.replace("\\", "/"))
    if "\x00" in name or not name or len(name) > MAX_PATH_LENGTH:
        return None
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    norm = posixpath.normpath("/".join(parts))
    if norm.startswith("..") or norm.startswith("/"):
        return None
    return norm


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return info.create_system == 3 and stat.S_ISLNK(mode)


def scan_archive(
    zf: zipfile.ZipFile, *, max_files: int, max_total_bytes: int, max_entry_bytes: int
) -> list[ZipEntry]:
    infos = zf.infolist()
    if len(infos) > max_files:
        raise UnsafeArchive(f"archive has {len(infos)} entries; limit is {max_files}")
    total = 0
    entries: list[ZipEntry] = []
    seen: set[str] = set()
    for info in infos:
        if info.is_dir():
            continue
        name = normalize_entry_name(info.filename)
        if name is None:
            raise UnsafeArchive("archive contains an unsafe path (absolute or traversal)")
        if _is_symlink(info):
            raise UnsafeArchive("archive contains a symbolic link")
        if info.flag_bits & 0x1:
            raise UnsafeArchive("archive contains encrypted entries")
        total += info.file_size
        if total > max_total_bytes:
            raise UnsafeArchive("archive expands beyond the allowed extracted size")
        if (
            info.file_size > 1024 * 1024
            and info.compress_size > 0
            and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise UnsafeArchive("archive entry has a suspicious compression ratio (possible zip bomb)")
        key = name.casefold()
        if key in seen:
            entries.append(ZipEntry(name, info, info.file_size, "rejected", "duplicate path in archive"))
            continue
        seen.add(key)
        parts = name.split("/")
        base = parts[-1]
        if base.casefold() in IGNORED_BASENAMES or any(p.casefold() in IGNORED_DIR_PARTS for p in parts):
            entries.append(ZipEntry(name, info, info.file_size, "ignored", "system file"))
            continue
        if base.startswith("._"):
            entries.append(ZipEntry(name, info, info.file_size, "ignored", "resource fork"))
            continue
        if info.file_size > max_entry_bytes:
            entries.append(
                ZipEntry(name, info, info.file_size, "rejected", "file exceeds per-file size limit")
            )
            continue
        entries.append(ZipEntry(name, info, info.file_size, "candidate"))
    return entries


def read_entry(zf: zipfile.ZipFile, entry: ZipEntry, max_bytes: int) -> bytes:
    """Reads one entry with an enforced byte limit (defends against lying headers)."""
    buf = bytearray()
    with zf.open(entry.info) as fh:
        while chunk := fh.read(1024 * 256):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ValueError("entry larger than declared/allowed")
    return bytes(buf)
