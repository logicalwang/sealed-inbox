"""sealed-inbox dashboard — a zero-dependency local web UI.

Port of the production blood-glucose dashboard. Every deployment
specific was replaced by config:

  * all paths come from ``config.yaml`` (records_csv, charts_dir,
    state files) — nothing is hard-coded
  * the access key lives in a git-ignored file, never in source
  * chart files are served only from ``charts_dir`` (the production
    version had a path-traversal bug: ``GET /../../x.png`` escaped the
    base directory; this port resolves and confines every candidate)
  * login is a POST form; the password is never accepted in a URL
    (it would end up in browser history and tunnel logs)
  * the watcher is detected via a configurable ``pgrep`` pattern

The UI language is Chinese, matching the reference web form. The
server speaks plain HTTP — put it behind a tunnel (cloudflared,
Tailscale) or keep it on the LAN; it has no TLS of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import http.server
import json
import logging
import secrets
import subprocess
import time
import urllib.parse
from http.cookies import SimpleCookie
from datetime import datetime
from pathlib import Path

from src.config import AppConfig, load_config

log = logging.getLogger("dashboard")

SESSION_COOKIE = "sealed_session"
SESSION_TTL = 7 * 24 * 3600
MAX_LOGIN_BODY = 65536          # bytes — a login body has no business being bigger
AUDIT_LOG_MAX_BYTES = 262144    # rotate the login audit beyond this
AUDIT_LOG_KEEP = 200            # lines kept when rotating


class _RateLimiter:
    """Per-IP login-failure lockout: after ``max_fails`` failures within
    ``window`` seconds, the IP is blocked until the window slides clear.
    In-memory only — a restart clears it, which is fine for a personal
    deployment (the real rate limiter is your tunnel provider's)."""

    def __init__(self, max_fails: int, window: int) -> None:
        self.max = max(1, max_fails)
        self.window = max(1, window)
        self._fails: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        hits = [t for t in self._fails.get(ip, []) if now - t < self.window]
        self._fails[ip] = hits
        return len(hits) >= self.max

    def fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)


# ── Secrets ─────────────────────────────────────────────────
def load_access_key(cfg: AppConfig) -> str:
    path = cfg.dashboard.access_key_file
    if not path.is_file():
        raise FileNotFoundError(
            f"dashboard access key file not found: {path}. "
            "Create it with: echo 'your-secret' > "
            f"{path}  (git-ignored, never commit it)"
        )
    key = path.read_text().strip()
    if not key:
        raise FileNotFoundError(f"dashboard access key file is empty: {path}")
    if path.stat().st_mode & 0o077:
        log.warning("access key file %s is group/other-readable — chmod 600 it", path)
    return key


# ── Data helpers ────────────────────────────────────────────
def get_watcher_status(pattern: str) -> dict:
    try:
        r = subprocess.run(["pgrep", "-f", pattern],
                           capture_output=True, text=True)
        if r.returncode == 0:
            pid = r.stdout.strip().split("\n")[0]
            uptime = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                                    capture_output=True, text=True).stdout.strip()
            return {"running": True, "pid": pid, "uptime": uptime}
    except Exception:
        pass
    return {"running": False}


def get_latest_readings(cfg: AppConfig, n: int = 15) -> list[dict]:
    records: list[dict] = []
    csv_path = cfg.storage.records_csv
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig") as f:
            records = list(__import__("csv").DictReader(f))
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return records[:n]


def get_state(cfg: AppConfig) -> dict:
    state: dict = {}
    for name, path in (("idle", cfg.storage.idle_state_path),
                       ("email", cfg.storage.state_path)):
        if path.exists():
            try:
                state[name] = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
    return state


def get_recent_log(path, n: int = 10) -> list[str]:
    if not path or not Path(path).exists():
        return []
    lines = Path(path).read_text(errors="replace").strip().split("\n")
    return [l for l in lines if l][-n:]


def get_client_ip(handler: http.server.BaseHTTPRequestHandler) -> str:
    headers = handler.headers
    for key in ("CF-Connecting-IP", "X-Real-IP"):
        value = headers.get(key)
        if value:
            return value.split(",")[0].strip()
    xff = headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return handler.client_address[0] if handler.client_address else "unknown"


