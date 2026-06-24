import os
import sys
import argparse
import pypff
import re
import socket
import multiprocessing
import traceback
import hashlib
import mailbox
import mimetypes
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import format_datetime, parsedate_to_datetime
import signal


class ExistingOutputError(Exception):
    """Raised in --fail-on-existing mode when a pre-existing mbox folder file is found."""


def format_mbox_from(message, message_dt=None):
    """Create a valid From_ line for mbox format.

    ``message_dt`` should be the message's own UTC datetime (as returned by
    ``message_datetime``) so the From_ separator reflects when the mail was sent
    rather than when the export ran. This keeps exports deterministic and
    preserves chronological ordering in mbox readers.
    """
    # mbox spec requires an email address in the From_ line, not a display name.
    # Try to extract from transport headers first, fall back to nobody@localhost.
    sender_email = "nobody@localhost"
    try:
        headers = message.get_transport_headers()
        if headers:
            if isinstance(headers, bytes):
                headers = headers.decode("utf-8", errors="replace")
            # Extract the first email address from the From: header.
            # The host part may be a single label (e.g. user@localhost) or a
            # dotted FQDN; do not require a dotted TLD so intranet/Exchange
            # addresses are preserved instead of falling back to nobody@localhost.
            from_match = re.search(
                r"From:.*?([\w.+-]+@[\w.-]+)", headers, re.IGNORECASE
            )
            if from_match:
                # Trim any trailing dot the loose host pattern may have captured.
                sender_email = from_match.group(1).rstrip(".")
    except Exception:
        pass

    # Sanitise: mbox From_ line must not contain spaces
    sender_email = re.sub(r"\s+", "", sender_email)
    if not sender_email:
        sender_email = "nobody@localhost"

    # Use the message's own date when available so reruns are reproducible.
    # Fall back to the current time only when the message date is unknown.
    if isinstance(message_dt, datetime):
        if message_dt.tzinfo is None:
            message_dt = message_dt.replace(tzinfo=timezone.utc)
        date_source = message_dt
    else:
        date_source = datetime.now(timezone.utc)

    # mbox syntax requires exactly: From user@host.com Fri Jun 23 12:00:00 2026
    date_str = date_source.strftime("%a %b %d %H:%M:%S %Y")
    return f"From {sender_email} {date_str}\n"


def format_bytes(size):
    """Convert bytes to a human-readable format."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:3.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def print_fidelity_warning():
    """Warn users that exports are reconstructed from pypff fields, not raw MIME."""
    print("=" * 72)
    print("MIME FIDELITY NOTICE")
    print(
        "This exporter reconstructs messages from OST headers, bodies, and attachments."
    )
    print("The installed pypff exposes plain/html/rtf bodies, but no raw MIME message.")
    print("Inline images, original multipart nesting, calendar invites, signatures,")
    print(
        "encrypted content, or TNEF/winmail.dat details may not be perfectly preserved."
    )
    print("Use --suppress-fidelity-warning to hide this notice.")
    print("=" * 72)


def parse_size(value):
    """Parse byte sizes like 500M, 2G, or 1048576."""
    if value is None:
        return None
    value = str(value).strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGT]?B?|)", value, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(
            "size must be a number with an optional K, M, G, or T suffix"
        )

    number = float(match.group(1))
    suffix = match.group(2).upper().rstrip("B")
    multiplier = {
        "": 1,
        "K": 1024,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
    }[suffix]
    return int(number * multiplier)


def parse_date_boundary(value, is_end=False):
    """Parse YYYY-MM-DD into a UTC datetime boundary."""
    if value is None:
        return None
    try:
        boundary = datetime.strptime(str(value).strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "date must be in YYYY-MM-DD format"
        ) from exc
    if is_end:
        boundary += timedelta(days=1)
    return boundary


def get_message_value(message, *names):
    """Read the first available pypff message value without trusting API consistency."""
    for name in names:
        try:
            value = getattr(message, name, None)
            if callable(value):
                value = value()
            if value:
                return value
        except Exception:
            continue
    return None


def format_message_date(value):
    """Format a pypff datetime-like value for an RFC 5322 Date header."""
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return format_datetime(value)
        return str(value)
    except Exception:
        return None


def message_datetime(email_msg, message):
    """Return a message datetime normalized to UTC, or None if unavailable."""
    date_header = email_msg.get("Date")
    if date_header:
        try:
            parsed = parsedate_to_datetime(str(date_header))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            pass

    fallback_value = get_message_value(
        message,
        "get_client_submit_time",
        "client_submit_time",
        "get_delivery_time",
        "delivery_time",
        "get_creation_time",
        "creation_time",
    )
    if fallback_value is None:
        return None

    try:
        if isinstance(fallback_value, datetime):
            if fallback_value.tzinfo is None:
                fallback_value = fallback_value.replace(tzinfo=timezone.utc)
            return fallback_value.astimezone(timezone.utc)
        parsed = parsedate_to_datetime(str(fallback_value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def message_matches_date_filter(
    message_dt,
    start_date=None,
    end_date=None,
    exclude_unknown_date=False,
):
    """Return (included, date_unknown) for the configured date filter."""
    if start_date is None and end_date is None:
        return True, False
    if message_dt is None:
        return (not exclude_unknown_date), True
    if start_date is not None and message_dt < start_date:
        return False, False
    if end_date is not None and message_dt >= end_date:
        return False, False
    return True, False


def sanitize_maildir_component(value, fallback="unknown", max_length=120):
    """Create a filesystem-safe Maildir filename component."""
    if value is None:
        value = fallback
    value = str(value).strip().strip("<>")
    value = re.sub(r"[^A-Za-z0-9_.@+-]+", "_", value)
    value = value.strip("._-")
    if not value:
        value = fallback
    return value[:max_length]


def sanitize_attachment_filename(value, fallback):
    """Create a safe MIME attachment filename without directory components."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value or "").replace("\x00", "").strip()
    if not value:
        value = fallback
    value = os.path.basename(value.replace("\\", "/"))
    value = re.sub(r'[\r\n\t<>:"/\\|?*]+', "_", value).strip(" ._")
    return value or fallback


