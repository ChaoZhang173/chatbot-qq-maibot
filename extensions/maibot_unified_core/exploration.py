"""Curiosity-driven exploration queue shared by Bilibili, Xiaohongshu and the public web.

Flow: an existing decision (Bili browse decision / Bili autonomy plan / XHS select plan) returns an optional public
question in the SAME model answer (no polling model call) -> propose() validates and enqueues -> an executor claims
the task (cooldown enforced) -> at most 1 search + 2 result reads -> report(). The web platform is executed by the core
itself through the shared public reader with its own trusted autonomous entry.

Budgets (all global, enforced here; the platform executors additionally keep their own stricter quotas):
  Policy-configured keyword opportunities, spacing and query reuse window (currently 10/day, 20 min, 12 h), <= 1 search + <= 2 reads per opportunity, a failed opportunity is never retried under a
  new task id, and a turn that itself came from a search result can never enqueue another search.
Search terms must be publicly safe (no private/personal material) and the originating event must be public.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import content_rules
from . import impressions
from . import v14_2
from . import web_access

TZ = ZoneInfo('Asia/Shanghai')
ORIGINS = ('bili_decide', 'bili_autonomy', 'xhs_plan', 'web_planner')
TRUSTED_SEEDS = ('bili_autonomy', 'xhs_plan', 'web_planner')   # built from public context by our own code
PLATFORMS = ('bilibili', 'xiaohongshu', 'web')
PRIVATE_QUERY = re.compile(r'https?://|www\.|[@＠]|\d{5,}|密码|密钥|Cookie|xsec|api[_ -]?key|手机号|QQ号|住址|私聊|群友|群里|群聊|主人|SESSDATA|token',
                           re.I)
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
CLAIM_EXPIRES = 24 * 3600      # a queued opportunity lapses after a day (covers the XHS interval and quiet hours); its
#                                created_at is never renewed, and the source/switches are revalidated when it is claimed
RUNNING_EXPIRES = 3600         # a claimed one that never reported lapses (counted as used, never retried)
LIMITS = {'searches': 1, 'reads': 2}


def norm(text):
    return ' '.join(str(text or '').casefold().split())


def _day_start(now):
    local = datetime.fromtimestamp(now, TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Exploration:
    def __init__(self, service):
        self.s = service

    @property
    def state(self):
        return self.s.state

    def cfg(self):
        return v14_2.section(self.s.policy, 'exploration')

    def now(self):
        return float(self.state.now())

    def enabled(self, platform=None):
        cfg = self.cfg()
        return cfg['enabled'] is True and (platform is None or platform in cfg['platforms'])

    # ---- enqueue -------------------------------------------------------------------------------
    def public_origin(self, origin_event_id, origin_kind=''):
        """The originating material must be a stored, active, public Bili/XHS event. A missing event is allowed ONLY for the
        explicitly trusted public seed/context kinds (the autonomy plan and the XHS select plan are built from public
        context by our own code), never for arbitrary model output such as a browse decision."""
        if not origin_event_id:
            return origin_kind in TRUSTED_SEEDS
        event = self.state.get_event(origin_event_id)
        if not event or not event['active']:
            return False
        from .daily_learning import is_private_event
        return event['platform'] in ('bilibili', 'xiaohongshu') and event.get('sharing_scope') == 'public' and not is_private_event(event)

    def search_derived_event(self, origin_event_id):
        """Recursion test from STORED source metadata, not from a caller-supplied string."""
        event = self.state.get_event(origin_event_id) if origin_event_id else None
        if not event:
            return False
        metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
        return bool(event.get('exploration_task') or metadata.get('exploration_task'))

    def turn_is_search_derived(self, platform):
        """While an opportunity of this platform is running, every decision it triggers belongs to the search result turn."""
        with self.state._lock:
            return self.state.db.execute("SELECT 1 FROM exploration_tasks WHERE platform=? AND status='running'", (platform,)).fetchone() is not None

    def propose(self, platform, purpose, query, *, origin_kind, origin_event_id='', exploration_task=''):
        """Validate and enqueue one curiosity opportunity. Returns {'accepted', 'reason', 'task_key'}."""
        def no(reason):
            return {'accepted': False, 'reason': reason}
        cfg = self.cfg()
        if not self.enabled(platform) or platform not in PLATFORMS:
            return no('exploration_disabled')
        if origin_kind not in ORIGINS:
            return no('origin_not_allowed')
        if exploration_task or self.search_derived_event(origin_event_id) or self.turn_is_search_derived(platform):
            return no('no_recursive_search')  # the turn came from a search result: never chain another search
        purpose, query = str(purpose or '').strip(), str(query or '').strip()
        limit = 20 if platform == 'xiaohongshu' else 40
        if not 1 <= len(purpose) <= 80 or not 1 <= len(query) <= limit or CONTROL.search(purpose + query):
            return no('invalid_text')
        if content_rules.blocked(purpose) or content_rules.blocked(query):
            return no('fixed_topic_restriction')
        if PRIVATE_QUERY.search(purpose) or PRIVATE_QUERY.search(query):
            return no('not_publicly_safe')
        if not self.public_origin(origin_event_id, origin_kind):
            return no('origin_not_public')
        moment = self.now()
        purpose_norm, query_norm = norm(purpose), norm(query)
        with self.state._transaction():
            rows = [dict(r) for r in self.state.db.execute(
                "SELECT * FROM exploration_tasks WHERE created_at>=? ORDER BY created_at", (min(moment - cfg['dedup_seconds'], _day_start(moment)),))]
            if any((r['purpose_norm'] == purpose_norm or norm(r['query']) == query_norm)
                   and moment - r['created_at'] < cfg['dedup_seconds'] for r in rows):
                return no('duplicate_purpose_or_query')
            today = [r for r in rows if r['created_at'] >= _day_start(moment)]
            if len(today) >= cfg['daily_opportunities']:
                return no('daily_opportunity_limit')
            recent = [r for r in rows if moment - r['created_at'] < cfg['cooldown_seconds']]
            if recent:
                return no('cooldown')
            key = 'ex:' + hashlib.sha256(json.dumps([platform, purpose_norm, query_norm, int(moment)]).encode()).hexdigest()[:24]
            self.state.db.execute(
                "INSERT INTO exploration_tasks(task_key,platform,purpose,query,purpose_norm,status,origin_event_id,origin_kind,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (key, platform, purpose, query, purpose_norm, 'queued', str(origin_event_id)[:200],
                                              origin_kind, moment))
        return {'accepted': True, 'reason': '', 'task_key': key}

    def propose_from(self, platform, curiosity, *, origin_kind, origin_event_id='', exploration_task=''):
        """Tolerant adapter for the optional `curiosity` object inside an existing decision answer. The decision may choose
        WHERE to look (bilibili / xiaohongshu / web); an unknown or disabled choice falls back to the deciding platform."""
        if not isinstance(curiosity, dict):
            return None
        chosen = curiosity.get('platform')
        if chosen in PLATFORMS and self.enabled(chosen):
            platform = chosen
        return self.propose(platform, curiosity.get('purpose'), curiosity.get('query'), origin_kind=origin_kind,
                            origin_event_id=origin_event_id, exploration_task=exploration_task)

    # ---- claim / report (executors) ----------------------------------------------------------------
    def expire(self):
        moment = self.now()
        with self.state._transaction():
            self.state.db.execute("UPDATE exploration_tasks SET status='expired',finished_at=? WHERE status='queued' AND created_at<?",
                                  (moment, moment - CLAIM_EXPIRES))
            self.state.db.execute("UPDATE exploration_tasks SET status='expired',finished_at=? WHERE status='running' AND started_at<?",
                                  (moment, moment - RUNNING_EXPIRES))

    def claim(self, platform):
        """Hand the oldest queued opportunity of this platform to its executor, respecting the global cooldown."""
        if not self.enabled(platform):
            return None
        self.expire()
        moment, cfg = self.now(), self.cfg()
        with self.state._transaction():
            last = self.state.db.execute("SELECT MAX(started_at) FROM exploration_tasks").fetchone()[0] or 0
            if moment - last < cfg['cooldown_seconds']:
                return None
            # The daily cap counts EXECUTIONS (started today), not only admissions, so a task queued before midnight cannot
            # push today's executions past the cap.
            started_today = self.state.db.execute("SELECT COUNT(*) FROM exploration_tasks WHERE started_at>=?",
                                                  (_day_start(moment),)).fetchone()[0]
            if started_today >= cfg['daily_opportunities']:
                return None
            row = self.state.db.execute("SELECT * FROM exploration_tasks WHERE platform=? AND status='queued' ORDER BY created_at LIMIT 1",
                                        (platform,)).fetchone()
            if not row:
                return None
            # Revalidate at claim time: the switches are still on, the source is still public/active, the text is still safe.
            if (not self.enabled(platform) or not self.public_origin(row['origin_event_id'], row['origin_kind'])
                    or self.search_derived_event(row['origin_event_id']) or PRIVATE_QUERY.search(row['query'] + row['purpose'])
                    or content_rules.blocked(row['query']) or content_rules.blocked(row['purpose'])):
                self.state.db.execute("UPDATE exploration_tasks SET status='rejected',finished_at=? WHERE task_key=?", (moment, row['task_key']))
                return None
            self.state.db.execute("UPDATE exploration_tasks SET status='running',started_at=? WHERE task_key=?", (moment, row['task_key']))
        return {'task_key': row['task_key'], 'query': row['query'], 'purpose': row['purpose'], 'limits': dict(LIMITS)}

    def report(self, platform, task_key, outcome, searches=0, reads=0, content_ids=()):
        """The executor's result. Counts above the per-opportunity bound mark the opportunity failed. No retry ever."""
        with self.state._transaction():
            row = self.state.db.execute("SELECT * FROM exploration_tasks WHERE task_key=? AND platform=?", (str(task_key), platform)).fetchone()
            if not row or row['status'] != 'running':
                return {'recorded': False, 'reason': 'not_running'}
            valid = (type(searches) is int and type(reads) is int and 0 <= searches <= LIMITS['searches']
                     and 0 <= reads <= LIMITS['reads'] and outcome in ('done', 'failed'))
            status = 'done' if valid and outcome == 'done' else 'failed'
            detail = {'content_ids': [str(x)[:200] for x in list(content_ids)[:LIMITS['reads']]], 'reported_outcome': outcome}
            self.state.db.execute("UPDATE exploration_tasks SET status=?,finished_at=?,searches=?,reads=?,detail=? WHERE task_key=?",
                                  (status, self.now(), min(max(int(searches or 0), 0), 9) if type(searches) is int else 9,
                                   min(max(int(reads or 0), 0), 9) if type(reads) is int else 9, json.dumps(detail), task_key))
        return {'recorded': True, 'status': status}

    def running(self, platform, task_key):
        row = self.state.db.execute("SELECT status FROM exploration_tasks WHERE task_key=? AND platform=?", (str(task_key), platform)).fetchone()
        return bool(row and row['status'] == 'running')

    # ---- web: executed by the core through the shared public reader ----------------------------------
    async def tick(self):
        if not self.enabled('web') or self.s.policy.get('web_enabled') is not True:
            return None
        task = self.claim('web')
        if not task:
            return None
        return await self.run_web(task)

    async def run_web(self, task):
        key, searches, reads, content_ids = task['task_key'], 0, 0, []
        try:
            searches = 1
            found = await web_access.autonomous(self.s, 'search', task['query'])
            if not found.get('success'):
                return {'task': key, **self.report('web', key, 'failed', searches, 0)}
            results = [r for r in (found['content'].get('results') or []) if isinstance(r, dict) and r.get('url')][:LIMITS['reads']]
            for result in results:
                reads += 1
                page = await web_access.autonomous(self.s, 'fetch', result['url'])
                if not page.get('success'):
                    continue
                event_id = self.ingest_web(task, result, page['content'])
                if event_id:
                    content_ids.append(event_id)
        except Exception:
            return {'task': key, **self.report('web', key, 'failed', searches, min(reads, LIMITS['reads']), content_ids)}
        if content_ids:
            # Short-term focus with its own original evidence time (72 h), only from the public material just read.
            try:
                self.state.set_temp('current_focus', ('想多了解：' + task['purpose'])[:80], content_ids[0],
                                    reason='curiosity_exploration:' + key, effect_key='explore-focus:' + key)
            except Exception:
                pass
        return {'task': key, **self.report('web', key, 'done' if content_ids else 'failed', searches, reads, content_ids)}

    def ingest_web(self, task, result, page):
        moment = self.now()
        url = str(page.get('url') or result.get('url'))
        text = (str(page.get('title') or result.get('title') or '') + '\n' + str(page.get('content') or ''))[:2000]
        native = 'web:' + hashlib.sha256((task['task_key'] + url).encode()).hexdigest()[:24]
        outcome = self.state.ingest({
            'event_id': native, 'platform': 'web', 'bot_account_id': 'public-web', 'conversation_id': 'web:public',
            'actor_id': 'web', 'event_type': 'content_read', 'origin': 'platform_content', 'content': text,
            'occurred_at': moment, 'source_id': url[:300], 'source_url': url, 'sharing_scope': 'public', 'is_private': False,
            'learning_allowed': not content_rules.blocked(text), 'material_evidence': 'page_text_excerpt',
            'metadata': {'material': 'page_text_excerpt', 'untrusted': True, 'exploration_task': task['task_key'],
                         'provenance': {'query': task['query'], 'url': url, 'fetched_at': page.get('fetched_at'),
                                        'search_index_time': result.get('search_index_time', ''),
                                        'truncated': bool(page.get('truncated'))}}})
        if impressions.v14_2.enabled(self.s.policy, 'impressions'):
            impressions.Ledger(self.state).record(
                platform='web', opportunity_id=task['task_key'], content_id=url[:300], target_type='content', target_ref=url[:300],
                direction='unknown', mode='autonomous', execution='read', action='read', occurred_at=moment,
                evidence_event_ids=[outcome['event_id']])
        return outcome['event_id']
