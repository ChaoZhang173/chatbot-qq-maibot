"""Administrative CLI for the Passport person/profile identity file (no network, no new service).

    python -m unified_core.passport_admin --root /srv/maibot/unified <command> [options] [--apply]

Every mutating command is a DRY RUN unless --apply is given. The file is validated before and after, written
atomically (0600, one private .bak of the previous version) under an exclusive lock. A malformed file is never
overwritten. The gateway credential file (device tokens) is a different file and is never written here: enabling
a device credential (passport_gateway.tokens) and binding a device to a person (this tool) are separate steps.
No secret exists in the identity file, so nothing secret can appear in list output, logs or errors.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import identity as ident


class AdminError(Exception):
    pass


def _name(value):
    value = (value or "").strip()
    if not 1 <= len(value) <= 32 or re.search(r"[\x00-\x1f]", value):
        raise AdminError("display name must be 1-32 printable characters (it is metadata, never a permission)")
    return value


def _address(value):
    if not ident.valid_address(value):
        raise AdminError("preferred address must be 1-16 letters/digits/CJK/space/-/_ (spoken form only; it never grants a role)")
    return value


def _qq(value):
    if not isinstance(value, str) or not ident.QQ_NUMBER_RE.match(value):
        raise AdminError("QQ number must be 5-12 digits")
    return value


def _profile(data, pid):
    if pid not in data["profiles"]:
        raise AdminError("unknown profile")
    return data["profiles"][pid]


def _readonly_state(root):
    path = Path(root) / "state.sqlite"
    if not path.exists():
        raise AdminError("state.sqlite not found under --root: cannot verify a QQ private binding")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _policy(root):
    try:
        policy = json.loads((Path(root) / "policy.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise AdminError("policy.json unreadable") from exc
    if not isinstance(policy, dict):
        raise AdminError("policy.json malformed")
    return policy


def apply_command(root, data, args):
    """Return a list of human-readable change lines; mutates `data` (a private copy)."""
    cmd, changes = args.command, []
    if cmd == "create-profile":
        pid = ident.new_profile_id()
        while pid in data["profiles"]:
            pid = ident.new_profile_id()
        number = _qq(args.qq) if args.qq else None
        data["profiles"][pid] = ident.new_profile(_name(args.name), "friend", number,
                                                  _address(args.address) if args.address else None)
        changes.append(f"create profile {pid} role=friend qq={'pending' if number else 'none'}")
    elif cmd in ("enable-profile", "disable-profile"):
        _profile(data, args.profile)["enabled"] = cmd == "enable-profile"
        changes.append(f"{cmd} {args.profile}")
    elif cmd == "set-address":
        auto = getattr(args, 'auto', False)
        _profile(data, args.profile)["preferred_address"] = '' if auto else None if args.clear else _address(args.address)
        changes.append(f"{args.profile}: spoken address " + ("autonomous" if auto else "cleared" if args.clear else "set (presentation only)"))
    elif cmd == "rename-profile":
        _profile(data, args.profile)["display_name"] = _name(args.name)
        changes.append(f"rename {args.profile}")
    elif cmd == "set-qq-pending":
        qq = _profile(data, args.profile)["qq"]
        if qq["state"] == "verified":
            raise AdminError("profile has a verified QQ link: unlink-qq first")
        qq.update(state="pending", number=_qq(args.qq))
        changes.append(f"{args.profile}: QQ number recorded as PENDING (grants nothing)")
    elif cmd == "clear-qq":
        qq = _profile(data, args.profile)["qq"]
        if qq["state"] == "verified":
            raise AdminError("profile has a verified QQ link: unlink-qq first")
        qq.update(state="none", number=None)
        changes.append(f"{args.profile}: QQ note cleared")
    elif cmd == "verify-qq":
        profile = _profile(data, args.profile)
        qq = profile["qq"]
        if qq["state"] != "pending":
            raise AdminError("profile must have a pending QQ number first (set-qq-pending / create-profile --qq)")
        if args.confirm_number != qq["number"]:
            raise AdminError("--confirm-number does not match the pending number: the administrator must confirm who this is")
        for pid, other in data["profiles"].items():
            if pid != args.profile and other["qq"]["private_id"] == args.private_id:
                raise AdminError("that QQ private binding already belongs to another profile")
        db = _readonly_state(root)
        try:
            session = ident.check_qq_link(db, _policy(root), args.private_id, ident.legacy_devices(root), qq["number"])
        except ValueError as exc:
            raise AdminError("QQ private binding rejected: " + str(exc)) from exc
        finally:
            db.close()
        qq.update(state="verified", private_id=args.private_id, session=session, confirmed_at=time.time())
        changes.append(f"{args.profile}: QQ private binding VERIFIED; Passport turns for this profile will now use that private session")
    elif cmd == "unlink-qq":
        qq = _profile(data, args.profile)["qq"]
        if qq["state"] != "verified":
            raise AdminError("profile has no verified QQ link")
        qq.update(state="pending", private_id=None, session=None, confirmed_at=None)
        changes.append(f"{args.profile}: QQ link removed (number stays pending); turns return to the Passport-native session")
    elif cmd == "assign-device":
        if not ident.DEVICE_RE.match(args.device):
            raise AdminError("invalid device id")
        _profile(data, args.profile)
        if args.device in ident.legacy_devices(root):
            raise AdminError("device is in the legacy owner voice_bindings.json: it is not touched or remapped here")
        current = data["devices"].get(args.device)
        if current and current["profile_id"] != args.profile and not args.reassign:
            raise AdminError("device is assigned to another profile: pass --reassign to move it")
        data["devices"][args.device] = {"profile_id": args.profile, "enabled": True}
        changes.append(f"assign device {args.device} -> {args.profile}" + (" (reassigned)" if current else ""))
    elif cmd == "unassign-device":
        if args.device not in data["devices"]:
            raise AdminError("device is not assigned")
        del data["devices"][args.device]
        changes.append(f"unassign device {args.device}")
    elif cmd in ("enable-device", "disable-device"):
        if args.device not in data["devices"]:
            raise AdminError("device is not assigned")
        data["devices"][args.device]["enabled"] = cmd == "enable-device"
        changes.append(f"{cmd} {args.device}")
    else:
        raise AdminError("unknown command")
    return changes


def credential_flags(path):
    """Read-only look at the gateway credential file: presence/enabled only, never any hash."""
    try:
        devices = json.loads(Path(path).read_text("utf-8"))["devices"]
        return {k: ("enabled" if v.get("enabled", True) is True else "disabled") for k, v in devices.items()}
    except (OSError, ValueError, KeyError, AttributeError):
        raise AdminError("gateway credential file unreadable")


def listing(root, data, bindings=None):
    creds = credential_flags(bindings) if bindings else None
    legacy = ident.legacy_devices(root)
    lines = [f"revision {data['revision']}  profiles {len(data['profiles'])}  devices {len(data['devices'])}"]
    for pid, p in sorted(data["profiles"].items()):
        qq = p["qq"]
        shown = {"none": "none", "pending": f"pending({ident.mask_qq(qq['number'])})",
                 "verified": f"verified({ident.mask_qq(qq['number'])})"}[qq["state"]]
        lines.append(f"{pid}  {p['display_name']!r}  address={p['preferred_address']!r}  role={p['role']}  {'enabled' if p['enabled'] else 'DISABLED'}  qq={shown}")
    for dev, d in sorted(data["devices"].items()):
        extra = ""
        if creds is not None:
            extra = "  gateway-credential=" + creds.get(dev, "MISSING")
        if dev in legacy:
            extra += "  CONFLICT:also-in-legacy-owner-file(runtime fails closed)"
        lines.append(f"device {dev} -> {d['profile_id']}  {'enabled' if d['enabled'] else 'DISABLED'}{extra}")
    return lines


def validate_all(root, data):
    problems = []
    legacy = ident.legacy_devices(root)
    for dev in data["devices"]:
        if dev in legacy:
            problems.append(f"device {dev}: also in the legacy owner file")
    links = [(pid, p["qq"]) for pid, p in data["profiles"].items() if p["qq"]["state"] == "verified"]
    if links:
        try:
            db, policy = _readonly_state(root), _policy(root)
        except AdminError as exc:
            problems.append("cannot re-check verified QQ links: " + str(exc))
        else:
            for pid, qq in links:
                try:
                    if ident.check_qq_link(db, policy, qq["private_id"], legacy, qq["number"]) != qq["session"]:
                        problems.append(f"{pid}: QQ link session no longer matches the private binding")
                except ValueError as exc:
                    problems.append(f"{pid}: QQ link no longer valid ({exc}); runtime falls closed for it")
            db.close()
    return problems


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m unified_core.passport_admin")
    ap.add_argument("--root", required=True, help="unified core root (policy.json, state.sqlite, voice_bindings.json)")
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("list")
    s.add_argument("--bindings", help="gateway credential file (read only; shows only enabled/disabled/missing)")
    sub.add_parser("validate")
    s = sub.add_parser("create-profile")
    s.add_argument("--name", required=True)
    s.add_argument("--qq", help="optional QQ number, recorded as PENDING only")
    s.add_argument("--address", help="optional spoken address preference (presentation only)")
    s = sub.add_parser("set-address")
    s.add_argument("--profile", required=True)
    mode = s.add_mutually_exclusive_group(required=True)
    mode.add_argument("--address")
    mode.add_argument("--clear", action="store_true", help="restore display-name fallback")
    mode.add_argument("--auto", action="store_true", help="let the model choose the spoken address")
    for name in ("enable-profile", "disable-profile", "clear-qq", "unlink-qq"):
        sub.add_parser(name).add_argument("--profile", required=True)
    s = sub.add_parser("rename-profile")
    s.add_argument("--profile", required=True)
    s.add_argument("--name", required=True)
    s = sub.add_parser("set-qq-pending")
    s.add_argument("--profile", required=True)
    s.add_argument("--qq", required=True)
    s = sub.add_parser("verify-qq")
    s.add_argument("--profile", required=True)
    s.add_argument("--private-id", required=True, help="private_id of an existing verified qq_private_bindings row")
    s.add_argument("--confirm-number", required=True, help="the pending QQ number, re-typed by the administrator")
    s = sub.add_parser("assign-device")
    s.add_argument("--profile", required=True)
    s.add_argument("--device", required=True)
    s.add_argument("--reassign", action="store_true")
    for name in ("unassign-device", "enable-device", "disable-device"):
        sub.add_parser(name).add_argument("--device", required=True)
    return ap


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    try:
        if args.command in ("list", "validate"):
            data = ident.load(root)
            if args.command == "list":
                print("\n".join(listing(root, data, args.bindings)), file=out)
                return 0
            problems = validate_all(root, data)
            print("\n".join(problems) if problems else "ok", file=out)
            return 1 if problems else 0
        with ident.locked(root):
            data = ident.load(root)                      # malformed => IdentityConfigError, file untouched
            before = copy.deepcopy(data)
            changes = apply_command(root, data, args)
            data["revision"] = before["revision"] + 1
            ident.validate(data)
            print("\n".join(("" if args.apply else "[dry run] ") + c for c in changes), file=out)
            if args.apply:
                ident.write_atomic(root, data)
                print(f"written: revision {data['revision']}", file=out)
            else:
                print("dry run: nothing written (add --apply)", file=out)
        return 0
    except (AdminError, ident.IdentityConfigError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
