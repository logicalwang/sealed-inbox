# Secure Record Wire Protocol (v1)

The receiver accepts any inbound message whose body is a single
envelope in the format described below. Senders can be web forms,
mobile apps, CLI scripts, or anything else that can paste a marker
line and a JSON object into an email body.

This document is the complete specification. If you follow it, your
sender will be compatible with the receiver.

## 1. The envelope

A sender produces the following JSON object, then prefixes it with a
single marker line and pretty-prints the JSON with two-space
indentation:

```
OPENCLAW_SECURE_RECORD_V1
{
  "v": 1,
  "kid": "<kid string>",
  "ts": 1756372800000,
  "nonce": "<base64url 16 bytes>",
  "alg": "RSA-OAEP-SHA256+AES-256-GCM",
  "ek":   "<base64url RSA-OAEP encrypted AES-256 key>",
  "iv":   "<base64url 12-byte AES-GCM nonce>",
  "ct":   "<base64url ciphertext || 16-byte GCM tag>",
  "mac":  "<base64url HMAC-SHA256>"
}
```

The body may have HTML-escaped characters (Formspree does this); the
receiver runs `html.unescape` over the JSON before parsing.

The receiver also accepts the alias marker
`HERMES_SECURE_RECORD_V1`. Both are recognised by the same code path;
whichever appears first in the body wins.

### Field reference

| Field | Type | Notes |
|---|---|---|
| `v` | int | Protocol version, must be `1`. |
| `kid` | string | A sender-chosen label (e.g. which device/form sent this). The receiver does **not** verify it or the `mac` — see §6. |
| `ts` | int | Unix epoch in **milliseconds** at which the sender produced the record. |
| `nonce` | base64url | 16 random bytes. Reserved for replay protection; the receiver currently does not check uniqueness. |
| `alg` | string | Informational. Must be `"RSA-OAEP-SHA256+AES-256-GCM"`. |
| `ek` | base64url | RSA-OAEP (SHA-256, MGF1-SHA-256) encryption of a freshly-generated AES-256 key. |
| `iv` | base64url | 12-byte AES-GCM nonce. |
| `ct` | base64url | AES-256-GCM ciphertext with the 16-byte GCM tag appended. |
| `mac` | base64url | `HMAC-SHA256(kid_secret, "1\|kid\|ts\|nonce\|ek\|iv\|ct")` (note: the literal `1` is the protocol version, not a field value). |

### base64url encoding

Standard base64 (RFC 4648 §5) with `+` → `-`, `/` → `_`, and the
trailing `=` padding stripped. The receiver compensates for missing
padding by re-adding `==` before calling `base64.urlsafe_b64decode`.

## 2. The inner record

The AES-GCM plaintext is itself a JSON object (UTF-8 encoded). The
receiver appends it to `records.csv` with the column order:

| Column | Type | Notes |
|---|---|---|
| `timestamp` | ISO 8601 string | The moment the value was measured. May also be a unix millisecond integer; the receiver formats it as `YYYY-MM-DDTHH:MM`. |
| `glucose_value` | number | The measurement. The chart y-axis. |
| `unit` | string | e.g. `mmol/L`. |
| `context` | string | e.g. `空腹`, `餐后2h`. |
| `note` | string | Optional free text. |
| `source` | string (constant) | The receiver writes `github-pages-secure-relay-form` here. |

The CSV is written in `utf-8-sig` so that Excel on Windows opens it
without mojibake. Senders may include additional fields; the receiver
only stores the columns above.

## 3. The crypto

* **Key encapsulation**: RSA-OAEP with SHA-256, MGF1-SHA-256. The
  receiver's key is RSA-2048. The on-wire length of `ek` is therefore
  256 bytes; v1 is fixed at 2048-bit RSA.
* **Symmetric cipher**: AES-256-GCM, 12-byte nonce, 16-byte tag.
* **AAD**: `None` (no additional authenticated data).
* **Replay protection**: the receiver currently does not enforce an
  `iat` window. The `nonce` field is reserved for future use; if you
  implement sender-side replay protection, include the message id in
  the nonce.

## 4. Reference sender (Python)

```python
import base64, hashlib, hmac, json, secrets, time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALG = "RSA-OAEP-SHA256+AES-256-GCM"
MARKER = "OPENCLAW_SECURE_RECORD_V1"


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def build(record, public_key_pem, kid, kid_secret):
    aes_key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)

    plaintext = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ct_with_tag = AESGCM(aes_key).encrypt(iv, plaintext, None)

    pub = serialization.load_pem_public_key(public_key_pem)
    ek = pub.encrypt(aes_key, padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(), label=None,
    ))

    ts = int(time.time() * 1000)
    ek_b64 = b64url(ek)
    iv_b64 = b64url(iv)
    ct_b64 = b64url(ct_with_tag)
    nonce_b64 = b64url(nonce)

    canonical = "|".join(["1", kid, str(ts), nonce_b64, ek_b64, iv_b64, ct_b64])
    mac = hmac.new(kid_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()

    envelope = {
        "v": 1, "kid": kid, "ts": ts, "nonce": nonce_b64, "alg": ALG,
        "ek": ek_b64, "iv": iv_b64, "ct": ct_b64, "mac": b64url(mac),
    }
    return f"{MARKER}\n{json.dumps(envelope, indent=2, ensure_ascii=False)}\n"
```

A full, runnable version is in `src/sender.py`.

## 5. Generating a receiver keypair

```bash
python3 -c "from pathlib import Path; from src.sender import generate_keypair; \
            generate_keypair(Path('keys'))"
# → keys/record_decrypt_private.pem
# → keys/record_encrypt_public.pem
```

Distribute `record_encrypt_public.pem` to your sender. **Never** share
the private key.

## 6. kid registry

`kid_secrets.json` is a **sender-side** convenience file; the receiver
never enforces it:

```json
{
  "phone-form-2026":   {"secret": "<32+ char random>", "enabled": true},
  "expired-form-2025": {"secret": "<unused>",          "enabled": false,
                         "expires_at": 1735689600}
}
```

The `secret` is what senders use to compute the envelope's `mac`. The
receiver reads the file only to *log* whether a record's `mac` would
have verified — **it does not check the `mac`, does not require `kid`
to be registered, and does not filter on `enabled`/`expires_at`**
(matching the production v2 receiver). Consequently the real
submission capability is possession of the RSA public key: rotate the
keypair if it leaks, and treat `kid` as a human-readable label, not an
authentication mechanism.

## 7. Failure modes the receiver surfaces

| Condition | Receiver action |
|---|---|
| Email body does not contain either marker | log + skip |
| `html.unescape` + brace-matching finds no JSON object | log + skip |
| Envelope missing any required field | log + skip |
| `ek` not valid base64url | log + skip |
| `iv` is not 12 bytes | log + skip |
| RSA-OAEP decryption fails | log + skip |
| AES-GCM tag mismatch | log + skip |
| Plaintext is not valid JSON | log + skip |

All failures are non-fatal; the pipeline moves on to the next message.

## 8. Versioning

The `v` field in the envelope is the only version negotiation. v1 is
the only currently-defined version. Future versions may add new
fields inside the envelope but must not change the meaning of `kid`,
`ts`, `ek`, `iv`, or `ct` for v1 compatibility.
