"""Manual, authenticated per-device birthday delivery. No calendar or network listener."""
from __future__ import annotations

import json
import time
from pathlib import Path


def pending(path: Path, device_id: str, now: int) -> dict | None:
    try:
        if path.stat().st_size > 16384:
            return None
        data = json.loads(path.read_text())
        event = data["events"].get(device_id)
        if data.get("schema") != 1 or not isinstance(event, dict):
            return None
        event_id, expires = event.get("event_id"), event.get("expires_at")
        if type(event_id) is not int or not 0 < event_id < 2**32:
            return None
        if type(expires) is not int or (expires != 0 and not now < expires <= now + 172800):
            return None
        return {"v": 1, "type": "birthday", "event_id": event_id, "expires_at": expires}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


async def deliver(session) -> None:
    # Old/standard firmware never sees this extension. A busy voice turn takes priority.
    if not session.ready or session.closing or session.active or getattr(session, "fw", "") not in ("0.2.0-shunhuahua", "0.3.0-shunhuahua", "0.3.1-shunhuahua"):
        return
    if not session.cfg.bindings_path:
        return
    path = Path(session.cfg.bindings_path).with_name("birthday-events.json")
    event = pending(path, session.device_id, int(time.time()))
    if event is None:
        return
    identity = event["event_id"], event["expires_at"]
    if getattr(session, "_birthday_sent", None) == identity:
        return
    await session._send_raw(json.dumps(event, separators=(",", ":")))
    if not session.closing:
        session._birthday_sent = identity
