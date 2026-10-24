"""Idempotent, hash-locked installation of the one Host memory RPC.

python host_patch.py --source-root /opt/.../site-packages/src --apply
Without --apply, validates the exact baseline and reports the proposed change.
The deployment must back up state separately; this utility backs up code only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

BASE_SHA256 = "c3c4d708997fe01028b5f262e14bc0065b161b8781d6464116ea075b510f081e"
ANCHOR = '    cap_service = supervisor.capability_service\n'
INSERTION = (
    '\n    # local.unified-core: explicit Host-owned memory access (Phase 2.5).\n'
    '    from .unified_memory import unified_memory\n'
    '    cap_service.register_capability("unified.memory", unified_memory)\n'
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic(path: Path, data: bytes, mode: int):
    temporary = path.with_name(path.name + ".phase25-tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def patch(source_root: Path, adapter: Path, apply=False) -> dict:
    registry = source_root / "plugin_runtime/capabilities/registry.py"
    target = registry.with_name("unified_memory.py")
    if registry.is_symlink() or target.is_symlink():
        raise ValueError("refusing_symlink_target")
    original = registry.read_bytes()
    text = original.decode("utf-8")
    unpatched = text.replace(INSERTION, "")
    if text.count(INSERTION) > 1 or digest(unpatched.encode()) != BASE_SHA256 or unpatched.count(ANCHOR) != 1:
        raise ValueError("host_registry_has_unreviewed_changes")
    replacement = unpatched.replace(ANCHOR, ANCHOR + INSERTION).encode()
    adapter_data = adapter.read_bytes()
    compile(adapter_data, str(target), "exec")
    compile(replacement, str(registry), "exec")
    if target.exists() and target.read_bytes() != adapter_data:
        raise ValueError("host_adapter_has_unreviewed_changes")
    report = {"applied": False, "already_installed": original == replacement and target.exists(),
              "registry": str(registry), "adapter": str(target),
              "registry_sha256": digest(replacement), "adapter_sha256": digest(adapter_data)}
    if not apply or report["already_installed"]:
        return report
    backup = source_root.parent / ("phase25-host-backup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    backup.mkdir(mode=0o700, exist_ok=False)
    shutil.copy2(registry, backup / "registry.py")
    if target.exists():
        shutil.copy2(target, backup / "unified_memory.py")
    mode = registry.stat().st_mode & 0o777
    atomic(target, adapter_data, mode)
    atomic(registry, replacement, mode)
    report.update(applied=True, backup=str(backup))
    (backup / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--adapter", type=Path, default=Path(__file__).with_name("host_adapter.py"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(patch(args.source_root, args.adapter, args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
