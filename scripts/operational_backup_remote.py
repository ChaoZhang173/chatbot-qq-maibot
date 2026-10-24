"""Local-only bounded backups; SQLite backup API, no live vector-file copying.

Run as root on the existing server. Each SQLite snapshot is independently
consistent; this is not a globally atomic snapshot across all databases.
Vector indexes must be rebuilt from metadata after restoration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

STATE_ROOTS = ("/srv/maibot/data", "/srv/maibot/unified", "/srv/bilibot/state")
CONFIG_FILES = ("/srv/maibot/config/bot_config.toml", "/srv/maibot/config/model_config.toml",
                "/srv/maibot/config/adapter_policy.toml", "/srv/maibot/unified/policy.json",
                "/srv/bilibot/private/config.json")
DEFAULT_DESTINATION = "/var/backups/maibot-unified"
MARKER = "phase25-operational-snapshot-v1"


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def rooted(root, path):
    return Path(root) / path.lstrip("/")


def sqlite_snapshot(source, destination, timeout=90, maximum_bytes=None):
    if source.is_symlink():
        raise ValueError("symlink_database_not_allowed")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as incoming, \
         closing(sqlite3.connect(destination)) as outgoing:
        def progress(status, remaining, total):
            if time.monotonic() - started > timeout:
                raise TimeoutError("sqlite_backup_deadline")
            if maximum_bytes is not None and destination.stat().st_size > maximum_bytes:
                raise ValueError("state_snapshot_size_limit")
        incoming.backup(outgoing, pages=128, progress=progress, sleep=0.05)
        result = outgoing.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise ValueError("sqlite_snapshot_integrity_failed")
    destination.chmod(0o600)
    return {"bytes": destination.stat().st_size, "sha256": digest(destination), "integrity": "ok"}


def database_size(source):
    """Include committed pages still resident in WAL, not just the main file."""
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    return pages * page_size


def discover(root=Path("/")):
    databases, omitted = [], []
    for logical in STATE_ROOTS:
        path = rooted(root, logical)
        if not path.exists():
            continue
        if path.is_symlink():
            raise ValueError("symlink_state_root_not_allowed")
        for file in sorted(path.rglob("*")):
            if any(part.startswith("._") or part == "__MACOSX" for part in file.relative_to(path).parts):
                continue
            if file.is_symlink() or not file.is_file():
                continue
            if file.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                databases.append(file)
            elif "a-memorix" in str(file) and not file.name.endswith(("-wal", "-shm", ".lock")):
                omitted.append(str(file.relative_to(root)))
    return databases, omitted


def create_snapshot(destination, root=Path("/"), minimum_free=1024 ** 3, maximum_state=512 * 1024 ** 2):
    root = Path(root).resolve()
    destination = Path(destination)
    if destination.exists():
        raise ValueError("snapshot_destination_exists")
    databases, omitted = discover(root)
    total = sum(database_size(file) for file in databases)
    if total > maximum_state:
        raise ValueError("state_snapshot_size_limit")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(destination.parent).free < total + minimum_free:
        raise ValueError("insufficient_backup_disk_space")
    destination.mkdir(mode=0o700)
    manifest = {"generator": MARKER, "created_at": time.time(), "status": "in_progress",
                "consistency": "SQLite backup API; each database consistent, not a cross-database transaction",
                "vector_restore": "Live vector/index files deliberately omitted; rebuild against restored metadata and the recorded embedding model",
                "omitted_live_memory_files": omitted, "databases": [], "configs": []}
    try:
        remaining_budget = maximum_state
        for file in databases:
            relative = file.relative_to(root)
            target = destination / "databases" / relative
            record = sqlite_snapshot(file, target, maximum_bytes=remaining_budget)
            remaining_budget -= record['bytes']
            manifest["databases"].append({"source": "/" + str(relative), "snapshot": str(target.relative_to(destination)), **record})
        for logical in CONFIG_FILES:
            source = rooted(root, logical)
            if not source.exists():
                continue
            if source.is_symlink():
                raise ValueError("symlink_configuration_not_allowed")
            target = destination / "configs" / logical.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Existing deploy writes these files using atomic rename.
            with source.open("rb") as handle:
                value = handle.read(2 * 1024 * 1024 + 1)
            if len(value) > 2 * 1024 * 1024:
                raise ValueError("configuration_size_limit")
            target.write_bytes(value)
            target.chmod(0o600)
            manifest["configs"].append({"source": logical, "snapshot": str(target.relative_to(destination)),
                                        "sha256": digest(target)})
        manifest["status"] = "complete"
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        raise
    finally:
        # Contains paths/checksums only. Credentials and message bodies are never printed.
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2))
        for file in destination.rglob("*"):
            if not file.is_symlink():
                file.chmod(0o700 if file.is_dir() else 0o600)


def rotate(destination, retain):
    """Retire only this helper's completed daily snapshots, never baseline backups."""
    managed = []
    for path in Path(destination).glob("daily-unified-*"):
        if path.is_symlink() or not path.is_dir():
            continue
        manifest = path / "manifest.json"
        try:
            info = json.loads(manifest.read_text())
            if info.get("generator") == MARKER and info.get("status") == "complete":
                managed.append((float(info["created_at"]), path))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    removed = 0
    for _, path in sorted(managed, reverse=True)[max(1, retain):]:
        shutil.rmtree(path)
        removed += 1
    return removed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=Path(DEFAULT_DESTINATION))
    parser.add_argument("--retain", type=int, default=3)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("root_required_for_private_server_backups")
    os.umask(0o077)
    if args.destination.is_symlink():
        raise SystemExit("symlink_backup_destination_not_allowed")
    args.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.destination.chmod(0o700)
    target = args.destination / ("daily-unified-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6])
    try:
        manifest = create_snapshot(target)
        removed = rotate(args.destination, max(1, min(7, args.retain)))
        print(json.dumps({"snapshot": str(target), "databases": len(manifest["databases"]),
                          "status": "complete", "retired_managed_snapshots": removed,
                          "vector_snapshots": "omitted_rebuild_from_metadata"}))
    except Exception as exc:  # noqa: BLE001 - suppress private path/config exception details.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
