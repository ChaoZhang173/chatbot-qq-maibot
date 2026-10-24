"""Credential-free connection to the single MaiBot decision authority.

The Bilibili process keeps its existing delivery ledger and quotas. A missing
core never enables the former platform-specific model as a fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
import socket
import time

from .api import public_image
from .core import Denied, InterestSkip, day, safe_text

SOCKET_PATH = "/srv/maibot/unified/core.sock"
MAX_REQUEST = 3 * 1024 * 1024 - 1024
MAX_RESPONSE = 2 * 1024 * 1024
FINAL_STATES = ("success", "failed", "unknown", "skipped", "cancelled")


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request(payload, path=SOCKET_PATH, timeout=50):
    wire = stable_json(payload).encode("utf-8") + b"\n"
    if len(wire) > MAX_REQUEST:
        raise Denied("unified_request_too_large")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(wire)
            data = bytearray()
            while b"\n" not in data:
                part = client.recv(min(65536, MAX_RESPONSE + 1 - len(data)))
                if not part:
                    raise ValueError("incomplete_response")
                data.extend(part)
                if len(data) > MAX_RESPONSE:
                    raise ValueError("response_too_large")
            result = json.loads(bytes(data).split(b"\n", 1)[0])
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("core_rejected")
        return result
    except (OSError, ValueError, TypeError):
        # Exception text may include paths or external prompt fragments.
        raise Denied("unified_core_unavailable") from None


def initialize(store):
    store.db.executescript("""
      CREATE TABLE IF NOT EXISTS action_context(
        action_id TEXT PRIMARY KEY,event_json TEXT NOT NULL,
        persona_version TEXT NOT NULL DEFAULT '',source_ids TEXT NOT NULL DEFAULT '[]');
      CREATE TABLE IF NOT EXISTS unified_events(
        event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL,
        synced INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS unified_receipts(
        action_id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 1,
        synced_revision INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS action_visibility(
        action_id TEXT PRIMARY KEY,checked_at REAL NOT NULL,payload TEXT NOT NULL);
      CREATE TRIGGER IF NOT EXISTS unified_visibility_insert AFTER INSERT ON action_visibility BEGIN
        INSERT OR IGNORE INTO unified_receipts(action_id) VALUES(NEW.action_id);
        UPDATE unified_receipts SET revision=revision+1 WHERE action_id=NEW.action_id;
      END;
      CREATE TRIGGER IF NOT EXISTS unified_visibility_update AFTER UPDATE ON action_visibility BEGIN
        UPDATE unified_receipts SET revision=revision+1 WHERE action_id=NEW.action_id;
      END;
      CREATE TRIGGER IF NOT EXISTS unified_action_insert AFTER INSERT ON actions BEGIN
        INSERT OR IGNORE INTO unified_receipts(action_id) VALUES(NEW.id);
      END;
      CREATE TRIGGER IF NOT EXISTS unified_action_update
      AFTER UPDATE OF status,text,remote_id,url,error ON actions BEGIN
        INSERT OR IGNORE INTO unified_receipts(action_id) VALUES(NEW.id);
        UPDATE unified_receipts SET revision=revision+1 WHERE action_id=NEW.id;
      END;
      INSERT OR IGNORE INTO unified_receipts(action_id) SELECT id FROM actions;
      CREATE INDEX IF NOT EXISTS unified_events_pending ON unified_events(synced,created);
    """)


class UnifiedModel:
    def __init__(self, store, config, event=None, transport=request, mention_candidates=()):
        self.s = store
        self.c = config
        self.event = event
        self.transport = transport
        self.mention_candidates = list(mention_candidates)
        self.last_decision = None
        self.last_persona_version = ""
        self.last_source_ids = []

    def generate(self, kind, material, images=(), history=(), *, event=None, comment_allowed=None):
        c = self.c
        event = event or self.event
        if not isinstance(event, dict) or not event.get("event_id"):
            raise Denied("unified_event_required")
        if event.get("platform") != "bilibili" or str(event.get("bot_account_id")) != str(c["bot_uid"]):
            raise Denied("unified_account_mismatch")
        now = time.time()
        if self.s.db.execute("select count(*) from usage where day=?", (day(now),)).fetchone()[0] >= c["model_daily"]:
            raise Denied("model_daily_limit")
        if self.s.get("model_cooldown", 0) > now:
            raise Denied("model_cooldown")
        cur = self.s.db.execute(
            "insert into usage(created,day,status,model) values(?,?,?,?)",
            (now, day(now), "pending", "maibot-unified"),
        )
        usage_id = cur.lastrowid
        action_id = str(event.get("action_id", ""))
        if action_id:
            self.s.db.execute(
                "insert or ignore into action_context(action_id,event_json) values(?,?)",
                (action_id, stable_json(event)),
            )
        try:
            # Only the existing bounded, trusted CDN decoder fetches images.
            inline_images = [public_image(url) for url in list(images or ())[:2]]
            permissions = {
                "bot_account_id": str(c["bot_uid"]),
                "task": kind,
                "enabled": bool(c["enabled"].get(kind)),
                "paused": bool(c["paused"] or (self.s.root / "PAUSE").exists() or self.s.get("pause_reason")),
                "daily_limit": c["limits"].get(kind, 0),
                "used_today": self.s.used(c["bot_uid"], kind, now),
                "reply_interest_min_score": c["reply_interest_min_score"],
                "max_model_tokens": c["max_model_tokens"],
                "active_hours": [c["active_start"], c["active_end"]],
                "timezone": "Asia/Shanghai",
                "proactive_gap": c["proactive_gap"],
                "author_daily": c["author_daily"],
                "thread_daily": c["thread_daily"],
                "mention_candidates": self.mention_candidates if kind != "dm" else [],
                "mention_max_per_action": 1,
            }
            if comment_allowed is not None:
                if type(comment_allowed) is not bool:raise Denied('invalid_comment_permission')
                permissions['comment_allowed']=comment_allowed
            if c.get("engagement", {}).get("enabled"):
                from .engagement import policy, used
                actual_video = (kind == "proactive" and event.get("event_type") == "content_read"
                                and event.get("origin") == "platform_content"
                                and (event.get("target") or {}).get("kind") == "video")
                settings = policy(c)
                permissions["engagement"] = {**settings, "enabled": settings["enabled"] and actual_video,
                    "used_today": used(self.s, str(c["bot_uid"]), now), "actual_read_video": actual_video}
            result = self.transport(
                {"op": "decide", "event": event, "task": kind, "material": material,
                 "images": inline_images, "history": list(history)[-10:], "permissions": permissions}
            )
            decision = result.get("decision")
            if not isinstance(decision, dict):
                raise Denied("unified_invalid_decision")
            version = str(result.get("persona_version", ""))
            if not version:
                raise Denied("unified_persona_version_required")
            sources = result.get("source_ids", [])
            if not isinstance(sources, list) or len(sources) > 100:
                raise Denied("unified_invalid_sources")
            self.last_decision = dict(decision)
            self.last_persona_version = version
            self.last_source_ids = list(sources)
            if action_id:
                self.s.db.execute(
                    "update action_context set persona_version=?,source_ids=? where action_id=?",
                    (version, stable_json(sources), action_id),
                )
            usage = result.get("usage") or {}
            self.s.db.execute(
                "update usage set status='success',prompt=?,completion=?,seconds=? where id=?",
                (usage.get("prompt_tokens"), usage.get("completion_tokens"), round(time.time() - now, 3), usage_id),
            )
            score = decision.get("interest_score")
            if (decision.get("interested") is not True or not isinstance(decision.get("text"), str)
                    or not decision["text"].strip()
                    or (kind == "reply" and (type(score) not in (int, float)
                        or not math.isfinite(score) or not c["reply_interest_min_score"] <= score <= 100))):
                self.s.db.execute("update usage set status='skipped_interest' where id=?", (usage_id,))
                raise InterestSkip("interest_or_evidence_skip")
            from .mentions import select
            return safe_text(select(decision["text"], decision.get("mention_uids", []), self.mention_candidates, kind))
        except InterestSkip:
            raise
        except Exception as exc:
            self.last_decision = None
            self.s.db.execute(
                "update usage set status=?,seconds=? where id=?",
                ("failed_" + type(exc).__name__, round(time.time() - now, 3), usage_id),
            )
            # A bounded failure can never lead to a former independent model call.
            self.s.put("model_cooldown", time.time() + 1800)
            raise Denied("unified_decision_failed") from None


class Outbox:
    def __init__(self, store, transport=request, now=time.time):
        self.s, self.transport, self.now = store, transport, now
        initialize(store)

    def event(self, event):
        encoded = stable_json(event)
        if len(encoded.encode()) > 48 * 1024:
            raise Denied("event_too_large")
        self.s.db.execute(
            "insert or ignore into unified_events(event_id,payload,created) values(?,?,?)",
            (event["event_id"], encoded, self.now()),
        )
        if event.get("action_id"):
            self.s.db.execute(
                "insert into action_context(action_id,event_json) values(?,?) "
                "on conflict(action_id) do update set event_json=excluded.event_json "
                "where action_context.persona_version=''",
                (event["action_id"], encoded),
            )

    def visibility(self, action_id, remote_id, checked_at, state, evidence):
        """Annotate an accepted receipt; never alter its status or dispatch path."""
        if state not in ("guest_api_visible", "account_only_or_not_publicly_verified", "not_publicly_verified"):
            raise Denied("invalid_visibility_state")
        if type(checked_at) not in (int, float) or not 0 < checked_at <= self.now() + 300:
            raise Denied("invalid_visibility_time")
        clean = {"method": "bounded_comment_api"}
        for field in ("authenticated_exact_match", "guest_exact_match"):
            if type(evidence.get(field)) is not bool:
                raise Denied("invalid_visibility_evidence")
            clean[field] = evidence[field]
        for field in ("authenticated_detail_code", "guest_detail_code", "authenticated_state"):
            value = evidence.get(field)
            if value is not None and type(value) is not int:
                raise Denied("invalid_visibility_evidence")
            clean[field] = value
        if state == "guest_api_visible" and not clean["guest_exact_match"]:
            raise Denied("unverified_guest_visibility")
        payload = {"public_visibility": state, "visibility_checked_at": checked_at, "visibility_evidence": clean}
        self.s.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.s.db.execute("SELECT status,remote_id FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row or row["status"] != "success" or str(row["remote_id"]) != str(remote_id):
                raise Denied("visibility_requires_matching_success_receipt")
            self.s.db.execute("INSERT INTO action_visibility(action_id,checked_at,payload) VALUES(?,?,?) "
                              "ON CONFLICT(action_id) DO UPDATE SET checked_at=excluded.checked_at,payload=excluded.payload "
                              "WHERE excluded.checked_at>action_visibility.checked_at",
                              (action_id, checked_at, stable_json(payload)))
            self.s.db.execute("COMMIT")
        except BaseException:
            self.s.db.execute("ROLLBACK")
            raise
        return payload

    def flush(self, limit=20, force=False):
        """Only reports state: this method has no API execution or model path."""
        if not force and self.s.get("unified_sync_retry_at", 0) > self.now():
            return {"synced": 0, "pending": True, "backoff": True}
        completed = 0
        # Committed delivery receipts take priority over optional input history.
        rows = self.s.db.execute(
            "select a.*,o.revision,x.event_json,x.persona_version,x.source_ids,v.payload as visibility_json "
            "from unified_receipts o join actions a on a.id=o.action_id "
            "left join action_context x on x.action_id=a.id "
            "left join action_visibility v on v.action_id=a.id "
            "where o.revision>o.synced_revision and a.status in ('success','failed','unknown','skipped','cancelled') "
            "order by a.created,a.id limit ?", (limit,),
        ).fetchall()
        for row in rows:
            action = {key: row[key] for key in row.keys() if key not in ("revision", "event_json", "source_ids", "visibility_json")}
            input_event = json.loads(row["event_json"] or "{}")
            action.update(
                action_id=row["id"], platform="bilibili", bot_account_id=row["uid"],
                conversation_id=input_event.get("conversation_id") or row["thread"] or row["target"] or "self:" + row["uid"],
                canonical_status="succeeded" if row["status"] == "success" else row["status"],
                origin="bot_self", learning_allowed=False,
                receipt_verified=row["status"] == "success" and bool(row["remote_id"]),
                result_stage="platform_api_receipt" if row["status"] == "success" else row["status"],
                public_visibility="not_independently_checked",
                sharing_scope="conversation" if row["kind"] in ("dm","account") else "public",
                is_private=row["kind"] in ("dm","account"),
                source_ids=json.loads(row["source_ids"] or "[]"),
                input_event=input_event,
            )
            if row["visibility_json"]:
                action.update(json.loads(row["visibility_json"]))
            if row["kind"] in ("like", "favorite", "follow", "coin") and self.s.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engagement_actions'").fetchone():
                engagement = self.s.db.execute("SELECT result_stage FROM engagement_actions WHERE action_id=?", (row["id"],)).fetchone()
                if engagement and engagement[0] in ("platform_api_receipt", "account_state_reconciled",
                                                    "platform_rejected", "delivery_unknown", "not_dispatched"):
                    action["result_stage"] = engagement[0]
                action["remote_id_type"] = "operation_receipt_not_content_id"
            digest = hashlib.sha256(stable_json(action).encode()).hexdigest()
            action["event_id"] = "bilibili:receipt:" + digest
            self.s.db.execute("update unified_receipts set attempts=attempts+1 where action_id=?", (row["id"],))
            try:
                self.transport({"op": "activity", "action": action})
            except Exception:
                self.s.put("unified_sync_error", "core_unavailable")
                self.s.put("unified_sync_retry_at", self.now() + 60)
                return {"synced": completed, "pending": True}
            self.s.db.execute(
                "update unified_receipts set synced_revision=? where action_id=? and revision=?",
                (row["revision"], row["id"], row["revision"]),
            )
            completed += 1
        remaining = limit - completed
        for row in self.s.db.execute(
            "select event_id,payload from unified_events where synced=0 order by created,event_id limit ?", (remaining,)
        ).fetchall():
            self.s.db.execute("update unified_events set attempts=attempts+1 where event_id=?", (row["event_id"],))
            try:
                self.transport({"op": "event", "event": json.loads(row["payload"])})
            except Exception:
                self.s.put("unified_sync_error", "core_unavailable")
                self.s.put("unified_sync_retry_at", self.now() + 60)
                return {"synced": completed, "pending": True}
            self.s.db.execute("update unified_events set synced=1 where event_id=?", (row["event_id"],))
            completed += 1
        if completed or self.s.get("unified_sync_error"):
            self.s.put("unified_sync_error", None)
            self.s.put("unified_sync_retry_at", 0)
        if completed:
            self.s.put("unified_last_sync", self.now())
        return {"synced": completed}