def attachment_record_entry_type(entry):
    """Return possible MAPI property identifiers for a pypff record entry."""
    try:
        entry_type = entry.get_entry_type()
    except Exception:
        return set()
    candidates = {entry_type}
    if entry_type > 0xFFFF:
        candidates.add(entry_type >> 16)
        candidates.add(entry_type & 0xFFFF)
    return candidates


def get_attachment_filename(attachment, index):
    """Best-effort extraction of an attachment filename from pypff metadata."""
    fallback = f"attachment_{index}"

    for name in (
        "get_long_filename",
        "get_filename",
        "get_name",
        "get_display_name",
        "long_filename",
        "filename",
        "name",
        "display_name",
    ):
        try:
            value = getattr(attachment, name, None)
            if callable(value):
                value = value()
            if value:
                return sanitize_attachment_filename(value, fallback), False
        except Exception:
            continue

    # Common MAPI property identifiers used for attachment filenames/names.
    filename_entry_types = {
        0x3001,  # PR_DISPLAY_NAME
        0x3704,  # PR_ATTACH_FILENAME
        0x3707,  # PR_ATTACH_LONG_FILENAME
    }

    try:
        num_record_sets = attachment.get_number_of_record_sets()
    except Exception:
        num_record_sets = 0

    for record_set_index in range(num_record_sets):
        try:
            record_set = attachment.get_record_set(record_set_index)
            num_entries = record_set.get_number_of_entries()
        except Exception:
            continue

        for entry_index in range(num_entries):
            try:
                entry = record_set.get_entry(entry_index)
                if not (attachment_record_entry_type(entry) & filename_entry_types):
                    continue
                value = entry.get_data_as_string()
                if value:
                    return sanitize_attachment_filename(value, fallback), False
            except Exception:
                continue

    return fallback, True


def guess_attachment_mimetype(filename):
    """Guess (maintype, subtype) from a filename, defaulting to octet-stream.

    pypff does not reliably expose the original MIME type, so we infer it from
    the attachment filename extension. Unknown or extension-less names fall back
    to application/octet-stream so they remain valid binary attachments.

    Attachment payloads from pypff are always raw bytes. ``EmailMessage.
    add_attachment`` requires a ``str`` payload for ``text/*`` parts, so we map
    text types to ``application/<subtype>`` (e.g. text/plain -> application/...)
    to keep the bytes path valid while still recording the original subtype.
    """
    guessed_type, _encoding = mimetypes.guess_type(filename or "")
    if not guessed_type or "/" not in guessed_type:
        return "application", "octet-stream"
    maintype, _, subtype = guessed_type.partition("/")
    if not maintype or not subtype:
        return "application", "octet-stream"
    if maintype == "text":
        # Preserve the subtype but keep a binary-safe maintype for bytes payloads.
        return "application", subtype
    return maintype, subtype


def maildir_timestamp(email_msg):
    """Return a stable timestamp for the Maildir unique name."""
    date_header = email_msg.get("Date")
    if not date_header:
        return "0"
    try:
        parsed_date = parsedate_to_datetime(str(date_header))
        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)
        return str(int(parsed_date.timestamp()))
    except Exception:
        return "0"


def make_maildir_basename(email_msg, folder_path, message_index):
    """Build the deterministic Maildir *base name* (without an info/flag suffix).

    The base name is what makes reruns idempotent. Maildir info suffixes such as
    ``:2,S`` are appended separately (see ``make_maildir_filename``) because a
    MUA may rewrite the suffix and move the file between ``new/`` and ``cur/``;
    dedup therefore matches on this flag-stripped base name only.
    """
    message_id = email_msg.get("Message-ID")
    if message_id:
        visible_id = sanitize_maildir_component(message_id, fallback="message-id")
    else:
        visible_id = "no-message-id"

    identity_parts = [
        "maildir-message",
        str(message_id or ""),
        str(email_msg.get("Subject", "")),
        str(email_msg.get("From", "")),
        str(email_msg.get("Date", "")),
        str(folder_path or ""),
        str(message_index if message_index is not None else ""),
    ]

    identity = "\0".join(identity_parts).encode("utf-8", errors="replace")
    stable_hash = hashlib.sha256(identity).hexdigest()[:24]
    hostname = sanitize_maildir_component(
        socket.gethostname(), fallback="localhost", max_length=80
    )
    return f"{maildir_timestamp(email_msg)}.{visible_id}.{stable_hash}.{hostname}"


def make_maildir_filename(email_msg, folder_path, message_index, maildir_state="read"):
    """Build a full Maildir filename for the requested state.

    ``read``   -> ``cur/`` file with a ``:2,S`` (Seen) info suffix.
    ``unread`` -> ``new/`` file with NO info suffix (required by the Maildir spec).
    """
    base = make_maildir_basename(email_msg, folder_path, message_index)
    if maildir_state == "unread":
        return base
    return f"{base}:2,S"


def maildir_basename_of(filename):
    """Strip a Maildir info suffix (``:2,<flags>`` or ``:1,<...>``) from a filename."""
    return filename.split(":", 1)[0]


def maildir_message_already_exported(dest_path, base_name):
    """Return True if a message with this base name already exists in new/ or cur/.

    Matches on the flag-stripped base name so a message a MUA has read (moving it
    from ``new/`` to ``cur/`` and appending ``:2,S``) is still recognised on
    rerun and not duplicated.
    """
    for subdir in ("new", "cur"):
        dir_path = os.path.join(dest_path, subdir)
        try:
            entries = os.scandir(dir_path)
        except FileNotFoundError:
            continue
        except Exception:
            continue
        with entries:
            for entry in entries:
                if maildir_basename_of(entry.name) == base_name:
                    return True
    return False


