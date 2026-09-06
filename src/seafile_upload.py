"""Upload records.csv and the latest chart PNGs to a Seafile library.

Two-step Seafile Web API, mirroring the production ``upload_to_seafile.py``
that has run for months:

  1. **GET** ``/api2/repos/{repo}/upload-link/?p={dir}`` → temporary upload
     URL (a JSON-quoted string). Do NOT use POST here — the endpoint
     answers 405 (this was the bug in the first port).
  2. **POST multipart/form-data** to that URL with fields ``file``,
     ``parent_dir`` and ``replace=1``. A raw octet-stream body is
     rejected (bug #2 in the first port).

All endpoints and the API token come from ``config.yaml``. ``remote_dir``
keeps files in the same library folder production used (e.g. a Chinese
path — it is URL-encoded for the upload-link query and sent raw in the
``parent_dir`` field).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from src.config import ArchiveConfig

log = logging.getLogger("seafile")


def _multipart(fields: dict[str, str], file_field: str, filename: str,
               content: bytes) -> tuple[bytes, str]:
    boundary = "sealed" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
    parts.append(content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def _open_json(url: str, headers: dict[str, str], timeout: int = 30):
    req = urllib.request.Request(url, headers=headers)          # GET
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")


def _open_post(url: str, body: bytes, content_type: str,
               headers: dict[str, str], timeout: int = 90) -> int:
    req = urllib.request.Request(url, data=body,
                                 headers={**headers, "Content-Type": content_type},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def archive_files(archive: ArchiveConfig, files: list[Path],
                  remote_dir: str | None = None) -> bool:
    """Upload ``files`` to the configured Seafile library. Returns True
    if every file was uploaded successfully (HTTP 200).
    """
    if archive.backend != "seafile" or archive.seafile is None:
        return True
    sf = archive.seafile
    remote_dir = remote_dir or "/"
    try:
        token = sf.load_token()
    except FileNotFoundError as e:
        log.error(str(e))
        return False
    if not sf.repo_id:
        log.error("seafile repo_id is empty; check config.yaml")
        return False

    headers = {"Authorization": f"Token {token}"}
    link_url = (f"{sf.server_url}/api2/repos/{sf.repo_id}/upload-link/"
                f"?p={urllib.parse.quote(remote_dir, safe='/')}")

    link = None
    try:
        link = _open_json(link_url, headers)
    except Exception as e:
        log.warning("seafile upload-link failed: %s", e)
        return False
    if not isinstance(link, str) or not link.startswith("http"):
        log.warning("seafile upload-link unexpected response: %r", link)
        return False
    log.info("seafile upload link obtained for %s", remote_dir)

    ok = True
    for f in files:
        if not f.is_file():
            log.warning("skip %s: not found locally", f.name)
            continue
        body, ctype = _multipart({"parent_dir": remote_dir, "replace": "1"},
                                 "file", f.name, f.read_bytes())
        try:
            status = _open_post(link, body, ctype, headers)
            if status == 200:
                log.info("uploaded %s (%d bytes)", f.name, f.stat().st_size)
            else:
                log.warning("seafile upload %s: HTTP %d", f.name, status)
                ok = False
        except Exception as e:
            log.warning("seafile upload %s failed: %s", f.name, e)
            ok = False
    return ok
