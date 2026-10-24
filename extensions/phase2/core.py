"""Single writer, fail-closed policy, and durable delivery ledger (stdlib only)."""

from __future__ import annotations
import contextlib, fcntl, hashlib, json, random, re, sqlite3, time, uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DEFAULTS = {
    "bot_uid": "",
    "owner_uid": "",
    "writer_machine": "",
    "paused": True,
    "enabled": {"dynamic": False, "proactive": False, "reply": False, "dm": False},
    "limits": {"dynamic": 10, "proactive": 6, "reply": 20, "dm": 20},
    "author_daily": 2,
    "thread_daily": 3,
    "active_start": 9,
    "active_end": 23,
    "proactive_gap": 1800,
    "browse_min": 2700,
    "browse_max": 5400,
    "poll_seconds": 60,
    "reply_interest_min_score": 65,
    "max_candidates": 3,
    "model_daily": 80,
    "max_model_tokens": 500,
    "interests": ["AI", "大模型", "游戏", "甜点", "美食", "轻松日常"],
    "dynamic_ids": [],
    "video_ids": [],
    "source_uids": [],
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com/v1",
}


def machine_id():
    p = Path("/etc/machine-id")
    return hashlib.sha256(
        (p.read_text().strip() if p.exists() else str(uuid.getnode())).encode()
    ).hexdigest()


def load_config(path, now=None):
    c = json.loads(Path(path).read_text())
    if '_activity_boost_active' in c:raise ValueError('reserved_computed_configuration_field')
    for k, v in DEFAULTS.items():
        c.setdefault(k, v)
    if c["base_url"].rstrip("/") not in (
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
    ):
        raise ValueError("nonofficial_model_endpoint")
    for k in ("dynamic", "proactive", "reply", "dm"):
        c["enabled"].setdefault(k, False)
        if not isinstance(c["enabled"][k], bool):
            raise ValueError("invalid_switch")
        if not 0 <= c["limits"][k] <= DEFAULTS["limits"][k]:
            raise ValueError("limit_exceeds_authorization")
    if not isinstance(c["paused"], bool):
        raise ValueError("invalid_pause")
    score = c["reply_interest_min_score"]
    if type(score) is not int or not 1 <= score <= 100:
        raise ValueError("invalid_reply_interest_threshold")
    if c["enabled"]["dm"] and not str(c["owner_uid"]).isdigit():
        raise ValueError("owner_required")
    if (
        c["max_candidates"] > 3
        or c["author_daily"] > 2
        or c["thread_daily"] > 3
        or c["proactive_gap"] < 1800
        or c["browse_min"] < 2700
        or c["browse_max"] > 5400
    ):
        raise ValueError("policy_exceeds_authorization")
    if c["active_start"] < 9 or c["active_end"] > 23:
        raise ValueError("hours_exceed_authorization")
    from .mentions import policy as mention_policy
    mention_policy(c)
    from .boost import policy as boost_policy
    boost_policy(c)
    from .engagement import policy as engagement_policy
    engagement_policy(c)
    from .live import policy as live_policy
    live_policy(c)
    from .boost import effective
    return effective(c,time.time() if now is None else now)


def day(ts):
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


def active(c, ts):
    return c["active_start"] <= datetime.fromtimestamp(ts, TZ).hour < c["active_end"]


def future(c, ts, kind, rng=random):
    now = datetime.fromtimestamp(ts, TZ)
    start = now.replace(hour=c["active_start"], minute=0, second=0, microsecond=0)
    end = now.replace(hour=c["active_end"], minute=0, second=0, microsecond=0)
    if now >= end:
        start += timedelta(days=1)
        end += timedelta(days=1)
    if kind == "proactive":
        candidate = max(ts, start.timestamp()) + rng.randint(
            c["browse_min"], c["browse_max"]
        )
    else:
        candidate = rng.uniform(max(ts + 60, start.timestamp()), end.timestamp())
    if candidate >= end.timestamp():
        candidate = (start + timedelta(days=1)).timestamp() + rng.randint(
            c["browse_min"], c["browse_max"]
        )
    return candidate


@dataclass
class Target:
    kind: str
    oid: str
    type: int
    author: str
    text: str = ""
    url: str = ""
    images: list | None = None
    source_id: str = ""
    evidence: str = ""

    @property
    def key(self):
        return f"{self.type}:{self.oid}"


