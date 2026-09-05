"""T1: a v4 sender (this repo's ``src.sender``) produces an envelope
that is accepted by both:

  * the production receiver ``process_email_v2.py`` (read-only, not
    modified) — IF ``$PROD_RECEIVER_PATH`` points at it.
  * this repo's ``src.envelope.decrypt_email_body``

and both decrypt to the same inner record.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from src.sender import build, generate_keypair  # noqa: E402
from src.envelope import decrypt_email_body  # noqa: E402

# Path to the reference production receiver used as a source-of-truth
# oracle. Override with $PROD_RECEIVER_PATH; default empty so the test
# does not need to know about any specific deployment.
PROD = Path(os.environ.get("PROD_RECEIVER_PATH", ""))


def main() -> int:
    work = ROOT / "tests" / "_t1_work"
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    priv, pub = generate_keypair(work / "keys")
    record = {
        "timestamp": "2026-08-29T07:30",
        "glucose_value": 5.8,
        "unit": "mmol/L",
        "context": "空腹",
        "note": "T1 self-test",
    }
    built = build(record, pub.read_bytes(), "t1-kid", "t1-secret")

    # 1. This repo's receiver.
    mine, marker = decrypt_email_body(built.body, priv.read_bytes(),
                                       {"t1-kid": "t1-secret"})
    assert marker == "OPENCLAW_SECURE_RECORD_V1", f"unexpected marker: {marker}"
    assert mine == record, f"this repo's receiver returned {mine!r}, expected {record!r}"
    print(f"OK this-repo:    kid={built.envelope['kid']} -> {mine}")

    # 2. Reference production receiver, if available. We never write
    #    anywhere; we just call its pure-Python extract + decrypt
    #    functions.
    if PROD.is_file():
        spec = importlib.util.spec_from_file_location("v2", PROD)
        assert spec is not None
        prod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prod)  # type: ignore[union-attr]

        parsed = prod.extract_encrypted_payload(built.body)
        assert parsed is not None, "production receiver rejected the envelope"
        prod_pk = serialization.load_pem_private_key(priv.read_bytes(), password=None)
        decrypted = prod.decrypt_record(parsed, prod_pk)
        assert decrypted == record, f"prod returned {decrypted!r}, expected {record!r}"
        print(f"OK production:   kid={parsed['kid']} -> {decrypted}")
    else:
        print(f"SKIP production oracle: set $PROD_RECEIVER_PATH to enable "
              f"(looked for {PROD!r})")

    # 3. Sanity: confirm the envelope shape the test exercised
    assert set(built.envelope) == {"v", "kid", "ts", "nonce", "alg", "ek", "iv", "ct", "mac"}
    print(f"OK envelope fields: {sorted(built.envelope)}")

    # 4. Wire body must start with the marker line, then pretty JSON.
    first_line, *rest = built.body.splitlines()
    assert first_line == "OPENCLAW_SECURE_RECORD_V1"
    parsed_json = json.loads("\n".join(rest))
    assert parsed_json == built.envelope
    print("OK wire body:    marker line + pretty JSON; round-trip OK")

    print("\nT1 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
