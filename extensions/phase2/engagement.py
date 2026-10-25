"""Bounded video engagement using the existing account, writer lock and ledger.

No independent model, retries, purchases, folder creation or unfollow/unlike.
Endpoints match bilibili-api-python 17.4.2 video/user/favorite_list source.
The caller holds Store.lock for the complete Engine lifetime.
"""

from __future__ import annotations

import json
import math
import time

from . import quota
from .core import Denied, PlatformError, active, day, machine_id


CAPS = {"like": 10, "favorite": 5, "follow": 20, "coin": 2}
CHOICE_KEYS = frozenset({"like", "favorite", "follow_author", "coin"})
READ_EVIDENCE = frozenset({"title_description", "title_description_subtitle_excerpt"})
RISK_CODES = {-101, -111, -412, -352, -509, 401, 403, 412, 429}


def policy(config):
    raw = config.get("engagement", {})
    if not isinstance(raw, dict) or set(raw) - {"enabled", "limits"}:
        raise Denied("invalid_engagement_policy")
    enabled = raw.get("enabled", False)
    limits = raw.get("limits", CAPS)
    if type(enabled) is not bool or not isinstance(limits, dict) or set(limits) != set(CAPS):
        raise Denied("invalid_engagement_policy")
    from .boost import TEMP_ENGAGEMENT
    ceiling=TEMP_ENGAGEMENT if config.get('_activity_boost_active') is True else CAPS
    if any(type(limits[k]) is not int or not 0 <= limits[k] <= ceiling[k] for k in CAPS):
        raise Denied("engagement_limit_exceeds_authorization")
    return {"enabled": enabled, "limits": dict(limits)}


def choices(decision):
    """Reject model-supplied targets, quantities or extra capability fields."""
    if not isinstance(decision, dict):
        raise Denied("invalid_engagement_decision")
    raw = decision.get("engagement")
    if raw is None:
        return []
    if not isinstance(raw, dict) or set(raw) != CHOICE_KEYS:
        raise Denied("invalid_engagement_decision")
    if any(type(raw[k]) is not bool for k in ("like", "favorite", "follow_author")):
        raise Denied("invalid_engagement_decision")
    if type(raw["coin"]) is not int or raw["coin"] not in (0, 1):
        raise Denied("invalid_engagement_coin_quantity")
    selected = [k for k in ("like", "favorite", "follow", "coin")
                if raw["follow_author" if k == "follow" else k]]
    score = decision.get("interest_score")
    if selected and (decision.get("interested") is not True or type(score) not in (int, float)
                     or not math.isfinite(score) or not 65 <= score <= 100):
        raise Denied("engagement_requires_actual_interest")
    return selected


def used(store, uid, now, c=None):
    # Every reserved attempt counts, including unknown and failures. A restart,
    # a policy change or public visibility cannot refund a possibly spent coin.
    return {kind: store.used(str(uid), kind, now, c) for kind in CAPS}


