"""T3: the pipeline must dedupe by IMAP UID.

Two consecutive ``run`` invocations against the same mailbox must
each append at most one new row per new message. The state file
records ``last_msg_id`` and a bounded ``processed_ids`` list, exactly
like the production ``process_email_v2.py``.
"""

from __future__ import annotations

import csv
import email
import sys
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.envelope import decrypt_email_body  # noqa: E402
from src.sender import build, generate_keypair  # noqa: E402
from src import pipeline as pipeline_mod  # noqa: E402


def _make_msg(sender_addr: str, body: str, subject: str) -> bytes:
    m = EmailMessage()
    m["From"] = sender_addr
    m["To"] = "your-receiver@example.com"
    m["Subject"] = subject
    m["Date"] = "Fri, 29 Aug 2026 12:10:00 +0000"
    m.set_content(body)
    return m.as_bytes()


class FakeMail:
    """Minimal IMAP shim. ``uids`` is a list of (uid, body_bytes)
    pairs returned in ascending UID order, which is what the
    production Gmail server does.
    """
    def __init__(self, uids):
        self._uids = uids
    def select(self, *a, **kw): return ("OK", [b""])
    def login(self, *a, **kw): return ("OK", None)
    def search(self, *a, **kw):
        ids = b" ".join(str(uid).encode() for uid, _ in self._uids)
        return ("OK", [ids])
    def fetch(self, msg_id, what):
        uid = int(msg_id)
        for u, body in self._uids:
            if u == uid:
                return ("OK", [(b"", body)])
        return ("NO", [None])
    def logout(self): return ("BYE", None)


class FakeIMAP:
    def __init__(self, *a, **kw): self._m = FakeMail([])
    def set_inbox(self, uids): self._m = FakeMail(uids)
    def login(self, *a, **kw): return self._m.login(*a, **kw)
    def select(self, *a, **kw): return self._m.select(*a, **kw)
    def search(self, *a, **kw): return self._m.search(*a, **kw)
    def fetch(self, *a, **kw): return self._m.fetch(*a, **kw)
    def logout(self): return self._m.logout()


def main() -> int:
    work = ROOT / "tests" / "_t3_work"
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    priv, pub = generate_keypair(work / "keys")

    # Build one real v4 envelope, wrap it in a multipart message.
    record = {
        "timestamp": "2026-08-29T09:00",
        "glucose_value": 6.0,
        "unit": "mmol/L",
        "context": "空腹",
        "note": "T3 dedupe",
    }
    built = build(record, pub.read_bytes(), "t3-kid", "t3-secret")
    raw = _make_msg("sender@example.com", built.body, "[OpenClaw Secure Record]")

    # Build the smallest possible config that points at our workdir.
    cfg = work / "config.yaml"
    (work / "app-pw").write_text("dummy")
    (work / "kid").write_text("{}")
    cfg.write_text(f"""
imap:
  host: imap.example.com
  port: 993
  username: your-receiver@example.com
  app_password_file: "{work / 'app-pw'}"
  subject_prefix: "[OpenClaw Secure Record]"
  since_days: 30
crypto:
  private_key_path: "{priv}"
  kid_secrets_path: "{work / 'kid'}"
storage:
  records_csv: "{work / 'records.csv'}"
  charts_dir: "{work / 'charts'}"
  state_path: "{work / 'state.json'}"
  idle_state_path: "{work / 'idle.json'}"
charts:
  windows: []
  metric_unit: "mmol/L"
""")

    import os
    os.environ["SECURE_RECORD_CONFIG"] = str(cfg)
    loaded = load_config()
    assert loaded.imap.username == "your-receiver@example.com"

    csv_path = work / "records.csv"
    state_path = work / "state.json"
    fake = FakeIMAP()

    # First run: mailbox has 1 message with UID 100.
    fake.set_inbox([(100, raw)])
    with patch.object(pipeline_mod.imaplib, "IMAP4_SSL", lambda *a, **kw: fake):
        rc = pipeline_mod.run()
    assert rc == 0
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert len(rows) == 1, f"after 1st run expected 1 row, got {len(rows)}"
    state = __import__("json").loads(state_path.read_text())
    assert state["last_msg_id"] == 100, state
    print(f"OK 1st run: 1 row, last_msg_id={state['last_msg_id']}")

    # Second run: same mailbox, same UID. Must NOT add a row.
    fake.set_inbox([(100, raw)])
    with patch.object(pipeline_mod.imaplib, "IMAP4_SSL", lambda *a, **kw: fake):
        rc = pipeline_mod.run()
    assert rc == 0
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert len(rows) == 1, f"after 2nd run expected still 1 row, got {len(rows)}"
    print(f"OK 2nd run (same uid): still {len(rows)} row")

    # Third run: a brand-new message with UID 101 arrives. Must add 1 row.
    record2 = dict(record); record2["glucose_value"] = 7.7; record2["note"] = "T3 second record"
    built2 = build(record2, pub.read_bytes(), "t3-kid", "t3-secret")
    raw2 = _make_msg("sender@example.com", built2.body, "[OpenClaw Secure Record]")
    fake.set_inbox([(100, raw), (101, raw2)])
    with patch.object(pipeline_mod.imaplib, "IMAP4_SSL", lambda *a, **kw: fake):
        rc = pipeline_mod.run()
    assert rc == 0
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    assert len(rows) == 2, f"after new uid expected 2 rows, got {len(rows)}"
    state = __import__("json").loads(state_path.read_text())
    assert state["last_msg_id"] == 101, state
    print(f"OK 3rd run (new uid 101): 2 rows, last_msg_id={state['last_msg_id']}")

    print("\nT3 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
