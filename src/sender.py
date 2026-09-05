"""Reference sender for OpenClaw Secure Record v4 envelopes.

Mirrors the JavaScript implementation in
``secure-relay-fast-v4.html``. The shape of the envelope, the field
names, the canonical HMAC string, and the base64url encoding all
match the production frontend so the receiver can read whatever the
frontend produces.

Wire format reminder:

    OPENCLAW_SECURE_RECORD_V1
    {
      "v": 1,
      "kid": "<kid string>",
      "ts": <ms since epoch>,
      "nonce": "<base64url 16 bytes>",
      "alg": "RSA-OAEP-SHA256+AES-256-GCM",
      "ek":   "<base64url RSA-OAEP encrypted AES-256 key>",
      "iv":   "<base64url 12-byte AES-GCM nonce>",
      "ct":   "<base64url ciphertext||16-byte GCM tag>",
      "mac":  "<base64url HMAC-SHA256>"
    }

The HMAC is computed over the canonical string
``"1|kid|ts|nonce|ek|iv|ct"`` (note: ``"1"`` is the protocol version
literal, not a field value). The receiver does not verify it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.envelope import b64url_encode

ALG = "RSA-OAEP-SHA256+AES-256-GCM"
MARKER = "OPENCLAW_SECURE_RECORD_V1"


def generate_keypair(out_dir: Path) -> tuple[Path, Path]:
    """Generate a fresh RSA-2048 keypair.

    ``out_dir`` must be a :class:`pathlib.Path`; passing a string is a
    programming error (this used to silently misbehave).
    """
    if not isinstance(out_dir, Path):
        raise TypeError(
            f"generate_keypair expects a Path, got {type(out_dir).__name__}. "
            "Wrap your path with pathlib.Path() before passing it in."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    priv_path = out_dir / "record_decrypt_private.pem"
    pub_path = out_dir / "record_encrypt_public.pem"
    priv_path.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path.write_bytes(
        pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return priv_path, pub_path


@dataclass
class Built:
    """A successfully built envelope plus the email body it should be
    sent in. The body is exactly what the receiver expects to see.
    """
    envelope: dict
    body: str  # the literal email body, including the marker line


def build(record: dict, public_key_pem: bytes, kid: str, kid_secret: str) -> Built:
    """Build a v4 envelope and email body for ``record``.

    The envelope is JSON-serialised with the same field order the JS
    form uses. The body is the marker line followed by the pretty
    JSON exactly as :func:`json.dumps(..., indent=2)` would produce.
    """
    if not isinstance(public_key_pem, bytes):
        raise TypeError("public_key_pem must be bytes")
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")

    aes_key = secrets.token_bytes(32)
    nonce_bytes = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)

    plaintext = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ct_with_tag = AESGCM(aes_key).encrypt(iv, plaintext, None)

    pub = serialization.load_pem_public_key(public_key_pem)
    ek = pub.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    ts = int(time.time() * 1000)
    ek_b64 = b64url_encode(ek)
    iv_b64 = b64url_encode(iv)
    ct_b64 = b64url_encode(ct_with_tag)
    nonce_b64 = b64url_encode(nonce_bytes)

    # The canonical HMAC string is version-literal, then fields joined
    # by "|", with NO field reordering. The JS code does this exactly:
    #   ["1", kid, ts, nonce, ek, iv, ct].join("|")
    canonical = "|".join(["1", kid, str(ts), nonce_b64, ek_b64, iv_b64, ct_b64])
    mac = hmac.new(kid_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    mac_b64 = b64url_encode(mac)

    envelope = {
        "v": 1,
        "kid": kid,
        "ts": ts,
        "nonce": nonce_b64,
        "alg": ALG,
        "ek": ek_b64,
        "iv": iv_b64,
        "ct": ct_b64,
        "mac": mac_b64,
    }
    body = f"{MARKER}\n{json.dumps(envelope, indent=2, ensure_ascii=False)}\n"
    return Built(envelope=envelope, body=body)


def new_kid(kid: str, kid_secrets_path: Path, *, length: int = 32,
            force: bool = False) -> str:
    """Generate a random kid secret and upsert it into ``kid_secrets.json``.

    Creates the file if missing. If ``kid`` already exists it is left
    untouched unless ``force=True`` (rotation) — silently replacing a
    secret would break an already-configured sender. Returns the
    (new or existing untouched) secret.
    """
    if not re.fullmatch(r"[\w.-]{1,64}", kid):
        raise ValueError(
            "kid may contain letters, digits, '_', '-', '.' (max 64 chars); "
            f"got {kid!r}"
        )
    if kid_secrets_path.exists():
        data = json.loads(kid_secrets_path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{kid_secrets_path} is not a JSON object")
    else:
        data = {}
    if kid in data and not force:
        existing = data[kid].get("secret") if isinstance(data[kid], dict) else None
        print(f"kid {kid!r} already exists in {kid_secrets_path}; "
              "leaving it untouched (use --force to rotate the secret).")
        if existing:
            return existing
        raise ValueError(f"kid {kid!r} exists but has no secret; use --force")
    secret = secrets.token_urlsafe(length)  # ~43 chars, same shape as any sender
    data[kid] = {"secret": secret, "enabled": True}
    kid_secrets_path.parent.mkdir(parents=True, exist_ok=True)
    kid_secrets_path.write_text(json.dumps(data, indent=2) + "\n")
    return secret


def main(argv: list[str] | None = None) -> int:
    """Setup CLI: ``python3 -m src.sender {generate,new-kid}``."""
    import argparse

    p = argparse.ArgumentParser(
        prog="python3 -m src.sender",
        description="One-time setup for the receiver: keypair + kid secrets.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate the receiver RSA-2048 keypair")
    g.add_argument("out_dir", nargs="?", default="keys",
                   help="output directory (default: ./keys)")

    k = sub.add_parser("new-kid", help="create a kid + secret in kid_secrets.json")
    k.add_argument("kid", help="a label for the sender, e.g. phone-form")
    k.add_argument("--out", default="kid_secrets.json",
                   help="registry file (default: ./kid_secrets.json — matches config)")
    k.add_argument("--force", action="store_true",
                   help="rotate the secret if the kid already exists "
                        "(invalidates senders configured with the old one)")

    args = p.parse_args(argv)
    if args.cmd == "generate":
        priv, pub = generate_keypair(Path(args.out_dir))
        print(f"private key: {priv}  (never share, never commit)")
        print(f"public key:  {pub}  (paste this into the sender web form)")
        return 0
    if args.cmd == "new-kid":
        secret = new_kid(args.kid, Path(args.out), force=args.force)
        print(f"kid:    {args.kid}")
        print(f"secret: {secret}")
        print("→ put kid + secret into the sender web form (auth token).")
        print(f"→ stored in {args.out}; the receiver only reads it for logging.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
