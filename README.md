# PSTmortem

**Primary repository:** [gitlab.com/seuffert/pstmortem](https://gitlab.com/seuffert/pstmortem)  
**GitHub mirror:** [github.com/seuffert/pstmortem](https://github.com/seuffert/pstmortem)

`PSTmortem` is a Python command-line tool for reading Microsoft Outlook OST and PST files and exporting their mail folders to formats that are useful outside Outlook:

- `mbox` for Thunderbird Local Folders
- `maildir` for Dovecot and other Maildir-compatible mail systems

It is designed for very large Outlook data files, supports folder filtering, date filtering, resumable Maildir exports, and careful export statistics.

It comfortably handles **huge** archives — tested on OST files larger than **30 GB** — while keeping
memory usage **low and steady**. Messages are streamed one at a time and written incrementally (mbox
files are flushed/closed as it goes, Maildir messages are written individually), and per-message
objects are explicitly released during the tree walk, so RAM stays roughly constant regardless of the
total file size.

## What It Does

Given an Outlook OST or PST file, the tool can:

- open and traverse the OST folder tree with `pypff`
- print progress while exporting
- export folders into Thunderbird-compatible `mbox` + `.sbd` folder layout
- export folders into Dovecot-compatible `maildir` layout
- preserve folder hierarchy
- include or exclude folders using regex filters
- filter exported mail by date range
- skip oversized attachments before loading them into memory
- create deterministic Maildir filenames so reruns can skip already-exported messages
- support graceful interruption with `Ctrl-C`

## Important Limitations

This exporter reconstructs messages from the fields exposed by `pypff`.

It does **not** have access to original raw MIME messages in the currently used `pypff` build. That means the following may not be preserved perfectly:

- inline image `Content-ID` relationships
- original multipart nesting
- calendar invite structure
- signed or encrypted message structure
- TNEF / `winmail.dat` details

Attachment content types are **inferred from the attachment filename extension** (via Python's
`mimetypes`), because `pypff` does not reliably expose the original MIME type. Known extensions get a
proper type (e.g. `.pdf` → `application/pdf`); unknown or extension-less attachments fall back to
`application/octet-stream`. Text-like attachments are stored with an `application/*` type to remain
binary-safe.

If a message has **no** plain-text or HTML body but does have a Rich Text (RTF) body, the inline body
is set to a short placeholder and the original RTF is preserved verbatim as an attached `body.rtf`
(`application/rtf`). This avoids rendering raw RTF control words as an unreadable body while keeping
the original content openable (e.g. in Word/LibreOffice). No new dependency is required and no RTF
decoding is attempted.

The tool prints a MIME fidelity warning by default. You can suppress it with:

```bash
--suppress-fidelity-warning
```

## Requirements

- Python 3
- `libpff-python` / `pypff`
- enough disk space for the export output

Python dependency:

```text
libpff-python
```

## Installation

### Using the provided run script

The repository includes `run.sh`, which:

- creates a Python virtual environment in `venv/`
- installs dependencies from `requirements.txt`
- runs the exporter

Example:

```bash
./run.sh /path/to/archive.ost ./out
./run.sh /path/to/archive.pst ./out
```

### Manual installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install --no-cache-dir -r requirements.txt
python pstmortem.py /path/to/archive.ost ./out
python pstmortem.py /path/to/archive.pst ./out
```

## Basic Usage

```bash
python pstmortem.py ARCHIVE_FILE OUT_DIR [options]
```

### Positional arguments

- `ARCHIVE_FILE` — path to the source Outlook data file (`.ost` or `.pst`)
- `OUT_DIR` — output directory for exported mail

### Main options

- `--format {mbox,maildir}`
  Export format. Default: `mbox`

- `--include REGEX`
  Include only folders whose names or paths match the regex

- `--exclude REGEX`
  Exclude folders whose names or paths match the regex

- `--match-leaf-folder-only`
  Apply `--include` / `--exclude` only to the current folder name instead of the full folder path

- `--max-folders N`
  Stop after successfully exporting `N` folders

- `--max-mails N`
  Stop after successfully exporting `N` mails globally

- `--overwrite`
  Overwrite existing output files instead of skipping them

- `--fail-on-existing`
  Abort immediately if a pre-existing `mbox` folder output file is found, instead of skipping it.
  Useful for automated/CI runs that must not silently skip folders. Cannot be combined with
  `--overwrite`, and only applies to `--format mbox`.

- `--maildir-state {read,unread}`
  For `--format maildir`, whether exported messages are marked read or unread.
  `read` (default) writes to `cur/` with a `:2,S` (Seen) flag; `unread` writes to `new/`
  with no flag suffix so the mail appears as new. Has no effect on `mbox` exports.

- `--allow-existing-output`
  Allow using an output directory that is already non-empty

- `--skip-attachments-larger-than SIZE`
  Skip attachments larger than the given size before loading them into memory
  Examples: `100M`, `2G`, `500K`

- `--start-date YYYY-MM-DD`
  Export only messages on or after the given date

- `--end-date YYYY-MM-DD`
  Export only messages up to and including the given date

- `--exclude-unknown-date`
  When a date filter is active, skip messages whose date cannot be determined

- `--suppress-fidelity-warning`
  Hide the MIME fidelity warning

## Folder Filtering Behavior

Folder filtering order is:

1. apply `--include` if set
2. then apply `--exclude` if set

By default, matching is done against the **full folder path**.

Example full path:

```text
Root/Stamm - Postfach/IPM_SUBTREE/Public/07 Einkauf/04_2020
```

If you want matching only against the current folder name (for example only `04_2020`), use:

```bash
--match-leaf-folder-only
```

## Date Filtering Behavior

Date filtering uses whole-day UTC boundaries.

Examples:

- `--start-date 2024-01-01` means: include mail from `2024-01-01 00:00:00 UTC` onward
- `--end-date 2024-01-31` means: include mail before `2024-02-01 00:00:00 UTC`

This makes the end date inclusive in normal usage.

If a message date cannot be determined:

- by default, the message is still included
- with `--exclude-unknown-date`, it is skipped

## Export Formats

### `mbox` export

This mode creates Thunderbird-compatible folder trees using:

- mbox files for folder contents
- `.sbd` directories for subfolders

Example layout:

```text
mbox/
├── Root
├── Root.sbd/
│   ├── Stamm - Postfach
│   ├── Stamm - Postfach.sbd/
│   │   ├── IPM_SUBTREE
│   │   ├── IPM_SUBTREE.sbd/
```

The exporter also creates empty placeholder mbox files for folders that only contain subfolders, because Thunderbird expects both:

- `FolderName`
- `FolderName.sbd/`

These placeholders and `.sbd` directories are created **lazily** — only when a folder (or one of its
descendants) is actually exported. Folders excluded via `--exclude` (or filtered out by `--include`)
with no exported descendants do **not** produce any output or placeholder files.

The `From_` separator line written for each mbox message uses the message's own date, so exports are
deterministic and chronologically ordered (re-running produces identical separators).

#### Re-running an mbox export (skip / overwrite / fail)

mbox rerun-safety is **folder-level**: if a folder's output file already exists, that folder is
**skipped** by default. The tool prints an honest notice that the existing file was *not verified as
complete* — if a previous run was interrupted mid-folder, the file may be **partial**. A prominent
summary note is also shown at the end of the run whenever folders were skipped this way.

To control this behavior:

- default — skip pre-existing folder files (and warn loudly)
- `--overwrite` — re-export skipped folders from scratch
- `--fail-on-existing` — abort on the first pre-existing folder file (good for automation)

There is intentionally **no** silent partial-vs-complete auto-detection for mbox; the responsibility
to re-export an interrupted folder is handed back to you via `--overwrite`.

#### Using the result in Thunderbird

Point Thunderbird Local Folders to the **parent export directory**.

For example, if your export looks like:

```text
/home/user/export/mbox/Root
/home/user/export/mbox/Root.sbd/
```

set Thunderbird Local Folders directory to:

```text
/home/user/export/mbox
```

Not to `Root.sbd/`.

### `maildir` export

This mode creates Maildir folder trees suitable for Dovecot-style mail storage.

Example layout:

```text
maildir/
├── .Root/
│   ├── cur/
│   ├── new/
│   └── tmp/
├── .Root.Stamm - Postfach/
│   ├── cur/
│   ├── new/
│   └── tmp/
```

Maildir filenames are deterministic, so rerunning the export can skip already-exported messages.

#### Read vs unread (`--maildir-state`)

- `read` (default) — messages are written to `cur/` with a `:2,S` (Seen) info suffix, so they appear
  as already read. This suits archival/recovery dumps where you don't want a flood of "new mail".
- `unread` — messages are written to `new/` with **no** info suffix (as required by the Maildir
  spec), so they appear as freshly delivered/new mail.

#### Rerun de-duplication

Each message has a deterministic **base name** (without any `:2,...` flag suffix). On rerun, the tool
considers a message already exported if a file with that base name exists in **either** `new/` or
`cur/`, ignoring any flag suffix. This means messages a mail client has since read (which moves the
file from `new/` to `cur/` and appends flags such as `:2,S`) are still correctly recognized and not
duplicated.

## Example Commands

### Export everything to Thunderbird mbox

```bash
./run.sh /data/archive.ost ./mbox-export --format mbox
```

### Export everything to Maildir

```bash
./run.sh /data/archive.ost ./maildir-export --format maildir
```

### Export only folders under `Public/07 Einkauf`

```bash
./run.sh /data/archive.ost ./mbox-export --format mbox --include "Public/07 Einkauf"
```

### Exclude trash / sync folders

```bash
./run.sh /data/archive.ost ./mbox-export --exclude "Gelöschte Elemente|Synchronisierungsprobleme"
```

### Match only leaf folder names

```bash
./run.sh /data/archive.ost ./mbox-export --include "^2024$" --match-leaf-folder-only
```

### Export only mail from 2024

```bash
./run.sh /data/archive.ost ./mbox-export --start-date 2024-01-01 --end-date 2024-12-31
```

### Skip unknown-date messages while filtering by date

```bash
./run.sh /data/archive.ost ./mbox-export --start-date 2024-01-01 --exclude-unknown-date
```

### Limit a test run to a few mails/folders

```bash
./run.sh /data/archive.ost ./test-export --max-mails 100 --max-folders 5
```

### Skip large attachments

```bash
./run.sh /data/archive.ost ./mbox-export --skip-attachments-larger-than 100M
```

### Export into an already used output directory

```bash
./run.sh /data/archive.ost ./mbox-export --allow-existing-output
```

### Overwrite existing mbox folder files

```bash
./run.sh /data/archive.ost ./mbox-export --overwrite --allow-existing-output
```

### Abort if any mbox folder output already exists (automation-friendly)

```bash
./run.sh /data/archive.ost ./mbox-export --fail-on-existing
```

### Export to Maildir as unread (appears as new mail)

```bash
./run.sh /data/archive.ost ./maildir-export --format maildir --maildir-state unread
```

## Interrupt Handling

The exporter uses two-stage `Ctrl-C` handling:

- first `Ctrl-C` requests a graceful shutdown
  - current message finishes
  - open files are flushed and closed
  - partial stats are printed
- second `Ctrl-C` forces immediate termination

## Statistics and Logging

The exporter prints:

- current folder being exported
- periodic progress updates inside large folders
- per-folder completion summaries
- final totals

Final statistics include counts such as:

- folders visited / exported / skipped / empty
- placeholder mbox files created
- total emails saved
- total bytes written
- message exceptions
- attachment exceptions
- write exceptions
- Maildir existing skips
- large attachments skipped
- attachment filename fallbacks
- interruption state
- elapsed time

## Notes on Large OST Files

PSTmortem is built for very large archives and has been tested on OST files **larger than 30 GB**.
Memory usage stays **low and roughly constant** regardless of the total file size, because:

- messages are processed and written **one at a time** (streamed, not buffered in bulk)
- mbox files are flushed and closed incrementally; Maildir messages are written individually
- per-message and per-subfolder objects are explicitly released during the tree walk
- the heavy `libpff`/`pypff` work runs in a separate child process

Practical notes:

- opening a multi-gigabyte OST can take a while before the first folder appears
- a single very large attachment is read into memory while it is written; use
  `--skip-attachments-larger-than` (e.g. `100M`) to cap that and avoid transient spikes
- using `--max-mails` / `--max-folders` is useful for quick partial runs before a full export

## Exit Behavior

- normal success exits with status `0`
- output directory already non-empty (without `--allow-existing-output`) exits with status `2`
- `--fail-on-existing` aborting on a pre-existing mbox folder file exits with status `3`
- other child process failures are propagated to the parent process
- graceful or forced interruption exits with status `130`

## Files in This Repository

- `pstmortem.py` — main exporter
- `run.sh` — helper script that sets up a venv and runs the exporter
- `requirements.txt` — Python dependency list

## Summary

Use `PSTmortem` when you need to extract large Outlook OST or PST archives into:

- Thunderbird Local Folders (`mbox`)
- Dovecot / IMAP migration targets (`maildir`)

with filtering, resumable Maildir exports, detailed stats, and safer handling for large archives.
