"""One-shot receiver pipeline.

The runtime is a near-line-for-line port of the production
``process_email_v2.py``:

* Connect to IMAP, ``search`` for messages with the configured subject.
* For each new message id, ``fetch (RFC822)`` and parse with
  :mod:`email` (so multipart bodies, Formspree HTML escaping, and
  non-UTF-8 transfer encodings all work).
* Extract the v4 envelope, RSA-OAEP + AES-256-GCM decrypt.
* Append to ``records.csv`` with the production column order and
  ``utf-8-sig`` encoding.

The hard-coded paths in the production file are replaced by a YAML
config (``config.yaml``). The state file is unchanged in spirit
(``last_msg_id`` + a bounded ``processed_ids`` list) but the file
path and JSON layout are config-driven.
"""

from __future__ import annotations

import csv
import email
import imaplib
import json
import logging
import ssl
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from src.config import AppConfig, load_config
from src.envelope import PROTOCOL_MARKERS, decrypt_email_body, extract_envelope
from src.seafile_upload import archive_files

log = logging.getLogger("pipeline")

CSV_FIELDS = ["timestamp", "glucose_value", "unit", "context", "note", "source"]


def _load_state(path: Path) -> dict[str, Any]:
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("state file corrupt; starting fresh at %s", path)
    return {"last_msg_id": 0, "processed_ids": []}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _read_private_key(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(
            f"private key not found at {path}. "
            "Run `python -m src.sender generate <out_dir>` first."
        )
    return path.read_bytes()


def _load_kid_secrets(path: Path) -> dict[str, str]:
    """Read ``{kid: secret}`` for MAC verification / logging.

    Missing file → empty registry (we still decrypt; the kid secret is
    not used cryptographically by the receiver, matching production).
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for kid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        sec = entry.get("secret")
        if isinstance(sec, str) and sec:
            out[kid] = sec
    return out


def _fetch(mail: imaplib.IMAP4_SSL, subject_prefix: str, since_days: int) -> list[tuple[int, str, email.message.Message]]:
    """Search for matching messages and return ``(seq, date, parsed)``.

    ``seq`` is the IMAP SEARCH sequence number (not a UID) — the same
    id the production file stores in ``last_msg_id``. The search is by
    subject prefix with a configurable ``SINCE`` window. Returning a
    parsed :class:`email.message.Message` keeps multipart handling in
    one place.
    """
    since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    status, data = mail.search(None, f'(SINCE {since} SUBJECT "{subject_prefix}")')
    if status != "OK":
        log.warning("IMAP search returned %s", status)
        return []
    ids = [int(x.decode()) for x in data[0].split()] if data[0] else []
    out: list[tuple[int, str, email.message.Message]] = []
    for uid in ids:
        status, fetched = mail.fetch(str(uid).encode(), "(RFC822)")
        if status != "OK" or not fetched or not fetched[0]:
            continue
        raw = fetched[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            msg = email.message_from_bytes(raw)
        except Exception as e:
            log.warning("email parse error for uid=%s: %s", uid, e)
            continue
        out.append((uid, msg.get("Date", ""), msg))
    return out


def _extract_body(msg: email.message.Message) -> str:
    """Walk a multipart message and return the first text/plain part,
    decoded as ``str``. Non-multipart messages are decoded directly.

    This is the literal production behaviour: only ``text/plain`` is
    accepted; ``text/html`` and other parts are ignored.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                try:
                    return payload.decode(part.get_content_charset() or "utf-8")
                except (UnicodeDecodeError, LookupError):
                    return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        # Some messages have no body at all; return the raw payload as
        # a last resort.
        return msg.get_payload() or ""
    try:
        return payload.decode(msg.get_content_charset() or "utf-8")
    except (UnicodeDecodeError, LookupError):
        return payload.decode("utf-8", errors="replace")


def _append_to_csv(csv_path: Path, record: dict[str, Any]) -> None:
    """Append one row to ``records.csv`` using the production column
    order. ``utf-8-sig`` is the production encoding.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = csv_path.is_file() and csv_path.stat().st_size > 0

    with csv_path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_FIELDS)

        ts = record.get("timestamp", record.get("ts", ""))
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(float(ts) / 1000).strftime("%Y-%m-%dT%H:%M")
        glucose = record.get("glucose_value", record.get("value", ""))
        unit = record.get("unit", "mmol/L")
        context = record.get("context", "未记录")
        note = record.get("note", "")
        writer.writerow(
            [ts, glucose, unit, context, note, "github-pages-secure-relay-form"]
        )
        log.info("Added: %s - %s %s (%s)", ts, glucose, unit, context)


def _regenerate_charts(cfg: AppConfig, csv_path: Path) -> list[Path]:
    """Run the charts script for the configured windows. Returns the
    list of PNGs produced (may be empty if no records or charts.py
    raises).
    """
    if not cfg.charts.windows:
        return []
    script = Path(__file__).resolve().parent / "charts.py"
    if not script.is_file():
        log.warning("charts.py not present; skipping chart generation")
        return []
    cfg.storage.charts_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for window in cfg.charts.windows:
        png = cfg.storage.charts_dir / f"records_{window}.png"
        try:
            subprocess.run(
                [sys.executable, str(script),
                 "--csv", str(csv_path),
                 "--out", str(png),
                 "--window", window,
                 "--unit", cfg.charts.metric_unit],
                check=True, capture_output=True, text=True, timeout=120,
            )
            out.append(png)
        except subprocess.CalledProcessError as e:
            log.warning("chart %s failed: rc=%s %s", window, e.returncode, e.stderr[-200:])
    return out


def process_one(body: str, csv_path: Path, private_key_pem: bytes,
                kid_secrets: dict[str, str]) -> bool:
    """Extract + decrypt + append. Returns True on success.

    Exposed at module level so tests can drive it without touching
    IMAP.
    """
    if not any(m in body for m in PROTOCOL_MARKERS):
        return False
    try:
        marker, env = extract_envelope(body)
    except ValueError as e:
        log.warning("envelope extraction failed: %s", e)
        return False
    try:
        record = decrypt_email_body(body, private_key_pem, kid_secrets)[0]
    except Exception as e:
        log.warning("decryption failed: %s", e)
        return False
    _append_to_csv(csv_path, record)
    return True


def run(cfg_path: str | None = None) -> int:
    cfg = load_config(cfg_path)
    log.info("pipeline start")

    try:
        password = cfg.imap.load_password()
    except FileNotFoundError as e:
        log.error(str(e))
        return 2

    priv_path = cfg.crypto.private_key_path
    try:
        private_key_pem = _read_private_key(priv_path)
    except FileNotFoundError as e:
        log.error(str(e))
        return 2
    kid_secrets = _load_kid_secrets(cfg.crypto.kid_secrets_path)

    state = _load_state(cfg.storage.state_path)
    processed_ids: list[int] = list(state.get("processed_ids", []))
    last_msg_id = int(state.get("last_msg_id", 0))

    ctx = ssl.create_default_context()
    try:
        mail = imaplib.IMAP4_SSL(cfg.imap.host, cfg.imap.port, ssl_context=ctx,
                                 timeout=30)
        mail.login(cfg.imap.username, password)
    except (imaplib.IMAP4.error, OSError) as e:
        log.error(
            "IMAP connect/login failed for %s@%s: %s — check imap.username, "
            "the app-password file, and your network.",
            cfg.imap.username, cfg.imap.host, e,
        )
        return 1
    try:
        mail.select("INBOX")
        candidates = _fetch(mail, cfg.imap.subject_prefix, cfg.imap.since_days)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    new_ids: list[int] = []
    for uid, date, msg in candidates:
        if uid <= last_msg_id or uid in processed_ids:
            continue
        body = _extract_body(msg)
        if process_one(body, cfg.storage.records_csv, private_key_pem, kid_secrets):
            new_ids.append(uid)
        processed_ids.append(uid)

    if new_ids:
        last_msg_id = max(last_msg_id, max(new_ids))
    # Keep the processed_ids list bounded like production (last 500).
    processed_ids = processed_ids[-500:]
    _save_state(cfg.storage.state_path, {
        "last_msg_id": last_msg_id,
        "processed_ids": processed_ids,
    })

    # Charts and archive only run if new records were appended.
    if new_ids:
        pngs = _regenerate_charts(cfg, cfg.storage.records_csv)
        if cfg.archive.backend == "seafile":
            files = [cfg.storage.records_csv] + pngs
            archive_files(cfg.archive, files)
        log.info("appended %d record(s)", len(new_ids))
    else:
        log.info("no new records")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
    )
    raise SystemExit(run())