def process_message(
    message,
    dest_path,
    fmt="mbox",
    folder_path=None,
    message_index=None,
    mbox_obj=None,
    max_attachment_size=None,
    start_date=None,
    end_date=None,
    exclude_unknown_date=False,
    maildir_state="read",
):
    """
    Append a single pypff message to an mbox file directly, or save it to a Maildir.
    File handles are opened/closed in append mode to conserve steady state memory.
    """
    att_errors = 0
    attachment_filename_fallbacks = 0
    large_attachments_skipped = 0
    large_attachment_bytes_skipped = 0
    date_filtered = 0
    date_unknown = 0

    # --- Phase 1: Parse the message from pypff into a Python email object ---
    try:
        headers = message.get_transport_headers()
        if isinstance(headers, str):
            headers = headers.encode("utf-8", errors="replace")

        if headers:
            email_msg = message_from_bytes(headers, policy=default)
        else:
            email_msg = EmailMessage()
            subject = (
                get_message_value(message, "get_subject", "subject") or "(No subject)"
            )
            sender = (
                get_message_value(
                    message,
                    "get_sender_email_address",
                    "sender_email_address",
                    "get_sender_name",
                    "sender_name",
                )
                or "unknown@localhost"
            )
            recipient = get_message_value(
                message,
                "get_display_to",
                "display_to",
                "get_received_by_name",
                "received_by_name",
            )
            submit_time = get_message_value(
                message,
                "get_client_submit_time",
                "client_submit_time",
                "get_delivery_time",
                "delivery_time",
                "get_creation_time",
                "creation_time",
            )

            email_msg["Subject"] = str(subject)
            email_msg["From"] = str(sender)
            if recipient:
                email_msg["To"] = str(recipient)
            formatted_date = format_message_date(submit_time)
            if formatted_date:
                email_msg["Date"] = formatted_date

        plain_text = message.get_plain_text_body()
        html_text = message.get_html_body()
        rtf_text = get_message_value(message, "get_rtf_body", "rtf_body")

        # Preserve the raw RTF bytes so an RTF-only message can keep a faithful
        # copy as a body.rtf attachment (pypff exposes RTF markup, not a body any
        # mail client renders inline).
        rtf_raw_bytes = None
        if rtf_text:
            rtf_raw_bytes = (
                rtf_text
                if isinstance(rtf_text, bytes)
                else str(rtf_text).encode("utf-8", errors="replace")
            )

        # Some pypff versions return bytes, some return str
        if plain_text and isinstance(plain_text, bytes):
            plain_text = plain_text.decode("utf-8", errors="replace")
        if html_text and isinstance(html_text, bytes):
            html_text = html_text.decode("utf-8", errors="replace")
        if rtf_text and isinstance(rtf_text, bytes):
            rtf_text = rtf_text.decode("utf-8", errors="replace")

        # Reset payload from headers that may contain malformed body parts.
        email_msg.set_payload([])
        # We MUST delete existing Content-Type headers before setting new content
        # otherwise Python's email library will crash throwing "set_content not valid on multipart"
        for h in ["Content-Type", "MIME-Version", "Content-Transfer-Encoding"]:
            if h in email_msg:
                del email_msg[h]

        # When the only available body is RTF, store it as an attached body.rtf
        # rather than rendering raw RTF control words inline (which no client
        # reads as a body). The inline body becomes a short readable placeholder.
        attach_rtf_body = False
        if plain_text:
            email_msg.set_content(plain_text)
            if html_text:
                email_msg.add_alternative(html_text, subtype="html")
        elif html_text:
            email_msg.set_content(html_text, subtype="html")
        elif rtf_raw_bytes:
            email_msg.set_content(
                "This message had no plain-text or HTML body. Its original "
                "Rich Text (RTF) body is preserved as the attached 'body.rtf'."
            )
            attach_rtf_body = True
        else:
            email_msg.set_content("")

        message_dt = message_datetime(email_msg, message)
        include_message, unknown_date = message_matches_date_filter(
            message_dt,
            start_date=start_date,
            end_date=end_date,
            exclude_unknown_date=exclude_unknown_date,
        )
        if unknown_date:
            date_unknown = 1
        if not include_message:
            date_filtered = 1
            return (
                0,
                0,
                att_errors,
                0,
                0,
                0,
                large_attachments_skipped,
                large_attachment_bytes_skipped,
                attachment_filename_fallbacks,
                date_filtered,
                date_unknown,
            )

        # Preserve an RTF-only body as a body.rtf attachment (see body handling above).
        if attach_rtf_body and rtf_raw_bytes:
            try:
                email_msg.add_attachment(
                    rtf_raw_bytes,
                    maintype="application",
                    subtype="rtf",
                    filename="body.rtf",
                )
            except Exception:
                # Never let body.rtf preservation abort the whole message export.
                att_errors += 1

        # Add attachments if any exist
        try:
            num_attachments = message.get_number_of_attachments()
        except Exception:
            # If the OST B-Tree descriptor is invalid/corrupt for attachments on this message,
            # we just assume 0 attachments and proceed to save the body text anyway!
            num_attachments = 0

        for i in range(num_attachments):
            try:
                attachment = message.get_attachment(i)
                if attachment:
                    filename, used_filename_fallback = get_attachment_filename(
                        attachment, i
                    )
                    if used_filename_fallback:
                        attachment_filename_fallbacks += 1
                    attachment_size = attachment.get_size()
                    if (
                        max_attachment_size is not None
                        and attachment_size > max_attachment_size
                    ):
                        large_attachments_skipped += 1
                        large_attachment_bytes_skipped += attachment_size
                        del attachment
                        continue

                    att_data = attachment.read_buffer(attachment_size)
                    if att_data:
                        maintype, subtype = guess_attachment_mimetype(filename)
                        email_msg.add_attachment(
                            att_data,
                            maintype=maintype,
                            subtype=subtype,
                            filename=filename,
                        )
                    del attachment
            except Exception:
                att_errors += 1

    except Exception as e:
        traceback.print_exc()
        return (
            0,
            1,
            att_errors,
            0,
            0,
            0,
            large_attachments_skipped,
            large_attachment_bytes_skipped,
            attachment_filename_fallbacks,
            date_filtered,
            date_unknown,
        )

    # --- Phase 2: Write the constructed email object to disk ---
    try:
        written_bytes = 0
        if fmt == "mbox":
            email_msg.set_unixfrom(format_mbox_from(message, message_dt).rstrip("\n"))
            if mbox_obj is not None:
                mbox_obj.add(email_msg)
            else:
                before_size = (
                    os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                )
                mbox = mailbox.mbox(dest_path, create=True)
                try:
                    mbox.add(email_msg)
                    mbox.flush()
                finally:
                    mbox.close()
                after_size = os.path.getsize(dest_path)
                written_bytes += after_size - before_size
        elif fmt == "maildir":
            base_name = make_maildir_basename(
                email_msg, folder_path, message_index
            )
            # Idempotent rerun: skip if this message already exists in new/ or
            # cur/ regardless of any MUA-applied flag suffix or new/->cur/ move.
            if maildir_message_already_exported(dest_path, base_name):
                return (
                    0,
                    0,
                    att_errors,
                    1,
                    0,
                    0,
                    large_attachments_skipped,
                    large_attachment_bytes_skipped,
                    attachment_filename_fallbacks,
                    date_filtered,
                    date_unknown,
                )
            # read   -> cur/ with :2,S (Seen); unread -> new/ with no suffix.
            subdir = "new" if maildir_state == "unread" else "cur"
            filename = make_maildir_filename(
                email_msg, folder_path, message_index, maildir_state
            )
            file_path = os.path.join(dest_path, subdir, filename)
            msg_bytes = email_msg.as_bytes(policy=default)
            with open(file_path, "wb") as f:
                f.write(msg_bytes)
            written_bytes += len(msg_bytes)

        return (
            written_bytes,
            0,
            att_errors,
            0,
            0,
            1,
            large_attachments_skipped,
            large_attachment_bytes_skipped,
            attachment_filename_fallbacks,
            date_filtered,
            date_unknown,
        )
    except Exception as e:
        print(f"\n[WRITE ERROR] Failed to write message to {dest_path}: {e}")
        traceback.print_exc()
        return (
            0,
            0,
            att_errors,
            0,
            1,
            0,
            large_attachments_skipped,
            large_attachment_bytes_skipped,
            attachment_filename_fallbacks,
            date_filtered,
            date_unknown,
        )


