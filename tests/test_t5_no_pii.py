"""T5: the repo contains zero personal / production details.

Two tiers of patterns are scanned for:

* **Generic patterns** (below, published with the repo) catch whole
  accident classes — real email addresses, tunnel URLs with passwords,
  Telegram chat ids, PEM key blocks, absolute Termux paths, stray UUIDs.
* **Local patterns** (optional) are *your* concrete values — email
  local-part, repo UUID, dashboard password. They must never be
  committed, so they live in a git-ignored file:

      tests/pii_patterns.local

  One pattern per line, ``label|regex``. If the file is absent the
  generic scan still runs; a note is printed.

The scan deliberately excludes ``docs/`` (the protocol document must
name the ``OPENCLAW_SECURE_RECORD_V1`` marker) and ``tests/_t*_work``
(test-time working directories with generated throwaway keys; they
are git-ignored and not shipped).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PATTERNS_FILE = Path(__file__).resolve().parent / "pii_patterns.local"

# Generic patterns: safe to publish, catch real accident classes.
# (label, regex) — each compiled with re.IGNORECASE.
GENERIC: list[tuple[str, str]] = [
    ("email_addr",      r"[a-zA-Z0-9._%+-]+@(?!example\.)[a-zA-Z0-9.-]+\.[a-z]{2,}"),
    ("telegram_chat",   r"-100\d{9,}"),
    ("trycloudflare",   r"trycloudflare\.com"),
    ("pw_query",        r"\?pw="),
    ("uuid",            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    ("termux_path",     r"data/data/com\.termux"),
    ("private_pem",     r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("public_pem",      r"-----BEGIN [A-Z ]*PUBLIC KEY-----"),
]

# Subtrees that are out of scope for the public repo.
EXCLUDE_DIR_PARTS = {
    ".git",
    "__pycache__",
    "docs",                  # protocol doc must name the marker
    "frontend",              # sender web page: its JS must name the marker
    "data",                  # runtime output (git-ignored): records, logs,
                             # login audits — free text the user wrote
    "keys",                  # runtime-generated keypair (git-ignored);
                             # PEMs living there are by design
    "_t1_work",
    "_t2_work",
    "_t3_work",
    "_t4_work",
    "_t5_work",
}
EXCLUDE_FILES = {
    # runtime secrets / user's own values (git-ignored by design)
    Path("kid_secrets.json"),
    Path("config.yaml"),     # contains the user's real imap.username
    # this test contains its own generic pattern strings
    Path("tests/test_t5_no_pii.py"),
    # the local patterns file contains your real values by design
    Path("tests/pii_patterns.local"),
    # the other tests reference the production marker by name
    Path("tests/test_t1_v4_compat.py"),
    Path("tests/test_t2_real_email.py"),
    Path("tests/test_t3_dedupe.py"),
    Path("tests/test_t6_dashboard.py"),
    Path("tests/test_t8_security.py"),
}

# The Termux:Boot shebang is the platform-required interpreter path —
# every Termux boot script on earth carries it. Not a leak.
PATTERN_ALLOWED: dict[str, set[Path]] = {
    "termux_path": {
        Path("deploy/termux/bg-watchdog.sh"),
        Path("deploy/termux/start-bg-watchdog.sh"),
    },
}

# In `src/`, the marker literal "OPENCLAW_SECURE_RECORD_V1" is a
# required part of the wire format and so it is allowed in those
# specific files. The same word in any other source file would be
# a leakage.
ALLOWED_OPENCLAW_FILES = {
    Path("src/envelope.py"),
    Path("src/sender.py"),
    Path("src/pipeline.py"),
    Path("src/watcher.py"),
    Path("src/demo.py"),   # its throwaway demo config carries the marker word
    Path("config.example.yaml"),
    Path("config.yaml"),   # user's runtime copy; its subject_prefix
                           # legitimately contains the marker word
    Path(".env.example"),
    Path("README.md"),
    Path("README.zh-CN.md"),
    Path("llms.txt"),
}


def _load_local_patterns() -> list[tuple[str, str]]:
    """Read ``label|regex`` lines from the git-ignored local file."""
    if not LOCAL_PATTERNS_FILE.is_file():
        return []
    out: list[tuple[str, str]] = []
    for line in LOCAL_PATTERNS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            label, pat = line.split("|", 1)
            out.append((label.strip(), pat.strip()))
        else:
            out.append(("local", line))
    return out


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIR_PARTS for part in rel.parts):
            continue
        if rel in EXCLUDE_FILES:
            continue
        if p.suffix in {".png", ".jpg", ".pdf", ".gz"}:
            continue
        yield rel, p


def main() -> int:
    generic = GENERIC
    local = _load_local_patterns()
    failures: list[str] = []
    n_scanned = 0
    for rel, path in _iter_files(ROOT):
        n_scanned += 1
        try:
            text = path.read_text()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for label, pat in generic + local:
            allowed = PATTERN_ALLOWED.get(label, set())
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                if rel in allowed:
                    continue
                failures.append(f"{rel}: [{label}] {m.group(0)!r}")
        # The OpenClaw marker is allowed only in protocol-aware files.
        if rel not in ALLOWED_OPENCLAW_FILES:
            for m in re.finditer(r"openclaw", text, flags=re.IGNORECASE):
                failures.append(f"{rel}: [openclaw_marker] {m.group(0)!r}")

    if failures:
        print("FORBIDDEN PATTERNS FOUND:")
        for f in failures:
            print(" -", f)
        return 1
    print(f"OK scanned {n_scanned} files; 0 forbidden matches")
    print(f"generic patterns: {len(generic)}; local patterns: {len(local)}")
    if not local:
        print(f"note: no {LOCAL_PATTERNS_FILE.relative_to(ROOT)} — add your own "
              "values there (git-ignored) to scan for them too")
    print("  openclaw_marker     case-insensitive, protocol files only")
    print("excluded: docs/, tests/_t*_work/, tests/test_t5_no_pii.py")
    print("\nT5 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
