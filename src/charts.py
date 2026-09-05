"""Rolling-window chart renderer.

Renders a single PNG from ``records.csv`` for the requested window
(``24h``, ``48h``, ``7d``, ``30d``). Pure stdlib + matplotlib; no
project-specific imports. Mirrors the production
the production charts script shape: one PNG per window, no fancy
styling.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

WINDOWS = {
    "24h": timedelta(hours=24),
    "48h": timedelta(hours=48),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _parse_ts(raw: str) -> datetime | None:
    """Parse the timestamp column. Production rows use either
    ``YYYY-MM-DDTHH:MM`` or, for the 1-hour-fallback path, ISO with
    seconds. We accept both.
    """
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def render(csv_path: Path, out_path: Path, window: str, unit: str) -> int:
    if window not in WINDOWS:
        print(f"unknown window {window!r}", file=sys.stderr)
        return 2
    if not csv_path.is_file():
        print(f"csv not found: {csv_path}", file=sys.stderr)
        return 1

    cutoff = datetime.now() - WINDOWS[window]
    xs: list[datetime] = []
    ys: list[float] = []
    with csv_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ts = _parse_ts(row.get("timestamp", ""))
            try:
                y = float(row.get("glucose_value", row.get("value", "")))
            except (TypeError, ValueError):
                continue
            if ts is None:
                continue
            if ts < cutoff:
                continue
            xs.append(ts)
            ys.append(y)

    if not xs:
        # Fall back to all records (fresh install scenario).
        xs, ys = [], []
        with csv_path.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ts = _parse_ts(row.get("timestamp", ""))
                try:
                    y = float(row.get("glucose_value", row.get("value", "")))
                except (TypeError, ValueError):
                    continue
                if ts is None:
                    continue
                xs.append(ts)
                ys.append(y)
        if not xs:
            print("no records to plot", file=sys.stderr)
            return 0
        print(f"window {window} empty; falling back to all {len(xs)} records", file=sys.stderr)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(xs, ys, marker="o", linewidth=1.5)
    ax.set_xlabel("time")
    ax.set_ylabel(unit)
    ax.set_title(f"records — last {window}")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"wrote {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--window", required=True, choices=sorted(WINDOWS))
    p.add_argument("--unit", default="mmol/L")
    args = p.parse_args()
    return render(Path(args.csv), Path(args.out), args.window, args.unit)


if __name__ == "__main__":
    raise SystemExit(main())
