"""Server-owned per-person Passport identity (profiles, device assignment, optional verified QQ link).

Authority model (see backend_patch/README.md, "Per-person identity"):
  * <root>/passport_identity.json is written ONLY by the administrative CLI (passport_admin.py). A device
    never names a profile, QQ number, session or role: it sends {device_id, turn_uid, text} and the server
    resolves device -> profile -> route here.
  * The owner is NOT a profile. The owner device stays on the legacy <root>/voice_bindings.json path
    (owner_private_id + session) which this module never reads, rewrites or reinterprets. The only role a
    profile can have is "friend"; any other value makes the file invalid.
  * QQ is optional. A QQ number is stored as a pending note and grants nothing. The association becomes
    "verified" only when an administrator confirms an EXISTING verified qq_private_bindings row (the same
    challenge the familiar-private flow already uses) AND there is trusted evidence that this account is that
    number (see number_evidenced); the runtime re-checks both on every turn. Without evidence it stays pending.
  * Malformed / inconsistent file => every profile route fails closed ('voice_unbound'). The legacy owner
    path lives in a different file and is unaffected.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

FILE_NAME = "passport_identity.json"
VERSION = 1
PROFILE_RE = re.compile(r"^pf_[0-9a-f]{16}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
QQ_NUMBER_RE = re.compile(r"^[1-9][0-9]{4,11}$")
PRIVATE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Spoken-address preference (e.g. 主人). Presentation only: it is never read for role, tools, scope or routing.
ADDRESS_RE = re.compile(r"^[\w\u4e00-\u9fff\u00b7 -]{1,16}$")
SESSION_RE = re.compile(r"^[^\x00-\x1f]{1,256}$")
ROLES = ("friend",)                 # "owner" is deliberately impossible here
QQ_STATES = ("none", "pending", "verified")
PASSPORT_SCOPE = "passport"         # Host route scope of Passport-native private sessions

# Tools a friend profile may use through the Host registry (Host-enforced allowlist; everything else - the
# Host's unscoped query_memory / query_person_profile, sub-agents, image/emoji/forward tools, gallery, B站
# actions, image sending, MCP and agent tools - is refused at the Host dispatch point).
FRIEND_TOOLS = ("reply", "wait", "stop", "tool_search", "read_tool_result",
                "public_web_search", "public_web_read", "unified_memory", "unified_activity", "unified_persona")


class IdentityConfigError(ValueError):
    """The identity file is malformed or inconsistent. Callers must fail closed."""


def empty() -> dict:
    return {"version": VERSION, "revision": 0, "profiles": {}, "devices": {}}


def valid_address(value) -> bool:
    return isinstance(value, str) and bool(ADDRESS_RE.match(value)) and value == value.strip()


def mask_qq(number) -> str:
    text = str(number or "")
    return "*" * max(0, len(text) - 2) + text[-2:] if text else ""


def _fail(message):
    raise IdentityConfigError(message)


def _exact(entry, keys, where):
    if not isinstance(entry, dict) or set(entry) != set(keys):
        _fail(f"{where}: unexpected or missing fields")


def validate(data) -> dict:
    """Strictly validate a parsed identity file and return it unchanged. Raises IdentityConfigError."""
    if not isinstance(data, dict) or set(data) != {"version", "revision", "profiles", "devices"}:
        _fail("top level must be exactly version/revision/profiles/devices")
    if data["version"] != VERSION or type(data["revision"]) is not int or data["revision"] < 0:
        _fail("unsupported version or revision")
    profiles, devices = data["profiles"], data["devices"]
    if not isinstance(profiles, dict) or not isinstance(devices, dict) or len(profiles) > 64 or len(devices) > 64:
        _fail("profiles/devices must be objects (max 64)")
    verified_private, verified_session = {}, {}
    for pid, entry in profiles.items():
        if not PROFILE_RE.match(pid):
            _fail("invalid profile id")
        _exact(entry, ("display_name", "preferred_address", "role", "enabled", "created_at", "qq"), "profile " + pid)
        if entry["preferred_address"] is not None and not valid_address(entry["preferred_address"]):
            _fail("profile " + pid + ": invalid preferred_address")
        name = entry["display_name"]
        if not isinstance(name, str) or not 1 <= len(name) <= 32 or re.search(r"[\x00-\x1f]", name):
            _fail("profile " + pid + ": invalid display_name")
        if entry["role"] not in ROLES:
            _fail("profile " + pid + ": role must be one of " + ",".join(ROLES))
        if type(entry["enabled"]) is not bool or not isinstance(entry["created_at"], (int, float)):
            _fail("profile " + pid + ": invalid enabled/created_at")
        qq = entry["qq"]
        _exact(qq, ("state", "number", "private_id", "session", "confirmed_at"), "profile " + pid + " qq")
        state = qq["state"]
        if state not in QQ_STATES:
            _fail("profile " + pid + ": invalid qq state")
        if state == "none":
            if any(qq[k] is not None for k in ("number", "private_id", "session", "confirmed_at")):
                _fail("profile " + pid + ": qq none must be empty")
        else:
            if not isinstance(qq["number"], str) or not QQ_NUMBER_RE.match(qq["number"]):
                _fail("profile " + pid + ": invalid qq number")
            if state == "pending":
                if any(qq[k] is not None for k in ("private_id", "session", "confirmed_at")):
                    _fail("profile " + pid + ": pending qq must not carry a private binding")
            else:
                if (not isinstance(qq["private_id"], str) or not PRIVATE_ID_RE.match(qq["private_id"])
                        or not isinstance(qq["session"], str) or not SESSION_RE.match(qq["session"])
                        or not isinstance(qq["confirmed_at"], (int, float))):
                    _fail("profile " + pid + ": verified qq needs private_id, session and confirmed_at")
                for table, key in ((verified_private, qq["private_id"]), (verified_session, qq["session"])):
                    if key in table:
                        _fail("one QQ private binding cannot belong to two profiles")
                    table[key] = pid
    for device, entry in devices.items():
        if not DEVICE_RE.match(device):
            _fail("invalid device id")
        _exact(entry, ("profile_id", "enabled"), "device " + device)
        if entry["profile_id"] not in profiles or type(entry["enabled"]) is not bool:
            _fail("device " + device + ": unknown profile or invalid enabled")
    return data


def parse(raw) -> dict:
    try:
        return validate(json.loads(raw))
    except (ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, IdentityConfigError):
            raise
        raise IdentityConfigError("not valid JSON") from exc


def path_for(root) -> Path:
    return Path(root) / FILE_NAME


def load(root, *, missing_ok=True) -> dict:
    """Read + validate. A missing file is an empty registry (no profiles); anything else invalid raises."""
    path = path_for(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return empty()
        raise IdentityConfigError("identity file does not exist")
    except OSError as exc:
        raise IdentityConfigError("identity file unreadable") from exc
    return parse(raw)


@contextlib.contextmanager
def locked(root):
    """Exclusive admin lock so two CLI runs cannot interleave their read-modify-write."""
    Path(root).mkdir(parents=True, exist_ok=True)
    fd = os.open(str(Path(root) / (FILE_NAME + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_atomic(root, data) -> None:
    """Validate, bump the revision, keep one private backup of the previous file, replace atomically (0600)."""
    validate(data)
    path = path_for(root)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".pp-identity-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            backup = path.with_name(FILE_NAME + ".bak")
            fd2 = os.open(str(backup), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd2, "wb") as handle:
                handle.write(path.read_bytes())
        os.replace(tmp, path)
        dirfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def legacy_devices(root) -> dict:
    """Devices of the legacy owner file (read only; used to keep the two namespaces disjoint)."""
    try:
        devices = json.loads((Path(root) / "voice_bindings.json").read_text("utf-8"))["devices"]
        return devices if isinstance(devices, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def number_evidenced(policy, number, private_id, group_actor) -> bool:
    """Is there TRUSTED evidence that the verified account (private_id / its group-side actor) is this QQ number?

    Accepted evidence, nothing else: the platform user id of the verified account IS the number (numeric QQ ids), or
    the server's own policy maps this number to this private_id (`passport_qq_number_evidence`, operator-supplied
    runtime configuration, never a CLI argument). Opaque platform ids with neither => no evidence => no link."""
    if number in (private_id, group_actor):
        return True
    mapping = policy.get("passport_qq_number_evidence")
    return isinstance(mapping, dict) and mapping.get(number) == private_id


def check_qq_link(db, policy, private_id, legacy, number):
    """Is `private_id` an existing, server-verified, NON-owner QQ private binding that belongs to the pending QQ
    `number`? Returns its session or raises ValueError(code). `legacy` and `number` are required (no default) so no
    caller can skip the owner or the number check. Pure over the sqlite connection + policy, so the admin CLI
    (read-only connection) and the runtime (every turn) apply exactly the same rule.

    A verified private binding alone proves only that SOME account completed the familiar-private challenge; it says
    nothing about whose number it is. Supplying somebody else's verified private_id therefore fails with
    qq_number_not_evidenced and the number stays pending."""
    if policy.get("qq_private_familiar_enabled") is not True:
        raise ValueError("qq_private_disabled")
    if not isinstance(private_id, str) or not PRIVATE_ID_RE.match(private_id):
        raise ValueError("qq_private_id_invalid")
    row = db.execute("SELECT group_session,group_actor,session FROM qq_private_bindings WHERE private_id=?",
                     (private_id,)).fetchone()
    groups = list((policy.get("qq_group_sessions") or {}).values())
    if not row or row["group_session"] not in groups:
        raise ValueError("qq_private_not_verified")
    session = row["session"]
    if not session:
        raise ValueError("qq_private_session_not_registered")
    if session in groups:
        raise ValueError("qq_private_session_invalid")
    owners = policy.get("qq_owner_bindings") or {}
    if isinstance(owners, dict) and owners.get(row["group_session"]) == row["group_actor"]:
        raise ValueError("qq_private_is_owner")
    for entry in (legacy or {}).values():
        if isinstance(entry, dict) and (entry.get("owner_private_id") == private_id or entry.get("session") == session):
            raise ValueError("qq_private_is_owner")
    if not number_evidenced(policy, number, private_id, row["group_actor"]):
        raise ValueError("qq_number_not_evidenced")
    return session


def new_profile_id() -> str:
    return "pf_" + os.urandom(8).hex()


def new_profile(name, role="friend", qq_number=None, address=None) -> dict:
    qq = {"state": "pending" if qq_number else "none", "number": qq_number or None,
          "private_id": None, "session": None, "confirmed_at": None}
    return {"display_name": name, "preferred_address": address, "role": role, "enabled": True, "created_at": time.time(), "qq": qq}
