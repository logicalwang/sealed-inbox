"""Upload records.csv and the latest chart PNGs to a Seafile library.

The transport is the same two-step Seafile Web API the production
``upload_to_seafile.py`` uses:

  1. POST ``/api2/repos/{repo}/upload-link/?p={dir}`` → temporary
     upload URL.
  2. POST to the upload URL with the file bytes; ``replace=1`` to
     overwrite.

Differences from production:

* All endpoints, paths, and the API token come from ``config.yaml``.
* The token is read from a file (production uses ``~/tmp/.seafile_token``).
* Files to upload are passed in by the caller; production hard-codes
  five names. The caller decides which PNGs exist.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

from src.config import ArchiveConfig

log = logging.getLogger("seafile")


def _http_post_json(url: str, headers: dict[str, str], timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_raw": body.decode("utf-8", errors="replace")}


def _http_post_bytes(url: str, data: bytes, headers: dict[str, str], timeout: int = 60) -> int:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
        return r.status


def archive_files(archive: ArchiveConfig, files: list[Path], remote_dir: str = "/") -> bool:
    """Upload ``files`` to the configured Seafile library. Returns True
    if every file returned HTTP 200.
    """
    if archive.backend != "seafile" or archive.seafile is None:
        return True
    sf = archive.seafile
    try:
        token = sf.load_token()
    except FileNotFoundError as e:
        log.error(str(e))
        return False
    if not sf.repo_id:
        log.error("seafile repo_id is empty; check config.yaml")
        return False

    headers = {"Authorization": f"Token {token}"}
    ok = True
    for f in files:
        if not f.is_file():
            log.warning("skip %s: not found locally", f.name)
            continue
        link_url = (
            f"{sf.server_url}/api2/repos/{sf.repo_id}/upload-link/"
            f"?p={urllib.parse.quote(remote_dir, safe='/')}"
        )
        try:
            data = _http_post_json(link_url, headers)
        except Exception as e:
            log.warning("seafile upload-link failed: %s", e)
            ok = False
            continue
        upload_path = data.get("_raw") if "_raw" in data else data
        if not isinstance(upload_path, str):
            log.warning("seafile upload-link response unexpected: %r", data)
            ok = False
            continue
        upload_url = f"{sf.server_url}{upload_path}"
        try:
            status = _http_post_bytes(
                upload_url,
                f.read_bytes(),
                {**headers, "Content-Type": "application/octet-stream"},
            )
            log.info("uploaded %s (%d bytes, HTTP %d)", f.name, f.stat().st_size, status)
        except Exception as e:
            log.warning("seafile upload %s failed: %s", f.name, e)
            ok = False
    return ok
