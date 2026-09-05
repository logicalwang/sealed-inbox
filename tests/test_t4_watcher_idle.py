"""T4: the watcher implements IMAP IDLE by hand and does not call
``mail.idle()`` (which doesn't exist on Python 3.13 stdlib).

This test:
  * inspects the watcher source to confirm it does NOT call
    ``mail.idle()`` or ``mail.IDLE()``
  * asserts that the symbols it uses (``_new_tag``, ``send``,
    ``readline``, ``socket``, ``select.select``) all resolve at import
    time
  * drives a single IDLE cycle with a fake IMAP and verifies that
    ``DONE`` is sent and the read path is exercised
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import imaplib  # noqa: E402
import select  # noqa: E402

from src import watcher as watcher_mod  # noqa: E402


def main() -> int:
    src = Path(watcher_mod.__file__).read_text()

    # 1. No actual call to mail.idle() / mail.IDLE(). Strip comments
    #    and string literals so a docstring mention of "mail.idle()"
    #    is allowed (the comment explicitly contrasts with the
    #    hand-rolled implementation).
    code = re.sub(r"#[^\n]*", "", src)  # line comments
    code = re.sub(r'\"\"\"[\s\S]*?\"\"\"', "", code)  # docstrings
    code = re.sub(r"'''[\s\S]*?'''", "", code)
    for bad in re.finditer(r"mail\.(idle|IDLE)\s*\(", code):
        raise AssertionError(
            f"watcher must not call mail.idle(); found at offset {bad.start()}: {bad.group(0)!r}"
        )
    print("OK source: no mail.idle()/mail.IDLE() call in code")

    # 2. The implementation uses _new_tag + send + readline + select.
    for needed in ("_new_tag", "send", "readline", "select.select", "DONE"):
        if needed not in src:
            raise AssertionError(f"watcher source missing required token: {needed!r}")
    print("OK source: contains _new_tag, send, readline, select, DONE")

    # 3. The symbols the watcher reaches into resolve at import time.
    assert hasattr(imaplib.IMAP4_SSL, "_new_tag"), "imaplib.IMAP4_SSL._new_tag missing"
    assert hasattr(imaplib.IMAP4_SSL, "send"), "imaplib.IMAP4_SSL.send missing"
    assert hasattr(imaplib.IMAP4_SSL, "readline"), "imaplib.IMAP4_SSL.readline missing"
    assert hasattr(imaplib.IMAP4_SSL, "socket"), "imaplib.IMAP4_SSL.socket missing"
    assert not hasattr(imaplib.IMAP4_SSL, "idle"), (
        "watcher is written assuming imaplib.IMAP4_SSL has no .idle() method; "
        "this is the current state on Python 3.13"
    )
    print("OK runtime: imaplib symbols the watcher uses all exist; .idle() correctly absent")

    # 4. Drive a single IDLE cycle. We don't talk to a real server;
    #    we hand-roll a fake ``mail`` that mimics the few attributes
    #    the watcher's _idle_loop touches. The fake socket uses a
    #    real, readable fd (a pipe) so ``select.select`` is happy.
    import os as _os

    r_fd, w_fd = _os.pipe()
    _os.set_blocking(r_fd, False)
    # Pre-load one byte so the first select() returns "ready".
    _os.write(w_fd, b"x")
    _os.close(w_fd)

    class FakeSock:
        def __init__(self, fd):
            self._fd = fd
        def fileno(self): return self._fd

    class FakeMail:
        def __init__(self):
            self.tag_counter = 0
            self.sent: list[bytes] = []
            self.responses = [b"+ idling\r\n"]
            self.responses_iter = iter(self.responses)
        def _new_tag(self):
            self.tag_counter += 1
            return f"A{self.tag_counter:03d}".encode()
        def send(self, data: bytes):
            self.sent.append(data)
        def readline(self) -> bytes:
            return next(self.responses_iter)
        def read(self, n: int) -> bytes:
            return b"* 5 EXISTS\r\n"
        def socket(self): return FakeSock(r_fd)
        def logout(self): pass

    fm = FakeMail()
    try:
        result = watcher_mod._idle_loop(fm, r_fd, timeout=10)
    finally:
        _os.close(r_fd)
    assert result == "notification", f"expected 'notification', got {result!r}"
    # The IDLE command was sent, then DONE was sent to exit.
    sent_str = b"".join(fm.sent)
    assert b" IDLE\r\n" in sent_str, f"IDLE command not sent: {fm.sent!r}"
    assert b"DONE\r\n" in sent_str, f"DONE not sent: {fm.sent!r}"
    print(f"OK runtime: idle_loop sent={fm.sent!r} -> {result}")

    # 5. Timeout path: pre-load a Sock without data and confirm the
    #    loop returns 'timeout' after the window elapses.
    r_fd2, w_fd2 = _os.pipe()
    _os.set_blocking(r_fd2, False)
    class QuietMail(FakeMail):
        def socket(self): return FakeSock(r_fd2)
    qm = QuietMail()
    try:
        result = qm.__class__()
        quiet = QuietMail()
        # Patch select to return empty immediately so the test runs
        # fast; we don't need a real 25-min timeout.
        import src.watcher as _wm
        import select as _sel
        original = _sel.select
        _sel.select = lambda *a, **kw: ([], [], [])
        try:
            r = watcher_mod._idle_loop(quiet, r_fd2, timeout=1)
        finally:
            _sel.select = original
        assert r == "timeout", f"expected 'timeout', got {r!r}"
        assert b"DONE\r\n" in b"".join(quiet.sent), "DONE not sent on timeout"
    finally:
        _os.close(r_fd2)
    print("OK runtime: idle_loop timeout path returns 'timeout' + DONE")

    print("\nT4 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
