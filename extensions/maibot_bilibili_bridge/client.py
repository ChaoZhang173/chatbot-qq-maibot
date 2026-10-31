"""Credential-free, filesystem-only QQ client for the single Bilibili writer."""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

ROOT = Path("/srv/bilibot/bridge")


def read_json(path, limit=65536):
    if path.stat().st_size > limit:
        raise ValueError("oversize")
    return json.loads(path.read_text())


def snapshot(root=ROOT):
    data = read_json(Path(root) / "public.json")
    if time.time() - data["updated_at"] > 180:
        data["warning"] = "运行快照已过期；不能据此声称机器人在线或发布成功。"
    return data


def receipt(request_id, root=ROOT):
    if not re.fullmatch(r"[a-f0-9]{64}", str(request_id)):
        raise ValueError("invalid_request_id")
    path = Path(root) / "results" / (request_id + ".json")
    return (
        read_json(path)
        if path.exists()
        else {"status": "pending", "request_id": request_id}
    )


def enqueue(text, stream_id, root=ROOT):
    root = Path(root)
    text = str(text).strip()
    if not text or len(text) > 250 or not stream_id:
        raise ValueError("invalid_public_draft")
    status = snapshot(root)
    if status.get("warning") or not status.get("bridge_enabled"):
        raise ValueError("bridge_offline_or_disabled")
    request_id = hashlib.sha256((str(stream_id) + "\n" + text).encode()).hexdigest()
    if (root / "results" / (request_id + ".json")).exists():
        return request_id
    final = root / "requests" / (request_id + ".json")
    if final.exists():
        return request_id
    if len(list((root / "requests").glob("*.json"))) >= 8:
        raise ValueError("queue_full")
    data = {
        "request_id": request_id,
        "stream_id": str(stream_id),
        "expected_uid": status["bot_uid"],
        "draft": text,
        "created_at": time.time(),
    }
    with tempfile.NamedTemporaryFile(
        mode="w", dir=root / "requests", prefix=".request-", delete=False
    ) as f:
        temp = Path(f.name)
        os.fchmod(f.fileno(), 0o640)
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    try:
        try:
            os.link(temp, final)
        except FileExistsError:
            pass
    finally:
        temp.unlink()
    return request_id


async def publish(text, stream_id, root=ROOT, wait_seconds=15):
    request_id = enqueue(text, stream_id, root)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        result = receipt(request_id, root)
        if result["status"] != "pending":
            return result
        await asyncio.sleep(0.5)
    return {
        "status": "pending",
        "request_id": request_id,
        "note": "已排队，尚未确认发布。请用回执工具查询，不能说已发出，也不要换文案重试。",
    }