class EngagementExecutor:
    def __init__(self, store, config_loader, api, identity_guard, now=time.time):
        self.s, self.config, self.api = store, config_loader, api
        self.identity_guard, self.now = identity_guard, now
        self.s.db.execute("""CREATE TABLE IF NOT EXISTS engagement_actions(
            action_id TEXT PRIMARY KEY REFERENCES actions(id),
            event_id TEXT NOT NULL,aid TEXT NOT NULL,author TEXT NOT NULL,
            evidence TEXT NOT NULL,before_json TEXT NOT NULL,
            result_stage TEXT NOT NULL DEFAULT '')""")

    def _locked(self):
        if getattr(self.s, "_lock_held", False) is not True:
            raise Denied("engagement_single_writer_lock_required")

    def recover(self):
        self._locked()
        self.s.db.execute("UPDATE actions SET status='unknown',error='process_interrupted' "
                          "WHERE id IN (SELECT action_id FROM engagement_actions) AND status='sending'")
        self.s.db.execute("UPDATE actions SET status='failed',error='interrupted_before_dispatch' "
                          "WHERE id IN (SELECT action_id FROM engagement_actions) AND status='pending'")

    def _permit(self, config, kind):
        self._locked()
        p = policy(config)
        if not p["enabled"] or not p["limits"][kind]:
            raise Denied("engagement_disabled")
        if config.get("paused") or (self.s.root / "PAUSE").exists() or self.s.get("pause_reason"):
            raise Denied("paused")
        uid = str(config.get("bot_uid", ""))
        if not uid.isdigit() or uid != str(self.s.get("bound_uid", "")):
            raise Denied("account_not_bound")
        if config.get("writer_machine") != machine_id():
            raise Denied("wrong_writer_machine")
        if not self.s.get("baseline_at"):
            raise Denied("baseline_required")
        if not active(config, self.now()):
            raise Denied("outside_active_hours")
        return p

    def _read_event(self, config, target, event_id):
        if (target.kind != "video" or target.type != 1 or not str(target.oid).isdigit()
                or int(target.oid) <= 0 or not str(target.author).isdigit()
                or str(target.author) == str(config["bot_uid"]) or not target.text.strip()
                or target.evidence not in READ_EVIDENCE):
            raise Denied("engagement_requires_read_nonself_video")
        row = self.s.db.execute("SELECT payload FROM unified_events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            raise Denied("engagement_read_evidence_missing")
        event = json.loads(row[0])
        checks = {"platform": "bilibili", "bot_account_id": str(config["bot_uid"]),
                  "event_type": "content_read", "origin": "platform_content",
                  "conversation_id": target.key, "actor_id": str(target.author),
                  "source_id": str(target.source_id or target.key),
                  "material_evidence": target.evidence, "content": str(target.text)[:8000],
                  "sharing_scope": "public"}
        if any(event.get(key) != value for key, value in checks.items()):
            raise Denied("engagement_read_evidence_mismatch")
        occurred = event.get("occurred_at")
        if type(occurred) not in (int, float) or not math.isfinite(occurred) or not 0 <= self.now() - occurred <= 1800:
            raise Denied("engagement_read_evidence_stale")
        return event

    def _observe(self, kind, aid, author, uid):
        if kind == "follow":
            response = self.api.request("/x/space/wbi/acc/relation", self.api.signed({"mid": author}))
            relation = response.get("relation", {}) if isinstance(response, dict) else {}
            attribute = relation.get("attribute")
            if type(attribute) is not int or attribute not in (0, 1, 2, 6, 128):
                raise Denied("unverified_follow_state")
            if attribute == 128:
                raise Denied("blocked_author")
            return {"present": attribute in (1, 2, 6), "attribute": attribute}
        relation = self.api.request("/x/web-interface/archive/relation", {"aid": aid})
        if not isinstance(relation, dict):
            raise Denied("unverified_video_relation")
        if kind in ("like", "favorite"):
            state = relation.get(kind)
            if type(state) not in (bool, int) or state not in (0, 1):
                raise Denied("unverified_video_relation")
            result = {"present": bool(state)}
        else:
            count = relation.get("coin")
            if type(count) is not int or not 0 <= count <= 2:
                raise Denied("unverified_coin_state")
            result = {"present": count >= 1, "coin_count": count}
        if kind == "favorite" and not result["present"]:
            folders = self.api.request("/x/v3/fav/folder/created/list-all",
                                       {"up_mid": uid, "type": 2, "rid": aid, "web_location": "333.1387"})
            rows = folders.get("list") if isinstance(folders, dict) else None
            if not isinstance(rows, list) or len(rows) > 100:
                raise Denied("default_favorite_folder_unverified")
            matches = [f for f in rows if isinstance(f, dict) and f.get("title") == "默认收藏夹"
                       and str(f.get("mid")) == str(uid) and str(f.get("id", "")).isdigit()
                       and int(f["id"]) > 0]
            if len(matches) != 1:
                raise Denied("default_favorite_folder_unverified")
            result["folder_id"] = str(matches[0]["id"])
        return result

    def _dispatch(self, kind, aid, author, before):
        csrf = self.api.credentials.get("BILI_JCT")
        if not isinstance(csrf, str) or not csrf:
            raise Denied("engagement_csrf_missing")
        if kind == "like":
            path, data = "/x/web-interface/archive/like", {"aid": aid, "like": 1}
        elif kind == "coin":
            self.api.ensure_device()
            path, data = "/x/web-interface/coin/add", {"aid": aid, "multiply": 1, "select_like": 0}
        elif kind == "favorite":
            path, data = "/x/v3/fav/resource/deal", {"rid": aid, "type": 2,
                "add_media_ids": before["folder_id"], "del_media_ids": ""}
        else:
            path, data = "/x/relation/modify", {"fid": author, "act": 1, "re_src": 11}
        # Bili.request has no retry, rejects nonzero platform codes, and turns
        # missing/malformed write responses into UnknownDelivery.
        self.api.request(path, data={**data, "csrf": csrf})

    def _finish(self, key, status, error="", stage=""):
        row = self.s.db.execute("SELECT kind,target FROM actions WHERE id=?", (key,)).fetchone()
        remote = "engagement:" + row["kind"] + ":" + row["target"] if status == "success" else ""
        self.s.update(key, status=status, remote_id=remote, error=error)
        self.s.db.execute("UPDATE engagement_actions SET result_stage=? WHERE action_id=?", (stage, key))
        return {"action_id": key, "kind": row["kind"], "status": status, "reason": error, "result_stage": stage}

    def reconcile(self, key):
        """Read-only convergence: never sends, refunds or opens a retry window."""
        self._locked()
        row = self.s.db.execute("SELECT a.*,e.aid,e.before_json FROM actions a JOIN engagement_actions e "
                                "ON a.id=e.action_id WHERE a.id=?", (key,)).fetchone()
        if not row or row["status"] != "unknown":
            return {"status": row["status"] if row else "missing"}
        config = self.config()
        if str(config["bot_uid"]) != row["uid"] or str(self.s.get("bound_uid")) != row["uid"]:
            raise Denied("account_not_bound")
        self.identity_guard(config)
        try:
            current = self._observe(row["kind"], row["aid"], row["author"], row["uid"])
            before = json.loads(row["before_json"])
            achieved = (current.get("coin_count", 0) >= before.get("coin_count", 0) + 1
                        if row["kind"] == "coin" else current["present"])
            if achieved:
                return self._finish(key, "success", "state_observed_after_unknown", "account_state_reconciled")
        except Exception:
            pass
        return {"action_id": key, "status": "unknown", "reason": "no_conclusive_state"}

    def execute(self, target, decision, event_id, persona_version="", source_ids=()):
        selected = choices(decision)
        if not selected:
            return []
        config = self.config()
        if not policy(config)["enabled"]:
            return [{"status": "skipped", "reason": "engagement_disabled"}]
        event = self._read_event(config, target, event_id)
        if not str(persona_version) or not isinstance(source_ids, (list, tuple)) or len(source_ids) > 100:
            raise Denied("engagement_decision_provenance_required")
        return self._execute_selected(target,selected,event,event_id,persona_version,source_ids)

    def execute_command(self,target,kinds,event,authorization,persona_version):
        # Authority comes from the Host-owned queue, already revalidated by CommandExecutor.
        from .command_schema import validate
        from .content_rules import blocked
        spec=validate(authorization['spec'])
        if (not authorization.get('id') or any(k not in spec['kinds'] or k not in CAPS for k in kinds)
                or blocked(target.text) or blocked(spec['text'])):
            raise Denied('invalid_command_authorization')
        if spec['target_type']=='user':
            if kinds!=['follow'] or str(target.author)!=spec['target'] or target.kind!='user':raise Denied('command_target_mismatch')
        elif (target.kind!='video' or spec['target'] not in (target.source_id,'av'+str(target.oid))):
            raise Denied('command_target_mismatch')
        if str(target.author)==str(self.config()['bot_uid']):raise Denied('invalid_or_self_target')
        return self._execute_selected(target,kinds,event,event['event_id'],persona_version,[authorization['id']],authorization['id'])

    def _execute_selected(self,target,selected,event,event_id,persona_version,source_ids,authorization_id=None):
        from .content_rules import blocked
        if blocked(target.text):raise Denied('fixed_topic_restriction')
        config=self.config()
        results = []
        for kind in selected:
            uid, aid, author = str(config["bot_uid"]), str(target.oid), str(target.author)
            canonical = "author:" + author if kind == "follow" else "video:" + aid
            key = uid + ":engagement:" + kind + ":" + canonical
            row = self.s.db.execute("SELECT status FROM actions WHERE id=?", (key,)).fetchone()
            if row and authorization_id and kind!='coin' and row['status']=='success':
                # A new explicit request checks live state; a historical success is not current state.
                self.identity_guard(config)
                current=self._observe(kind,aid,author,uid)
                if current['present']:
                    results.append({'action_id':key,'kind':kind,'status':'skipped','reason':'already_present'})
                    continue
                # Only a distinct, authenticated request can re-add an externally removed item.
                # Unknown/failed prior dispatches never receive this path.
                key+=':command:'+authorization_id
                row=self.s.db.execute('SELECT status FROM actions WHERE id=?',(key,)).fetchone()
            if row:
                # Unknown requires an explicit read-only reconciliation call;
                # repeated model opportunities never retry a write implicitly.
                results.append({"action_id": key, "kind": kind, "status": row[0], "reason": "already_attempted"})
                continue
            try:
                limits = self._permit(config, kind)["limits"]
                if self.s.used(uid, kind, self.now(), config) >= limits[kind]:
                    raise Denied("engagement_daily_limit")
                self.identity_guard(config)
                before = self._observe(kind, aid, author, uid)
                if before["present"]:
                    results.append({"kind": kind, "status": "skipped", "reason": "already_present"})
                    continue
                if kind == "coin":
                    money = getattr(self.api, "nav", {}).get("money")
                    if type(money) not in (int, float) or not math.isfinite(money) or money < 1:
                        raise Denied("insufficient_or_unverified_coin_balance")
                self.s.db.execute("BEGIN IMMEDIATE")
                try:
                    if self.s.used(uid, kind, self.now(), config) >= limits[kind]:
                        raise Denied("engagement_daily_limit")
                    reserved = self.now()
                    self.s.db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (key, uid, kind, canonical, author, target.key, day(reserved), reserved,
                                       "pending", "", "", target.url, ""))
                    self.s.db.execute("INSERT INTO engagement_actions VALUES(?,?,?,?,?,?,?)",
                                      (key, event_id, aid, author, target.evidence, json.dumps(before), "reserved"))
                    self.s.db.execute("INSERT INTO action_context(action_id,event_json,persona_version,source_ids) VALUES(?,?,?,?)",
                                      (key, json.dumps(event, ensure_ascii=False), str(persona_version), json.dumps(list(source_ids))))
                    self.s.db.execute("COMMIT")
                except BaseException:
                    self.s.db.execute("ROLLBACK")
                    raise
                fresh = self.config()
                if fresh != config or day(self.now()) != day(reserved):
                    raise Denied("engagement_policy_or_day_changed")
                self._permit(fresh, kind)
                self.identity_guard(fresh)
                if self.config() != fresh or day(self.now()) != day(reserved):
                    raise Denied("engagement_policy_or_day_changed")
                self._permit(fresh, kind)
                if kind == "coin":
                    money = getattr(self.api, "nav", {}).get("money")
                    if type(money) not in (int, float) or not math.isfinite(money) or money < 1:
                        raise Denied("insufficient_or_unverified_coin_balance")
                self.s.update(key, status="sending")
                try:
                    self._dispatch(kind, aid, author, before)
                except PlatformError as exc:
                    detail=getattr(self.api,'last_platform_error',None)
                    if detail:self.s.put('platform_error:'+key,detail)
                    results.append(self._finish(key, "failed", "platform_" + str(exc.code), "platform_rejected"))
                    if exc.code in RISK_CODES:
                        self.s.put("pause_reason", "engagement_platform_" + str(exc.code))
                        break
                except Exception:
                    results.append(self._finish(key, "unknown", "no_reliable_receipt", "delivery_unknown"))
                    break
                else:
                    results.append(self._finish(key, "success", stage="platform_api_receipt"))
            except (Denied, PlatformError) as exc:
                reason = str(exc) if isinstance(exc, Denied) else "platform_" + str(exc.code)
                reserved_row = self.s.db.execute("SELECT status FROM actions WHERE id=?", (key,)).fetchone()
                if reserved_row and reserved_row[0] == "pending":
                    results.append(self._finish(key, "failed", reason, "not_dispatched"))
                else:
                    results.append({"kind": kind, "status": "skipped", "reason": reason})
                if isinstance(exc, PlatformError) and exc.code in RISK_CODES:
                    self.s.put("pause_reason", "engagement_platform_" + str(exc.code))
                    break
            except Exception:
                # A read-side or local error cannot cross the dispatch boundary.
                reserved_row = self.s.db.execute("SELECT status FROM actions WHERE id=?", (key,)).fetchone()
                if reserved_row and reserved_row[0] == "pending":
                    results.append(self._finish(key, "failed", "engagement_preflight_failed", "not_dispatched"))
                elif reserved_row and reserved_row[0] == "sending":
                    results.append(self._finish(key, "unknown", "receipt_persistence_failed", "delivery_unknown"))
                else:
                    results.append({"kind": kind, "status": "skipped", "reason": "engagement_preflight_failed"})
                break
        return results