def ensure_mbox_parent_structure(base_out_dir, path_so_far, global_stats):
    """Lazily create the Thunderbird `.sbd` ancestor chain for an exported folder.

    Thunderbird expects every parent of an exported folder to exist as both a
    mbox file (`Parent`) and a `.sbd` directory (`Parent.sbd/`). We only create
    this structure on demand - i.e. right before a folder actually writes its
    messages - so that excluded or fully-filtered subtrees never produce stray
    placeholder files.
    """
    if not path_so_far:
        return
    tb_dir = base_out_dir
    for ancestor in path_so_far:
        os.makedirs(tb_dir, exist_ok=True)
        ancestor_mbox = os.path.join(tb_dir, ancestor)
        if not os.path.exists(ancestor_mbox):
            open(ancestor_mbox, "wb").close()
            global_stats["folders_placeholder_created"] += 1
        tb_dir = os.path.join(tb_dir, f"{ancestor}.sbd")


def process_folder(
    folder,
    base_out_dir,
    include_regex,
    exclude_regex,
    fmt="mbox",
    path_so_far=None,
    global_stats=None,
    max_folders=None,
    max_mails=None,
    overwrite=False,
    match_leaf_folder_only=False,
    max_attachment_size=None,
    shutdown_event=None,
    start_date=None,
    end_date=None,
    exclude_unknown_date=False,
    fail_on_existing=False,
    maildir_state="read",
):
    if path_so_far is None:
        path_so_far = []
    if global_stats is None:
        global_stats = {
            "messages": 0,
            "bytes": 0,
            "exceptions": 0,
            "att_exceptions": 0,
            "folders_exported": 0,
            "folders_visited": 0,
            "folders_empty": 0,
            "folders_skipped": 0,
            "folders_existing_skipped": 0,
            "messages_existing_skipped": 0,
            "maildir_existing_skipped": 0,
            "write_exceptions": 0,
            "large_attachments_skipped": 0,
            "large_attachment_bytes_skipped": 0,
            "attachment_filename_fallbacks": 0,
            "folders_placeholder_created": 0,
            "date_filtered": 0,
            "date_unknown": 0,
            "interrupted": False,
        }

    if shutdown_event is not None and shutdown_event.is_set():
        global_stats["interrupted"] = True
        return global_stats

    if max_folders is not None and global_stats["folders_exported"] >= max_folders:
        return global_stats
    if max_mails is not None and global_stats["messages"] >= max_mails:
        return global_stats

    # Get folder name, default to "Root" if empty (common for absolute root)
    try:
        folder_name = folder.get_name()
    except Exception:
        folder_name = None
    if not folder_name:
        folder_name = "Root"

    # Sanitize folder name so it can be a clean filesystem path
    safe_folder_name = re.sub(r'[\\/*?:"<>|]', "_", folder_name)
    current_path_list = path_so_far + [safe_folder_name]
    current_path_str = "/".join(current_path_list)

    # ------------------
    # Filtering Logic
    # ------------------
    process_this = True

    match_target = folder_name if match_leaf_folder_only else current_path_str

    # Order: first match include list if set, then also check exclude list
    if include_regex:
        if not include_regex.search(match_target):
            process_this = False

    if process_this and exclude_regex:
        if exclude_regex.search(match_target):
            process_this = False

    num_messages = folder.get_number_of_sub_messages()
    try:
        num_folders = folder.get_number_of_sub_folders()
    except Exception as e:
        print(f"\n[ERROR] Exception reading subfolder count of {current_path_str}: {e}")
        traceback.print_exc()
        num_folders = 0

    global_stats["folders_visited"] += 1

    if num_messages == 0:
        global_stats["folders_empty"] += 1

    # Compute the mbox destination path WITHOUT touching the filesystem. The
    # actual `.sbd` directories and any placeholder files are created lazily,
    # only when a folder is genuinely exported, so excluded or fully-filtered
    # subtrees never leave stray placeholder files behind.
    mbox_dest_path = None
    if fmt == "mbox":
        tb_dir = base_out_dir
        for p in path_so_far:
            tb_dir = os.path.join(tb_dir, f"{p}.sbd")
        mbox_dest_path = os.path.join(tb_dir, safe_folder_name)

    if num_messages > 0:
        if process_this:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] EXTRACTING folder: "
                f"{current_path_str} ({num_messages} messages)"
            )

            skip_current_folder_export = False

            if fmt == "mbox":
                dest_path = mbox_dest_path

                # Lazily create the Thunderbird `.sbd` ancestor chain now that we
                # know this folder is actually being exported.
                ensure_mbox_parent_structure(
                    base_out_dir, path_so_far, global_stats
                )
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)

                if os.path.exists(dest_path) and not overwrite:
                    if fail_on_existing:
                        raise ExistingOutputError(
                            f"Pre-existing mbox output file found for folder "
                            f"'{current_path_str}': {dest_path}. Aborting because "
                            "--fail-on-existing is set. Re-run with --overwrite to "
                            "re-export, or remove the file to resume."
                        )
                    global_stats["folders_existing_skipped"] += 1
                    global_stats["messages_existing_skipped"] += num_messages
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] SKIPPING folder "
                        f"'{current_path_str}' ({num_messages} messages): output file "
                        f"already exists at {dest_path}."
                    )
                    print(
                        "             NOT verified as complete - if a previous run was "
                        "interrupted this folder may be PARTIAL. Re-run with --overwrite "
                        "to re-export it."
                    )
                    skip_current_folder_export = True

                if not skip_current_folder_export:
                    # Make sure we start with a clean file
                    with open(dest_path, "wb") as f:
                        pass
            elif fmt == "maildir":
                # Determine directory path for Dovecot Maildir hierarchical structure
                # Dovecot uses `.FolderName.SubFolderName`
                folder_dot_path = ".".join(current_path_list)
                dest_path = os.path.join(base_out_dir, f".{folder_dot_path}")

                os.makedirs(os.path.join(dest_path, "cur"), exist_ok=True)
                os.makedirs(os.path.join(dest_path, "new"), exist_ok=True)
                os.makedirs(os.path.join(dest_path, "tmp"), exist_ok=True)

            if not skip_current_folder_export:
                folder_bytes = 0
                folder_msgs = 0
                folder_exceptions = 0
                folder_att_exceptions = 0
                folder_maildir_existing_skipped = 0
                folder_write_exceptions = 0
                folder_large_attachments_skipped = 0
                folder_large_attachment_bytes_skipped = 0
                folder_attachment_filename_fallbacks = 0
                folder_date_filtered = 0
                folder_date_unknown = 0
                mbox_obj = None
                mbox_start_size = 0

                if fmt == "mbox":
                    mbox_start_size = (
                        os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                    )
                    mbox_obj = mailbox.mbox(dest_path, create=True)

                def record_mbox_write_error(action, error):
                    nonlocal folder_write_exceptions
                    print(
                        f"\n[WRITE ERROR] Failed to {action} mbox "
                        f"{dest_path}: {error}"
                    )
                    traceback.print_exc()
                    folder_write_exceptions += 1
                    global_stats["write_exceptions"] += 1

                try:
                    # Iterate through messages
                    for i in range(num_messages):
                        if shutdown_event is not None and shutdown_event.is_set():
                            global_stats["interrupted"] = True
                            print(
                                f"    - Graceful shutdown requested. Stopping "
                                f"after current folder state: {current_path_str}"
                            )
                            break

                        if (
                            max_mails is not None
                            and global_stats["messages"] >= max_mails
                        ):
                            print(
                                f"    - Reached --max-mails limit ({max_mails}). "
                                "Stopping message extraction for this folder."
                            )
                            break

                        msg = folder.get_sub_message(i)
                        if msg:
                            (
                                b_written,
                                has_error,
                                att_errs,
                                maildir_existing_skip,
                                write_error,
                                wrote_message,
                                large_att_skips,
                                large_att_bytes,
                                filename_fallbacks,
                                message_date_filtered,
                                message_date_unknown,
                            ) = process_message(
                                msg,
                                dest_path,
                                fmt,
                                current_path_str,
                                i,
                                mbox_obj,
                                max_attachment_size,
                                start_date,
                                end_date,
                                exclude_unknown_date,
                                maildir_state,
                            )
                            if message_date_filtered:
                                folder_date_filtered += 1
                                global_stats["date_filtered"] += 1

                            if message_date_unknown:
                                folder_date_unknown += 1
                                global_stats["date_unknown"] += 1

                            if maildir_existing_skip:
                                folder_maildir_existing_skipped += 1
                                global_stats["maildir_existing_skipped"] += 1

                            if large_att_skips > 0:
                                folder_large_attachments_skipped += large_att_skips
                                folder_large_attachment_bytes_skipped += large_att_bytes
                                global_stats[
                                    "large_attachments_skipped"
                                ] += large_att_skips
                                global_stats[
                                    "large_attachment_bytes_skipped"
                                ] += large_att_bytes

                            if filename_fallbacks > 0:
                                folder_attachment_filename_fallbacks += (
                                    filename_fallbacks
                                )
                                global_stats[
                                    "attachment_filename_fallbacks"
                                ] += filename_fallbacks

                            if write_error:
                                folder_write_exceptions += 1
                                global_stats["write_exceptions"] += 1

                            if att_errs > 0:
                                folder_att_exceptions += att_errs
                                global_stats["att_exceptions"] += att_errs

                            if has_error:
                                folder_exceptions += 1
                                global_stats["exceptions"] += 1
                            elif wrote_message:
                                folder_msgs += 1
                                global_stats["messages"] += 1
                                if fmt != "mbox":
                                    folder_bytes += b_written
                                    global_stats["bytes"] += b_written

                            # Delete explicitly to keep memory stable on huge exports.
                            del msg

                        if (i + 1) % 1000 == 0:
                            if mbox_obj is not None:
                                try:
                                    mbox_obj.flush()
                                except Exception as e:
                                    record_mbox_write_error("flush", e)
                            progress_pct = ((i + 1) / num_messages) * 100
                            print(
                                f"    - Processed {i + 1}/{num_messages} "
                                f"({progress_pct:.1f}%)..."
                            )
                finally:
                    if mbox_obj is not None:
                        try:
                            mbox_obj.flush()
                        except Exception as e:
                            record_mbox_write_error("flush", e)
                        try:
                            mbox_obj.close()
                        except Exception as e:
                            record_mbox_write_error("close", e)

                if fmt == "mbox":
                    folder_bytes = os.path.getsize(dest_path) - mbox_start_size
                    global_stats["bytes"] += folder_bytes

                exc_str = []
                if folder_exceptions > 0:
                    exc_str.append(f"{folder_exceptions} msg exceptions")
                if folder_att_exceptions > 0:
                    exc_str.append(f"{folder_att_exceptions} att exceptions")
                if folder_maildir_existing_skipped > 0:
                    exc_str.append(
                        f"{folder_maildir_existing_skipped} existing maildir skips"
                    )
                if folder_write_exceptions > 0:
                    exc_str.append(f"{folder_write_exceptions} write exceptions")
                if folder_large_attachments_skipped > 0:
                    exc_str.append(
                        f"{folder_large_attachments_skipped} large attachments "
                        f"skipped ({format_bytes(folder_large_attachment_bytes_skipped)})"
                    )
                if folder_attachment_filename_fallbacks > 0:
                    exc_str.append(
                        f"{folder_attachment_filename_fallbacks} attachment filename fallbacks"
                    )
                if folder_date_filtered > 0:
                    exc_str.append(f"{folder_date_filtered} date-filtered")
                if folder_date_unknown > 0:
                    exc_str.append(f"{folder_date_unknown} unknown-date")
                exc_suffix = f", {', '.join(exc_str)}" if exc_str else ""

                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] FINISHED folder: "
                    f"{current_path_str} - Extracted {folder_msgs} messages, "
                    f"{format_bytes(folder_bytes)}{exc_suffix}"
                )
                # Only count as exported if at least one message was successfully written
                if folder_msgs > 0:
                    global_stats["folders_exported"] += 1
        else:
            global_stats["folders_skipped"] += 1
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] SKIPPING filtered "
                f"folder: {current_path_str}"
            )

    # Process subfolders regardless so we don't accidentally skip a matched
    # subfolder sitting inside an excluded parent folder.
    try:
        for i in range(num_folders):
            if shutdown_event is not None and shutdown_event.is_set():
                global_stats["interrupted"] = True
                break

            sub_folder = folder.get_sub_folder(i)
            if sub_folder:
                process_folder(
                    sub_folder,
                    base_out_dir,
                    include_regex,
                    exclude_regex,
                    fmt,
                    current_path_list,
                    global_stats,
                    max_folders,
                    max_mails,
                    overwrite,
                    match_leaf_folder_only,
                    max_attachment_size,
                    shutdown_event,
                    start_date,
                    end_date,
                    exclude_unknown_date,
                    fail_on_existing,
                    maildir_state,
                )
                # Important memory relief for OST tree walking
                del sub_folder

                if (
                    max_folders is not None
                    and global_stats["folders_exported"] >= max_folders
                ):
                    break
                if max_mails is not None and global_stats["messages"] >= max_mails:
                    break
                if shutdown_event is not None and shutdown_event.is_set():
                    global_stats["interrupted"] = True
                    break
    except ExistingOutputError:
        # --fail-on-existing: propagate to abort the whole extraction.
        raise
    except Exception as e:
        print(f"\n[ERROR] Exception reading subfolders of {current_path_str}: {e}")
        traceback.print_exc()

    return global_stats