def dynamic_target(item):
    basic = item.get("basic", {})
    modules = item.get("modules", {})
    oid = str(basic.get("comment_id_str") or "")
    typ = int(basic.get("comment_type") or 0)
    if not oid.isdigit() or int(oid) <= 0 or typ not in (1, 11, 17):
        return None
    if modules.get("module_stat", {}).get("comment", {}).get("forbidden"):
        return None
    if item.get("type") not in (
        "DYNAMIC_TYPE_DRAW",
        "DYNAMIC_TYPE_WORD",
        "DYNAMIC_TYPE_FORWARD",
        "DYNAMIC_TYPE_AV",
    ):
        return None
    content = modules.get("module_dynamic", {})
    major = content.get("major") or {}
    text = (content.get("desc") or {}).get("text", "")
    opus = major.get("opus") or {}
    if not text:
        text = (opus.get("summary") or {}).get("text", "")
    common = (content.get("additional") or {}).get("common") or {}
    if common.get("sub_type") == "game" and common.get("title"):
        text += "\n平台公开内容标签：游戏 · " + str(common["title"])[:120]
    images = []
    if major.get("draw"):
        images = [i.get("src", "") for i in major["draw"].get("items", [])[:2]]
    elif opus:
        images = [i.get("url", "") for i in opus.get("pics", [])[:2]]
    if major.get("archive"):
        a = major["archive"]
        text += "\n" + a.get("title", "") + "\n" + a.get("desc", "")
    if item.get("orig"):
        original = item["orig"].get("modules", {}).get("module_dynamic", {})
        original_opus = (original.get("major") or {}).get("opus") or {}
        text += "\n转发的公开原文：" + (
            (original.get("desc") or {}).get("text", "")
            or (original_opus.get("summary") or {}).get("text", "")
        )
        images += [
            x.get("src", "")
            for x in (original.get("major") or {}).get("draw", {}).get("items", [])[:2]
        ]
        images += [x.get("url", "") for x in original_opus.get("pics", [])[:2]]
    return Target(
        "video" if typ == 1 else "dynamic",
        oid,
        typ,
        str(modules.get("module_author", {}).get("mid", "")),
        text[:6000],
        f"https://t.bilibili.com/{item.get('id_str', '')}",
        images[:2],
        str(item.get("id_str", "")),
        str(item.get("type", "")),
    )


class Denied(Exception):
    pass


class InterestSkip(Denied):
    """An intentional no-reply decision, not a model/network failure."""


class PlatformError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(str(code))


class UnknownDelivery(Exception):
    pass


