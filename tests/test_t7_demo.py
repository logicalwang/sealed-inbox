"""T7: demo mode — throwaway workspace builds and serves with fake data.

Run with:  python3 -m tests.test_t7_demo
"""

from __future__ import annotations

import http.client
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.dashboard import make_server  # noqa: E402
from src.demo import build_demo_workspace  # noqa: E402


def main() -> int:
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="sealed-t7-"))
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    cfg, access_key = build_demo_workspace(work)

    # Workspace contents
    import csv
    with cfg.storage.records_csv.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    check("fake readings generated", len(rows) >= 30,
          f"{len(rows)} rows, values {rows[0]['glucose_value']}…")
    check("charts pre-rendered",
          all((cfg.storage.charts_dir / f"records_{w}.png").stat().st_size > 1000
              for w in cfg.charts.windows),
          f"windows={cfg.charts.windows}")
    check("keys + kid registry + access key exist",
          (work / "keys" / "record_decrypt_private.pem").is_file()
          and (work / "kid_secrets.json").is_file()
          and cfg.dashboard.access_key_file.is_file())
    check("demo CSV does not pollute the repo",
          not (ROOT / "data" / "records.csv").exists()
          or "demo-mode" not in (ROOT / "data" / "records.csv").read_text())

    # Server: login page → POST → main page with a reading
    cfg.dashboard.port = 0
    cfg.dashboard.bind = "127.0.0.1"
    server = make_server(cfg, access_key)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        r = conn.getresponse(); body = r.read()
        check("unauth → login page", r.status == 200 and "访问密码".encode() in body)
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/login", body=f"pw={access_key}".encode(),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = conn.getresponse(); r.read()
        cookie = (r.getheader("Set-Cookie") or "").split("sealed_session=", 1)[1].split(";", 1)[0]
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/", headers={"Cookie": f"sealed_session={cookie}"})
        r = conn.getresponse(); body = r.read().decode()
        check("auth main page shows demo readings",
              r.status == 200 and "监测面板" in body and "mmol/L" in body)
        conn.close()
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print(f"\nT7 FAIL: {failures}")
        return 1
    print("\nT7 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
