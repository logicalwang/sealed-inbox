"""T9: Telegram notification on new records.

  1. notifications.telegram enabled + new records → the Telegram API is
     called with the configured chat id and a body containing the values.
  2. no new records → no call.
  3. disabled → no call (even with new records).

Run with:  python3 -m tests.test_t9_notify
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.sender import build, generate_keypair  # noqa: E402


def _instance_cfg(work: Path, priv: Path, kids: Path, *,
                  tg_enabled: bool) -> Path:
    token_file = work / "bot-token"
    token_file.write_text("123456:FAKE-TOKEN\n")
    cfg = work / "config.yaml"
    cfg.write_text(f"""
imap:
  host: h
  port: 993
  username: u@example.com
  app_password_file: "{work / 'pw'}"
  subject_prefix: "[OpenClaw Secure Record]"
crypto:
  private_key_path: "{priv}"
  kid_secrets_path: "{kids}"
storage:
  records_csv: "{work / 'records.csv'}"
  charts_dir: "{work / 'charts'}"
  state_path: "{work / 'state.json'}"
  idle_state_path: "{work / 'idle.json'}"
archive:
  backend: "local"
notifications:
  telegram:
    enabled: {'true' if tg_enabled else 'false'}
    chat_id: "-1001234567890"
    bot_token_file: "{token_file}"
charts:
  windows: []
""")
    (work / "pw").write_text("x")
    return cfg


def main() -> int:
    import csv
    from unittest.mock import patch as mock_patch
    from src import pipeline as P

    work = Path(tempfile.mkdtemp(prefix="sealed-t9-"))
    priv, pub = generate_keypair(work / "keys")
    kids = work / "kids.json"
    kids.write_text(json.dumps({"tg-kid": {"secret": "S" * 43, "enabled": True}}))
    (work / "pw").write_text("x")

    rec = {"timestamp": "2026-09-06T09:25", "glucose_value": 6.1,
           "unit": "mmol/L", "context": "空腹", "note": "t9"}
    body = build(rec, pub.read_bytes(), "tg-kid", "S" * 43).body

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    class FakeResp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def run_once(cfg_path: Path, bodies: list[bytes], calls: list):
        class FakeIMAP:
            def __init__(self, *a, **k): pass
            def login(self, *a, **k): return ("OK", None)
            def select(self, *a, **k): return ("OK", [b""])
            def search(self, *a, **k):
                return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(bodies)))])
            def fetch(self, i, w): return ("OK", [(b"", bodies[int(i) - 1])])
            def logout(self): return ("BYE", None)
        with mock_patch.object(P.urllib.request, "urlopen",
                               lambda req, timeout=20: calls.append(req) or FakeResp(b"{}")), \
             mock_patch.object(P.imaplib, "IMAP4_SSL", FakeIMAP):
            rc = P.run(str(cfg_path))
        assert rc == 0, f"pipeline rc={rc}"
        return rc

    import os
    os.environ.pop("SECURE_RECORD_CONFIG", None)

    # 1. enabled + 1 new record → TG called
    cfg = _instance_cfg(work, priv, kids, tg_enabled=True)
    calls: list = []
    run_once(cfg, [body.encode()], calls)
    ok = len(calls) == 1
    detail = ""
    if ok:
        req = calls[0]
        import urllib.parse
        full = req.full_url + "|" + urllib.parse.unquote(req.data.decode())
        ok = ("api.telegram.org" in full and "123456" in full
              and "-1001234567890" in full and "6.1" in full and "空腹" in full)
        detail = f"calls={len(calls)}, 含 chat_id/数值: {ok}"
    check("启用通知 + 新记录 → TG 调用携带 chat_id 与内容", ok, detail)

    # 2. same mailbox again → no new records → no call
    calls.clear()
    run_once(cfg, [body.encode()], calls)
    check("重复投递 → 不再调用 TG", len(calls) == 0, f"calls={len(calls)}")

    # 3. new record but notifications disabled → no call
    cfg_off = _instance_cfg(work, priv, kids, tg_enabled=False)
    cfg_off.write_text(cfg_off.read_text().replace("enabled: true", "enabled: false"))
    (work / "state.json").unlink()          # 重置去重状态, 让 uid 重新算"新"
    calls.clear()
    body2 = build({**rec, "timestamp": "2026-09-06T10:25", "glucose_value": 7.2},
                  pub.read_bytes(), "tg-kid", "S" * 43).body
    run_once(cfg_off, [body2.encode()], calls)
    check("禁用通知 → 不调用", len(calls) == 0, f"calls={len(calls)}")

    if failures:
        print(f"\nT9 FAIL: {failures}")
        return 1
    print("\nT9 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
