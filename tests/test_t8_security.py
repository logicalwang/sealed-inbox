"""T8: pipeline security controls.

  1. CSV formula injection: a note like "=HYPERLINK(...)" is written
     quote-prefixed so Excel/LibreOffice can't execute it.
  2. crypto.require_valid_mac: records from an unknown kid or with a
     tampered mac are rejected; a genuinely signed record passes.
  3. crypto.max_age_hours: stale records are rejected; fresh ones pass;
     default 0 keeps production behaviour.

Run with:  python3 -m tests.test_t8_security
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.pipeline import CSV_FIELDS, process_one  # noqa: E402
from src.sender import build, generate_keypair  # noqa: E402

KID = "sec-kid"
SECRET = "S" * 43


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="sealed-t8-"))
    priv, pub = generate_keypair(work / "keys")
    kids = {KID: {"secret": SECRET, "enabled": True}}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    def csv_rows(path: Path) -> list[dict]:
        with path.open(encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    # ── 1. CSV formula injection ────────────────────────────
    csv1 = work / "injection.csv"
    nasty = {"timestamp": "2026-09-05T12:00", "glucose_value": 6.1,
             "unit": "mmol/L", "context": "空腹",
             "note": '=HYPERLINK("http://evil.test","click")'}
    body = build(nasty, pub.read_bytes(), KID, SECRET).body
    ok = process_one(body, csv1, priv.read_bytes(), kids)
    note_cell = csv_rows(csv1)[0]["note"]
    check("CSV formula injection neutralised",
          ok and note_cell.startswith("'="),
          f"note cell = {note_cell!r}")

    # ── 2. strict MAC authentication ────────────────────────
    csv2 = work / "mac.csv"
    rec = {"timestamp": "2026-09-05T09:25", "glucose_value": 5.4,
           "unit": "mmol/L", "context": "空腹", "note": ""}
    good = build(rec, pub.read_bytes(), KID, SECRET)

    ok = process_one(good.body, csv2, priv.read_bytes(), kids,
                     require_valid_mac=True)
    check("strict MAC: genuinely signed record accepted", ok)

    tampered = dict(good.envelope)
    tampered["mac"] = "AAAA" + good.envelope["mac"][4:]
    bad_body = "OPENCLAW_SECURE_RECORD_V1\n" + json.dumps(tampered, indent=2)
    ok = process_one(bad_body, csv2, priv.read_bytes(), kids,
                     require_valid_mac=True)
    check("strict MAC: tampered mac rejected", not ok)

    ok = process_one(bad_body, csv2, priv.read_bytes(), kids,
                     require_valid_mac=False)
    check("strict MAC off (default): same record accepted (production parity)", ok)

    stranger = build(rec, pub.read_bytes(), "unknown-kid", SECRET)
    ok = process_one(stranger.body, csv2, priv.read_bytes(), kids,
                     require_valid_mac=True)
    check("strict MAC: unknown kid rejected", not ok)

    # ── 3. freshness window ─────────────────────────────────
    csv3 = work / "fresh.csv"
    stale_env = dict(good.envelope)
    stale_env["ts"] = int(time.time() * 1000) - 8 * 3600 * 1000   # 8 h old
    stale_body = "OPENCLAW_SECURE_RECORD_V1\n" + json.dumps(stale_env, indent=2)

    ok = process_one(stale_body, csv3, priv.read_bytes(), kids,
                     max_age_hours=0.0)
    check("freshness off (default): 8 h-old record accepted (production parity)", ok)

    ok = process_one(stale_body, csv3, priv.read_bytes(), kids,
                     max_age_hours=4.0)
    check("freshness on: 8 h-old record rejected with max_age_hours=4", not ok)

    fresh_env = dict(good.envelope)
    fresh_env["ts"] = int(time.time() * 1000) - 2 * 3600 * 1000   # 2 h old
    fresh_body = "OPENCLAW_SECURE_RECORD_V1\n" + json.dumps(fresh_env, indent=2)
    ok = process_one(fresh_body, csv3, priv.read_bytes(), kids,
                     max_age_hours=4.0)
    check("freshness on: 2 h-old record accepted with max_age_hours=4", ok)

    if failures:
        print(f"\nT8 FAIL: {failures}")
        return 1
    print("\nT8 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
