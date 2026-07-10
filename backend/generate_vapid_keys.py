"""
Generate a fresh VAPID key pair for Web Push notifications.

Produces:
  - An EC P-256 private key in PEM format  (for VAPID_PRIVATE_KEY_PEM env var / vapid_keys.json)
  - The matching uncompressed public key in URL-safe base64 (for VAPID_PUBLIC_KEY env var / vapid_keys.json)

Usage:
    python generate_vapid_keys.py              # prints keys + writes vapid_keys.json
    python generate_vapid_keys.py --no-write   # prints keys only, doesn't touch any file
"""

import argparse
import base64
import json
import os

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def generate_vapid_keys() -> dict:
    """Generate a new VAPID key pair and return as a dict."""
    # 1. Generate an EC private key on the P-256 (secp256r1) curve
    private_key = ec.generate_private_key(ec.SECP256R1())

    # 2. Serialize private key as PKCS8 PEM  (what pywebpush / py_vapid expect)
    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")

    # 3. Get the 65-byte uncompressed public key (0x04 || x || y)
    public_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.UncompressedPoint,
    )

    # 4. URL-safe base64 encode, strip padding  (Web Push / applicationServerKey format)
    public_b64 = base64.urlsafe_b64encode(public_bytes).decode("utf-8").rstrip("=")

    return {
        "private_key_pem": private_pem,
        "public_key": public_b64,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate VAPID keys for Web Push")
    parser.add_argument("--no-write", action="store_true",
                        help="Only print keys, don't write vapid_keys.json")
    args = parser.parse_args()

    keys = generate_vapid_keys()

    print("=" * 60)
    print("VAPID KEY PAIR GENERATED SUCCESSFULLY")
    print("=" * 60)

    print("\n--- Private Key (PEM) ---")
    print(keys["private_key_pem"])

    print("--- Public Key (URL-safe base64, no padding) ---")
    print(keys["public_key"])
    print(f"  Length: {len(keys['public_key'])} chars")
    print(f"  Raw bytes: {len(base64.urlsafe_b64decode(keys['public_key'] + '=='))} bytes (must be 65)")

    out_path = os.path.join(os.path.dirname(__file__), "vapid_keys.json")

    if not args.no_write:
        # Back up old file if it exists
        if os.path.exists(out_path):
            backup = out_path + ".bak"
            os.replace(out_path, backup)
            print(f"\n[OK] Old keys backed up to: {backup}")

        with open(out_path, "w") as f:
            json.dump(keys, f, indent=2)
        print(f"[OK] New keys written to:   {out_path}")
    else:
        print(f"\n(--no-write: {out_path} was NOT modified)")

    print("\n--- For Render env vars ---")
    # Show the single-line version for pasting into Render dashboard
    pem_oneline = keys["private_key_pem"].strip().replace("\n", "\\n")
    print(f'VAPID_PRIVATE_KEY_PEM = {pem_oneline}')
    print(f'VAPID_PUBLIC_KEY      = {keys["public_key"]}')

    print("\n[!] IMPORTANT: After changing VAPID keys, all existing browser")
    print("   push subscriptions become invalid. Users must re-enable alerts.")
    print("=" * 60)


if __name__ == "__main__":
    main()
