"""YAML-driven configuration.

All deployment-specific values (IMAP host, app-password file, the
Seafile repo UUID, the private-key path, the kid-secrets path, etc.)
live in ``config.yaml``. The file is git-ignored; the bundled
``config.example.yaml`` only contains placeholders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_CONFIG_PATH = Path("config.yaml")


def _project_root(cfg_path: Path) -> Path:
    return cfg_path.resolve().parent


def _resolve(p: str, root: Path) -> Path:
    pp = Path(p).expanduser()   # "~/.config/..." must resolve against $HOME,
                                # not be treated as a repo-relative path
    return pp if pp.is_absolute() else (root / pp).resolve()


# ── Config sections ───────────────────────────────────────
@dataclass
class ImapConfig:
    host: str
    port: int
    username: str
    app_password_file: str
    subject_prefix: str
    since_days: int

    def load_password(self) -> str:
        path = Path(self.app_password_file).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"IMAP app password file not found: {path}. "
                "Create it with your provider's app password (NOT the account password)."
            )
        return path.read_text().strip()


@dataclass
class CryptoConfig:
    private_key_path: Path
    kid_secrets_path: Path
    # Sender authentication: the reference frontend signs every record with
    # the kid secret (mac). Default off = production parity (mac ignored).
    # When true, records from unknown kids or with a bad mac are rejected.
    require_valid_mac: bool = False
    # Optional freshness window in hours: reject records whose ts is older.
    # 0 = disabled (production parity).
    max_age_hours: float = 0.0


@dataclass
class StorageConfig:
    records_csv: Path
    charts_dir: Path
    state_path: Path
    idle_state_path: Path


@dataclass
class SeafileConfig:
    server_url: str
    repo_id: str
    token_file: str
    replace_existing: bool = True

    def load_token(self) -> str:
        path = Path(self.token_file).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Seafile token file not found: {path}")
        return path.read_text().strip()


@dataclass
class ArchiveConfig:
    backend: str = "local"
    seafile: SeafileConfig | None = None


@dataclass
class ChartsConfig:
    windows: list[str] = field(default_factory=lambda: ["24h", "48h", "7d", "30d"])
    metric_unit: str = "mmol/L"


@dataclass
class DashboardConfig:
    bind: str = "0.0.0.0"
    port: int = 8086
    access_key_file: Path = Path("~/.config/secure-record/dashboard-access-key")
    low: float = 3.9          # below → red (mmol/L)
    high: float = 7.0         # above → amber
    watcher_process_pattern: str = "src.watcher"
    pipeline_log: Path | None = None   # optional log tail on the page
    watcher_log: Path | None = None
    # Login rate limiting: an IP that fails this many times within the
    # window is locked out (429) until the window slides clear.
    rate_limit_max: int = 10
    rate_limit_window: int = 300


@dataclass
class AppConfig:
    imap: ImapConfig
    crypto: CryptoConfig
    storage: StorageConfig
    charts: ChartsConfig
    dashboard: DashboardConfig
    archive: ArchiveConfig
    project_root: Path


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise KeyError(f"missing required key '{key}' in {where}")
    return d[key]


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    cfg_path = Path(path) if path else Path(
        os.environ.get("SECURE_RECORD_CONFIG", _DEFAULT_CONFIG_PATH)
    )
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"config file not found: {cfg_path}. "
            "Copy config.example.yaml to config.yaml and edit it."
        )
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    root = _project_root(cfg_path)

    imap_raw = _require(raw, "imap", "config.yaml")
    imap = ImapConfig(
        host=imap_raw.get("host", "imap.gmail.com"),
        port=int(imap_raw.get("port", 993)),
        username=_require(imap_raw, "username", "imap"),
        app_password_file=_require(imap_raw, "app_password_file", "imap"),
        subject_prefix=imap_raw.get("subject_prefix", "[Secure Record]"),
        since_days=int(imap_raw.get("since_days", 30)),
    )

    crypto_raw = _require(raw, "crypto", "config.yaml")
    crypto = CryptoConfig(
        private_key_path=_resolve(_require(crypto_raw, "private_key_path", "crypto"), root),
        kid_secrets_path=_resolve(_require(crypto_raw, "kid_secrets_path", "crypto"), root),
        require_valid_mac=bool(crypto_raw.get("require_valid_mac", False)),
        max_age_hours=float(crypto_raw.get("max_age_hours", 0) or 0),
    )

    storage_raw = _require(raw, "storage", "config.yaml")
    storage = StorageConfig(
        records_csv=_resolve(storage_raw.get("records_csv", "./data/records.csv"), root),
        charts_dir=_resolve(storage_raw.get("charts_dir", "./data/charts"), root),
        state_path=_resolve(storage_raw.get("state_path", "./data/email_state.json"), root),
        idle_state_path=_resolve(storage_raw.get("idle_state_path", "./data/idle_state.json"), root),
    )

    charts_raw = raw.get("charts", {}) or {}
    charts = ChartsConfig(
        windows=charts_raw.get("windows", ["24h", "48h", "7d", "30d"]),
        metric_unit=charts_raw.get("metric_unit", "mmol/L"),
    )

    archive_raw = raw.get("archive", {}) or {}
    archive = ArchiveConfig(backend=archive_raw.get("backend", "local"))
    if archive.backend == "seafile":
        sf = archive_raw.get("seafile", {}) or {}
        archive.seafile = SeafileConfig(
            server_url=sf.get("server_url", ""),
            repo_id=sf.get("repo_id", ""),
            token_file=sf.get("token_file", ""),
            replace_existing=bool(sf.get("replace_existing", True)),
        )

    d_raw = raw.get("dashboard", {}) or {}
    dashboard = DashboardConfig(
        bind=str(d_raw.get("bind", "0.0.0.0")),
        port=int(d_raw.get("port", 8086)),
        access_key_file=_resolve(
            d_raw.get("access_key_file", "~/.config/secure-record/dashboard-access-key"),
            root),
        low=float(d_raw.get("low", 3.9)),
        high=float(d_raw.get("high", 7.0)),
        watcher_process_pattern=str(d_raw.get("watcher_process_pattern", "src.watcher")),
        pipeline_log=_resolve(d_raw["pipeline_log"], root) if d_raw.get("pipeline_log") else None,
        watcher_log=_resolve(d_raw["watcher_log"], root) if d_raw.get("watcher_log") else None,
        rate_limit_max=int(d_raw.get("rate_limit_max", 10)),
        rate_limit_window=int(d_raw.get("rate_limit_window", 300)),
    )

    return AppConfig(
        imap=imap,
        crypto=crypto,
        storage=storage,
        charts=charts,
        dashboard=dashboard,
        archive=archive,
        project_root=root,
    )
