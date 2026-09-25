# Dataset formats

DataCourt audits **single-label image classification** datasets uploaded as a ZIP archive of folders.

## Supported layouts

A single common top-level folder, such as `my-dataset/`, is stripped automatically. After that, one of three layouts is detected:

**1. Split first** (recommended)

```
train/cat/0001.jpg
train/dog/0002.jpg
val/cat/0101.jpg
test/dog/0201.jpg
```

**2. Class first, with split subfolders**

```
cat/train/0001.jpg
cat/test/0101.jpg
dog/train/0002.jpg
```

**3. Class folders only** (no split, samples marked `unsplit`)

```
cat/0001.jpg
dog/0002.jpg
```

Folders nested below the class folder are allowed; the class is the first folder under the split. Files that cannot be assigned a class, such as images at the archive root, are listed as rejected with a reason.

### Split names

Split folder names are matched case-insensitively:

| Split | Accepted names |
|---|---|
| train | `train`, `training`, `trn` |
| val | `val`, `valid`, `validation`, `dev`, `development` |
| test | `test`, `testing`, `tst`, `holdout` |

Without an evaluation split, leakage analysis has nothing to compare, and metrics are cross-validated estimates. Preflight warns about this.

## Image files

- Formats: JPEG, PNG, WebP, BMP, TIFF, GIF (first frame) and HEIC/HEIF (`.heic`, `.heif`, `.hif`: iPhone photos, many Android phones and cameras; the primary image is used). The format is determined by decoding, not by the extension. A mismatch is reported as a finding.
- Every image is measured as it is displayed: the rotation stored in the file is applied (the EXIF orientation, or the HEIF container's transform), transparency is composited on white, and pixels become 8-bit RGB (10- and 12-bit HEIF included). A sample's width and height are its displayed size.
- Originals are never changed. Each sample is stored byte for byte as it was in the archive, and the archive itself is kept as uploaded. Because most browsers cannot display HEIC, HEIC/HEIF samples also get a WebP copy for viewing; the sample view links to the original file.
- For HEIC/HEIF files, a sample's attributes record the brand, bit depth, chroma, the orientation written in the file's EXIF, the colour profile and the decoder (libheif) version.
- Maximum 64 megapixels per image, and 64 MiB per file.
- Files that cannot be decoded are recorded as rejected with a reason and counted in the preflight "readable" check.
- System files are ignored: `__MACOSX/`, `.git/`, `.svn/`, `@eaDir/`, `.DS_Store`, `Thumbs.db`, `desktop.ini` and `._` resource forks. Class folders starting with `.` are rejected.

## Optional metadata files

Put these at the archive root, next to the split or class folders:

| File | Purpose |
|---|---|
| `provenance.csv` | Per-file provenance. Columns: `path` (required, relative to the dataset root) plus any of `source`, `license`, `collector`, `collection_method`, `capture_date`, `consent_note`. Values are truncated to 500 characters, and the file may be up to 20 MiB. Feeds Provenance Debt and the audit report. |
| `datacourt.json`, `labels.csv`, `README.md`, `LICENSE` | Recognised as metadata and skipped without error. They are not interpreted in this version. |

Dataset-level provenance (source, license, collection notes) can also be entered in the UI for each dataset.

Example `provenance.csv`:

```csv
path,source,license,collector,capture_date
train/cat/0001.jpg,internal-camera-rig-3,proprietary,field-team-a,2025-11-02
train/dog/0002.jpg,https://example.org/dataset,CC-BY-4.0,,
```

## Limits (defaults)

| Limit | Default | Setting |
|---|---|---|
| Upload size | 2 GiB | `MAX_UPLOAD_BYTES` |
| Total uncompressed size | 8 GiB | `MAX_EXTRACTED_BYTES` |
| Entries per archive | 200,000 | `MAX_FILES_PER_ARCHIVE` |
| Per-file size | 64 MiB | fixed |
| Compression ratio per entry | 250:1 (entries > 1 MiB) | fixed |
| Pixels per image | 64 MP | `MAX_IMAGE_PIXELS` |

Archives that break these limits, or that contain absolute paths, `..` traversal, symlinks or encrypted entries, are rejected. See [SECURITY.md](SECURITY.md#uploads-and-archive-safety).

## Clean export format

A clean export is a ZIP in the split-first layout, built from the source version plus final human decisions:

```
<dataset>-v<N>/
  train/<class>/<sha8>-<file>
  val/<class>/<sha8>-<file>
  test/<class>/<sha8>-<file>
  datacourt/
    manifest.json          every file: archive path, original path, SHA-256, label, original label, split, action
    action_log.json        each applied decision: case number, action, path, reviewer, time, adjudicated
    removed_samples.csv    path, sha256, label, split, case_number, decision_id, note
    relabel_mapping.csv    path, sha256, from, to, case_number, decision_id
    SHA256SUMS             checksums for every file in the archive
    README.txt
```

The export is registered as the next version of the dataset and audited automatically, so Version Intelligence can compare it with its parent.

## Not supported (yet)

Multi-label classification, detection and segmentation annotations (COCO, VOC, YOLO), video, and CSV-manifest-only datasets. See the roadmap in the README.
