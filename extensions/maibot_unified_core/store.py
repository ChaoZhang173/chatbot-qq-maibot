"""Small durable coordination ledger; MaiBot owns long-term facts and profiles.

The Host authenticates callers before identity binding or administration. This
module deliberately contains no platform sender, LLM client or fact extractor.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo
from . import persona as character
from .content_rules import blocked as blocked_text


CHINA = ZoneInfo("Asia/Shanghai")
EXTERNAL_ORIGINS = {"external_user", "platform_content"}
SELF_ORIGINS = {"bot_self", "self", "bot", "system", "system_task"}
DYNAMIC_FIELDS = frozenset({
    "interests", "expression_preferences", "expression_habits",
    "communication_preferences", "behavior_preferences", "current_focus",
    "current_state", "avoid_phrases", "preferred_phrases", "tone",
    "verbosity", "response_length", "curiosity",
})
FEEDBACK_FIELDS = DYNAMIC_FIELDS - {"interests", "behavior_preferences", "curiosity"}
HOST_PERSONA_FIELDS = frozenset({
    "bot.nickname", "bot.alias_names", "personality.personality",
    "personality.reply_style", "personality.multiple_reply_style", "personality.behavior_style",
})
ACTIVITY_STATES = {
    "planned", "queued", "executing", "succeeded", "failed", "unknown",
    "skipped", "cancelled", "historical_unverified", "accepted", "pending_visibility",
}
STATUS_ALIASES = {"success": "succeeded", "sending": "executing", "pending": "planned"}
VISIBILITY_FIELDS = frozenset({"public_visibility", "visibility_checked_at", "visibility_evidence"})
VISIBILITY_STATES = frozenset({"guest_api_visible", "account_only_or_not_publicly_verified",
                               "not_publicly_verified", "not_independently_checked"})


def abstract_topic_summary(text):
    """Defense in depth over the learner's public flag, at write and use time.

    Public QQ material is an abstract topic, never an attributed account of a
    participant. This deliberately rejects ambiguous personal narratives.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return False
    return not re.search(
        r'https?://|@|\d{5,}|群友|群里|私聊|昵称|主人|账号|账户|真名|住址|电话|'
        r'有(?:位|个)?(?:用户|网友|朋友|人)|(?:用户|网友|朋友|群成员)(?:会|说|表示|认为|喜欢|偏好|要求|纠正|提到)|'
        r'(?:我|你|您|他|她)(?:们)?(?:喜欢|偏好|叫|是|住|在|的|会|说|想|最近)|'
        r'个人经历|亲身经历|[“”「」『』]', text)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _copy(value):
    return json.loads(_json(value))


def _text(value, limit=4096):
    return str(value or "").strip()[:limit]


def _platform(value):
    value = _text(value, 64).lower()
    return {"bili": "bilibili", "qq_official": "qq", "qq-official": "qq"}.get(value, value)


def _public_url(value):
    """Keep source links, never retain signed queries or embedded credentials."""
    try:
        parts = urlsplit(_text(value, 2048))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        fragment = (parts.fragment if (parts.hostname == "bilibili.com" or parts.hostname.endswith(".bilibili.com"))
                    and re.fullmatch(r"reply\d+", parts.fragment) else "")
        return urlunsplit((parts.scheme, parts.hostname, parts.path, "", fragment))
    except ValueError:
        return ""


def _visibility_annotation(action):
    """A receipt annotation contains bounded API facts, never content or secrets."""
    result = {key: action[key] for key in VISIBILITY_FIELDS if key in action}
    if "public_visibility" in result and result["public_visibility"] not in VISIBILITY_STATES:
        raise ValueError("invalid_public_visibility")
    if "visibility_checked_at" in result:
        value = result["visibility_checked_at"]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("invalid_visibility_timestamp")
    if "visibility_evidence" in result:
        evidence = result["visibility_evidence"]
        if not isinstance(evidence, dict):
            raise ValueError("invalid_visibility_evidence")
        safe = {}
        for key in ("authenticated_exact_match", "guest_exact_match", "guest_list_exact_match"):
            if key in evidence:
                if type(evidence[key]) is not bool:
                    raise ValueError("invalid_visibility_evidence")
                safe[key] = evidence[key]
        for key in ("authenticated_detail_code", "guest_detail_code", "authenticated_state", "guest_list_code"):
            if key in evidence:
                if evidence[key] is not None and (type(evidence[key]) is not int or abs(evidence[key]) > 10**9):
                    raise ValueError("invalid_visibility_evidence")
                safe[key] = evidence[key]
        if "method" in evidence:
            if evidence["method"] not in ("bounded_comment_api", "bounded_dynamic_api"):
                raise ValueError("invalid_visibility_method")
            safe["method"] = evidence["method"]
        if any(field in evidence for field in ('observer_list_seen', 'observer_source', 'observer_recorded_at')):
            stamp = evidence.get('observer_recorded_at')
            if (type(evidence.get('observer_list_seen')) is not bool or evidence.get('observer_source') != 'user_report'
                    or type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0
                    or stamp > result.get('visibility_checked_at', 0)):
                raise ValueError('invalid_observer_evidence')
            safe.update({field:evidence[field] for field in ('observer_list_seen', 'observer_source', 'observer_recorded_at')})
        moderation_fields = ('moderation_state', 'moderation_source', 'moderation_recorded_at')
        if any(field in evidence for field in moderation_fields):
            stamp = evidence.get('moderation_recorded_at')
            if (action.get('platform') != 'xiaohongshu' or action.get('kind') not in ('comment', 'reply')
                    or evidence.get('moderation_source') != 'user_report'
                    or evidence.get('moderation_state') not in ('pending_review', 'approved', 'rejected')
                    or type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0
                    or stamp > result.get('visibility_checked_at', 0)):
                raise ValueError('invalid_moderation_evidence')
            safe.update({field:evidence[field] for field in moderation_fields})
        result["visibility_evidence"] = safe
    if result.get("public_visibility") == "guest_api_visible":
        if result.get("visibility_evidence", {}).get("guest_exact_match") is not True:
            raise ValueError("public_visibility_requires_exact_evidence")
    return result


