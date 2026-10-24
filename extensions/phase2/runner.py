"""Headless scheduler and CLI; every live operation goes through Sender."""

from __future__ import annotations
import argparse, json, os, re, time
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
from .core import (
    Store,
    Sender,
    Denied,
    PlatformError,
    TZ,
    active,
    day,
    future,
    load_config,
)
from .api import Bili
from .unified import UnifiedModel, Outbox, request as unified_request

STOP = re.compile(
    r"不要(?:再)?(?:回复|回我|评论|互动)|别(?:再)?(?:回|回复|评论)|停止(?:互动|回复)|stop\s+(?:reply|respond|comment)",
    re.I,
)


def interest(c, text):
    aliases = {
        "AI": ["人工智能", "机器学习", "LLM", "DeepSeek", "Claude"],
        "大模型": ["模型", "LLM", "DeepSeek", "Claude"],
        "甜点": ["蛋糕", "布丁", "甜品"],
        "美食": ["蛋糕", "布丁", "烹饪"],
    }
    words = list(c["interests"])
    for word in c["interests"]:
        words.extend(aliases.get(word, []))
    return any(w.casefold() in text.casefold() for w in words)


class Engine:
    def __init__(self, config, state, private, api=None, now=time.time):
        self.path = Path(config)
        self.private = Path(private)
        self.s = Store(state)
        self.now = now
        self.api = api or Bili(self.private / "bili-credentials.json")
        self.sender = Sender(self.s, self.path, self.api, now)
        self.unified_request = unified_request
        self.outbox = Outbox(self.s, lambda payload: self.unified_request(payload), now)
        self.api.screen_content=self.screen_content

    def bounded_core(self,payload):
        c=self.config();now=self.now()
        if self.s.db.execute('SELECT count(*) FROM usage WHERE day=?',(day(now),)).fetchone()[0]>=c['model_daily']:
            raise Denied('model_daily_limit')
        cur=self.s.db.execute('INSERT INTO usage(created,day,status,model) VALUES(?,?,?,?)',
            (now,day(now),'pending','maibot-unified'))
        try:
            result=self.unified_request(payload)
            self.s.db.execute("UPDATE usage SET status='success',seconds=? WHERE id=?",(self.now()-now,cur.lastrowid))
            return result
        except Exception:
            self.s.db.execute("UPDATE usage SET status='failed_core' WHERE id=?",(cur.lastrowid,));raise

    def screen_content(self,text):
        if not self.config().get('content_policy_enabled'):return {'allowed':True}
        from .content_rules import blocked
        from hashlib import sha256
        if blocked(text):return {'allowed':False}
        key='screen_cache:'+sha256(text.encode()).hexdigest()
        old=self.s.get(key)
        if old and self.now()-old['at']<86400:return old['result']
        result=self.bounded_core({'op':'content_screen','material':text[:10000]})
        self.s.put(key,{'at':self.now(),'result':result});return result

    def config(self):
        return load_config(self.path,self.now())

    def live_chat(self):
        from .live import LiveChat
        if not hasattr(self,'_live_chat'):self._live_chat=LiveChat(self)
        return self._live_chat

    def engagement(self):
        from .engagement import EngagementExecutor
        if not hasattr(self, "_engagement_executor"):
            self._engagement_executor = EngagementExecutor(
                self.s, self.config, self.api, self.sender.identity, self.now)
        return self._engagement_executor

    def recover(self):
        self.s.recover()
        self.engagement().recover()

    def bind(self):
        c = self.config()
        account = json.loads((self.private / "account.json").read_text())
        live = self.api.identity()
        selected = str(account.get("user_selected_uid", ""))
        if (
            not selected
            or selected != str(live["mid"])
            or str(c["bot_uid"]) != selected
        ):
            raise Denied("user_account_confirmation_required")
        previous = self.s.get("bound_uid")
        if previous and previous != selected:
            raise Denied("state_bound_to_different_uid")
        self.s.put("bound_uid", selected)
        self.s.put("account_name", live["name"])
        self.s.put("verified_at", self.now())
        return live

    def baseline(self):
        self.bind()
        c = self.config()
        if self.s.get("baseline_at"):
            return {"status": "existing_baseline_preserved"}
        # Establish baseline only when a complete bounded read succeeds.
        stamp = self.now()
        messages = self.api.notifications()
        if c["owner_uid"]:
            messages += self.api.private_messages(c["owner_uid"])
        with self.s.db:
            for m in messages:
                self.s.db.execute(
                    "insert or ignore into seen values(?,?,?)",
                    (m["id"], m["time"], "baseline"),
                )
            self.s.put("baseline_at", stamp)
        return {"status": "baseline_saved", "count": len(messages), "at": stamp}

    def model(self, event=None):
        from .mentions import registry
        config = self.config()
        candidates = registry(self.s, config, self.api, self.now())
        return UnifiedModel(self.s, config, event,
                            transport=lambda payload: self.unified_request(payload), mention_candidates=candidates)

    def event(self, kind, key, content, target=None, message=None, stream_id=""):
        c = self.config()
        m = message or {}
        incoming = bool(message)
        source_id = str(m.get("source") or m.get("id") or
                        (target.source_id or target.key if target else key))
        event_type = "message_received" if incoming else "content_read" if target else "opportunity"
        event = {
            "schema_version": 1,
            "event_id": f"bilibili:{c['bot_uid']}:{event_type}:{source_id}",
            "platform": "bilibili", "bot_account_id": str(c["bot_uid"]),
            "conversation_id": str(m.get("thread") or (target.key if target else "self:" + c["bot_uid"])),
            "thread_id": str(m.get("thread", "")),
            "actor_id": str(m.get("uid") or (target.author if target else c["bot_uid"])),
            "origin": "external_user" if incoming else "platform_content" if target else "system_task",
            "event_type": event_type, "content": str(content)[:8000],
            "source_id": source_id, "source_url": target.url if target else "",
            "sharing_scope": "conversation" if kind == "dm" else "public",
            "learning_allowed": incoming or bool(target),
            "occurred_at": float(m.get("time") or self.now()), "received_at": self.now(),
            "action_id": key, "correlation_id": key,
            "material_evidence": target.evidence if target else "text",
            "attachment_refs": [f"bilibili-image:{source_id}:{i}"
                                for i in range(min(2, len(target.images or [])))] if target else [],
        }
        if stream_id:
            event["source_conversation_id"] = str(stream_id)
        if target:
            event["target"] = {"kind": target.kind, "key": target.key, "oid": target.oid,
                               "type": target.type, "author": target.author}
        return event

    def sync(self):
        try:
            return self.outbox.flush()
        except Exception:
            # Synchronization is separate from execution; never turn a committed
            # success into a caller-visible failure that might encourage resend.
            self.s.put("unified_sync_error", "bounded_sync_failure")
            return {"pending": True}

    def history(self, thread=None):
        if thread:
            return [
                r[0]
                for r in self.s.db.execute(
                    "select text from memory where thread=? order by created desc limit 8",
                    (thread,),
                )
            ]
        return [
            r[0]
            for r in self.s.db.execute(
                "select text from actions where kind!='dm' and status='success' order by created desc limit 10"
            )
        ]

    def dynamic(self, slot=None):
        c = self.config()
        key = f"{c['bot_uid']}:dynamic:{day(self.now())}" if slot is None else f"{c['bot_uid']}:dynamic:boost:{slot}"
        material = "今日可以分享一个轻松的小想法。不要宣称真实经历了未发生的事情。"
        event = self.event("dynamic", key, material)
        self.outbox.event(event)
        result = self.sender.deliver(
            "dynamic",
            None,
            key,
            lambda: self.model().generate(
                "dynamic",
                material,
                history=self.history(),
                event=event,
            ),
        )
        self.sync()
        return result

    def proactive(self, target=None):
        c = self.config()
        cycle = self.s.get("browse_cycle", 0)
        self.s.put("browse_cycle", cycle + 1)
        targets = [target] if target else self.api.candidates(c, cycle)
        reasons = []
        for target in targets[: c["max_candidates"]]:
            if not target:
                reasons.append("missing_target")
                continue
            key = f"{c['bot_uid']}:proactive:{target.key}"
            event = self.event("proactive", key, target.text, target)
            self.outbox.event(event)
            try:
                self.s.permit(c, "proactive", target, self.now(), key=key)
                if target.kind == "video":
                    target = self.api.video(target.oid, with_subtitle=True)
                    event = self.event("proactive", key, target.text, target)
                    # Distinguish actually obtained richer material from the
                    # earlier metadata-only observation of this same source.
                    event["event_id"] += ":" + target.evidence
                    self.outbox.event(event)
                model = self.model()
                result = self.sender.deliver(
                    "proactive",
                    target,
                    key,
                    lambda: model.generate(
                        "proactive",
                        {"text": target.text, "evidence": target.evidence},
                        target.images,
                        history=self.history(),
                        event=event,
                    ),
                )
                if self.config().get("engagement", {}).get("enabled") and model.last_decision:
                    try:
                        result["engagement"] = self.engagement().execute(
                            target, model.last_decision, event["event_id"],
                            persona_version=model.last_persona_version, source_ids=model.last_source_ids)
                    except Denied as exc:
                        result["engagement"] = [{"status": "skipped", "reason": str(exc)}]
                    except Exception:
                        # Optional engagement cannot hide a committed comment
                        # receipt or cause another candidate/comment attempt.
                        result["engagement"] = [{"status": "failed", "reason": "engagement_processing_error"}]
                self.sync()
                if result["status"] in ("success", "unknown", "paused"):
                    return result
                reasons.append(result["status"])
            except Denied as e:
                reasons.append(str(e))
        self.sync()
        return {"status": "skipped", "reasons": reasons}

    def replies(self):
        c = self.config()
        messages = []
        if c["enabled"]["reply"]:
            # Reply and comment-mention feeds share native reply IDs, baseline,
            # seen rows, the existing reply quota and this single Sender path.
            messages += self.api.notifications()
        if c["enabled"]["dm"] and c["owner_uid"]:
            private = self.api.private_messages(c["owner_uid"])
            if self.s.get("dm_baseline_owner") != str(c["owner_uid"]):
                for m in private:
                    self.s.db.execute(
                        "insert or ignore into seen values(?,?,?)",
                        (m["id"], m["time"], "dm_baseline"),
                    )
                self.s.put("dm_baseline_owner", str(c["owner_uid"]))
                self.s.put("dm_baseline_at", self.now())
            else:
                messages += private
        results = []
        for m in sorted(messages, key=lambda x: x["time"]):
            if self.s.db.execute(
                "select 1 from seen where id=?", (m["id"],)
            ).fetchone():
                continue
            if (
                not m["time"]
                or m["time"] <= self.s.get("baseline_at", self.now())
                or m["uid"] == str(c["bot_uid"])
            ):
                self.s.db.execute(
                    "insert or ignore into seen values(?,?,?)",
                    (m["id"], m["time"], "old_or_self"),
                )
                continue
            kind = "dm" if m["id"].startswith("dm:") else "reply"
            key = f"{c['bot_uid']}:{m['id']}"
            event = self.event(kind, key, m["text"], m.get("target"), m)
            self.outbox.event(event)
            if STOP.search(m["text"]):
                self.s.stop_thread(m["thread"])
                self.s.db.execute(
                    "insert or ignore into seen values(?,?,?)",
                    (m["id"], m["time"], "stop_requested"),
                )
                continue
            if kind == "dm" and m["time"] <= self.s.get("dm_baseline_at", self.now()):
                self.s.db.execute(
                    "insert or ignore into seen values(?,?,?)",
                    (m["id"], m["time"], "dm_old"),
                )
                continue
            try:
                self.s.permit(
                    c, kind, m.get("target"), self.now(), m["thread"], m["uid"], key
                )
            except Denied as e:
                # No backlog catch-up on later days when quota was exhausted.
                self.s.db.execute(
                    "insert or ignore into seen values(?,?,?)",
                    (m["id"], m["time"], "skip_" + str(e)),
                )
                continue
            self.s.db.execute(
                "insert or ignore into seen values(?,?,?)",
                (m["id"], m["time"], "processing"),
            )
            result = self.sender.deliver(
                kind,
                m.get("target"),
                key,
                lambda: self.model().generate(
                    kind, m["text"], history=self.history(m["thread"]), event=event
                ),
                thread=m["thread"],
                root=m.get("root", ""),
                parent=m.get("source", ""),
                recipient=m["uid"],
                memory=lambda text: self.s.db.execute(
                    "insert or replace into memory values(?,?,?,?)",
                    (
                        key,
                        m["thread"],
                        json.dumps(
                            {"incoming": m["text"], "reply": text}, ensure_ascii=False
                        ),
                        self.now(),
                    ),
                ),
            )
            self.s.db.execute(
                "update seen set status=? where id=?", (result["status"], m["id"])
            )
            results.append(result)
            self.sync()
            if len(results) >= 3 or result["status"] == "paused":
                break
        self.sync()
        return results

    def initialize_plans(self):
        c = self.config()
        now = self.now()
        for kind in ("dynamic", "proactive"):
            plan = self.s.get("next_" + kind)
            if not plan or plan <= now:
                self.s.put("next_" + kind, future(c, now, kind))

    def tick(self):
        c = self.config()
        now = self.now()
        self.s.maintain(now)
        self.sync()
        if (
            c["paused"]
            or (self.s.root / "PAUSE").exists()
            or self.s.get("pause_reason")
        ):
            return {"status": "paused"}
        self.sender.identity(c)
        if not self.s.get("baseline_at"):
            raise Denied("baseline_required")
        results = {"reply": self.replies()}
        if c.get('autonomy_enabled'):
            from .autonomy import Autonomy
            if not hasattr(self,'_autonomy'):self._autonomy=Autonomy(self)
            results['autonomy']=self._autonomy.tick()
            self.s.put('last_tick',{'at':now,'result':results})
            return results
        from . import boost
        if active(c,now) and boost.active(c,now):
            results['boost']=boost.tick(self,c,now)
            self.s.put('last_tick',{'at':now,'result':results})
            return results
        if active(c, now):
            for kind in ("proactive", "dynamic"):
                due = self.s.get("next_" + kind)
                if due and due <= now:
                    # Advance persistently BEFORE execution; a crash never replays the slot.
                    self.s.put(
                        "next_" + kind,
                        future(
                            c,
                            now
                            if kind == "proactive"
                            else datetime.fromtimestamp(now, TZ)
                            .replace(hour=23, minute=0, second=0)
                            .timestamp(),
                            kind,
                        ),
                    )
                    if c["enabled"][kind]:
                        try:
                            results[kind] = getattr(self, kind)()
                        except Denied as e:
                            results[kind] = {"status": "skipped", "reason": str(e)}
        else:
            for kind in ("dynamic", "proactive"):
                if self.s.get("next_" + kind, 0) <= now:
                    self.s.put("next_" + kind, future(c, now, kind))
        self.s.put("last_tick", {"at": now, "result": results})
        return results

    def run(self, bridge_only=False):
        from .bridge import Bridge
        from .commands import CommandExecutor
        from .live import LiveChat

        with self.s.lock():
            self.recover()
            self.bind()
            if not bridge_only:
                self.initialize_plans()
            self.s.put("service_started", self.now())
            bridge = Bridge(self)
            commands=CommandExecutor(self)
            live=self.live_chat();next_live=0
            next_tick = 0
            while True:
                try:
                    commands.process_one()
                    if not bridge_only and time.monotonic()>=next_live:
                        live_result=live.tick();self.s.put('last_live',{'at':self.now(),'result':live_result})
                        if live_result.get('status') not in ('waiting','no_new_chat','disabled','message_gap'):
                            print(json.dumps({'event':'live','at':self.now(),'result':live_result},ensure_ascii=False),flush=True)
                        next_live=time.monotonic()+15
                    bridge.process_one()
                    self.sync()
                    if bridge_only or time.monotonic() < next_tick:
                        time.sleep(1)
                        continue
                    result = self.tick()
                    self.s.put("last_error", None)
                    if result != {"status": "paused"}:
                        print(
                            json.dumps(
                                {"event": "tick", "at": self.now(), "result": result},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    bridge.export(force=True)
                    from .boost import poll_seconds
                    next_tick = time.monotonic() + poll_seconds(self.config(),self.now())
                    delay = 1
                except PlatformError as e:
                    self.s.put("pause_reason", "platform_" + str(e.code))
                    print(json.dumps({"event": "paused", "code": e.code}), flush=True)
                    delay = 60
                except Exception as e:
                    name = type(e).__name__
                    if self.s.get("last_error") != name:
                        print(
                            json.dumps({"event": "bounded_error", "type": name}),
                            flush=True,
                        )
                    self.s.put("last_error", name)
                    delay = 300
                time.sleep(delay)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--private", required=True)
    p.add_argument(
        "command",
        choices=[
            "status",
            "bind",
            "baseline",
            "run",
            "bridge-run",
            "pause",
            "resume",
            "once",
            "inspect",
            "reconcile",
        ],
    )
    p.add_argument("--action", choices=["dynamic", "proactive", "reply"])
    p.add_argument("--target")
    a = p.parse_args()
    if a.command == "status":
        print(json.dumps(Store(a.state).summary(), ensure_ascii=False, indent=2))
        return
    if a.command == "pause":
        Path(a.state).mkdir(mode=0o700, exist_ok=True, parents=True)
        (Path(a.state) / "PAUSE").touch(mode=0o600)
        print("ALL_OUTBOUND_PAUSED")
        return
    e = Engine(a.config, a.state, a.private)
    if a.command in ("run", "bridge-run"):
        e.run(bridge_only=a.command == "bridge-run")
        return
    with e.s.lock():
        if a.command == "resume":
            e.bind()
            e.s.put("pause_reason", None)
            (e.s.root / "PAUSE").unlink(missing_ok=True)
            e.initialize_plans()
            print("PAUSE_CLEARED_CONFIG_SWITCHES_UNCHANGED")
            return
        if a.command in ("baseline", "bind"):
            result = getattr(e, a.command)()
        elif a.command == "inspect":
            e.bind()
            c = e.config()
            targets = e.api.candidates(c, e.s.get("browse_cycle", 0))
            result = [
                asdict(t)
                | {"canonical_target": t.key, "interest_decision": "unified_core_required"}
                for t in targets
            ]
        elif a.command == "reconcile":
            e.bind()
            result = []
            # Unknown writes are NEVER resent. Automatic reconciliation happened once at send time.
            for r in e.s.db.execute(
                "select id,kind,target,created,status from actions where status='unknown'"
            ):
                result.append(dict(r))
        else:
            if not a.action:
                p.error("--action is required for once")
            e.bind()
            e.recover()
            target = None
            if a.target:
                if a.action != "proactive":
                    p.error("--target is only for proactive")
                target = (
                    e.api.video(a.target)
                    if a.target.startswith("BV")
                    else e.api.dynamic(a.target)
                )
                if not target:
                    raise Denied("invalid_target")
            result = (
                e.proactive(target)
                if a.action == "proactive"
                else e.dynamic()
                if a.action == "dynamic"
                else e.replies()
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
