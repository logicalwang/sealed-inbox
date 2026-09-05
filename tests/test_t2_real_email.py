"""T2: a real-shaped MIME message reaches the receiver.

The sender in this test:

* uses the v4 wire format (``OPENCLAW_SECURE_RECORD_V1`` + pretty JSON)
* delivers it inside a ``multipart/alternative`` email (Formspree style)
* HTML-escapes the JSON inside the ``text/plain`` part — as Formspree
  actually does
* includes a Chinese ``context`` value

The receiver must:

* parse the message with :mod:`email` (not raw decode)
* walk to the ``text/plain`` part
* ``html.unescape`` the JSON
* decrypt and append a row to ``records.csv``
"""

from __future__ import annotations

import csv
import email
import sys
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.envelope import decrypt_email_body  # noqa: E402
from src.sender import build, generate_keypair  # noqa: E402
from src.pipeline import _extract_body, _append_to_csv  # noqa: E402


def main() -> int:
    work = ROOT / "tests" / "_t2_work"
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    priv, pub = generate_keypair(work / "keys")

    record = {
        "timestamp": "2026-08-29T12:10",
        "glucose_value": 8.2,
        "unit": "mmol/L",
        "context": "餐后2h",
        "note": "T2 multipart test",
    }
    built = build(record, pub.read_bytes(), "t2-kid", "t2-secret")

    # Formspree-style multipart: an HTML part with the JSON escaped
    # inside, and a text/plain part with the same JSON. The receiver
    # only reads text/plain.
    inner_json_html = (
        "<p>以下为加密记录（请勿修改）:</p>\n"
        f"<pre>{built.body.replace(chr(10), '<br/>').replace('<', '&lt;').replace('>', '&gt;')}</pre>"
    )

    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "your-receiver@example.com"
    msg["Subject"] = "[OpenClaw Secure Record]"
    msg["Date"] = "Fri, 29 Aug 2026 12:10:00 +0000"
    msg.set_content(built.body)  # text/plain
    msg.add_alternative(inner_json_html, subtype="html")  # text/html

    raw_bytes = msg.as_bytes()
    parsed = email.message_from_bytes(raw_bytes)
    assert parsed.is_multipart(), "expected multipart message"
    body = _extract_body(parsed)
    assert "OPENCLAW_SECURE_RECORD_V1" in body
    assert "餐后2h" not in body, "text/plain part should NOT include the HTML part"

    csv_path = work / "records.csv"
    if csv_path.exists():
        csv_path.unlink()
    _append_to_csv(csv_path, decrypt_email_body(body, priv.read_bytes())[0])

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    r = rows[0]
    assert r["timestamp"] == "2026-08-29T12:10"
    assert r["glucose_value"] == "8.2"
    assert r["unit"] == "mmol/L"
    assert r["context"] == "餐后2h"
    assert r["note"] == "T2 multipart test"
    assert r["source"] == "github-pages-secure-relay-form"
    print(f"OK csv row: {r}")

    # Also exercise html.unescape on a body where the JSON was
    # accidentally HTML-escaped (Formspree's reality).
    escaped = built.body.replace("<", "&lt;").replace(">", "&gt;")
    body2 = f"From: ...\n\nOPENCLAW_SECURE_RECORD_V1\n{escaped}\n"
    mine, _ = decrypt_email_body(body2, priv.read_bytes())
    assert mine == record, f"unescape path returned {mine!r}"
    print("OK HTML-escaped JSON also decodes back to the original record")

    print("\nT2 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
