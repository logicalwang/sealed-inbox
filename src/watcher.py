"""Gmail IMAP IDLE watcher — port of the production
``gmail_idle_watcher.py``.

Key differences from the production version:

* All hard-coded paths (IMAP host, password file, subject, state file,
  log file, pipeline command, URL watch stuff) come from
  ``config.yaml``.
* The Cloudflare-tunnel URL watcher and the Telegram push are NOT
  included. They are deployment-specific and don't belong in a
  reusable library.
* The IMAP IDLE implementation is line-for-line equivalent: the
  production code uses Python's stdlib ``imaplib`` but does NOT call
  its (non-existent) ``mail.idle()`` method. It hand-rolls the
  IMAP IDLE protocol with ``_new_tag`` + ``send`` + ``readline`` +
  ``select`` + ``DONE``. This port does exactly the same.
"""

from __future__ import annotations

import json
import logging
import re
import select
import signal
import ssl
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import imaplib

from src.config import load_config

log = logging.getLogger("watcher")

# Gmail's IMAP IDLE has a server-side timeout of 29 minutes; we
# reconnect every 25 to stay safely below it. These are the production
# numbers.
IDLE_TIMEOUT = 25 * 60
COOLDOWN = 1
BACKOFF_START = 5
BACKOFF_MAX = 300

# RFC3501 IMAP response tag pattern, used to match the "+ idling"
# continuation line that the server sends after a successful IDLE.
_IDLING_RE = re.compile(rb"\+ ?idling", re.IGNORECASE)


def _load_state(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {"last_uid": 0, "total_processed": 0, "started": None}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _connect(host: str, port: int, username: str, password: str) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    mail = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=30)
    mail.login(username, password)
    mail.select("INBOX")
    return mail


def _search_new(mail: imaplib.IMAP4_SSL, subject_prefix: str, last_uid: int) -> list[int]:
    """IMAP search for messages matching the subject prefix whose
    SEARCH sequence number is strictly greater than ``last_uid``.

    Identical to the production implementation: a single ``SEARCH``
    with the subject literal, then a Python-side numeric filter. The
    ids are SEARCH sequence numbers, not UIDs — good enough for
    dedup because the mailbox is only appended to.
    """
    status, data = mail.search(None, f'(SUBJECT "{subject_prefix}")')
    if status != "OK" or not data[0].strip():
        return []
    return [int(uid) for uid in data[0].split() if int(uid) > last_uid]


def _idle_loop(mail: imaplib.IMAP4_SSL, sock_fd: int, timeout: int) -> str:
    """Hand-rolled IMAP IDLE — equivalent to the production code.

    Returns ``'notification'`` if the server reported new mail during
    the window, or ``'timeout'`` if the window elapsed with no
    activity. Raises :class:`imaplib.IMAP4.abort` if IDLE couldn't be
    started.
    """
    tag = mail._new_tag()
    mail.send(tag + b" IDLE\r\n")

    # The server replies with a continuation line "+ idling" before
    # sending unsolicited notifications. We must wait for that before
    # entering the read loop.
    resp = mail.readline()
    if not _IDLING_RE.search(resp):
        raise imaplib.IMAP4.abort(f"IDLE start failed: {resp!r}")

    # Block on the socket until either there's data (notification) or
    # the timeout elapses. This is the same ``select.select`` pattern
    # the production watcher uses; the timeout is 25 minutes so we
    # proactively reconnect before Gmail drops us.
    ready = select.select([sock_fd], [], [], timeout)
    if ready[0]:
        # Drain the notification line. The exact bytes don't matter
        # because we re-search after IDLE ends.
        try:
            mail.read(4096)
        except (OSError, ssl.SSLError) as e:
            raise imaplib.IMAP4.abort(f"idle read failed: {e}") from e
        # Exit IDLE.
        mail.send(b"DONE\r\n")
        try:
            mail.readline()
        except Exception:
            pass
        return "notification"

    # Timeout path: tell the server we're done so it can release the
    # mailbox, then return.
    try:
        mail.send(b"DONE\r\n")
        mail.readline()
    except Exception:
        pass
    return "timeout"


def run(cfg_path: str | None = None) -> int:
    cfg = load_config(cfg_path)
    log.info("watcher start; imap=%s user=%s", cfg.imap.host, cfg.imap.username)
    state = _load_state(cfg.storage.idle_state_path)
    state["started"] = datetime.now().isoformat()

    stopping = False

    def _stop(_sig, _frame):
        nonlocal stopping
        stopping = True
        log.info("signal received; stopping")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    backoff = BACKOFF_START
    while not stopping:
        mail = None
        try:
            password = cfg.imap.load_password()
            mail = _connect(cfg.imap.host, cfg.imap.port, cfg.imap.username, password)
            sock_fd = mail.socket().fileno()
            backoff = BACKOFF_START
            log.info("connected (fd=%d)", sock_fd)

            # Initial sweep: pick up anything queued before IDLE began.
            queued = _search_new(mail, cfg.imap.subject_prefix, state.get("last_uid", 0))
            if queued:
                log.info("found %d queued email(s) before IDLE", len(queued))
                subprocess.run([sys.executable, "-m", "src.pipeline"],
                               check=False, timeout=300)
                state["last_uid"] = max(queued)
                state["total_processed"] = state.get("total_processed", 0) + len(queued)
                _save_state(cfg.storage.idle_state_path, state)

            while not stopping:
                result = _idle_loop(mail, sock_fd, IDLE_TIMEOUT)
                if result == "notification":
                    log.info("IDLE notification received")
                    new_uids = _search_new(mail, cfg.imap.subject_prefix, state.get("last_uid", 0))
                    if new_uids:
                        log.info("processing %d new email(s)", len(new_uids))
                        subprocess.run([sys.executable, "-m", "src.pipeline"],
                                       check=False, timeout=300)
                        state["last_uid"] = max(new_uids)
                        state["total_processed"] = state.get("total_processed", 0) + len(new_uids)
                        _save_state(cfg.storage.idle_state_path, state)
                    else:
                        log.info("spurious notification; no matching email")
                    time.sleep(COOLDOWN)
                else:
                    log.info("IDLE timeout (%ds); reconnecting", IDLE_TIMEOUT)
                    break
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
            log.warning("connection error: %s: %s", type(e).__name__, e)
        except Exception as e:
            log.error("unexpected error: %s: %s", type(e).__name__, e)
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass
        if stopping:
            break
        log.info("reconnecting in %ds", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)
    _save_state(cfg.storage.idle_state_path, state)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
    )
    raise SystemExit(run())