class State:
    def __init__(self, path, policy=None, now=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self._lock = threading.RLock()
        self._writer_file = open(str(self.path) + ".writer.lock", "a")
        try:
            fcntl.flock(self._writer_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._writer_file.close()
            raise RuntimeError("unified_state_writer_already_running") from None
        self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=10000;
            PRAGMA journal_size_limit=4194304;
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS identities(
                identity_key TEXT PRIMARY KEY, platform TEXT NOT NULL,
                bot_account_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                person_id TEXT NOT NULL,nickname TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS identities_person ON identities(person_id);
            CREATE TABLE IF NOT EXISTS person_aliases(
                old_id TEXT PRIMARY KEY,person_id TEXT NOT NULL,confirmed_by TEXT NOT NULL,
                reason TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS identity_merges(
                merge_id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                event_id TEXT PRIMARY KEY,platform TEXT NOT NULL,bot_account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,actor_id TEXT NOT NULL,person_id TEXT NOT NULL,
                origin TEXT NOT NULL,event_type TEXT NOT NULL,occurred_at REAL NOT NULL,
                received_at REAL NOT NULL,learning_allowed INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,payload_hash TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_learning ON events(learning_allowed,active,received_at);
            CREATE INDEX IF NOT EXISTS events_conversation ON events(platform,conversation_id,occurred_at);
            CREATE TABLE IF NOT EXISTS activities(
                action_id TEXT PRIMARY KEY,platform TEXT NOT NULL,conversation_id TEXT NOT NULL,
                status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS activity_recent ON activities(platform,created_at DESC);
            CREATE TABLE IF NOT EXISTS persona_versions(
                version INTEGER PRIMARY KEY,stable TEXT NOT NULL,dynamic TEXT NOT NULL,
                evidence_ids TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL,
                previous_version INTEGER,rollback_of INTEGER);
            CREATE TABLE IF NOT EXISTS persona_proposals(
                proposal_id TEXT PRIMARY KEY,status TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS learning(
                event_id TEXT PRIMARY KEY REFERENCES events(event_id),status TEXT NOT NULL,
                attempts INTEGER NOT NULL,stored_ids TEXT NOT NULL,detail TEXT NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS quotas(
                reservation_key TEXT PRIMARY KEY,kind TEXT NOT NULL,day TEXT NOT NULL,
                created_at REAL NOT NULL,status TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS quotas_day ON quotas(kind,day);
            CREATE TABLE IF NOT EXISTS context_audits(
                audit_id TEXT PRIMARY KEY,created_at REAL NOT NULL,payload TEXT NOT NULL);
        """)
        from . import migrations_v14_2
        migrations_v14_2.apply(self.db)
        self.path.chmod(0o600)
        try:
            if policy is not None:
                self.set_policy(policy)
            else:
                self.policy = self.get("policy", {})
            self.recover_interrupted()
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._lock:
            self.db.close()
            if not self._writer_file.closed:
                fcntl.flock(self._writer_file.fileno(), fcntl.LOCK_UN)
                self._writer_file.close()

    @contextlib.contextmanager
    def _transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def get(self, key, default=None):
        with self._lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (str(key),)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self._lock:
            self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                            (str(key), _json(value)))

    def _bump_revision(self):
        value = int(self.get("context_revision", 0)) + 1
        self.put("context_revision", value)
        return value

    def set_policy(self, policy):
        if not isinstance(policy, dict):
            raise ValueError("policy_must_be_object")
        value = _copy(policy)
        with self._transaction():
            if value != self.get("policy", {}):
                self._bump_revision()
                self.put("policy_revision", int(self.get("policy_revision", 0)) + 1)
            self.put("policy", value)
            self.policy = value
        return self.get("context_revision", 0)

    def flag(self, name, default=True):
        flags = self.policy.get("flags", {})
        return bool(flags.get(name, self.policy.get(name, default)))

    def _bot_account(self, platform):
        accounts = self.policy.get("bot_accounts", {})
        value = accounts.get(platform, "") if isinstance(accounts, dict) else ""
        if platform == "xiaohongshu":
            return _text(value or self.policy.get("xhs_bot_account", ""), 256)
        if not value:
            value = self.policy.get("bili_bot_uid" if platform == "bilibili" else "qq_bot_account", "")
        return _text(value, 256)

    @staticmethod
    def _identity_key(platform, bot_account_id, actor_id):
        return _json([_platform(platform), _text(bot_account_id, 256), _text(actor_id, 256)])

    def canonical_person(self, person_id):
        person_id = _text(person_id, 256)
        with self._lock:
            visited = set()
            while person_id and person_id not in visited:
                visited.add(person_id)
                row = self.db.execute("SELECT person_id FROM person_aliases WHERE old_id=?", (person_id,)).fetchone()
                if not row:
                    return person_id
                person_id = row[0]
        if person_id:
            raise ValueError("identity_alias_cycle")
        return ""

    def person_ids(self, person_id):
        """Canonical and historical IDs for Host memory queries after binding."""
        canonical = self.canonical_person(person_id)
        with self._lock:
            rows = self.db.execute("SELECT old_id FROM person_aliases").fetchall()
        return [canonical] + sorted(row[0] for row in rows
                                    if row[0] != canonical and self.canonical_person(row[0]) == canonical)

    def resolve_identity(self, platform, bot_account_id, actor_id, nickname="", person_id=""):
        platform, bot_account_id, actor_id = _platform(platform), _text(bot_account_id, 256), _text(actor_id, 256)
        if not platform or not bot_account_id or not actor_id:
            raise ValueError("identity_namespace_required")
        key = self._identity_key(platform, bot_account_id, actor_id)
        with self._lock:
            row = self.db.execute("SELECT person_id FROM identities WHERE identity_key=?", (key,)).fetchone()
            if row:
                if nickname:
                    self.db.execute("UPDATE identities SET nickname=? WHERE identity_key=?", (_text(nickname, 256), key))
                return self.canonical_person(row[0])
            pid = self.canonical_person(person_id) if person_id else "person:" + hashlib.sha256(key.encode()).hexdigest()
            self.db.execute("INSERT INTO identities VALUES(?,?,?,?,?,?,?)",
                            (key, platform, bot_account_id, actor_id, pid, _text(nickname, 256), float(self.now())))
            return pid

    def bind_identities(self, primary, alias, confirmed_by, reason):
        """Bind only after the Host authenticated an explicit administrator action.

        Existing source provenance is retained. `person_ids()` exposes old IDs
        until the Host's supported memory migration has reconciled old profiles.
        """
        if not isinstance(confirmed_by, str) or not confirmed_by.strip() or not _text(reason):
            raise ValueError("confirmed_admin_and_reason_required")
        for identity in (primary, alias):
            if not isinstance(identity, dict):
                raise ValueError("identity_must_be_object")
            if str(identity.get("actor_id", "")) == str(identity.get("bot_account_id", "")):
                raise ValueError("cannot_bind_bot_to_person")
            bot_id = self._bot_account(_platform(identity.get("platform")))
            if bot_id and str(identity.get("actor_id")) == str(bot_id):
                raise ValueError("cannot_bind_bot_to_person")
        with self._transaction():
            def resolve(value):
                return self.resolve_identity(value["platform"], value["bot_account_id"], value["actor_id"],
                                             value.get("nickname", ""), value.get("person_id", ""))
            target, old = resolve(primary), resolve(alias)
            old_ids = self.person_ids(old)
            if target == old:
                return {"person_id": target, "merged_person_ids": [], "is_new": False}
            self.db.execute("INSERT INTO person_aliases VALUES(?,?,?,?,?)",
                            (old, target, _text(confirmed_by, 256), _text(reason, 1024), float(self.now())))
            placeholders = ",".join("?" for _ in old_ids)
            self.db.execute(f"UPDATE identities SET person_id=? WHERE person_id IN ({placeholders})", [target, *old_ids])
            self.db.execute(f"UPDATE events SET person_id=? WHERE person_id IN ({placeholders})", [target, *old_ids])
            merge = {"merge_id": "merge:" + uuid.uuid4().hex, "person_id": target,
                     "merged_person_ids": old_ids, "confirmed_by": _text(confirmed_by, 256),
                     "reason": _text(reason, 1024), "is_new": True,
                     "host_memory_reconciliation": "pending"}
            self.db.execute("INSERT INTO identity_merges VALUES(?,?,?)", (merge["merge_id"], _json(merge), float(self.now())))
            self._bump_revision()
            return merge

    def ingest(self, event):
        if not isinstance(event, dict):
            raise ValueError("event_must_be_object")
        e = _copy(event)
        platform = _platform(e.get("platform"))
        account = _text(e.get("bot_account_id"), 256)
        conversation = _text(e.get("conversation_id") or e.get("chat_id") or e.get("session_id"), 512)
        event_type = _text(e.get("event_type") or "message_received", 80)
        native = _text(e.get("native_event_id") or e.get("native_id") or e.get("message_id") or e.get("event_id") or e.get("source_id"), 1024)
        if not native:
            native = _text(e.get("action_id") or e.get("job_id"), 1024)
        if not platform or not account or not native:
            raise ValueError("platform_account_and_stable_event_id_required")
        event_id = "evt:" + hashlib.sha256(_json([platform, account, event_type, native]).encode()).hexdigest()
        actor = _text(e.get("actor_id") or e.get("user_id"), 256)
        origin = _text(e.get("origin") or "external_user", 64)
        known_bot = self._bot_account(platform)
        if actor and (actor == account or actor == str(known_bot)):
            origin = "bot_self"
        content = _text(e.get("content") or e.get("text"), 16000)
        immutable_hash = hashlib.sha256(_json([platform, account, conversation, actor, origin, event_type, content]).encode()).hexdigest()
        with self._transaction():
            previous = self.db.execute("SELECT event_id,person_id,payload_hash FROM events WHERE event_id=?", (event_id,)).fetchone()
            if previous:
                if previous["payload_hash"] != immutable_hash:
                    raise ValueError("event_id_payload_conflict")
                return {"event_id": event_id, "person_id": self.canonical_person(previous["person_id"]), "is_new": False}
            pid = ""
            if actor and origin not in SELF_ORIGINS:
                pid = self.resolve_identity(platform, account, actor, e.get("nickname", ""), e.get("person_id", ""))
            received = float(e.get("received_at") or self.now())
            occurred = float(e.get("occurred_at") or e.get("timestamp") or received)
            permitted = bool(e.get("learning_allowed", True)) and origin in EXTERNAL_ORIGINS
            if platform == "qq":
                permitted = permitted and conversation in self.policy.get("allowed_qq_sessions", [])
            if e.get("evidence_class") in {"roleplay", "joke", "speculation"}:
                permitted = False
            e.update({"schema_version": 1, "event_id": event_id, "native_event_id": native,
                      "platform": platform, "bot_account_id": account, "conversation_id": conversation,
                      "actor_id": actor, "person_id": pid, "event_type": event_type, "origin": origin,
                      "content": content, "occurred_at": occurred, "received_at": received,
                      "learning_allowed": permitted, "sharing_scope": e.get("sharing_scope", "policy"), "active": True})
            e.pop("text", None)
            e["source_url"] = _public_url(e.get("source_url"))
            e["attachment_refs"] = [_text(ref, 512) for ref in e.get("attachment_refs", [])[:8]
                                    if isinstance(ref, str) and not ref.startswith(("http:", "https:", "data:"))]
            # Event producers must pass references, never credentials or full prompts.
            for key in ("api_key", "cookie", "authorization", "prompt", "system_prompt", "attachments"):
                e.pop(key, None)
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (event_id, platform, account, conversation, actor, pid, origin, event_type,
                             occurred, received, int(permitted), 1, immutable_hash, _json(e)))
            return {"event_id": event_id, "person_id": pid, "is_new": True}

    def stored_event_id(self, e):
        """The id `ingest` assigns to this event dict (native ids are hashed with platform/account/type)."""
        platform, account = _platform(e.get("platform")), _text(e.get("bot_account_id"), 256)
        event_type = _text(e.get("event_type") or "message_received", 80)
        native = _text(e.get("native_event_id") or e.get("native_id") or e.get("message_id") or e.get("event_id") or e.get("source_id"), 1024)
        if not native:
            native = _text(e.get("action_id") or e.get("job_id"), 1024)
        return "evt:" + hashlib.sha256(_json([platform, account, event_type, native]).encode()).hexdigest()

    def get_event(self, event_id):
        with self._lock:
            row = self.db.execute("SELECT payload,person_id,active FROM events WHERE event_id=?", (str(event_id),)).fetchone()
        if not row:
            return None
        event = json.loads(row["payload"])
        event["person_id"] = self.canonical_person(row["person_id"])
        event["active"] = bool(row["active"])
        return event

    def recent_events(self, limit=20, external_only=True, since=None):
        """Return active provenance, including evidence already written to MaiBot."""
        count = min(100, max(0, int(limit)))
        where, args = ["active=1"], []
        if external_only:
            where.extend(["origin IN ('external_user','platform_content')", "learning_allowed=1"])
        if since is not None:
            where.append("received_at>=?")
            args.append(float(since))
        with self._lock:
            rows = self.db.execute("SELECT event_id FROM events WHERE " + " AND ".join(where)
                                   + " ORDER BY received_at DESC,rowid DESC LIMIT ?", [*args, min(1000, count * 10)]).fetchall()
        events = [self.get_event(row[0]) for row in rows]
        if external_only:
            events = [e for e in events if e["platform"] != "qq"
                      or e["conversation_id"] in self.policy.get("allowed_qq_sessions", [])]
        return events[:count]

    def invalidate_event(self, event_id, reason):
        if not _text(reason):
            raise ValueError("invalidation_reason_required")
        with self._transaction():
            cursor = self.db.execute("UPDATE events SET active=0 WHERE event_id=? AND active=1", (str(event_id),))
            if cursor.rowcount:
                self.put("invalidated:" + str(event_id), {"reason": _text(reason, 1024), "at": float(self.now())})
                self._bump_revision()
            return bool(cursor.rowcount)

    def _in_allowlist(self, name, platform, conversation):
        values = self.policy.get(name, [])
        if isinstance(values, dict):
            values = values.get(platform, [])
        if not isinstance(values, (list, tuple, set)):
            return False
        return conversation in values or f"{platform}:{conversation}" in values

    def allowed(self, source, platform, conversation_id, purpose="internal"):
        """Evaluate current policy every time, including at final prompt assembly."""
        if purpose not in {"internal", "public"} or not isinstance(source, dict):
            return False
        metadata = source.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        s = {**metadata, **source}
        if s.get("active") is False or s.get("status") in {"invalid", "invalidated", "deleted", "superseded"}:
            return False
        event_id = s.get("event_id")
        if event_id:
            stored = self.get_event(event_id)
            if stored is not None and not stored["active"]:
                return False
        for source_id in s.get("source_event_ids", s.get("evidence_event_ids", [])):
            evidence = self.get_event(source_id)
            if evidence is None or not evidence["active"]:
                return False
        source_platform = _platform(s.get("platform"))
        source_conversation = _text(s.get("conversation_id") or s.get("chat_id") or s.get("session_id"), 512)
        target_platform, target_conversation = _platform(platform), _text(conversation_id, 512)
        if not source_platform or not source_conversation or not target_platform:
            return False
        if s.get("unified_kind") == "relationship_pref":
            return False  # relationship preferences are read only from the scoped table, never as generic memory
        scope = s.get("sharing_scope", "policy")
        private = bool(s.get("is_private")) or s.get("conversation_type") in {"private", "dm"} or scope == "private"
        if purpose == "internal" and source_platform == target_platform and source_conversation == target_conversation:
            return True
        if private or s.get("sensitive") is True or scope in ("conversation", "local", "none"):
            return False
        if purpose == "public":
            if s.get("public_allowed") is False:
                return False
            if source_platform == "qq":
                return (self._safe_qq_summary(s)
                        and self._in_allowlist("public_qq_sessions", source_platform, source_conversation))
            return source_platform in {"bilibili", "qq_channel", "xiaohongshu"} and scope == "public"
        if (source_platform in {"bilibili", "qq_channel", "xiaohongshu"} and target_platform == "qq" and scope == "public"
                and s.get("origin") in {"bot_self", "self", "bot", "platform_content", "external_user"}):
            account = self._bot_account(source_platform)
            return bool(account) and str(s.get("bot_account_id", "")) == account
        if source_platform == target_platform == "qq":
            groups = self.policy.get("shared_groups", [])
            if groups and all(isinstance(item, str) for item in groups):
                groups = [groups]
            for group in groups:
                if isinstance(group, dict):
                    group = group.get("sessions", group.get("session_ids", []))
                if isinstance(group, (list, tuple)) and source_conversation in group and target_conversation in group:
                    return True
            return False
        if source_platform != target_platform:
            return (self._in_allowlist("internal_cross_platform", source_platform, source_conversation)
                    and (source_platform != "qq" or self._safe_qq_summary(s)))
        return False

    def _safe_qq_summary(self, source):
        """Only approved topic summaries may leave QQ, even for internal use."""
        category = _text(source.get("category") or source.get("topic"), 128).casefold()
        topics = {_text(topic, 128).casefold() for topic in self.policy.get("public_topics", [])}
        return (source.get("public_summary") is True and category in topics
                and not source.get("personal_details") and source.get("source_type") != "person_fact"
                and abstract_topic_summary(source.get("summary_content") or source.get("content")))

    def record_activity(self, action):
        if not isinstance(action, dict):
            raise ValueError("activity_must_be_object")
        a = _copy(action)
        action_id = _text(a.get("action_id") or a.get("id"), 1024)
        platform = _platform(a.get("platform"))
        if not action_id or not platform:
            raise ValueError("action_id_and_platform_required")
        original_status = _text(a.get("status") or a.get("canonical_status"), 64)
        status = STATUS_ALIASES.get(a.get("canonical_status") or original_status, a.get("canonical_status") or original_status)
        if status not in ACTIVITY_STATES:
            raise ValueError("invalid_activity_status")
        remote_id = _text(a.get("remote_id"), 1024)
        if status == "succeeded" and not (a.get("receipt_verified") is True or remote_id):
            raise ValueError("success_requires_executor_receipt")
        if a.get("receipt_verified") is False and status == "succeeded":
            raise ValueError("unverified_receipt_cannot_succeed")
        annotation = _visibility_annotation(a)
        stamp = float(self.now())
        with self._transaction():
            row = self.db.execute("SELECT payload,status FROM activities WHERE action_id=?", (action_id,)).fetchone()
            previous = json.loads(row["payload"]) if row else {}
            if row:
                if previous["platform"] != platform:
                    raise ValueError("activity_namespace_conflict")
                if row["status"] == "succeeded":
                    if status != "succeeded" or (remote_id and previous.get("remote_id") != remote_id):
                        raise ValueError("confirmed_activity_is_immutable")
                    for key in ("text", "content", "target", "conversation_id", "bot_account_id",
                                "persona_version", "source_ids", "source_id", "input_event", "kind",
                                "sharing_scope", "origin", "learning_allowed"):
                        if key in a and a[key] != previous.get(key):
                            raise ValueError("confirmed_activity_payload_conflict")
                    # A later public-visibility check never changes delivery status,
                    # remote ID or send provenance. Stale outbox replays cannot erase
                    # a newer observation or turn an accepted send into a retry.
                    previous_check = previous.get("visibility_checked_at", 0)
                    if annotation and (not previous_check or annotation.get("visibility_checked_at", 0) >= previous_check):
                        merged = {**previous, **annotation}
                        if merged != previous:
                            merged["updated_at"] = stamp
                            self.db.execute("UPDATE activities SET updated_at=?,payload=? WHERE action_id=?",
                                            (stamp, _json(merged), action_id))
                            return merged
                    return previous
                if row["status"] == "unknown" and status in {"planned", "queued", "executing"}:
                    raise ValueError("unknown_activity_requires_reconciliation")
                if row["status"] in {"failed", "skipped", "cancelled"} and status != row["status"]:
                    raise ValueError("terminal_activity_cannot_be_replayed")
            created = float(previous.get("created_at") or a.get("created_at") or a.get("created") or stamp)
            a = {**previous, **a, "action_id": action_id, "platform": platform,
                 "status": status, "canonical_status": status, "original_status": original_status,
                 "created_at": created, "updated_at": stamp,
                 "origin": "bot_self", "learning_allowed": False}
            a.update(annotation)
            a["conversation_id"] = _text(a.get("conversation_id") or a.get("thread_id") or a.get("thread") or "self", 512)
            a["sharing_scope"] = a.get("sharing_scope", "conversation")
            a["url"] = _public_url(a.get("url") or a.get("source_url"))
            self.db.execute("INSERT INTO activities VALUES(?,?,?,?,?,?,?) ON CONFLICT(action_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,payload=excluded.payload",
                            (action_id, platform, a["conversation_id"], status, created, stamp, _json(a)))
            return a

    def recover_interrupted(self):
        """An interrupted executor may have sent; preserve uncertainty, never retry."""
        with self._transaction():
            rows = self.db.execute("SELECT action_id,payload FROM activities WHERE status='executing'").fetchall()
            for row in rows:
                action = json.loads(row["payload"])
                action.update(status="unknown", canonical_status="unknown", error="process_interrupted",
                              updated_at=float(self.now()))
                self.db.execute("UPDATE activities SET status='unknown',updated_at=?,payload=? WHERE action_id=?",
                                (float(self.now()), _json(action), row["action_id"]))
            # Reserved extraction calls are never silently repeated after a crash.
            # Their Host write may have succeeded; retain an explicit failed audit
            # and continue later events instead of blocking the whole learning queue.
            reservations = self.db.execute("SELECT reservation_key FROM quotas WHERE kind='background'").fetchall()
            for reservation in reservations:
                key = json.loads(reservation[0])[1]
                if not key.startswith("learn:"):
                    continue
                event_id = key[len("learn:"):]
                if self.db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is None:
                    continue
                learning = self.db.execute("SELECT status FROM learning WHERE event_id=?", (event_id,)).fetchone()
                if learning is None or learning[0] == "deferred":
                    self.db.execute("INSERT INTO learning VALUES(?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,attempts=learning.attempts+1,detail=excluded.detail,updated_at=excluded.updated_at",
                                    (event_id, "failed", 1, "[]", "process_interrupted_after_reservation_host_write_unverified", float(self.now())))
            return len(rows)

    def recent_activity(self, platform=None, since=None, limit=12, context=None):
        limit = min(50, max(0, int(limit)))
        where, args = [], []
        if platform:
            where.append("platform=?")
            args.append(_platform(platform))
        if since is not None:
            where.append("created_at>=?")
            args.append(float(since))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._lock:
            rows = self.db.execute("SELECT payload FROM activities" + clause + " ORDER BY created_at DESC,action_id DESC LIMIT ?", [*args, 200 if context else limit]).fetchall()
        activities = [json.loads(row[0]) for row in rows]
        if context:
            activities = [a for a in activities if self.allowed(a, context.get("platform"), context.get("conversation_id", ""), context.get("purpose", "internal"))]
        return activities[:limit]

    def init_persona(self, stable, dynamic=None):
        if not isinstance(stable, dict) or not isinstance(dynamic or {}, dict):
            raise ValueError("persona_layers_must_be_objects")
        allowed = character.LEARNABLE_FIELDS if character.expanded(self.policy) else DYNAMIC_FIELDS
        if set(dynamic or {}) - allowed:
            raise ValueError("unsupported_dynamic_field")
        with self._transaction():
            existing = self.persona()
            if existing:
                if existing["stable"] != stable:
                    raise ValueError("stable_persona_requires_explicit_admin_revision")
                return existing
            self.db.execute("INSERT INTO persona_versions VALUES(1,?,?,?,?,?,?,?)",
                            (_json(stable), _json(dynamic or {}), "[]", "initial_user_persona", float(self.now()), None, None))
            self.put("persona_version", 1)
            self._bump_revision()
            return self.persona()

    def sync_learnable_persona(self, host, defaults, seed_revision):
        """Trusted config migration; never called with model or user-message values.

        A named administrator seed revision is applied once. Ordinary restarts
        and Host refreshes preserve all learned fields and every quota/ledger.
        """
        if set(host) != HOST_PERSONA_FIELDS or not character.expanded(self.policy):
            raise ValueError('complete_host_and_expanded_schema_required')
        if not isinstance(seed_revision, str) or not seed_revision.strip():
            raise ValueError('explicit_seed_revision_required')
        if not isinstance(defaults, dict) or any(not character.valid_value(k, v) for k, v in defaults.items()):
            raise ValueError('invalid_persona_seed')
        stable = {key: host[key] for key in character.CORE_FIELDS}
        if any(not isinstance(v, str) or not v.strip() for v in stable.values()):
            raise ValueError('core_persona_required')
        with self._transaction():
            current = self.persona()
            revision_changed = self.get('persona_seed_revision') != seed_revision
            previous = current['dynamic'] if current else {}
            dynamic = {k: _copy(v) for k, v in previous.items() if character.valid_value(k, v)}
            if revision_changed:
                for field, seeded in defaults.items():
                    if field in character.TEMP_TTL:
                        continue  # never seed a fresh 24h/72h state from deployment time
                    # Migrate only ABSENT fields and exact known pre-seeded variants. A stored [] is an
                    # explicit clear, not "missing"; grown knowledge and Owner settings differ from every seed.
                    if field not in dynamic or dynamic[field] == seeded or character.is_legacy_seed(field, dynamic[field]):
                        dynamic[field] = _copy(seeded)
            if not current or current['stable'] != stable or current['dynamic'] != dynamic or revision_changed:
                version = int(self.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM persona_versions').fetchone()[0])
                self.db.execute('INSERT INTO persona_versions VALUES(?,?,?,?,?,?,?,?)',
                    (version, _json(stable), _json(dynamic), '[]', 'trusted_persona_profile_revision:' + seed_revision,
                     float(self.now()), current['version'] if current else None, None))
                self.put('persona_version', version)
                self.put('persona_seed_revision', seed_revision)
                self._bump_revision()
            return self.persona()

    def revise_host_persona(self, stable):
        """Apply a complete snapshot read directly from trusted Host config.

        This is deliberately separate from proposal/reflection and IPC dispatch.
        Host configuration edits are authoritative, while learned dynamic state
        and prior stable snapshots remain available in the version history.
        """
        if not isinstance(stable, dict) or set(stable) != HOST_PERSONA_FIELDS:
            raise ValueError("complete_host_persona_required")
        for key, value in stable.items():
            if key in {"bot.alias_names", "personality.multiple_reply_style"}:
                valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise ValueError("invalid_host_persona_field")
        if not stable["bot.nickname"].strip():
            raise ValueError("host_nickname_required")
        with self._transaction():
            current = self.persona()
            if not current:
                raise ValueError("persona_not_initialized")
            if current["stable"] == stable:
                return current
            version = int(self.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM persona_versions").fetchone()[0])
            self.db.execute("INSERT INTO persona_versions VALUES(?,?,?,?,?,?,?,?)",
                            (version, _json(stable), _json(current["dynamic"]), "[]", "trusted_host_config_revision",
                             float(self.now()), current["version"], None))
            self.put("persona_version", version)
            self._bump_revision()
            return self.persona()

    def persona(self, version=None):
        with self._lock:
            if version is None:
                version = self.get("persona_version")
            row = self.db.execute("SELECT * FROM persona_versions WHERE version=?", (version,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("stable", "dynamic", "evidence_ids"):
            result[key] = json.loads(result[key])
        return result

    def propose_changes(self, changes, evidence_ids, reason, automatic=True, contact_keys=None, effect_key=None):
        per_field_evidence = {}
        if isinstance(changes, list):
            rows = changes
            normalized = {}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("field"), str) or row["field"] in normalized:
                    normalized = None
                    break
                normalized[row["field"]] = row.get("value")
                per_field_evidence[row["field"]] = list(row.get("evidence_ids") or evidence_ids or [])
            changes = normalized
        with self._transaction():
            if effect_key and self.get("effect:" + str(effect_key)) is not None:
                # Same logical effect already decided (accepted OR rejected): a crash replay can never
                # apply a stale value after another update.
                return {"accepted": False, "reason": "effect_replayed", "changes": changes, "evidence_ids": [],
                        "version": (self.persona() or {}).get("version"), "replayed": True}
            current = self.persona()
            expanded = character.expanded(self.policy)
            allowed_fields = character.LEARNABLE_FIELDS if expanded else DYNAMIC_FIELDS
            feedback_fields = character.FEEDBACK_FIELDS if expanded else FEEDBACK_FIELDS
            rejection = ""
            if not current:
                rejection = "persona_not_initialized"
            elif not self.flag("personality_enabled") or not self.flag("personality_updates_enabled") or not self.flag("reflection_enabled"):
                rejection = "personality_updates_paused"
            elif not isinstance(changes, dict) or not 1 <= len(changes) <= 2:
                rejection = "one_or_two_changes_required"
            elif set(changes) - allowed_fields:
                rejection = "stable_or_unsupported_field"
            elif expanded and any(not character.valid_value(k, v) for k, v in changes.items()):
                rejection = "invalid_or_protected_dynamic_value"
            elif any(len(_json(value)) > 2000 for value in changes.values()):
                rejection = "change_too_large"
            elif not _text(reason):
                rejection = "change_reason_required"
            ids = list(dict.fromkeys(str(value) for value in (evidence_ids or [])))[:20]
            evidence = [self.get_event(value) for value in ids]
            if not rejection and (not evidence or any(not event or not event["active"] or not event["learning_allowed"]
                                                      or event["origin"] not in EXTERNAL_ORIGINS for event in evidence)):
                rejection = "valid_external_evidence_required"
            if not rejection and any(not self.growth_evidence_allowed(event) for event in evidence):
                rejection = "private_or_unshared_evidence_cannot_change_global_persona"
            if not rejection:
                distinct_sources = {(e["platform"], e["bot_account_id"], e.get("source_id") or e["native_event_id"]) for e in evidence}
                explicit_feedback = any(self._explicit_feedback(e) for e in evidence)
                # Validated contact evidence (impression ledger) satisfies the sustained-evidence gate for
                # interest fields only; the distinct-source rule is NOT weakened for any other path.
                contact_fields = set()
                if contact_keys and automatic:
                    from . import impressions
                    for field in changes:
                        if field in impressions.INTEREST_FIELDS and impressions.contacts_qualify(
                                self, contact_keys, per_field_evidence.get(field, ids), field, changes[field]):
                            contact_fields.add(field)
                if (automatic and len(distinct_sources) < 2 and not (explicit_feedback and set(changes) <= feedback_fields)
                        and not set(changes) <= contact_fields):
                    rejection = "sustained_evidence_or_explicit_feedback_required"
                for field, field_ids in per_field_evidence.items():
                    field_evidence = [e for e in evidence if e["event_id"] in field_ids]
                    field_sources = {(e["platform"], e["bot_account_id"], e.get("source_id") or e["native_event_id"]) for e in field_evidence}
                    if not field_evidence or (automatic and len(field_sources) < 2 and field not in contact_fields
                                              and not (field in feedback_fields and any(self._explicit_feedback(e) for e in field_evidence))):
                        rejection = "field_requires_its_own_valid_evidence"
                if not rejection and automatic:
                    held = [f for f, v in changes.items() if self.owner_setting_effective(f, current["dynamic"].get(f))
                            and current["dynamic"].get(f) != v]
                    if held:
                        rejection = "owner_setting_in_effect"
                if not rejection and automatic:
                    from . import impressions, v14_2
                    # With the impression ledger active, EVERY automatic stable-interest promotion needs qualified
                    # contact evidence (6 points, >=2 independent autonomous positive choices); two distinct
                    # external messages no longer suffice. Historical behaviour only when the feature is disabled.
                    if (v14_2.enabled(self.policy, "impressions")
                            and (set(changes) & impressions.INTEREST_FIELDS) - contact_fields):
                        rejection = "interest_requires_contact_evidence"
                if all(current["dynamic"].get(key) == value for key, value in changes.items()):
                    rejection = "no_change"
            proposal_id = "proposal:" + uuid.uuid4().hex
            result = {"proposal_id": proposal_id, "accepted": not bool(rejection),
                      "version": current["version"] if current else None, "reason": rejection or _text(reason, 1024),
                      "changes": changes, "evidence_ids": ids,
                      "previous_values": {field: current["dynamic"].get(field) for field in (changes or {})} if current else {}}
            if not rejection:
                dynamic = {**current["dynamic"], **_copy(changes)}
                version = int(self.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM persona_versions").fetchone()[0])
                self.db.execute("INSERT INTO persona_versions VALUES(?,?,?,?,?,?,?,?)",
                                (version, _json(current["stable"]), _json(dynamic), _json(ids), _text(reason, 1024),
                                 float(self.now()), current["version"], None))
                self.put("persona_version", version)
                self._bump_revision()
                result["version"] = version
                for field in character.TEMP_TTL:
                    if field in changes:
                        field_ids = per_field_evidence.get(field, ids)
                        times = [float(e.get("occurred_at") or e.get("received_at") or 0)
                                 for e in evidence if e["event_id"] in field_ids]
                        if times:  # original evidence time, never "now"
                            self._set_temp_locked(field, changes[field], "global", sorted(field_ids)[0], max(times))
            self.db.execute("INSERT INTO persona_proposals VALUES(?,?,?,?)",
                            (proposal_id, "accepted" if result["accepted"] else "rejected", _json(result), float(self.now())))
            if effect_key:
                self.put("effect:" + str(effect_key), {"accepted": result["accepted"], "proposal_id": proposal_id})
            return result

    # ---- v14.2: short-lived fields, Owner settings, scoped relationship memory -----------------
    def owner_setting_effective(self, field, current_value):
        record = self.get("owner_set:" + str(field))
        return bool(record) and current_value is not None and record.get("value") == current_value

    def _set_temp_locked(self, field, value, scope, source_event_id, evidence_at):
        expires = float(evidence_at) + character.TEMP_TTL[field]
        self.db.execute(
            "INSERT INTO persona_temp VALUES(?,?,?,?,?,?,?) ON CONFLICT(field) DO UPDATE SET value=excluded.value,"
            "scope=excluded.scope,source_event_id=excluded.source_event_id,evidence_at=excluded.evidence_at,"
            "expires_at=excluded.expires_at,set_at=excluded.set_at",
            (field, _json(value), scope, str(source_event_id), float(evidence_at), expires, float(self.now())))

    def set_temp(self, field, value, evidence_event_id, reason="short_term_focus", effect_key=None):
        """Set current_state/current_focus from ONE genuine global-usable event. TTL counts from that
        event's original time (24h state / 72h focus); a restatement or cache rebuild never renews it."""
        if field not in character.TEMP_TTL or not character.valid_value(field, value):
            return {"accepted": False, "reason": "invalid_temp_field_or_value"}
        with self._transaction():
            if effect_key and self.get("effect:" + str(effect_key)) is not None:
                return {"accepted": False, "reason": "effect_replayed", "replayed": True}
            current = self.persona()
            event = self.get_event(evidence_event_id)
            if not current:
                return {"accepted": False, "reason": "persona_not_initialized"}
            if not self.flag("personality_enabled") or not self.flag("personality_updates_enabled"):
                return {"accepted": False, "reason": "personality_updates_paused"}
            if (not event or not event["active"] or event["origin"] not in EXTERNAL_ORIGINS
                    or not self.growth_evidence_allowed(event)):
                return {"accepted": False, "reason": "global_usable_evidence_required"}
            evidence_at = float(event.get("occurred_at") or event.get("received_at") or 0)
            if evidence_at + character.TEMP_TTL[field] <= float(self.now()):
                return {"accepted": False, "reason": "evidence_already_expired"}
            previous = self.db.execute("SELECT value,evidence_at FROM persona_temp WHERE field=?", (field,)).fetchone()
            if previous and json.loads(previous["value"]) == value and current["dynamic"].get(field) == value:
                return {"accepted": False, "reason": "no_change"}
            version = int(self.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM persona_versions").fetchone()[0])
            self.db.execute("INSERT INTO persona_versions VALUES(?,?,?,?,?,?,?,?)",
                            (version, _json(current["stable"]), _json({**current["dynamic"], field: value}),
                             _json([event["event_id"]]), _text(reason, 1024), float(self.now()), current["version"], None))
            self.put("persona_version", version)
            self._bump_revision()
            self._set_temp_locked(field, value, "global", event["event_id"], evidence_at)
            if effect_key:
                self.put("effect:" + str(effect_key), {"accepted": True, "version": version})
            return {"accepted": True, "version": version, "expires_at": evidence_at + character.TEMP_TTL[field]}

    def persona_view(self, now=None):
        """Persona for display: a short-lived field without valid original-evidence metadata, or past its
        expiry, is omitted (stored history is untouched). Expired content silently disappears."""
        persona = self.persona()
        if not persona:
            return persona
        moment = float(self.now() if now is None else now)
        dynamic = dict(persona["dynamic"])
        with self._lock:
            rows = {row["field"]: row for row in self.db.execute("SELECT * FROM persona_temp")}
        for field in character.TEMP_TTL:
            if field in dynamic:
                row = rows.get(field)
                if not row or json.loads(row["value"]) != dynamic[field] or row["expires_at"] <= moment:
                    dynamic.pop(field)
        priority = [f for f in dynamic if self.owner_setting_effective(f, dynamic[f])]
        return {**persona, "dynamic": dynamic, "priority_fields": priority}

    def temp_signature(self, now=None):
        """Changes the moment any displayed short-lived field expires; part of every context cache key."""
        moment = float(self.now() if now is None else now)
        with self._lock:
            rows = self.db.execute("SELECT field,expires_at FROM persona_temp WHERE expires_at>? ORDER BY field", (moment,)).fetchall()
        return tuple((row[0], row[1]) for row in rows)

    def temp_next_expiry(self, now=None):
        moment = float(self.now() if now is None else now)
        with self._lock:
            row = self.db.execute("SELECT MIN(expires_at) FROM persona_temp WHERE expires_at>?", (moment,)).fetchone()
        return row[0]

    def log_feedback(self, event_id, route, status, detail=None, version=None, actor=""):
        with self._lock:
            self.db.execute(
                "INSERT INTO feedback_log VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET route=excluded.route,"
                "status=excluded.status,detail=excluded.detail,persona_version=excluded.persona_version",
                (str(event_id), route, status, _json(detail or {}), version, _text(actor, 256), float(self.now())))

    def feedback_logged(self, event_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM feedback_log WHERE event_id=?", (str(event_id),)).fetchone()
        return dict(row) if row else None

    RELATIONSHIP_ATTRIBUTES = {"short": "length", "detail": "length", "gentle": "tone", "serious": "humor"}

    def add_relationship_pref(self, platform, bot_account_id, actor_id, session_id, kind, text, quote, source_event_id):
        """Scoped to platform + bot account + VERIFIED actor + session. Idempotent per source event."""
        platform, bot_account_id = _platform(platform), _text(bot_account_id, 256)
        actor_id, session_id = _text(actor_id, 256), _text(session_id, 512)
        text, quote, kind = _text(text, 160), _text(quote, 200), _text(kind, 32) or "free"
        if not (platform and bot_account_id and actor_id and session_id and text and source_event_id):
            raise ValueError("relationship_scope_required")
        if blocked_text(text) or character.PROTECTED_OVERRIDE.search(text) or re.search(r"https?://|<\|", text):
            return {"created": False, "reason": "invalid_relationship_preference"}
        pref_id = "rel:" + hashlib.sha256(_json([platform, bot_account_id, actor_id, session_id, source_event_id]).encode()).hexdigest()[:32]
        with self._transaction():
            if self.db.execute("SELECT 1 FROM relationship_prefs WHERE source_event_id=?", (str(source_event_id),)).fetchone():
                return {"created": False, "reason": "replayed", "pref_id": pref_id}
            # Supersede by ATTRIBUTE: 'short' and 'detail' are opposite values of one attribute (reply length), so only the
            # latest effective one stays active; other attributes and other actors/sessions are untouched.
            same = [k for k, attribute in self.RELATIONSHIP_ATTRIBUTES.items()
                    if attribute == self.RELATIONSHIP_ATTRIBUTES.get(kind, kind)] or [kind]
            self.db.execute("UPDATE relationship_prefs SET active=0 WHERE platform=? AND bot_account_id=? AND actor_id=? "
                            "AND session_id=? AND kind IN (%s) AND active=1" % ",".join("?" for _ in {*same, kind}),
                            (platform, bot_account_id, actor_id, session_id, *{*same, kind}))
            self.db.execute("INSERT INTO relationship_prefs(pref_id,platform,bot_account_id,actor_id,session_id,kind,text,quote,"
                            "source_event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (pref_id, platform, bot_account_id, actor_id, session_id, kind, text, quote,
                             str(source_event_id), float(self.now())))
            return {"created": True, "pref_id": pref_id}

    def relationship_prefs(self, platform, bot_account_id, actor_id, session_id, limit=4):
        with self._lock:
            rows = self.db.execute(
                "SELECT pref_id,kind,text,source_event_id,created_at FROM relationship_prefs WHERE platform=? AND bot_account_id=? "
                "AND actor_id=? AND session_id=? AND active=1 ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (_platform(platform), _text(bot_account_id, 256), _text(actor_id, 256), _text(session_id, 512),
                 min(8, max(0, int(limit))))).fetchall()
        return [dict(row) for row in rows]

    def set_relationship_memory_status(self, pref_id, status):
        with self._lock:
            self.db.execute("UPDATE relationship_prefs SET memory_status=? WHERE pref_id=?", (_text(status, 32), pref_id))

    OWNER_WINDOW = 600.0
    OWNER_MAX_UPDATES = 3

    def owner_scope(self, session, actor, is_private):
        """'owner_group' / 'owner_private' only for the Owner bound in qq_owner_bindings (verified private
        binding for DMs). A generic group admin, a claim or a quoted owner never qualifies. '' otherwise."""
        bindings = self.policy.get("qq_owner_bindings") or {}
        if not isinstance(bindings, dict) or not session or not actor:
            return ""
        groups = list((self.policy.get("qq_group_sessions") or {}).values())
        if not is_private:
            return "owner_group" if session in groups and bindings.get(session) == actor else ""
        if self.policy.get("qq_private_familiar_enabled") is not True:
            return ""
        try:
            row = self.db.execute("SELECT * FROM qq_private_bindings WHERE private_id=?", (str(actor),)).fetchone()
        except sqlite3.Error:
            return ""
        if (row and row["session"] == session and row["group_session"] in groups
                and bindings.get(row["group_session"]) == row["group_actor"]):
            return "owner_private"
        return ""

    def reserve_many(self, items, effect=None):
        """All-or-nothing reservation of several quotas [(key, kind, limit, protected)]: a purpose budget and the
        shared background cap are accounted atomically, so one cannot be spent without the other. `effect`, if given, runs
        INSIDE the same transaction after every slot was granted (e.g. persisting an attempt counter), so a crash can never
        leave a reservation without its bookkeeping or bookkeeping without its reservation."""
        day = datetime.fromtimestamp(float(self.now()), CHINA).date().isoformat()
        with self._transaction():
            planned = []
            for key, kind, limit, protected in items:
                hard = max(0, int(limit))
                if kind == "background":
                    hard = min(self.BACKGROUND_CAP, hard)
                    if not protected:
                        hard = min(hard, self.BACKGROUND_CAP - self.DAILY_RESERVED)
                reservation = _json([str(kind), str(key)])
                if self.db.execute("SELECT 1 FROM quotas WHERE reservation_key=?", (reservation,)).fetchone():
                    return False
                used = self.db.execute("SELECT COUNT(*) FROM quotas WHERE kind=? AND day=?", (str(kind), day)).fetchone()[0]
                if used >= hard:
                    return False
                planned.append((reservation, str(kind)))
            for reservation, kind in planned:
                self.db.execute("INSERT INTO quotas VALUES(?,?,?,?,?)", (reservation, kind, day, float(self.now()), "reserved"))
            if effect is not None:
                effect()
            return True

    def apply_owner_config(self, changes, event_id, quote, reason="owner_configuration"):
        """Authenticated Owner configuration route. The Owner is RE-VERIFIED here from the stored original
        event (actor and session come from the event, never from the caller), against qq_owner_bindings, on
        every application including a deferred one; the quote must be verbatim in that stored message. Only
        allowed expression fields; fixed core and account permissions are untouched. Same event is idempotent;
        identical settings are de-duplicated; more than OWNER_MAX_UPDATES real version updates per OWNER_WINDOW
        are merged into one pending latest request per Owner."""
        if (not isinstance(changes, dict) or not 1 <= len(changes) <= 2
                or any(k not in character.FEEDBACK_FIELDS or not character.valid_value(k, v) for k, v in changes.items())):
            return {"status": "rejected", "reason": "invalid_owner_setting"}
        if not _text(quote) or not _text(event_id):
            return {"status": "rejected", "reason": "source_quote_required"}
        with self._transaction():
            logged = self.feedback_logged(event_id)
            if logged and logged["status"] in ("applied", "no_change", "duplicate"):
                return {"status": logged["status"], "replayed": True, "version": logged["persona_version"]}
            event = self.get_event(event_id)
            if (not event or not event["active"] or event.get("platform") != "qq" or event.get("origin") != "external_user"):
                return {"status": "rejected", "reason": "owner_evidence_invalid"}
            owner_actor = _text(event.get("actor_id"), 256)
            private = bool((event.get("metadata") or {}).get("is_private")) or event.get("sharing_scope") == "conversation"
            if not self.owner_scope(event.get("conversation_id"), owner_actor, private):
                return {"status": "rejected", "reason": "owner_not_verified"}
            if not 4 <= len(quote) <= 200 or quote not in str(event.get("content") or ""):
                return {"status": "rejected", "reason": "quote_not_in_original_message"}
            current = self.persona()
            if not current:
                return {"status": "rejected", "reason": "persona_not_initialized"}
            if not self.flag("personality_enabled") or not self.flag("personality_updates_enabled"):
                return {"status": "rejected", "reason": "personality_updates_paused"}
            if all(current["dynamic"].get(k) == v for k, v in changes.items()):
                self.log_feedback(event_id, "owner_global", "no_change", {"changes": changes}, current["version"], owner_actor)
                return {"status": "no_change", "version": current["version"]}
            moment = float(self.now())
            recent = self.db.execute("SELECT COUNT(*) FROM feedback_log WHERE route='owner_global' AND status='applied' "
                                     "AND actor_id=? AND created_at>?", (owner_actor, moment - self.OWNER_WINDOW)).fetchone()[0]
            if recent >= self.OWNER_MAX_UPDATES:
                self.db.execute("INSERT INTO owner_global_pending VALUES(?,?,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET "
                                "event_id=excluded.event_id,changes=excluded.changes,quote=excluded.quote,created_at=excluded.created_at",
                                (owner_actor, str(event_id), _json(changes), _text(quote, 200), moment))
                self.log_feedback(event_id, "owner_global", "pending", {"changes": changes}, current["version"], owner_actor)
                return {"status": "pending", "reason": "owner_update_rate_limited"}
            previous = {k: current["dynamic"].get(k) for k in changes}
            version = int(self.db.execute("SELECT COALESCE(MAX(version),0)+1 FROM persona_versions").fetchone()[0])
            self.db.execute("INSERT INTO persona_versions VALUES(?,?,?,?,?,?,?,?)",
                            (version, _json(current["stable"]), _json({**current["dynamic"], **_copy(changes)}), "[]",
                             _text(reason + ":" + str(event_id), 1024), moment, current["version"], None))
            self.put("persona_version", version)
            for field, value in changes.items():
                self.put("owner_set:" + field, {"value": value, "version": version, "at": moment, "event_id": str(event_id)})
            self.db.execute("DELETE FROM owner_global_pending WHERE actor_id=?", (owner_actor,))
            self.log_feedback(event_id, "owner_global", "applied", {"changes": changes, "previous": previous, "quote": _text(quote, 200)},
                              version, owner_actor)
            self._bump_revision()
            return {"status": "applied", "version": version, "previous_values": previous}

    def flush_owner_pending(self):
        """Apply merged pending Owner requests whose rate window has opened. Returns applied results."""
        with self._lock:
            rows = [dict(row) for row in self.db.execute("SELECT * FROM owner_global_pending")]
        results = []
        for row in rows:
            with self._transaction():
                self.db.execute("DELETE FROM feedback_log WHERE event_id=? AND status='pending'", (row["event_id"],))
                self.db.execute("DELETE FROM owner_global_pending WHERE actor_id=? AND event_id=?", (row["actor_id"], row["event_id"]))
            # Re-validates the original event, the Owner binding and the quote now (not only at enqueue time).
            outcome = self.apply_owner_config(json.loads(row["changes"]), row["event_id"], row["quote"])
            if outcome["status"] == "rejected":
                self.log_feedback(row["event_id"], "owner_global", "rejected", {"reason": outcome.get("reason")}, None, row["actor_id"])
            results.append({"actor_id": row["actor_id"], **outcome})
        return results

    @staticmethod
    def _explicit_feedback(event):
        if event.get("origin") != "external_user":
            return False
        if event.get("event_type") in {"user_feedback", "feedback", "user_correction", "correction"}:
            return True
        # The original user message must itself contain direct expression feedback.
        # Model-generated labels or confidence do not establish this evidence.
        content = _text(event.get("content"), 2000)
        return bool(re.search(r"(?:请|不要|别|少用|少说|减少|尽量|能不能|以后|多用|多说).{0,60}(?:口头禅|套话|啰嗦|简短|简洁|短一点|短一些|称呼|语气|回复|回答|名字|改名|昵称|外号|自称|表情|颜文字|表达|措辞|接话|提问)", content)
                    or re.search(r"(?:回复|回答|说话).{0,30}(?:短一点|短一些|简短|简洁|别太长|不要太长|少啰嗦)", content))

    def growth_evidence_allowed(self, event):
        """Apply before reflection model input and again before committing changes."""
        if not isinstance(event, dict) or not event.get("active", True) or not event.get("learning_allowed"):
            return False
        metadata = event.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        evidence = {**metadata, **event}
        if (evidence.get("origin") not in EXTERNAL_ORIGINS or evidence.get("is_private")
                or evidence.get("conversation_type") in {"private", "dm"} or evidence.get("sensitive")
                or evidence.get("sharing_scope") in {"private", "conversation", "local", "none"}):
            return False
        if _platform(evidence.get("platform")) == "qq":
            conversation = evidence.get("conversation_id") or evidence.get("chat_id")
            if conversation not in self.policy.get("allowed_qq_sessions", []):
                return False
            groups = self.policy.get("shared_groups", [])
            if groups and all(isinstance(item, str) for item in groups):
                groups = [groups]
            return any(conversation in (group.get("sessions", group.get("session_ids", []))
                                        if isinstance(group, dict) else group)
                       for group in groups if isinstance(group, (dict, list, tuple)))
        return True

    def rollback_persona(self, version, reason="administrator_rollback"):
        with self._transaction():
            target, current = self.persona(int(version)), self.persona()
            if not target or not current or not _text(reason):
                raise ValueError("valid_persona_version_and_reason_required")
            if target["stable"] != current["stable"]:
                raise ValueError("cannot_rollback_stable_identity_implicitly")
            new_version = int(self.db.execute("SELECT MAX(version)+1 FROM persona_versions").fetchone()[0])
            self.db.execute("INSERT INTO persona_versions VALUES(?,?,?,?,?,?,?,?)",
                            (new_version, _json(current["stable"]), _json(target["dynamic"]), _json(target["evidence_ids"]),
                             _text(reason, 1024), float(self.now()), current["version"], int(version)))
            self.put("persona_version", new_version)
            self._bump_revision()
            return self.persona()

    BACKGROUND_CAP = 20
    DAILY_RESERVED = 5   # positions only the daily learning batch (and its retries) may use

    def reserve_quota(self, key, kind="background", limit=20, protected=False):
        """Reserve before a request. Replayed keys and failed calls cannot evade cap.

        Background callers other than the daily batch (protected=True) never use the last
        DAILY_RESERVED positions of the 20/day learning/reflection background cap."""
        if not _text(key) or not _text(kind):
            raise ValueError("quota_key_and_kind_required")
        hard_limit = min(self.BACKGROUND_CAP, max(0, int(limit))) if kind == "background" else max(0, int(limit))
        if kind == "background" and not protected:
            hard_limit = min(hard_limit, self.BACKGROUND_CAP - self.DAILY_RESERVED)
        day = datetime.fromtimestamp(float(self.now()), CHINA).date().isoformat()
        reservation = _json([str(kind), str(key)])
        with self._transaction():
            if self.db.execute("SELECT 1 FROM quotas WHERE reservation_key=?", (reservation,)).fetchone():
                return False
            if self.quota_used(kind) >= hard_limit:
                return False
            self.db.execute("INSERT INTO quotas VALUES(?,?,?,?,?)", (reservation, str(kind), day, float(self.now()), "reserved"))
            return True

    def quota_used(self, kind="background", at=None):
        day = datetime.fromtimestamp(float(self.now() if at is None else at), CHINA).date().isoformat()
        with self._lock:
            return int(self.db.execute("SELECT COUNT(*) FROM quotas WHERE kind=? AND day=?", (str(kind), day)).fetchone()[0])

    def learning_candidates(self, limit=5):
        if not self.flag("learning_enabled"):
            return []
        count = min(5, max(0, int(limit)))
        if not count:
            return []
        pending = """SELECT e.event_id FROM events e LEFT JOIN learning l ON e.event_id=l.event_id
            WHERE e.learning_allowed=1 AND e.active=1 AND e.origin IN ('external_user','platform_content')
            AND (l.event_id IS NULL OR l.status='deferred')"""
        result, selected = [], set()

        def eligible(row):
            event = self.get_event(row[0])
            if event["platform"] == "qq" and event["conversation_id"] not in self.policy.get("allowed_qq_sessions", []):
                return None
            return event

        with self._lock:
            # User corrections must not wait behind a long video-reading queue.
            # Scan the pending QQ stream independently of the FIFO's bounded
            # prefix, using the same original-message feedback validator as
            # personality updates. The cursor is lazy and no quota/state changes.
            priority = self.db.execute(pending + " AND e.platform='qq' AND e.origin='external_user'"
                                       " ORDER BY e.received_at,e.event_id")
            for row in priority:
                event = eligible(row)
                if event is not None and self._explicit_feedback(event):
                    result.append(event)
                    selected.add(event["event_id"])
                    if len(result) >= count:
                        return result
            for row in self.db.execute(pending + " ORDER BY e.received_at,e.event_id LIMIT ?", (count * 10,)):
                if row[0] in selected:
                    continue
                event = eligible(row)
                if event is not None:
                    result.append(event)
                    if len(result) >= count:
                        break
        return result

    def mark_learned(self, event_id, status="stored", stored_ids=None, detail=""):
        if status not in {"stored", "processed", "skipped", "failed", "deferred"}:
            raise ValueError("invalid_learning_status")
        with self._transaction():
            event = self.get_event(event_id)
            if event is None:
                raise ValueError("event_not_found")
            previous = self.db.execute("SELECT status,attempts FROM learning WHERE event_id=?", (str(event_id),)).fetchone()
            if previous and previous["status"] in {"stored", "processed", "skipped"}:
                return False
            attempts = (previous["attempts"] if previous else 0) + (status != "deferred")
            self.db.execute("INSERT INTO learning VALUES(?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,stored_ids=excluded.stored_ids,detail=excluded.detail,updated_at=excluded.updated_at",
                            (str(event_id), status, attempts, _json(list(stored_ids or [])[:30]), _text(detail, 512), float(self.now())))
            return True

    def audit_context(self, task, platform, conversation_id, source_ids, persona_version,
                      permissions=None, model_task=""):
        audit_id = "context:" + uuid.uuid4().hex
        payload = {"task": _text(task, 128), "platform": _platform(platform),
                   "conversation_id": _text(conversation_id, 512),
                   "source_ids": list(dict.fromkeys(_text(value, 512) for value in source_ids))[:100],
                   "persona_version": persona_version, "permissions": [_text(value, 128) for value in (permissions or [])][:20],
                   "model_task": _text(model_task, 128), "context_revision": self.get("context_revision", 0)}
        with self._transaction():
            self.db.execute("INSERT INTO context_audits VALUES(?,?,?)", (audit_id, float(self.now()), _json(payload)))
            self.db.execute("DELETE FROM context_audits WHERE audit_id IN (SELECT audit_id FROM context_audits ORDER BY created_at DESC,rowid DESC LIMIT -1 OFFSET 2000)")
        return audit_id

    def backup(self, destination):
        """SQLite online backup; do not copy the live DB file without its WAL."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with sqlite3.connect(str(destination)) as backup:
                self.db.backup(backup)
        destination.chmod(0o600)
        return str(destination)
