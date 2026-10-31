---
name: archive-tools
description: Compresses files and folders into archives (zip, tar, tar.gz, tar.bz2, tar.xz), lists the contents of an archive without extracting it, and extracts (decompresses) archives (zip, tar, tar.gz/tgz, tar.bz2, tar.xz, and single-file .gz/.bz2/.xz). It reads zip files with Japanese file names made on Japanese Windows without garbled names, and supports password-protected zip (ZipCrypto) extraction. Use it when the user wants to zip/compress files or a folder, unzip/extract/decompress/thaw an archive, or check what is inside a zip or tar file. 7z and RAR are not supported.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# archive-tools

A skill that compresses and extracts archives. It has three scripts: listing contents (`list_archive.py`),
extracting (`extract_archive.py`) and creating (`create_archive.py`).

On success each script prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

Supported formats:

| Operation | Formats |
|---|---|
| List / extract | zip, tar, tar.gz (.tgz), tar.bz2 (.tbz2), tar.xz (.txz), single-file .gz / .bz2 / .xz |
| Create | zip, tar, tar.gz (.tgz), tar.bz2 (.tbz2), tar.xz (.txz) (decided by the extension of the output path) |

The format of an archive to read is detected from its content, not its extension.
7z and RAR cannot be handled; tell the user so when the script reports it.

Large archives can take a long time to extract or create.

## 1. list_archive.py — check the contents without extracting

Call example:
```bash
python list_archive.py "C:\Users\me\data.zip" --max-entries 200
```
`--max-entries` can be omitted (default 200).

Output example:
```json
{"path": "C:\\Users\\me\\data.zip", "format": "zip", "archive_size_bytes": 52311,
 "total_entries": 3, "file_count": 2, "dir_count": 1, "total_size_bytes": 180244, "encrypted": false,
 "entries": [
   {"name": "data/", "is_dir": true, "size": 0, "modified": "2026-10-01T09:12:00"},
   {"name": "data/見積書.xlsx", "is_dir": false, "size": 120244, "modified": "2026-10-01T09:12:00"},
   {"name": "data/memo.txt", "is_dir": false, "size": 60000, "modified": "2026-10-01T09:10:00"}
 ],
 "truncated": false}
```
- `entries` has at most `--max-entries` items. If `truncated` is `true`, tell the user that only the first
  items are shown out of `total_entries`.
- `size`/`total_size_bytes` are sizes after extraction (`null` for single-file .gz/.bz2/.xz, where it is unknown).
- `encrypted: true` means a password is needed to extract.
- An entry with `not_extractable` (links, device files in tar) will not be extracted; the value is the reason.

## 2. extract_archive.py — extract (decompress) an archive

Call example:
```bash
python extract_archive.py "C:\Users\me\data.zip" --dest "C:\Users\me\work\data"
```
Options (all optional):

| Option | Default | Description |
|---|---|---|
| `--dest` | A folder named after the archive (without extension), next to the archive | Destination folder (created if missing) |
| `--overwrite` | Disabled | Overwrite files that already exist in the destination (without it they are skipped) |
| `--password` | None | Password of an encrypted zip |
| `--max-total-mb` | `4096` | Upper limit of the total extracted size in MB (protection against zip bombs) |

Output example:
```json
{"archive_path": "C:\\Users\\me\\data.zip", "format": "zip", "output_dir": "C:\\Users\\me\\work\\data",
 "extracted_files": 2, "created_dirs": 1, "total_size_bytes": 180244,
 "skipped_count": 0, "skipped": [],
 "path_memory": {"@5": "C:\\Users\\me\\work\\data"}}
```
Report `output_dir` and `extracted_files` to the user. If `skipped_count` is not 0, also tell the user
which entries were not extracted and why (`skipped` holds up to 50 items with `name` and `reason`).
To work with the extracted files afterwards, search under `output_dir` (use the `@N` of `path_memory` for the folder).

Edge cases:
- Files that already exist are not overwritten; they appear in `skipped` with "already exists".
  If the user wants them replaced, run again with `--overwrite` (confirm with the user first).
- If the zip is password-protected and `--password` is not given, it ends with an error. Ask the user for the
  password and run again with `--password`. A wrong password also ends with an error.
  AES-encrypted zips (made by 7-Zip etc. with AES-256) cannot be extracted.
- Entries that would be written outside the destination (absolute paths or `..`), and links/special files in tar,
  are not extracted and appear in `skipped`. Characters that cannot be used in Windows file names are replaced with `_`.
- If the total extracted size exceeds `--max-total-mb`, nothing is extracted and it ends with an error.
  Only raise the limit if the user confirms the archive is trustworthy and really that large.
- If the destination cannot be written (e.g. the archive is outside the working directory and the default
  destination is next to it), it ends with an error. Run again with `--dest` set to a folder under the working directory.
- A broken or non-archive file ends with an error. If only some entries are broken, they appear in `skipped`
  and the others are extracted.

## 3. create_archive.py — compress files/folders into an archive

Call example:
```bash
python create_archive.py "C:\Users\me\work\report.zip" "C:\Users\me\work\report" "C:\Users\me\work\summary.xlsx"
```
The first argument is the output path; the format is decided by its extension (`.zip`, `.tar`, `.tar.gz`/`.tgz`,
`.tar.bz2`/`.tbz2`, `.tar.xz`/`.txz`). The rest are the files/folders to put in (one or more).
A folder is stored with its folder name (`C:\Users\me\work\report` → `report/...` in the archive);
to store only the contents of a folder, pass the files inside it instead.
Add `--overwrite` to replace an existing output file (confirm with the user first).

Output example:
```json
{"output_path": "C:\\Users\\me\\work\\report.zip", "format": "zip", "file_count": 12, "dir_count": 2,
 "total_size_bytes": 3402211, "size_bytes": 1203345,
 "path_memory": {"@7": "C:\\Users\\me\\work\\report.zip"}}
```
Tell the user `output_path`, the number of files (`file_count`), and the size before (`total_size_bytes`) and
after (`size_bytes`) compression.

Edge cases:
- If the output file already exists, it ends with an error unless `--overwrite` is given.
- A missing input, or an unknown output extension, ends with an error.
- If two inputs would have the same name in the archive (e.g. two folders both named `data`), it ends with an error.
- If the output file is inside an input folder, it is not put into itself.
- Password-protected archives cannot be created.
- File names in zip are stored as UTF-8, so Japanese names are shown correctly by Windows Explorer and other tools.

## Common edge cases

- Only the Python standard library is used; no extra installation is needed.
- Writing is limited to the working directory. If writing fails with a permission error, choose an output
  path / destination under the working directory.
