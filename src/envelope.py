"""Parse and decrypt OpenClaw Secure Record v4 envelopes.

The wire format is fixed by the production web form
(``secure-relay-fast-v4.html``) and accepted verbatim by the production
receiver (``process_email_v2.py``). This module is a port of that
receiver with the hard-coded paths replaced by arguments.

Format on the wire (after the email body is plain-text extracted):

    OPENCLAW_SECURE_RECORD_V1
    {
      "v": 1,
      "kid": "<kid string>",
      "ts": 1756372800000,         // ms since epoch
      "nonce": "<base64url 16 bytes>",
      "alg": "RSA-OAEP-SHA256+AES-256-GCM",
      "ek":   "<base64url RSA-OAEP encrypted AES-256 key>",
      "iv":   "<base64url 12-byte AES-GCM nonce>",
      "ct":   "<base64url ciphertext || 16-byte GCM tag>",
      "mac":  "<base64url HMAC-SHA256(kid_secret, [1,kid,ts,nonce,ek,iv,ct].join('|'))>"
    }

The receiver parses the JSON, RSA-OAEP-decrypts ``ek`` with the
configured private key, AES-256-GCM-decrypts ``ct`` with ``iv`` and the
recovered key, and returns the inner record dict. The ``mac`` field is
NOT verified by the receiver (matches production v2 behaviour); it is
only used by the sender to bind the record to a kid.

Two markers are accepted:

* ``OPENCLAW_SECURE_RECORD_V1`` — the original production marker.
* ``HERMES_SECURE_RECORD_V1``   — accepted as an additional alias so a
                                  sender can pick either.

Both are recognised by :func:`extract_envelope`; whichever appears
first wins.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# Both markers are accepted by the receiver. The first one found in the
# body wins. The list is ordered most-recently-defined first; the
# production marker remains the canonical one.
PROTOCOL_MARKERS: tuple[str, ...] = (
    "OPENCLAW_SECURE_RECORD_V1",
    "HERMES_SECURE_RECORD_V1",
)


def b64url_decode(s: str) -> bytes:
    """Decode a base64url string. ``+`` and ``/`` substitution and
    missing padding are both handled — same approach as the production
    receiver (``urlsafe_b64decode(s + "==")``).
    """
    return base64.urlsafe_b64decode(s + "==")


def b64url_encode(b: bytes) -> str:
    """Standard base64url, no padding. Used by tests and the reference
    sender. Mirrors the JavaScript ``btoa`` + ``replace`` chain.
    """
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


@dataclass
class ParsedEnvelope:
    kid: str
    ts: int
    nonce: str
    alg: str
    ek: str
    iv: str
    ct: str
    mac: str | None
    v: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ParsedEnvelope":
        required = ("kid", "ts", "nonce", "alg", "ek", "iv", "ct")
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError(f"envelope missing required fields: {missing}")
        return cls(
            kid=str(d["kid"]),
            ts=int(d["ts"]),
            nonce=str(d["nonce"]),
            alg=str(d["alg"]),
            ek=str(d["ek"]),
            iv=str(d["iv"]),
            ct=str(d["ct"]),
            mac=(str(d["mac"]) if "mac" in d and d["mac"] is not None else None),
            v=int(d.get("v", 1)),
        )


def extract_envelope(body: str) -> tuple[str, ParsedEnvelope]:
    """Find the marker in the email body and parse the JSON payload.

    Mirrors the production approach exactly: locate the marker, skip
    past it, ``html.unescape`` the rest (Formspree emails contain
    HTML-escaped JSON), strip ``\\r``, then find the first ``{`` and
    match braces by depth to find the end of the JSON object.
    """
    if not isinstance(body, str):
        raise TypeError("extract_envelope expects a str body")

    idx = -1
    chosen = None
    for marker in PROTOCOL_MARKERS:
        i = body.find(marker)
        if i != -1:
            idx = i
            chosen = marker
            break
    if idx == -1 or chosen is None:
        raise ValueError("no protocol marker found in body")

    tail = body[idx + len(chosen):].strip()
    tail = html.unescape(tail).replace("\r", "")
    start = tail.find("{")
    if start == -1:
        raise ValueError("no JSON object found after marker")

    depth = 0
    end = start
    for i in range(start, len(tail)):
        ch = tail[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    json_str = tail[start:end]
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"envelope JSON parse error: {e}") from e
    return chosen, ParsedEnvelope.from_dict(obj)


def verify_mac(env: ParsedEnvelope, kid_secret: str | None) -> bool:
    """Recompute the HMAC and compare. Returns True if the kid secret
    is known and the MAC matches. Returns False if the kid secret is
    not known (e.g. receiver is configured with an empty registry) so
    the caller can decide whether to accept or reject.

    The production receiver does not verify the MAC; this function is
    provided for senders / send-side testing only.
    """
    if not kid_secret:
        return False
    msg = "|".join([
        "1", env.kid, str(env.ts), env.nonce, env.ek, env.iv, env.ct,
    ]).encode("utf-8")
    expected = hmac.new(kid_secret.encode("utf-8"), msg, hashlib.sha256).digest()
    try:
        got = b64url_decode(env.mac or "")
    except Exception:
        return False
    return hmac.compare_digest(expected, got)


def decrypt_envelope(env: ParsedEnvelope, private_key_pem: bytes) -> dict:
    """RSA-OAEP-decrypt ``ek``, then AES-256-GCM-decrypt ``ct``.

    Returns the inner JSON record as a dict. Raises on any failure.
    """
    ek = b64url_decode(env.ek)
    iv = b64url_decode(env.iv)
    ct = b64url_decode(env.ct)
    if len(iv) != 12:
        raise ValueError(f"iv must be 12 bytes, got {len(iv)}")

    priv = load_pem_private_key(private_key_pem, password=None)
    aes_key = priv.decrypt(
        ek,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    if len(aes_key) != 32:
        raise ValueError(f"unexpected AES key length: {len(aes_key)}")
    plaintext = AESGCM(aes_key).decrypt(iv, ct, None)
    return json.loads(plaintext.decode("utf-8"))


def decrypt_email_body(
    body: str,
    private_key_pem: bytes,
    kid_secrets: dict[str, str] | None = None,
) -> tuple[dict, str]:
    """Convenience: parse + decrypt one email body. Returns the inner
    record dict and the marker that was matched.
    """
    marker, env = extract_envelope(body)
    record = decrypt_envelope(env, private_key_pem)
    # Verification is not enforced (matches production v2). We just
    # record whether the kid is known so the caller can log it.
    if kid_secrets and env.kid in kid_secrets:
        verify_mac(env, kid_secrets[env.kid])
    return record, marker
