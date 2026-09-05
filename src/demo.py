"""60-second demo: fake glucose data + dashboard, in a throwaway workspace.

``python3 -m src.demo [--port 8086] [--bind 0.0.0.0]``

Creates a temp directory with its own config, keypair, kid secret and two
weeks of simulated readings, renders the trend charts, then serves the
dashboard. Nothing is written to your real config or data directories, and
everything disappears when the process exits (the workspace lives under the
system temp dir). Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import AppConfig, load_config  # noqa: E402
from src.dashboard import make_server  # noqa: E402
from src.sender import generate_keypair  # noqa: E402


def _fake_readings(days: int = 14) -> list[dict]:
    """Plausible-looking fingerstick log: 3–4 checks/day, 4.3–9.6 mmol/L."""
    import random

    rng = random.Random(20260905)  # deterministic demo
    contexts = ["空腹", "早餐后2h", "午餐后2h", "睡前"]
    notes = ["", "", "散步后", "", "餐后偏高", ""]
    out: list[dict] = []
    value = 6.4
    base = datetime.now() - timedelta(days=days)
    for day in range(days):
        for slot, hour in ((0, 7), (1, 9), (2, 13), (3, 22)):
            if slot == 1 and rng.random() < 0.4:   # skip some mid-morning checks
                continue
            value += rng.uniform(-1.1, 1.2)
            value = min(9.6, max(4.3, value))
            ts = base + timedelta(days=day, hours=hour, minutes=rng.randrange(60))
            out.append({
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M"),
                "glucose_value": round(value, 1),
                "unit": "mmol/L",
                "context": contexts[slot],
                "note": rng.choice(notes),
                "source": "demo-mode",
            })
    return out


def build_demo_workspace(root: Path) -> tuple[AppConfig, str]:
    """Create config/keys/sample CSV/charts under ``root``. Returns (cfg, key)."""
    root.mkdir(parents=True, exist_ok=True)
    priv, pub = generate_keypair(root / "keys")
    kid_secret = secrets.token_urlsafe(32)
    (root / "kid_secrets.json").write_text(json.dumps(
        {"demo": {"secret": kid_secret, "enabled": True}}, indent=2) + "\n")
    access_key = "demo-" + secrets.token_urlsafe(12)
    (root / "dashboard-access-key").write_text(access_key + "\n")

    readings = _fake_readings()
    with (root / "records.csv").open("w", encoding="utf-8-sig", newline="") as f:
        import csv
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "glucose_value", "unit", "context", "note", "source"])
        writer.writeheader()
        writer.writerows(readings)

    (root / "config.yaml").write_text(f"""
imap:
  host: imap.example.com
  port: 993
  username: demo@example.com
  app_password_file: "{root / 'app-password'}"
  subject_prefix: "[OpenClaw Secure Record]"
crypto:
  private_key_path: "{priv}"
  kid_secrets_path: "{root / 'kid_secrets.json'}"
storage:
  records_csv: "{root / 'records.csv'}"
  charts_dir: "{root / 'charts'}"
  state_path: "{root / 'state.json'}"
  idle_state_path: "{root / 'idle.json'}"
archive:
  backend: "local"
charts:
  windows: ["24h", "48h", "7d", "30d"]
  metric_unit: "mmol/L"
dashboard:
  access_key_file: "{root / 'dashboard-access-key'}"
  watcher_process_pattern: "definitely-not-running"
""")
    (root / "app-password").write_text("demo-not-a-real-password\n")

    cfg = load_config(root / "config.yaml")

    # Pre-render the trend charts so the page is alive on first open.
    import subprocess
    for window in cfg.charts.windows:
        subprocess.run(
            [sys.executable, str(ROOT / "src" / "charts.py"),
             "--csv", str(cfg.storage.records_csv),
             "--out", str(cfg.storage.charts_dir / f"records_{window}.png"),
             "--window", window, "--unit", cfg.charts.metric_unit],
            check=True, capture_output=True, text=True, timeout=120)
    return cfg, access_key


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m src.demo",
        description="60-second demo: fake data + dashboard in a throwaway workspace.")
    p.add_argument("--port", type=int, default=8086)
    p.add_argument("--bind", default="0.0.0.0")
    args = p.parse_args()

    work = Path(tempfile.mkdtemp(prefix="sealed-inbox-demo-"))
    cfg, access_key = build_demo_workspace(work)
    cfg.dashboard.port = args.port
    cfg.dashboard.bind = args.bind

    print()
    print("── sealed-inbox DEMO ──────────────────────────────────")
    print(f"  打开:  http://localhost:{args.port}" +
          ("" if args.bind == "127.0.0.1" else f"   (局域网: http://<本机IP>:{args.port})"))
    print(f"  口令:  {access_key}")
    print("  数据是假的（两周模拟血糖），写在临时目录，Ctrl-C 全部丢弃。")
    print(f"  工作区: {work}")
    print("───────────────────────────────────────────────────────")

    server = make_server(cfg, access_key)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