class Store:
    def __init__(self, root):
        self._lock_held = False
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            self.root / "state.sqlite", timeout=10, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
  PRAGMA max_page_count=32768;PRAGMA journal_size_limit=4194304;
  CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,uid TEXT,kind TEXT,target TEXT,author TEXT,thread TEXT,day TEXT,created REAL,status TEXT,text TEXT,remote_id TEXT,url TEXT,error TEXT);
  CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY,received REAL,status TEXT);
  CREATE TABLE IF NOT EXISTS stopped(thread TEXT PRIMARY KEY);
  CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,created REAL,day TEXT,status TEXT,prompt INTEGER,completion INTEGER,model TEXT,seconds REAL);
  CREATE TABLE IF NOT EXISTS memory(id TEXT PRIMARY KEY,thread TEXT,text TEXT,created REAL);
  CREATE INDEX IF NOT EXISTS actions_quota ON actions(uid,kind,day);
  """)

    def get(self, k, default=None):
        r = self.db.execute("select v from meta where k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, k, v):
        self.db.execute(
            "insert or replace into meta values(?,?)",
            (k, json.dumps(v, ensure_ascii=False)),
        )

    def maintain(self, ts):
        if self.get("maintenance_day") == day(ts):
            return
        self.db.execute("delete from memory where created<?", (ts - 30 * 86400,))
        # Keep delivery/dedupe ledger; expire only optional thread text history.
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.put("maintenance_day", day(ts))

    @contextlib.contextmanager
    def lock(self):
        with open(self.root / "instance.lock", "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Denied("another_writer_is_running")
            try:
                self._lock_held = True
                yield
            finally:
                self._lock_held = False
                fcntl.flock(f, fcntl.LOCK_UN)

    def recover(self):
        self.db.execute(
            "update actions set status='unknown',error='process_interrupted' where status='sending'"
        )
        self.db.execute(
            "update actions set status='failed',error='generation_interrupted_no_send' where status='pending'"
        )
        self.db.execute(
            "update seen set status='interrupted' where status='processing'"
        )

    def stop_thread(self, thread):
        self.db.execute("insert or ignore into stopped values(?)", (thread,))

    def used(self, uid, kind, ts):
        return self.db.execute(
            "select count(*) from actions where uid=? and kind=? and day=?",
            (uid, kind, day(ts)),
        ).fetchone()[0]

    def permit(self, c, kind, target, ts, thread="", recipient="", key=""):
        if c["paused"] or (self.root / "PAUSE").exists() or self.get("pause_reason"):
            raise Denied("paused")
        if not c["enabled"].get(kind, False):
            raise Denied("feature_disabled")
        uid = str(c["bot_uid"])
        if not uid.isdigit() or uid != str(self.get("bound_uid", "")):
            raise Denied("account_not_bound")
        if c["writer_machine"] != machine_id():
            raise Denied("wrong_writer_machine")
        if not self.get("baseline_at"):
            raise Denied("baseline_required")
        if kind in ("dynamic", "proactive") and not active(c, ts):
            raise Denied("outside_active_hours")
        if kind == "dm" and (
            not recipient or recipient != str(c["owner_uid"]) or recipient == uid
        ):
            raise Denied("owner_only")
        if (
            key
            and self.db.execute("select 1 from actions where id=?", (key,)).fetchone()
        ):
            raise Denied("already_attempted")
        if self.used(uid, kind, ts) >= c["limits"][kind]:
            raise Denied("daily_limit")
        if (
            thread
            and self.db.execute(
                "select 1 from stopped where thread=?", (thread,)
            ).fetchone()
        ):
            raise Denied("thread_stopped")
        if (
            kind == "reply"
            and self.db.execute(
                "select count(*) from actions where uid=? and kind='reply' and thread=? and day=?",
                (uid, thread, day(ts)),
            ).fetchone()[0]
            >= c["thread_daily"]
        ):
            raise Denied("thread_limit")
        if kind == "proactive":
            if not target or not target.author.isdigit() or target.author == uid:
                raise Denied("invalid_or_self_target")
            if self.db.execute(
                "select 1 from actions where uid=? and kind='proactive' and target=?",
                (uid, target.key),
            ).fetchone():
                raise Denied("duplicate_target")
            last = self.db.execute(
                "select max(created) from actions where uid=? and kind='proactive'",
                (uid,),
            ).fetchone()[0]
            if last and ts - last < c["proactive_gap"]:
                raise Denied("minimum_gap")
            if (
                self.db.execute(
                    "select count(*) from actions where uid=? and kind='proactive' and author=? and day=?",
                    (uid, target.author, day(ts)),
                ).fetchone()[0]
                >= c["author_daily"]
            ):
                raise Denied("author_limit")

    def reserve(self, c, kind, target, ts, key, thread="", recipient=""):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.permit(c, kind, target, ts, thread, recipient, key)
            self.db.execute(
                "insert into actions values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    str(c["bot_uid"]),
                    kind,
                    target.key if target else "",
                    target.author if target else recipient,
                    thread,
                    day(ts),
                    ts,
                    "pending",
                    "",
                    "",
                    "",
                    "",
                ),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def update(self, key, **values):
        assert set(values) <= {"status", "text", "remote_id", "url", "error"}
        self.db.execute(
            "update actions set " + ",".join(k + "=?" for k in values) + " where id=?",
            (*values.values(), key),
        )

    def summary(self):
        result = {
            "bound_uid": self.get("bound_uid"),
            "baseline_at": self.get("baseline_at"),
            "pause_reason": self.get("pause_reason"),
            "pause_file": (self.root / "PAUSE").exists(),
            "plans": {k: self.get("next_" + k) for k in ("dynamic", "proactive")},
            "actions": [
                dict(r)
                for r in self.db.execute(
                    "select kind,day,status,count(*) as count from actions group by kind,day,status"
                )
            ],
            "usage": dict(
                self.db.execute(
                    "select count(*) calls,sum(prompt) prompt_tokens,sum(completion) completion_tokens from usage"
                ).fetchone()
            ),
        }
        if self.db.execute("select 1 from sqlite_master where type='table' and name='unified_receipts'").fetchone():
            result["unified"] = {
                "decision_source": "maibot_unified_core",
                "pending_events": self.db.execute("select count(*) from unified_events where synced=0").fetchone()[0],
                "pending_receipts": self.db.execute("select count(*) from unified_receipts where revision>synced_revision").fetchone()[0],
                "last_sync": self.get("unified_last_sync"),
                "last_sync_error": self.get("unified_sync_error"),
                "retry_at": self.get("unified_sync_retry_at"),
                "calls": [dict(row) for row in self.db.execute(
                    "select day,status,count(*) as calls,sum(prompt) as prompt_tokens,sum(completion) as completion_tokens "
                    "from usage where model='maibot-unified' group by day,status")],
            }
        return result


def safe_text(text, limit=250):
    from .mentions import MentionText
    if isinstance(text, MentionText):
        if len(text) > limit:
            raise Denied("invalid_generated_text")
        return text.checked()
    text = str(text).strip()
    from .content_rules import blocked
    if blocked(text):raise Denied("fixed_topic_restriction")
    if (
        not text
        or len(text) > limit
        or re.search(r"https?://|www\.|[@＠]|SESSDATA|bili_jct|api[_ -]?key", text, re.I)
    ):
        raise Denied("invalid_generated_text")
    return text


class Sender:
    def __init__(self, store, config_path, api, now=time.time):
        self.s = store
        self.config_path = config_path
        self.api = api
        self.now = now

    def identity(self, c):
        try:
            account = self.api.identity()
        except PlatformError as e:
            self.s.put("pause_reason", "identity_platform_" + str(e.code))
            raise
        if str(account["mid"]) != str(c["bot_uid"]) or str(account["mid"]) != str(
            self.s.get("bound_uid")
        ):
            self.s.put("pause_reason", "uid_mismatch")
            raise Denied("uid_mismatch")
        return account

    def deliver(
        self,
        kind,
        target,
        key,
        generate,
        thread="",
        root="",
        parent="",
        recipient="",
        memory=None,
        media=None,
    ):
        if media and (kind!='dynamic' or len(media)!=1):raise Denied('invalid_dynamic_images')
        c = load_config(self.config_path,self.now())
        self.s.permit(c, kind, target, self.now(), thread, recipient, key)
        self.identity(c)
        reserved_at = self.now()
        self.s.reserve(c, kind, target, reserved_at, key, thread, recipient)
        try:
            text = safe_text(generate())
            from .mentions import verify_before_send
            text = verify_before_send(text, c, self.s, self.api, self.now(), kind)
        except InterestSkip:
            self.s.update(key, status="skipped", error="interest_or_evidence_skip")
            return {"status": "skipped", "reason": "interest_or_evidence_skip"}
        except Exception as e:
            self.s.update(key, status="failed", error="generation_" + type(e).__name__)
            return {"status": "failed"}
        if self.s.db.execute(
            "select 1 from actions where status in ('success','unknown','sending') and text=?",
            (text,),
        ).fetchone():
            self.s.update(key, status="failed", error="duplicate_text")
            return {"status": "skipped"}
        # Re-read switches after a slow model call, immediately before any write.
        c2 = load_config(self.config_path,self.now())
        if (
            c2 != c
            or c2["paused"]
            or (self.s.root / "PAUSE").exists()
            or self.s.get("pause_reason")
        ):
            self.s.update(key, status="failed", error="policy_changed")
            return {"status": "paused"}
        if kind in ("dynamic", "proactive") and not active(c2, self.now()):
            self.s.update(key, status="failed", error="outside_active_hours")
            return {"status": "skipped"}
        if day(self.now()) != day(reserved_at):
            self.s.update(key, status="failed", error="date_changed_during_generation")
            return {"status": "skipped"}
        self.identity(c2)
        # The gap is measured from the actual dispatch boundary, not generation start.
        self.s.db.execute("update actions set created=? where id=?", (self.now(), key))
        self.s.update(key, status="sending", text=text)
        try:
            result = self.api.send(
                kind, target, text, root=root, parent=parent, recipient=recipient, **({"media":media} if media else {})
            )
            remote = str(result.get("id") or "")
            if not remote:
                raise UnknownDelivery("missing_receipt")
            self.s.update(
                key, status="success", remote_id=remote, url=result.get("url", "")
            )
        except PlatformError as e:
            self.s.update(key, status="failed", error="platform_" + str(e.code))
            self.s.put("pause_reason", "platform_" + str(e.code))
            return {"status": "paused", "code": e.code}
        except Exception:
            self.s.update(key, status="unknown", error="no_reliable_receipt")
            if media:return {'status':'unknown','reason':'image_dynamic_no_reliable_receipt'}
            try:
                found = self.api.reconcile(
                    kind, target, text, root=root, since=reserved_at
                )
                if found:
                    self.s.update(
                        key,
                        status="success",
                        remote_id=str(found["id"]),
                        url=found.get("url", ""),
                    )
                    result = found
                else:
                    return {"status": "unknown"}
            except Exception:
                return {"status": "unknown"}
        # Delivery state is committed before optional memory work.
        try:
            if memory:
                memory(text)
            else:
                self.s.db.execute(
                    "insert or replace into memory values(?,?,?,?)",
                    (key, thread or (target.key if target else kind), text, self.now()),
                )
        except Exception:
            self.s.put("last_memory_error", "optional_memory_failed")
        return {"status": "success", **result}
