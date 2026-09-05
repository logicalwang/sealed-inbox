"""T6: dashboard — auth flow, session cookie, chart sandbox.

Runs the real HTTP server (ThreadingHTTPServer on an ephemeral port,
in a background thread) and exercises it with raw http.client:

  1. unauthenticated GET /        → login page
  2. wrong password (POST)        → no session
  3. correct password (POST)      → 302 + HttpOnly session cookie
  4. password in a URL (?pw=)     → rejected by design
  5. authenticated GET /          → readings rendered
  6. GET /records_24h.png         → chart served
  7. GET /../../evil.png          → 404 (path traversal confined)
  8. GET /api/status              → JSON with counts
  9. tampered session cookie      → back to login page

Run with:  python3 -m tests.test_t6_dashboard
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.dashboard import make_server  # noqa: E402
from src.sender import generate_keypair  # noqa: E402

KEY = "test-access-key-123"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="sealed-t6-"))
    priv, _pub = generate_keypair(work / "keys")
    csv_path = work / "records.csv"
    csv_path.write_text(
        "timestamp,glucose_value,unit,context,note,source\r\n"
        "2026-08-29T09:25,5.4,mmol/L,空腹,t6,github-pages-secure-relay-form\r\n"
        "2026-08-29T12:10,8.2,mmol/L,餐后2h,t6,github-pages-secure-relay-form\r\n",
        encoding="utf-8-sig")
    charts_dir = work / "charts"
    charts_dir.mkdir()
    (charts_dir / "records_24h.png").write_bytes(b"\x89PNG fake-bytes")
    # A file OUTSIDE charts_dir that the old traversal bug could reach.
    outside = work / "evil.png"
    outside.write_bytes(b"should never be served")
    (work / "access-key").write_text(KEY + "\n")

    cfg_path = work / "config.yaml"
    cfg_path.write_text(f"""
imap:
  host: h
  port: 993
  username: u@example.com
  app_password_file: "{work / 'pw'}"
  subject_prefix: "[OpenClaw Secure Record]"
crypto:
  private_key_path: "{priv}"
  kid_secrets_path: "{work / 'kids.json'}"
storage:
  records_csv: "{csv_path}"
  charts_dir: "{charts_dir}"
  state_path: "{work / 'state.json'}"
  idle_state_path: "{work / 'idle.json'}"
charts:
  windows: ["24h"]
dashboard:
  bind: "127.0.0.1"
  port: 0
  access_key_file: "{work / 'access-key'}"
  low: 3.9
  high: 7.0
  watcher_process_pattern: "definitely-not-running"
""")
    (work / "pw").write_text("x")

    cfg = load_config(cfg_path)
    server = make_server(cfg, KEY)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    def request(method: str, path: str, body: bytes | None = None,
                cookie: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if cookie:
            headers["Cookie"] = f"sealed_session={cookie}"
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        set_cookie = resp.getheader("Set-Cookie") or ""
        conn.close()
        return resp.status, data, set_cookie

    try:
        # 1. unauthenticated → login page
        st, body, _ = request("GET", "/")
        check("unauth GET / → login page", st == 200 and "访问密码" in body.decode())

        # 2a. wrong password via POST
        st, body, sc = request("POST", "/login", b"pw=wrong-key")
        check("wrong POST /login → no session", st == 200 and "密码不对" in body.decode() and "sealed_session=" not in sc)

        # 2b. password in a URL is rejected by design (never in history/logs)
        st, _, sc = request("GET", "/?pw=wrong-key")
        check("wrong GET ?pw= → no session", st == 200 and "sealed_session=" not in sc)

        # 3. correct password via POST → session cookie
        st, _, sc = request("POST", "/login", f"pw={KEY}".encode())
        cookie = sc.split("sealed_session=", 1)[1].split(";", 1)[0] if "sealed_session=" in sc else ""
        check("POST /login → 302 + cookie", st == 302 and bool(cookie) and "HttpOnly" in sc and "SameSite=Lax" in sc)

        # 2c. even the CORRECT password in a URL is rejected by design
        st, _, sc2 = request("GET", f"/?pw={KEY}")
        check("GET ?pw=<correct> → rejected, no session", st == 200 and "sealed_session=" not in sc2)

        # 4. authenticated main page shows the readings
        st, body, _ = request("GET", "/", cookie=cookie)
        text = body.decode()
        check("auth GET / → readings rendered",
              st == 200 and "5.4" in text and "8.2" in text and "餐后2h" in text and "records_24h.png" in text)

        # 5. chart served from charts_dir
        st, body, _ = request("GET", "/records_24h.png", cookie=cookie)
        check("chart PNG served", st == 200 and body == b"\x89PNG fake-bytes")

        # 6. path traversal confined to charts_dir
        st, _, _ = request("GET", "/../evil.png", cookie=cookie)
        st2, _, _ = request("GET", "/..%2fevil.png", cookie=cookie)
        st3, _, _ = request("GET", f"/../../{work.name}/evil.png", cookie=cookie)
        check("path traversal → 404", st == 404 and st2 in (404, 200) and st3 == 404,
              f"({st}, {st2}, {st3})")

        # 7. status API
        st, body, _ = request("GET", "/api/status", cookie=cookie)
        data = json.loads(body)
        check("api/status", st == 200 and data["readings_count"] == 2 and "watcher" in data)

        # 8. tampered session cookie rejected
        st, body, _ = request("GET", "/", cookie="1756000000.deadbeef")
        check("tampered cookie → login page", st == 200 and "访问密码" in body.decode())

        # 9. hardening headers + no server fingerprint
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        r = conn.getresponse(); r.read()
        headers = {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        check("security headers present",
              headers.get("x-content-type-options") == "nosniff"
              and headers.get("x-frame-options") == "DENY"
              and headers.get("referrer-policy") == "no-referrer")
        check("server banner suppressed", "python" not in headers.get("server", "").lower())

        # 10. oversized login body → 413 (memory-DoS guard)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/login", body=b"pw=x" + b"A" * 70000,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = conn.getresponse(); r.read()
        conn.close()
        check("oversized POST → 413", r.status == 413)
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print(f"\nT6 FAIL: {failures}")
        return 1

    # ── Rate limiting: dedicated server with a tight limit ──
    text = (work / "config.yaml").read_text().replace(
        'access_key_file: "',
        'rate_limit_max: 2\n  rate_limit_window: 60\n  access_key_file: "')
    cfg2_path = work / "config-rl.yaml"
    cfg2_path.write_text(text)
    cfg2 = load_config(cfg2_path)
    server2 = make_server(cfg2, KEY)
    port2 = server2.server_address[1]
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    try:
        def rl(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", port2, timeout=10)
            h = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
            c.request(method, path, body=body, headers=h)
            r = c.getresponse(); b = r.read()
            c.close(); return r.status, b
        for i in range(2):
            st, _ = rl("POST", "/login", b"pw=wrong")
        st, body = rl("POST", "/login", b"pw=wrong")
        ok429 = st == 429 and "尝试次数过多".encode() in body
        st, body = rl("POST", "/login", f"pw={KEY}".encode())
        ok_even_correct = st == 429
        print(f"{'OK  ' if ok429 and ok_even_correct else 'FAIL'} rate limiting: "
              f"2 fails → 429 (even with the correct password) [{st}]")
        if not (ok429 and ok_even_correct):
            failures.append("rate limiting")
    finally:
        server2.shutdown()
        server2.server_close()

    if failures:
        print(f"\nT6 FAIL: {failures}")
        return 1
    print("\nT6 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
