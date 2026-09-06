#!/usr/bin/env python3
"""Compose the one-tap v6 link for the family group.

Reads the production instance's own files (config.yaml, kid_secrets.json,
public key) and prints a single URL whose #hash carries the full form
config + current dashboard address. The hash never reaches any server —
browsers don't send fragments — so the link is safe to paste in Telegram.

Usage: make_v6_link.py [--app-dir ~/apps/sealed-inbox] --dash <current dashboard tunnel URL>
Env:   FORM_URL (your GitHub Pages sender URL, see notify.env)
       RELAY_URL (the form-relay endpoint configured on the sender page)
"""
import argparse, base64, json, os, sys, urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # deploy/termux → 仓库根

def b64u(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).rstrip(b"=").decode("ascii")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", default=os.path.expanduser("~/apps/sealed-inbox"))
    ap.add_argument("--dash", required=True, help="current dashboard tunnel URL")
    ap.add_argument("--kid", default=None, help="pick a specific kid (default: first enabled)")
    ap.add_argument("--form-url", default=os.environ.get(
        "FORM_URL", "https://<owner>.github.io/secure-relay-fast-v6.html"))
    ap.add_argument("--relay-url", default=os.environ.get("RELAY_URL", ""),
                    help="form-relay endpoint (relay_post mode)")
    args = ap.parse_args()
    app = Path(os.path.expanduser(args.app_dir))

    from src.config import load_config
    cfg = load_config(app / "config.yaml")
    kids = json.loads((app / "kid_secrets.json").read_text())
    enabled = [(k, v["secret"]) for k, v in kids.items()
               if isinstance(v, dict) and v.get("enabled") and v.get("secret")]
    if not enabled:
        print("no enabled kid in kid_secrets.json", file=sys.stderr); return 1
    kid, secret = ((args.kid, kids[args.kid]["secret"]) if args.kid else enabled[0])

    pub = (app / "keys" / "record_encrypt_public.pem").read_text().strip()

    params = {
        "recipientEmail": cfg.imap.username,
        "subjectPrefix": cfg.imap.subject_prefix,
        "sendMode": "relay_post",
        "keyId": kid,
        "pub": b64u(pub),
        "authToken": secret,
        "dash": args.dash,
    }
    if args.relay_url:
        params["relayUrl"] = args.relay_url
        params["relayBodyField"] = "message"
        params["relaySubjectField"] = "subject"
        params["relayToField"] = "to"
    fragment = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    print(f"{args.form_url}#{fragment}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
