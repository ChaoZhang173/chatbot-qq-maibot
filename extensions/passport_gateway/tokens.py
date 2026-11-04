"""Device CREDENTIAL administration (token hash + enabled flag only). Identity is a separate concern.

    python -m passport_gateway.tokens --bindings F --device-id D            # issue (refuses to replace an existing one)
    python -m passport_gateway.tokens --bindings F --device-id D --rotate   # replace: the old token stops working
    python -m passport_gateway.tokens --bindings F --device-id D --disable  # credential off; no token is issued or shown
    python -m passport_gateway.tokens --bindings F --device-id D --enable
    python -m passport_gateway.tokens --bindings F --list                    # device ids + enabled/disabled, never a hash

The file holds NO person / profile / QQ / session / role: which person a device belongs to is decided only by the
chosen backend. The Dafeiyu adapter uses unified_core.passport_admin. A new token is printed once; only its SHA-256 is stored.
The file is replaced atomically (0600) under a lock; a malformed file is never overwritten.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

from .config import DEVICE_ID_RE, token_hash


def _load(path: Path) -> dict:
    if not path.exists():
        return {"devices": {}}
    try:
        data = json.loads(path.read_text("utf-8"))
        devices = data["devices"]
        if not isinstance(devices, dict) or any(not isinstance(v, dict) for v in devices.values()):
            raise ValueError("devices")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit("error: binding file is malformed; refusing to overwrite it") from exc
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pp-bindings-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m passport_gateway.tokens")
    ap.add_argument("--device-id")
    ap.add_argument("--bindings", required=True, help="binding JSON file to create/update (mode 0600)")
    ap.add_argument("--rotate", action="store_true", help="replace the credential of an existing device")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.bindings)
    if args.list:
        devices = _load(path)["devices"]
        for dev in sorted(devices):
            print(f"{dev}  {'enabled' if devices[dev].get('enabled', True) is True else 'disabled'}")
        return 0
    if not args.device_id or not DEVICE_ID_RE.match(args.device_id):
        print("device id must match [A-Za-z0-9_-]{1,32}", file=sys.stderr)
        return 2
    if args.disable and args.enable:
        print("choose one of --enable / --disable", file=sys.stderr)
        return 2
    with _locked(path):
        data = _load(path)
        devices = data.setdefault("devices", {})
        entry = devices.get(args.device_id)
        if args.disable or args.enable:
            if entry is None:
                print("no credential exists for that device", file=sys.stderr)
                return 2
            entry["enabled"] = bool(args.enable)
            _write_atomic(path, data)
            print(f"device {args.device_id}: credential {'enabled' if args.enable else 'disabled'} (no token issued).")
            return 0
        if entry is not None and not args.rotate:
            print("a credential already exists for this device; pass --rotate to replace it "
                  "(the old token stops working at once)", file=sys.stderr)
            return 2
        token = secrets.token_urlsafe(32)
        devices[args.device_id] = {"token_sha256": token_hash(token), "enabled": True}
        _write_atomic(path, data)
    print(f"device {args.device_id}: binding written to {path} (hash only). "
          "Assign the device to a person in your backend (Dafeiyu: unified_core.passport_admin).")
    print("Device token (shown once; provision it into the device NVS, never commit it):")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
