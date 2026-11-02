"""Create a device token + the server-side binding entry. The token is printed once."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from .config import DEVICE_ID_RE, token_hash


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m passport_gateway.tokens")
    ap.add_argument("--device-id", required=True)
    ap.add_argument("--bindings", required=True, help="binding JSON file to create/update (mode 0600)")
    args = ap.parse_args(argv)
    if not DEVICE_ID_RE.match(args.device_id):
        print("device id must match [A-Za-z0-9_-]{1,32}", file=sys.stderr)
        return 2
    path = Path(args.bindings)
    data = json.loads(path.read_text("utf-8")) if path.exists() else {"devices": {}}
    token = secrets.token_urlsafe(32)
    data.setdefault("devices", {})[args.device_id] = {"token_sha256": token_hash(token), "enabled": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")
    print(f"device {args.device_id}: binding written to {path} (hash only).")
    print("Device token (shown once; provision it into the device NVS, never commit it):")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