def run_extraction(
    archive_file_path,
    out_dir,
    include_re,
    exclude_re,
    fmt,
    max_folders,
    max_mails,
    overwrite,
    match_leaf_folder_only,
    max_attachment_size,
    shutdown_event,
    start_date,
    end_date,
    exclude_unknown_date,
    fail_on_existing,
    maildir_state,
):
    """
    Worker function to run the actual extraction. Runs in a separate process.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    ost = pypff.file()
    try:
        ost.open(archive_file_path)
    except Exception as e:
        print(f"Failed to open archive file: {e}")
        sys.exit(1)

    start_time = datetime.now()

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Starting extraction. "
        "Large files >30GB will take time."
    )
    try:
        root_folder = ost.get_root_folder()
        final_stats = process_folder(
            root_folder,
            out_dir,
            include_re,
            exclude_re,
            fmt,
            max_folders=max_folders,
            max_mails=max_mails,
            overwrite=overwrite,
            match_leaf_folder_only=match_leaf_folder_only,
            max_attachment_size=max_attachment_size,
            shutdown_event=shutdown_event,
            start_date=start_date,
            end_date=end_date,
            exclude_unknown_date=exclude_unknown_date,
            fail_on_existing=fail_on_existing,
            maildir_state=maildir_state,
        )
    except ExistingOutputError as e:
        print(f"\n[ABORTED] {e}")
        ost.close()
        sys.exit(3)
    except Exception as e:
        print(f"\n[ERROR] Exceptional failure during global tree extraction: {e}")
        traceback.print_exc()
        final_stats = {"messages": 0, "bytes": 0, "exceptions": 1}

    ost.close()

    print(
        f"\n[{datetime.now().strftime('%H:%M:%S')}] Extraction complete. Files are in '{out_dir}'."
    )
    print("=" * 45)
    print("      EXTRACTION STATISTICS")
    print("=" * 45)
    print(f"Folders Visited        : {final_stats.get('folders_visited', 0):,}")
    print(f"Folders Exported       : {final_stats.get('folders_exported', 0):,}")
    print(f"Folders Empty          : {final_stats.get('folders_empty', 0):,}")
    print(f"Folders Skipped        : {final_stats.get('folders_skipped', 0):,}")
    print(
        f"Folders Existing Skip  : {final_stats.get('folders_existing_skipped', 0):,}"
    )
    print(
        f"Emails Existing Skip   : {final_stats.get('messages_existing_skipped', 0):,}"
    )
    print(
        f"Maildir Existing Skip  : {final_stats.get('maildir_existing_skipped', 0):,}"
    )
    print(
        "Mbox Placeholders     : "
        f"{final_stats.get('folders_placeholder_created', 0):,}"
    )
    print(f"Total Emails Saved     : {final_stats['messages']:,}")
    print(f"Total Bytes Written    : {format_bytes(final_stats['bytes'])}")
    print(f"Msg Exceptions Handled : {final_stats['exceptions']:,}")
    print(f"Att Exceptions Handled : {final_stats.get('att_exceptions', 0):,}")
    print(f"Write Exceptions       : {final_stats.get('write_exceptions', 0):,}")
    print(f"Date Filtered          : {final_stats.get('date_filtered', 0):,}")
    print(f"Date Unknown           : {final_stats.get('date_unknown', 0):,}")
    print(f"Interrupted            : {'yes' if final_stats.get('interrupted') else 'no'}")
    print(
        f"Large Att Skipped      : {final_stats.get('large_attachments_skipped', 0):,}"
    )
    print(
        "Large Att Bytes Skip   : "
        f"{format_bytes(final_stats.get('large_attachment_bytes_skipped', 0))}"
    )
    print(
        f"Att Name Fallbacks     : {final_stats.get('attachment_filename_fallbacks', 0):,}"
    )
    print(f"Elapsed Time           : {datetime.now() - start_time}")
    print("=" * 45)
    print()

    existing_skipped = final_stats.get("folders_existing_skipped", 0)
    if existing_skipped > 0:
        existing_msgs_skipped = final_stats.get("messages_existing_skipped", 0)
        print("!" * 72)
        print("NOTE: PRE-EXISTING OUTPUT WAS SKIPPED")
        print(
            f"  {existing_skipped:,} folder(s) already had an mbox output file and were "
            f"SKIPPED\n  ({existing_msgs_skipped:,} message(s) were NOT re-checked)."
        )
        print(
            "  These folders were NOT verified as complete. If a previous run was\n"
            "  interrupted, any of them may be PARTIAL."
        )
        print(
            "  To re-export them, re-run with --overwrite (or delete the specific\n"
            "  mbox files you want regenerated)."
        )
        print("!" * 72)
        print()

    if fmt == "mbox":
        print(
            "To use with Thunderbird: copy the contents of the output directory "
            "directly into your local profile's 'Mail/Local Folders' directory."
        )
    elif fmt == "maildir":
        print(
            "To use with Dovecot: sync the contents of the output directory to "
            "the user's Maildir location (e.g. /var/mail/vhosts/domain/user/Maildir/)."
        )


def main():
    shutdown_event = multiprocessing.Event()
    interrupt_count = {"count": 0}
    process = None

    # First Ctrl-C requests graceful shutdown. Second Ctrl-C terminates the child.
    def parent_interrupt_handler(signum, frame):
        interrupt_count["count"] += 1

        if interrupt_count["count"] == 1:
            print(
                "\nGraceful shutdown requested. Finishing the current mail, "
                "closing files, and exiting. Press Ctrl-C again to force stop."
            )
            shutdown_event.set()
            return

        print("\nSecond interrupt received. Terminating extraction process now...")
        if process is not None and process.is_alive():
            process.terminate()
            process.join()
        sys.exit(130)

    signal.signal(signal.SIGINT, parent_interrupt_handler)

    parser = argparse.ArgumentParser(
        description=(
            "PSTmortem - Extract Microsoft Outlook OST and PST files to Thunderbird MBOX or Maildir formats "
            "efficiently."
        )
    )
    parser.add_argument("archive_file", help="Path to the source Outlook data file (.ost or .pst)")
    parser.add_argument("out_dir", help="Output directory to place exported files")
    parser.add_argument(
        "--include", help="Regex to include folders by name", default=None
    )
    parser.add_argument(
        "--exclude", help="Regex to exclude folders by name", default=None
    )
    parser.add_argument(
        "--format",
        help="Export format: mbox or maildir",
        choices=["mbox", "maildir"],
        default="mbox",
    )
    parser.add_argument(
        "--maildir-state",
        help=(
            "For --format maildir, whether exported messages are marked read or "
            "unread. 'read' (default) writes to cur/ with a :2,S (Seen) flag; "
            "'unread' writes to new/ with no flag suffix (appears as new mail). "
            "Has no effect on mbox exports."
        ),
        choices=["read", "unread"],
        default="read",
    )
    parser.add_argument(
        "--max-folders",
        help="Stop after successfully exporting N folders",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--max-mails",
        help="Stop after successfully exporting N emails globally",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--overwrite",
        help="Overwrite existing output files instead of skipping them",
        action="store_true",
    )
    parser.add_argument(
        "--fail-on-existing",
        help=(
            "Abort immediately if a pre-existing mbox folder output file is found, "
            "instead of skipping it. Useful for automated runs that must not silently "
            "skip folders. Cannot be combined with --overwrite."
        ),
        action="store_true",
    )
    parser.add_argument(
        "--allow-existing-output",
        help="Allow exporting into an output directory that is not empty",
        action="store_true",
    )
    parser.add_argument(
        "--match-leaf-folder-only",
        help=(
            "Apply include/exclude regexes only to the current folder name "
            "instead of the full folder path"
        ),
        action="store_true",
    )
    parser.add_argument(
        "--skip-attachments-larger-than",
        help="Skip attachments larger than SIZE before reading them into memory, e.g. 100M or 2G",
        type=parse_size,
        default=None,
    )
    parser.add_argument(
        "--suppress-fidelity-warning",
        help="Hide the warning about reconstructed MIME fidelity limitations",
        action="store_true",
    )
    parser.add_argument(
        "--start-date",
        help="Only export messages on or after YYYY-MM-DD",
        type=parse_date_boundary,
        default=None,
    )
    parser.add_argument(
        "--end-date",
        help="Only export messages before the day after YYYY-MM-DD",
        type=lambda value: parse_date_boundary(value, is_end=True),
        default=None,
    )
    parser.add_argument(
        "--exclude-unknown-date",
        help="Skip messages whose date cannot be determined when a date filter is active",
        action="store_true",
    )

    args = parser.parse_args()

    if (
        args.start_date is not None
        and args.end_date is not None
        and args.start_date >= args.end_date
    ):
        print("start-date must be before or equal to end-date")
        sys.exit(1)

    if args.overwrite and args.fail_on_existing:
        print("--overwrite and --fail-on-existing cannot be used together.")
        sys.exit(1)

    if args.fail_on_existing and args.format != "mbox":
        print("--fail-on-existing only applies to --format mbox.")
        sys.exit(1)

    include_re = re.compile(args.include, re.IGNORECASE) if args.include else None
    exclude_re = re.compile(args.exclude, re.IGNORECASE) if args.exclude else None

    if os.path.exists(args.out_dir) and not os.path.isdir(args.out_dir):
        print(f"Output path exists but is not a directory: {args.out_dir}")
        sys.exit(1)

    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)

    try:
        out_dir_has_entries = any(os.scandir(args.out_dir))
    except Exception as e:
        print(f"Failed to inspect output directory '{args.out_dir}': {e}")
        sys.exit(1)

    if out_dir_has_entries and not args.allow_existing_output:
        print(f"Output directory is not empty: {args.out_dir}")
        print("Refusing to continue to avoid mixing or overwriting exports.")
        print("Re-run with --allow-existing-output if this is intentional.")
        sys.exit(2)

    try:
        ost_stat = os.stat(args.archive_file)
        ost_size_str = format_bytes(ost_stat.st_size)
    except Exception:
        ost_size_str = "Unknown"

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Opening {args.archive_file} "
        f"(Size: {ost_size_str})..."
    )
    if not args.suppress_fidelity_warning:
        print_fidelity_warning()
    if args.skip_attachments_larger_than is not None:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Skipping attachments larger "
            f"than {format_bytes(args.skip_attachments_larger_than)}."
        )
    if args.start_date is not None or args.end_date is not None:
        start_label = (
            args.start_date.strftime("%Y-%m-%d") if args.start_date is not None else "-inf"
        )
        end_label = (
            (args.end_date - timedelta(days=1)).strftime("%Y-%m-%d")
            if args.end_date is not None
            else "+inf"
        )
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] Date filter active: "
            f"{start_label} .. {end_label}"
        )
        if args.exclude_unknown_date:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Messages with unknown dates will be skipped."
            )

    # Run the heavy C-bindings code in a child process so this parent process
    # remains unblocked and can instantly respond to CTRL-C.
    process = multiprocessing.Process(
        target=run_extraction,
        args=(
            args.archive_file,
            args.out_dir,
            include_re,
            exclude_re,
            args.format,
            args.max_folders,
            args.max_mails,
            args.overwrite,
            args.match_leaf_folder_only,
            args.skip_attachments_larger_than,
            shutdown_event,
            args.start_date,
            args.end_date,
            args.exclude_unknown_date,
            args.fail_on_existing,
            args.maildir_state,
        ),
    )

    process.start()

    try:
        # Wait for the child process. The timeout gives the Python interpreter
        # time to catch the SIGINT hook repeatedly, preventing deadlocks.
        while process.is_alive():
            process.join(timeout=0.1)
    except SystemExit:
        # Catch our own sys.exit from the signal handler and violently
        # terminate the child process executing the C-library bindings.
        process.terminate()
        process.join()
        sys.exit(130)

    if process.exitcode and process.exitcode != 0:
        sys.exit(process.exitcode)

    if shutdown_event.is_set():
        sys.exit(130)


if __name__ == "__main__":
    main()