# ── Sessions ────────────────────────────────────────────────
def make_session_token(access_key: str) -> str:
    """``ts.nonce.signature`` — the nonce keeps two logins in the same
    second from sharing a token."""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    sig = hmac.new(access_key.encode(), f"{ts}|{nonce}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{ts}.{nonce}.{sig}"


def verify_session_token(token: str, access_key: str) -> bool:
    parts = token.split(".")
    if len(parts) == 3:
        ts_s, nonce, sig = parts
        msg = f"{ts_s}|{nonce}"
    elif len(parts) == 2:                      # pre-nonce token: keep valid
        ts_s, sig = parts                      # until its TTL expires
        msg = ts_s
    else:
        return False
    try:
        ts = int(ts_s)
    except ValueError:
        return False
    if int(time.time()) - ts > SESSION_TTL:
        return False
    expected = hmac.new(access_key.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def get_cookie(handler: http.server.BaseHTTPRequestHandler, name: str) -> str | None:
    raw = handler.headers.get("Cookie", "")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


# ── Login audit (local jsonl, git-ignored data dir) ────────
def login_log_path(cfg: AppConfig) -> Path:
    return cfg.storage.state_path.parent / "dashboard_login.jsonl"


def record_login(cfg: AppConfig, ip: str, path: str, user_agent: str, source: str) -> None:
    p = login_log_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size > AUDIT_LOG_MAX_BYTES:
        keep = p.read_text(encoding="utf-8").splitlines()[-AUDIT_LOG_KEEP:]
        p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    event = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "ip": ip, "path": path, "ua": user_agent, "source": source}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def recent_logins(cfg: AppConfig, n: int = 8) -> list[dict]:
    p = login_log_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ── Page rendering ──────────────────────────────────────────
def _color_for(value: str, low: float, high: float) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "#374151"
    if v < low:
        return "#dc2626"      # below range
    if v > high:
        return "#f59e0b"      # above range
    return "#16a34a"          # in range


def _login_page(error: str = "") -> bytes:
    err = f'<div class="error">{html.escape(error)}</div>' if error else '<div class="error" id="err"></div>'
    page = f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🔒 sealed-inbox - 访问验证</title>
<style>
body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background:#f0f2f5; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
.card {{ background:#fff; border-radius:12px; padding:32px; box-shadow:0 1px 3px rgba(0,0,0,0.08); text-align:center; max-width:360px; }}
h1 {{ font-size:1.4em; color:#1a1a2e; margin-bottom:8px; }}
p {{ color:#64748b; font-size:0.9em; margin-bottom:20px; }}
input {{ padding:10px 14px; font-size:1em; border:1px solid #d1d5db; border-radius:8px; width:100%; box-sizing:border-box; margin-bottom:12px; }}
button {{ padding:10px 24px; font-size:1em; background:#2563eb; color:#fff; border:none; border-radius:8px; cursor:pointer; width:100%; }}
button:hover {{ background:#1d4ed8; }}
.error {{ color:#dc2626; font-size:0.85em; margin-top:8px; min-height:1.2em; }}
</style>
</head>
<body>
<div class="card">
<h1>🔒 sealed-inbox</h1>
<p>请输入访问密码</p>
<form method="post" action="/login">
<input type="password" name="pw" placeholder="密码" autofocus>
<button type="submit">进入</button>
</form>
{err}
</div>
</body>
</html>"""
    return page.encode("utf-8")


def build_page(cfg: AppConfig, access_key: str) -> bytes:
    d = cfg.dashboard
    watcher = get_watcher_status(d.watcher_process_pattern)
    readings = get_latest_readings(cfg, 15)
    state = get_state(cfg)
    pipeline_lines = get_recent_log(d.pipeline_log, 8) if d.pipeline_log else []
    idle_lines = get_recent_log(d.watcher_log, 8) if d.watcher_log else []
    login_records = recent_logins(cfg, 8)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if watcher["running"]:
        badge = (f'<span class="badge green">● 运行中</span> '
                 f'PID {html.escape(str(watcher["pid"]))}，已运行 {html.escape(watcher["uptime"])}')
    else:
        badge = '<span class="badge red">● 已停止</span>'

    rows = ""
    for r in readings:
        val = r.get("glucose_value", "")
        color = _color_for(val, d.low, d.high)
        rows += (
            f'<tr><td>{html.escape(str(r.get("timestamp", "")))}</td>'
            f'<td style="color:{color};font-weight:bold">{html.escape(str(val))}</td>'
            f'<td>{html.escape(str(r.get("unit", "")))}</td>'
            f'<td>{html.escape(str(r.get("context", "")))}</td>'
            f'<td>{html.escape(str(r.get("note", "")))}</td></tr>\n'
        )

    idle = state.get("idle", {})
    email = state.get("email", {})
    total_processed = idle.get("total_processed", 0)
    last_id = idle.get("last_uid", email.get("last_msg_id", 0))

    def log_block(lines):
        return ("<pre>" + html.escape("\n".join(lines[-8:])) + "</pre>") if lines \
            else "<pre>（未配置或为空）</pre>"

    login_rows = ""
    for rec in login_records:
        login_rows += (
            f'<tr><td>{html.escape(str(rec.get("ts", "")))}</td>'
            f'<td>{html.escape(str(rec.get("ip", "")))}</td>'
            f'<td>{html.escape(str(rec.get("source", "")))}</td>'
            f'<td>{html.escape(str(rec.get("path", "")))}</td>'
            f'<td>{html.escape(str(rec.get("ua", "")))}</td></tr>\n'
        )

    charts = "".join(
        f'<a href="/records_{html.escape(w)}.png"><img src="/records_{html.escape(w)}.png" alt="{html.escape(w)}"></a> '
        for w in cfg.charts.windows
    )

    page = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>🩸 sealed-inbox 监测面板</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background:#f0f2f5; color:#1a1a2e; padding:12px; }}
.header {{ text-align:center; padding:16px 0 8px; }}
.header h1 {{ font-size:1.5em; }}
.header .time {{ color:#64748b; font-size:0.85em; margin-top:4px; }}
.card {{ background:#fff; border-radius:12px; padding:16px; margin:10px 0; box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
.card h2 {{ font-size:1.1em; margin-bottom:10px; color:#334155; }}
.badge {{ padding:3px 10px; border-radius:12px; font-size:0.8em; font-weight:600; }}
.badge.green {{ background:#dcfce7; color:#166534; }}
.badge.red {{ background:#fee2e2; color:#991b1b; }}
.status-line {{ font-size:0.9em; color:#475569; margin:6px 0; }}
.charts {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:10px 0; }}
.charts img {{ width:100%; border-radius:8px; cursor:pointer; }}
.charts img:hover {{ opacity:0.85; }}
@media (max-width:600px) {{ .charts {{ grid-template-columns:1fr; }} }}
table {{ width:100%; border-collapse:collapse; font-size:0.85em; }}
th {{ background:#f8fafc; text-align:left; padding:8px 6px; border-bottom:2px solid #e2e8f0; }}
td {{ padding:6px; border-bottom:1px solid #f1f5f9; }}
tr:hover {{ background:#f8fafc; }}
pre {{ background:#1e293b; color:#e2e8f0; padding:10px; border-radius:8px; font-size:0.78em; overflow-x:auto; line-height:1.5; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(140px,1fr)); gap:10px; }}
.stat {{ text-align:center; padding:12px; background:#f8fafc; border-radius:8px; }}
.stat .num {{ font-size:1.6em; font-weight:700; color:#2563eb; }}
.stat .label {{ font-size:0.78em; color:#64748b; margin-top:2px; }}
</style>
</head>
<body>
<div class="header">
  <h1>🩸 sealed-inbox 监测面板</h1>
  <div class="time">更新于 {now} · 每30秒自动刷新</div>
</div>

<div class="card">
  <h2>📡 服务状态</h2>
  <div class="status-line">IMAP IDLE Watcher: {badge}</div>
  <div class="stats">
    <div class="stat"><div class="num">{total_processed}</div><div class="label">已处理邮件</div></div>
    <div class="stat"><div class="num">{last_id}</div><div class="label">最新邮件 ID</div></div>
    <div class="stat"><div class="num">{len(readings)}</div><div class="label">最近记录数</div></div>
  </div>
</div>

<div class="card">
  <h2>📈 趋势图</h2>
  <div class="charts">{charts}</div>
</div>

<div class="card">
  <h2>📋 最近记录</h2>
  <table>
    <tr><th>时间</th><th>数值</th><th>单位</th><th>情境</th><th>备注</th></tr>
    {rows}
  </table>
</div>

<div class="card">
  <h2>📜 运行日志</h2>
  <h3 style="font-size:0.85em;color:#64748b;margin:8px 0 4px;">Pipeline</h3>
  {log_block(pipeline_lines)}
  <h3 style="font-size:0.85em;color:#64748b;margin:12px 0 4px;">IDLE Watcher</h3>
  {log_block(idle_lines)}
</div>

<div class="card">
  <h2>🔐 最近登录</h2>
  <table>
    <tr><th>时间</th><th>IP</th><th>来源</th><th>路径</th><th>User-Agent</th></tr>
    {login_rows if login_rows else '<tr><td colspan="5">暂无登录记录</td></tr>'}
  </table>
</div>

</body>
</html>"""
    return page.encode("utf-8")


# ── HTTP server ─────────────────────────────────────────────
def make_server(cfg: AppConfig, access_key: str) -> http.server.ThreadingHTTPServer:
    d = cfg.dashboard
    limiter = _RateLimiter(d.rate_limit_max, d.rate_limit_window)

    class Handler(http.server.BaseHTTPRequestHandler):
        def version_string(self) -> str:
            return "sealed-inbox"        # don't advertise the Python version

        def end_headers(self) -> None:
            # Applied to every response, errors included.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def _authenticated(self) -> bool:
            token = get_cookie(self, SESSION_COOKIE)
            return bool(token and verify_session_token(token, access_key))

        def _issue_session(self, target: str) -> None:
            client_ip = get_client_ip(self)
            ua = self.headers.get("User-Agent", "")
            if self.headers.get("CF-Connecting-IP"):
                source = "CF-Connecting-IP"
            elif self.headers.get("X-Real-IP"):
                source = "X-Real-IP"
            elif self.headers.get("X-Forwarded-For"):
                source = "X-Forwarded-For"
            else:
                source = "direct"
            record_login(cfg, client_ip, target or "/", ua, source)
            self.send_response(302)
            self.send_header("Location", target or "/")
            self.send_header("Set-Cookie",
                             f"{SESSION_COOKIE}={make_session_token(access_key)}; "
                             f"Max-Age={SESSION_TTL}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()

        def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urllib.parse.urlparse(self.path)
            clean_path = parsed.path

            if not self._authenticated():
                self._send(_login_page(), "text/html; charset=utf-8")
                return

            if clean_path in ("/", "/index.html"):
                self._send(build_page(cfg, access_key), "text/html; charset=utf-8")
                return

            if clean_path.endswith(".png"):
                charts_dir = cfg.storage.charts_dir.resolve()
                candidate = (charts_dir / clean_path.lstrip("/")).resolve()
                # Confined to charts_dir — fixes the production path traversal.
                if candidate.is_relative_to(charts_dir) and candidate.is_file():
                    self._send(candidate.read_bytes(), "image/png")
                else:
                    self.send_error(404)
                return

            if clean_path == "/api/status":
                data = {
                    "watcher": get_watcher_status(d.watcher_process_pattern),
                    "state": get_state(cfg),
                    "readings_count": len(get_latest_readings(cfg, 9999)),
                }
                self._send(json.dumps(data).encode(), "application/json")
                return

            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/login":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if length > MAX_LOGIN_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length).decode("utf-8", "replace")
            sent = urllib.parse.parse_qs(body).get("pw", [""])[0]
            ip = get_client_ip(self)
            if limiter.blocked(ip):
                log.warning("login rate-limited for %s", ip)
                self._send(_login_page("尝试次数过多，请几分钟后再试"),
                           "text/html; charset=utf-8", code=429)
                return
            if sent and hmac.compare_digest(sent, access_key):
                limiter.reset(ip)
                self._issue_session("/")
                return
            limiter.fail(ip)
            self._send(_login_page("密码不对，再试一次"), "text/html; charset=utf-8")

        def log_message(self, format, *args):  # noqa: A002
            pass  # silent

    return http.server.ThreadingHTTPServer(
        (cfg.dashboard.bind, cfg.dashboard.port), Handler)


def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
    cfg = load_config()
    access_key = load_access_key(cfg)
    try:
        server = make_server(cfg, access_key)
    except OSError as e:
        print(f"cannot bind {cfg.dashboard.bind}:{cfg.dashboard.port}: {e}\n"
              f"  → 端口被占用？改 config.yaml 里的 dashboard.port 再启动。")
        return 1
    d = cfg.dashboard
    print(f"sealed-inbox dashboard listening on http://{d.bind}:{d.port}")
    print("  本机: http://localhost:" + str(d.port))
    print("  局域网/隧道: 用你自己的内网 IP 或隧道地址访问（本服务不自带 TLS，"
          "请置于 cloudflared/Tailscale 之后或仅限内网使用）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
