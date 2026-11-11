"""Beijing-03:00 daily batch learning. Crash-safe, budgeted, fair across scopes.

Durable state machine per batch (table daily_batches):
  pending --lease--> running --(model output validated and PERSISTED)--> running --(every effect committed)--> completed | no_change
                                                                                                               \\--> failed
Crash safety:
  * the validated model output is saved before any memory write or persona commit;
  * every effect has a stable identity (memory external_id, per-event learning status, persona effect_key written in
    the SAME transaction as the persona change) so a replay after a crash can never re-apply a stale value;
  * unfinished batches of ANY earlier window day are recovered and adopted (with their saved output, without a new
    model call); retries are charged to the real current quota day;
  * transient model errors retry with an interval, bounded by 2 retries per batch AND per day, every request is
    reserved first (failed calls count); a batch is "completed" only after durable output handling;
  * a night with nothing to do is a successful no-change day; deferred material stays pending but is never re-offered
    in a hot loop (explicit outcome per event, durable deferral otherwise).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import content_rules
from . import persona as character
from . import impressions
from . import v14_2
from .store import abstract_topic_summary

TZ = ZoneInfo('Asia/Shanghai')
LEASE_SECONDS = 600
RETRY_BACKOFF = (600, 1800)
DEFER_SECONDS = 86400
PUBLIC_TOPICS = {'AI', '游戏', '美食'}
PROMPT_CHAR_LIMIT = 16000
EVENT_FAIL_LIMIT = 2
PRIVATE_SCOPES = ('private', 'conversation', 'local', 'none')


TRIVIAL = re.compile(r'(?:哈+|呵+|嘿+|草+|6{2,}|好+的?|嗯+|哦+|噢+|行+|ok(?:ay)?|收到|谢谢|感谢|早安?|晚安|午安|在吗|在不在|'
                     r'\[链接\]|\[图片\]|\[表情\]|[\U0001F300-\U0001FAFF☀-➿]+)+', re.I)


def is_trivial(text):
    """Local, zero-token chit-chat filter: too short, only laughter/acks/greetings/emoji/link placeholders, or one repeated glyph."""
    squeezed = re.sub(r'[\s，。！？!?,.、~～…:：;；\-—]+', '', str(text))
    if len(squeezed) < 4 or TRIVIAL.fullmatch(squeezed):
        return True
    return len(squeezed) >= 3 and len(set(squeezed)) <= 2


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def window_day(now, hour):
    """The learning window day: the day whose `hour`:00 Beijing time most recently passed."""
    return datetime.fromtimestamp(float(now) - hour * 3600, TZ).date().isoformat()


def is_private_event(event):
    metadata = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
    return bool(event.get('is_private') or metadata.get('is_private') or event.get('conversation_type') in ('private', 'dm')
                or event.get('sharing_scope') in PRIVATE_SCOPES
                or str(event.get('conversation_id', '')).startswith(('dm:', 'private:')))


def scope_of(event):
    """Explicit privacy / conversation scope is checked FIRST for every platform; a platform name is no proof of public."""
    if is_private_event(event):
        return 'private:%s:%s' % (event['platform'], event['conversation_id'])
    if event['platform'] == 'qq':
        return 'qq:' + event['conversation_id']
    if event.get('sharing_scope') == 'public':
        return event['platform'] + ':public'
    return event['platform'] + ':' + event['conversation_id']


class DailyLearning:
    def __init__(self, service):
        self.s = service
        self.lock = asyncio.Lock()

    @property
    def state(self):
        return self.s.state

    def cfg(self):
        return v14_2.section(self.s.policy, 'daily_learning')

    def enabled(self):
        return self.cfg()['enabled'] is True

    def now(self):
        return float(self.state.now())

    # ---- storage helpers ----------------------------------------------------------------------
    def batches(self, day=None, statuses=None):
        sql, args, where = 'SELECT * FROM daily_batches', [], []
        if day is not None:
            where.append('day=?')
            args.append(day)
        if statuses:
            where.append('status IN (%s)' % ','.join('?' for _ in statuses))
            args += list(statuses)
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        with self.state._lock:
            return [dict(r) for r in self.state.db.execute(sql + ' ORDER BY created_at,batch_key', args)]

    def batch(self, key):
        with self.state._lock:
            return dict(self.state.db.execute('SELECT * FROM daily_batches WHERE batch_key=?', (key,)).fetchone())

    def update(self, key, **fields):
        fields['updated_at'] = self.now()
        with self.state._lock:
            self.state.db.execute('UPDATE daily_batches SET ' + ','.join(k + '=?' for k in fields) + ' WHERE batch_key=?',
                                  [*fields.values(), key])

    def reclaim(self):
        """Expired leases of ANY day go back to pending; saved output and commit list are kept."""
        with self.state._transaction():
            # (legacy rows written by an earlier build as 'deferred' are recovered the same way, next quota day)
            self.state.db.execute("UPDATE daily_batches SET status='pending',next_at=? WHERE status='deferred'",
                                  (self.next_quota_day_start(),))
            return self.state.db.execute(
                "UPDATE daily_batches SET status='pending',lease_until=0 WHERE status='running' AND lease_until<?",
                (self.now(),)).rowcount

    def next_ready(self):
        moment = self.now()
        for batch in self.batches(statuses=('pending',)):
            if batch['next_at'] <= moment:
                return batch
        return None

    # ---- eligibility (checked before model input AND again before commit, also for resumed output) ----
    def eligible(self, event):
        if (not event or not event.get('active') or not event.get('learning_allowed')
                or event.get('origin') not in ('external_user', 'platform_content')):
            return False
        if event.get('event_type') == 'device_voice_turn' or event.get('platform') == 'passport':
            if not self.s.voice.learning_route(event):
                return False
        if event['platform'] == 'qq' and event['conversation_id'] not in self.s.policy.get('allowed_qq_sessions', []):
            return False
        return not content_rules.blocked(event.get('content', ''))

    def recent_items(self, platform, conversation_id, hours=24):
        """Feed recent learned summaries back into conversation without another model call."""
        since = self.now() - hours * 3600
        with self.state._lock:
            batches = self.state.db.execute(
                "SELECT output,updated_at FROM daily_batches WHERE kind='events' AND output!='' "
                "AND updated_at>=? ORDER BY updated_at DESC", (since,)).fetchall()
        result, seen = [], set()
        for batch in batches:
            for item in json.loads(batch['output']).get('items', []):
                eid = item.get('event_id')
                if eid in seen or not item.get('remember') or not item.get('summary'):
                    continue
                event = self.state.get_event(eid)
                if not self.eligible(event):
                    continue
                public_web = (event.get('platform') == 'web' and event.get('sharing_scope') == 'public'
                              and not is_private_event(event))
                if not public_web and not self.state.allowed(event, platform, conversation_id, 'internal'):
                    continue
                seen.add(eid)
                result.append({'event_id': eid, 'summary': item['summary'],
                               'category': item.get('category'), 'source_platform': event['platform'],
                               'learned_at': batch['updated_at'], 'occurred_at': event.get('occurred_at')})
        return result

    def deferred_until(self, event_id):
        record = self.state.get('daily_defer:' + event_id)
        return float(record['not_before']) if isinstance(record, dict) else 0.0

    # ---- planning -----------------------------------------------------------------------------
    def open_manifest_ids(self):
        ids = set()
        for batch in self.batches(statuses=('pending', 'running')):
            if batch['kind'] == 'events':
                ids.update(x for x in json.loads(batch['manifest']) if isinstance(x, str))
        return ids

    PENDING_SQL = ("SELECT e.event_id FROM events e LEFT JOIN learning l ON e.event_id=l.event_id "
                   "WHERE e.learning_allowed=1 AND e.active=1 AND e.origin IN ('external_user','platform_content') "
                   "AND (l.event_id IS NULL OR l.status='deferred') AND e.received_at%s? ORDER BY e.received_at DESC,e.event_id LIMIT ?")

    def activation_at(self):
        value = self.state.get('daily_activation_at')
        return float(value) if isinstance(value, (int, float)) else None

    def activate(self):
        """Persist the activation watermark ONCE, the first time the selective mode is really enabled. A restart never
        resets it, and nothing received before it (existing memories, person profiles and raw events stay untouched)
        ever becomes learning material or a retroactive 'fresh contact'."""
        if self.cfg()['enabled'] and self.activation_at() is None:
            self.state.put('daily_activation_at', self.now())
        return self.activation_at()

    def fresh_since(self):
        """Lower bound of candidate material = max(activation watermark, now - fresh_hours). Everything received before the
        moment the selective mode was first enabled is legacy history and is never a candidate, even when it is only hours
        old. Before activation nothing qualifies."""
        activation = self.activation_at()
        if activation is None:
            return self.now() + 1
        return max(activation, self.now() - self.cfg()['fresh_hours'] * 3600)

    def _eligible_pending(self, comparison, limit):
        with self.state._lock:
            ids = [r[0] for r in self.state.db.execute(self.PENDING_SQL % comparison, (self.fresh_since(), limit))]
        busy, moment, events = self.open_manifest_ids(), self.now(), []
        for eid in ids:
            if eid in busy or self.deferred_until(eid) > moment:
                continue
            event = self.state.get_event(eid)
            if self.eligible(event):
                events.append(event)
        return events

    def interest_terms(self):
        dynamic = (self.state.persona_view() or {}).get('dynamic', {})
        terms = []
        for field in ('interests', 'gaming_preferences', 'anime_preferences', 'food_preferences', 'technology_preferences',
                      'current_focus', 'current_wishes'):
            value = dynamic.get(field)
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, str) and 2 <= len(item.strip()) <= 20:
                    terms.append(item.strip().casefold())
        return terms

    def preselect(self, events):
        """LOCAL, zero-token candidate choice within ONE scope: drop trivial/duplicate text (deterministic content
        hash), rank by match with current interests, platform material and substance, then recency."""
        terms, seen, ranked = self.interest_terms(), set(), []
        for event in events:                       # events arrive newest first
            text = ' '.join(str(event.get('content', '')).split())
            if is_trivial(text):
                continue
            fingerprint = hashlib.sha256(text.casefold().encode()).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            lowered = text.casefold()
            meta = event.get('metadata') if isinstance(event.get('metadata'), dict) else {}
            # v41: material from autonomous curiosity exploration (exploration.ingest_web stores metadata.exploration_task)
            is_curiosity = bool(event.get('exploration_task') or meta.get('exploration_task') or meta.get('curiosity')
                                or meta.get('source_kind') in ('curiosity', 'search', 'exploration'))
            score = (4 if is_curiosity else 0) + (3 if any(t in lowered for t in terms) else 0) \
                + (2 if event.get('origin') == 'platform_content' else 0) + (1 if len(text) >= 30 else 0)
            ranked.append((-score, -float(event['received_at']), event['event_id'], event))
        ranked.sort(key=lambda row: row[:3])
        return [row[3] for row in ranked[:self.cfg()['scope_cap']]]

    def fresh_by_scope(self):
        """{scope: preselected events} from the FRESH window only, bounded, independent of any old backlog."""
        by_scope, chosen = {}, {}
        for event in self._eligible_pending('>=', 1500):
            by_scope.setdefault(scope_of(event), []).append(event)
        for scope, events in by_scope.items():
            selected = self.preselect(events)
            if selected:
                chosen[scope] = selected
        return chosen

    COUNT_SQL = ("SELECT COUNT(*) FROM events e LEFT JOIN learning l ON e.event_id=l.event_id WHERE e.learning_allowed=1 AND e.active=1 "
                 "AND e.origin IN ('external_user','platform_content') AND (l.event_id IS NULL OR l.status='deferred') AND e.received_at%s?")

    def historical_raw_count(self):
        """Raw events older than the sampling window. They are HISTORY: retained, never replayed, never counted as a learning
        debt, and never reported as learned or failed."""
        with self.state._lock:
            return self.state.db.execute(self.COUNT_SQL % '<', (self.fresh_since(),)).fetchone()[0]

    def recent_unselected_count(self):
        """Recent (within the window) raw events that no batch selected - simply not sampled today."""
        with self.state._lock:
            return self.state.db.execute(self.COUNT_SQL % '>=', (self.fresh_since(),)).fetchone()[0]

    def pending_events(self):
        """The recent, locally preselected sample (flat). Old history is deliberately never included."""
        return [event for events in self.fresh_by_scope().values() for event in events]

    def interest_manifest(self, candidates):
        manifest = []
        for c in candidates[:5]:
            evidence = []
            for event_id in c['evidence_ids'][:3]:
                event = self.state.get_event(event_id)
                if event and event['active']:
                    evidence.append({'event_id': event_id, 'platform': event['platform'], 'text': str(event.get('content', ''))[:300]})
            by_key = {r['impression_key']: r for r in impressions.Ledger(self.state).rows(self.now())}
            reactions = [{'impression_key': k, 'action': by_key[k]['action'], 'execution': by_key[k]['execution'],
                          'direction': by_key[k]['direction'], 'mode': by_key[k]['mode'], 'weight': by_key[k]['weight'],
                          'at': by_key[k]['occurred_at']} for k in c['contact_keys'] if k in by_key]
            manifest.append({**{k: c[k] for k in ('candidate_key', 'label', 'points', 'independent', 'fingerprint',
                                                  'contact_keys', 'evidence_ids')}, 'evidence': evidence, 'reactions': reactions})
        return manifest

    def plan_next(self, day):
        """Create at most one new ordinary batch while the day's ordinary-call budget allows it."""
        cfg = self.cfg()
        planned = [b for b in self.batches(day) if b['kind'] in ('events', 'interest')]
        if len(planned) >= cfg['max_batch_calls'] or self.ordinary_remaining() <= 0:
            return None      # also bounded by the actual quota day's ordinary calls, not only by batches created per window day
        candidates = []
        if not any(b['kind'] == 'interest' for b in planned) and v14_2.enabled(self.s.policy, 'impressions'):
            candidates = impressions.Ledger(self.state).candidates(self.now())
        by_scope = self.fresh_by_scope()
        # Material first (its entity/direction resolution can complete the topic labels the interest review needs), but
        # always keep one ordinary call for a pending interest review.
        if candidates and not (by_scope and len(planned) + 1 < cfg['max_batch_calls']):
            return self.create(day, 'interest:public', 'interest', self.interest_manifest(candidates))
        if not by_scope:
            return None
        served = self.state.get('daily_scope_served', {})
        # rotation first (least recently served), then the scope holding the newest candidate: a busy scope cannot starve others
        scope = min(by_scope, key=lambda s: (served.get(s, 0), -float(by_scope[s][0]['received_at']), s))
        picked, chars = [], 0
        for event in by_scope[scope]:
            excerpt = str(event.get('content', ''))[:cfg['excerpt_chars']]
            if len(picked) >= cfg['batch_events'] or (picked and chars + len(excerpt) > PROMPT_CHAR_LIMIT - 3000):
                break
            picked.append(event['event_id'])
            chars += len(excerpt)
        served[scope] = self.now()
        self.state.put('daily_scope_served', served)
        return self.create(day, scope, 'events', picked)

    def create(self, day, scope, kind, manifest):
        count = len([b for b in self.batches(day) if b['kind'] == kind])
        key = '%s:%s:%s:%d' % (day, kind, hashlib.sha256(scope.encode()).hexdigest()[:10], count + 1)
        moment = self.now()
        with self.state._lock:
            self.state.db.execute(
                'INSERT OR IGNORE INTO daily_batches(batch_key,day,scope,kind,status,manifest,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                (key, day, scope, kind, 'pending', _json(manifest), moment, moment))
        return key

    def plan_possible(self, day):
        planned = [b for b in self.batches(day) if b['kind'] in ('events', 'interest')]
        if len(planned) >= self.cfg()['max_batch_calls'] or self.ordinary_remaining() <= 0:
            return False
        has_interest = (v14_2.enabled(self.s.policy, 'impressions') and not any(b['kind'] == 'interest' for b in planned)
                        and bool(impressions.Ledger(self.state).candidates(self.now())))
        return bool(self.fresh_by_scope()) or has_interest

    # ---- main entry ---------------------------------------------------------------------------
    async def run(self, now=None):
        """One scheduler step. Safe to call every 30 s, after a restart, after a missed window and across 03:00."""
        cfg = self.cfg()
        if not cfg['enabled']:
            return {'skipped': 'daily_learning_disabled'}
        async with self.lock:
            moment = self.now() if now is None else float(now)
            self.activate()
            day = window_day(moment, cfg['hour'])
            self.reclaim()
            results = []
            for _ in range(cfg['max_batch_calls'] + cfg['max_retries'] + 6):
                batch = self.next_ready()
                if batch is None:
                    if self.state.get('daily_done:' + day) or self.plan_next(day) is None:
                        break
                    batch = self.next_ready()
                    if batch is None:
                        break
                outcome = await self.execute(batch)
                results.append(outcome)
                if outcome.get('status') in ('retry_later', 'deferred_budget'):
                    break
            if self.state.get('daily_done:' + day) and not self.batches(statuses=('pending', 'running')):
                return {'skipped': 'already_done', 'day': day, 'results': results}
            if not self.batches(statuses=('pending', 'running')) and not self.plan_possible(day):
                statuses = [(b['batch_key'], b['status']) for b in self.batches(day)]
                kinds = {s for _, s in statuses}
                summary = {'day': day, 'at': moment, 'batches': statuses, **self.report(day),
                           'result': 'failed' if kinds and kinds <= {'failed'} else ('completed' if kinds & {'completed'} else 'no_change')}
                self.state.put('daily_done:' + day, summary)
                self.state.put('last_daily_learning', summary)
                return {'day': day, 'done': True, 'summary': summary, 'results': results}
            return {'day': day, 'done': False, 'results': results}

    def report(self, day):
        """Honest accounting that keeps three things apart: SELECTED pending batches (today's small sample and its progress),
        recent raw events that were simply not sampled, and HISTORICAL raw events (retained history, not a learning debt,
        never reported as learned or failed)."""
        selected, processed = 0, 0
        for batch in self.batches(day):
            if batch['kind'] != 'events':
                continue
            ids = [x for x in json.loads(batch['manifest']) if isinstance(x, str)]
            selected += len(ids)
            with self.state._lock:
                processed += sum(1 for eid in ids if (self.state.db.execute('SELECT status FROM learning WHERE event_id=?', (eid,)).fetchone()
                                                      or [''])[0] in ('stored', 'processed', 'skipped'))
        return {'selected': selected, 'processed': processed,
                'selected_batches_open': len(self.batches(statuses=('pending', 'running'))),
                'recent_unselected': self.recent_unselected_count(), 'historical_raw': self.historical_raw_count()}

    def status(self, day=None):
        """Operator view (no chat content): today's selection versus untouched history."""
        day = day or window_day(self.now(), self.cfg()['hour'])
        return {'day': day, 'enabled': self.enabled(), 'activation_at': self.activation_at(), **self.report(day), 'done': self.state.get('daily_done:' + day) is not None,
                'batches': [{'key': b['batch_key'], 'kind': b['kind'], 'status': b['status'], 'attempts': b['attempts']}
                            for b in self.batches(day)]}

    # ---- execution ----------------------------------------------------------------------------
    async def execute(self, batch):
        key = batch['batch_key']
        with self.state._transaction():
            got = self.state.db.execute("UPDATE daily_batches SET status='running',lease_until=?,updated_at=? WHERE batch_key=? AND status='pending'",
                                        (self.now() + LEASE_SECONDS, self.now(), key)).rowcount
        if not got:
            return {'batch': key, 'status': 'not_claimed'}
        batch = self.batch(key)
        if not batch['output']:
            outcome = await self.call_model(batch)
            if outcome is not None:
                return outcome
            batch = self.batch(key)
        try:
            effects = await self.apply(batch, json.loads(batch['output']))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # keep the saved output: the next tick resumes it (no new model call)
            self.update(key, status='pending', lease_until=0, next_at=self.now() + 60, detail='apply:' + type(exc).__name__)
            return {'batch': key, 'status': 'retry_later', 'error': type(exc).__name__}
        status = 'completed' if effects else 'no_change'
        self.update(key, status=status, lease_until=0, detail='')
        return {'batch': key, 'status': status}

    def next_quota_day_start(self):
        local = datetime.fromtimestamp(self.now(), TZ)
        return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()

    def defer_to_next_day(self, key):
        """Quota denied: keep the SELECTED batch (manifest and any saved output) durably pending until the next Beijing quota
        day. Nothing is lost, nothing is reported as done, and there is no hot loop (next_at is in the future)."""
        self.update(key, status='pending', lease_until=0, next_at=self.next_quota_day_start(), detail='budget:next_quota_day')

    def ordinary_remaining(self):
        return self.cfg()['max_batch_calls'] - self.state.quota_used('daily_ordinary')

    def fail_events(self, ids, permanent):
        for event_id in ids:
            if not isinstance(event_id, str):
                continue
            count = int(self.state.get('daily_fail:' + event_id, 0)) + 1
            self.state.put('daily_fail:' + event_id, count)
            # the same material is not re-sent the same day after a failed batch (no same-input hot loop)
            previous = self.state.get('daily_defer:' + event_id) or {}
            self.state.put('daily_defer:' + event_id, {'count': previous.get('count', 0), 'not_before': self.now() + DEFER_SECONDS})
            if permanent or count >= EVENT_FAIL_LIMIT:
                try:
                    self.state.mark_learned(event_id, 'failed', [], detail='daily_batch_failed')
                except ValueError:
                    pass

    def manifest_events(self, batch):
        return [self.state.get_event(e) for e in json.loads(batch['manifest'])] if batch['kind'] == 'events' else []

    async def call_model(self, batch):
        """Reserve first, then call. Returns None when validated output was persisted, else an outcome dict."""
        cfg, key = self.cfg(), batch['batch_key']
        # Inspect BEFORE spending anything: a manifest with nothing authorized any more is a successful no-change outcome.
        if batch['kind'] == 'events' and not [e for e in self.manifest_events(batch) if self.eligible(e)]:
            self.update(key, status='no_change', lease_until=0, detail='no_eligible_events')
            return {'batch': key, 'status': 'no_change'}
        attempt = batch['attempts'] + 1
        if attempt > 1 + cfg['max_retries']:
            # Every permitted request (the first plus max_retries) was already issued - e.g. the process died at the final
            # request boundary before its output was saved. That is a TERMINAL failure: no extra request, no endless deferral.
            self.update(key, status='failed', lease_until=0, detail='attempts_exhausted')
            self.fail_events(json.loads(batch['manifest']) if batch['kind'] == 'events' else [], permanent=False)
            return {'batch': key, 'status': 'failed', 'error': 'attempts_exhausted'}
        if attempt == 1:     # an ordinary call of the ACTUAL Beijing quota day (<= max_batch_calls, across recovery and new batches)
            purpose = ('ordinary:%s' % key, 'daily_ordinary', cfg['max_batch_calls'], False)
        else:                # a retry: <= max_retries per actual day (and per batch, enforced above)
            purpose = ('retry:%d:%s' % (attempt, key), 'daily_retry', cfg['max_retries'], False)

        def persist_attempt():
            self.state.db.execute('UPDATE daily_batches SET attempts=?,updated_at=? WHERE batch_key=?', (attempt, self.now(), key))
        # Purpose slot, shared background slot AND the persisted attempt number commit in ONE transaction: a denial changes
        # nothing (no phantom attempt, no burnt slot) and a crash cannot separate the reservation from the counter.
        if not self.state.reserve_many([purpose, ('daily:%s:%d' % (key, attempt), 'background', self.state.BACKGROUND_CAP, True)],
                                       effect=persist_attempt):
            self.defer_to_next_day(key)          # temporary budget only: the batch stays pending, attempts unchanged
            return {'batch': key, 'status': 'deferred_budget'}
        prompt, user = self.build_prompt(batch)
        try:
            async with self.s.busy:
                result = await self.s.ctx.llm.generate(prompt=[{'role': 'system', 'content': prompt},
                                                               {'role': 'user', 'content': user}],
                                                       task_name='learner', max_tokens=2400, temperature=0.4)
            if not isinstance(result, dict) or not result.get('success', False):
                raise RuntimeError('host_model_failed')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt - 1 >= cfg['max_retries']:
                self.update(key, status='failed', lease_until=0, detail='transient_retries_exhausted:' + type(exc).__name__)
                self.fail_events(json.loads(batch['manifest']) if batch['kind'] == 'events' else [], permanent=False)
                return {'batch': key, 'status': 'failed', 'error': type(exc).__name__}
            wait = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
            self.update(key, status='pending', lease_until=0, next_at=self.now() + wait, detail='transient:' + type(exc).__name__)
            return {'batch': key, 'status': 'retry_later', 'error': type(exc).__name__}
        text = str(result.get('response', ''))
        usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
        try:
            clean = self.validate(batch, text)
        except ValueError as exc:
            self.update(key, status='failed', lease_until=0, detail='invalid_output:' + str(exc)[:80],
                        input_chars=len(prompt) + len(user), output_chars=len(text))
            self.fail_events(json.loads(batch['manifest']) if batch['kind'] == 'events' else [], permanent=True)
            return {'batch': key, 'status': 'failed', 'error': 'invalid_output'}
        # Durable BEFORE any memory write or persona commit.
        self.update(key, output=_json(clean), input_chars=len(prompt) + len(user), output_chars=len(text),
                    tokens_in=usage.get('prompt_tokens'), tokens_out=usage.get('completion_tokens'))
        return None

    # ---- prompts ------------------------------------------------------------------------------
    GUIDANCE = '把冲浪、搜索或聊天中有趣的知识细节、新鲜事变成自己的新喜好、愿望和谈资。1条明确材料就可以积极提出最多3项动态更新；列表型兴趣需合并同义项，单人特定称呼或挑衅要求（如叫杂鱼）不得写入全局口头禅或全局行为偏好。'

    def reaction_evidence(self, item):
        """True when the contact carries a real recorded robot reaction: an action beyond a bare read, or the reason the
        robot gave in its own decision at the time."""
        if item['row']['action'] != 'read':
            return True
        for event_id in item['evidence_ids']:
            event = self.state.get_event(event_id) or {}
            if self.state.get('opp_reason:' + str(event.get('native_event_id') or '')):
                return True
        return False

    def unresolved_for(self, batch):
        return impressions.Ledger(self.state).unresolved(json.loads(batch['manifest']), self.now())

    def build_prompt(self, batch):
        cfg = self.cfg()
        manifest = json.loads(batch['manifest'])
        if batch['kind'] == 'events':
            unresolved = self.unresolved_for(batch)
            rows = []
            for event in self.manifest_events(batch):
                if not self.eligible(event):
                    continue
                recorded = self.state.get('opp_reason:' + str(event.get('native_event_id') or ''), '')
                linked = [{'impression_key': k, 'action': v['row']['action'], 'direction': v['row']['direction'],
                           'topic': v['row']['topic'], 'bot_reason_at_the_time': recorded}
                          for k, v in unresolved.items() if event['event_id'] in v['evidence_ids']]
                rows.append({'event_id': event['event_id'], 'platform': event['platform'],
                             'text': str(event.get('content', ''))[:cfg['excerpt_chars']], 'contacts': linked})
            system = ('你在做每日集中学习。只用给定的真实外部材料。' + self.GUIDANCE +
                      '每条材料都必须在items里给出明确结果（remember为true或false），不可遗漏或重复。返回JSON {"items":[{"event_id":"",'
                      '"remember":true或false,"summary":"最多180字","category":"AI或游戏或美食或其他","public_summary":true或false,'
                      '"knowledge_type":"fact或opinion或inference"}],"proposals":[{"field":"interests、gaming_preferences、anime_preferences、food_preferences、preferred_phrases、current_wishes或其他动态字段",'
                      '"value":"或数组","evidence_ids":["event_id"],"reason":""}],"focus":[{"value":"短期关注","event_id":""}],'
                      '"entities":[{"impression_keys":["仅限材料contacts里给出的key"],"topic":"具体作品或题材","direction":"positive或negative或空"}]}。'
                      'entities用于补全材料下尚未确定主题或喜恶方向的接触记录。direction可根据材料内容是否契合大肥鱼的胃口及当时的反应综合判断；'
                      '只要内容有趣、好玩，哪怕当时只是静默浏览（browse），也可以大胆标为 positive 并在 proposals 和 focus 中提议新的口头禅、短期关注点或愿望。'
                      'public_summary只能为抽象的AI/游戏/美食话题，不含姓名、昵称、账户、个人经历或私人事实。可学习字段：'
                      + json.dumps(character.schema(), ensure_ascii=False))
            return system, json.dumps({'scope': batch['scope'], 'persona': self.state.persona_view(), 'events': rows}, ensure_ascii=False)
        system = ('你在做每日兴趣评审。下列候选已满足本地接触门槛（这只是提案资格，不是结论）。请依据给出的原始材料和机器人当时的真实反应'
                  '（动作、是否执行、方向、是否自主）判断是否真是稳定偏好，而不是复述分数。' + self.GUIDANCE +
                  '对每个候选返回promote或continue_observe；promote需给field（interests、anime_preferences、gaming_preferences、'
                  'food_preferences、technology_preferences之一）和value（字符串数组，须包含候选主题）。'
                  '返回JSON {"verdicts":[{"candidate_key":"","verdict":"promote或continue_observe","field":"","value":[],"reason":""}]}。')
        payload = [{k: c[k] for k in ('candidate_key', 'label', 'points', 'independent', 'evidence', 'reactions')} for c in manifest]
        return system, json.dumps({'candidates': payload}, ensure_ascii=False)

    def validate(self, batch, text):
        from .service import clean_json
        out = clean_json(text)
        manifest = json.loads(batch['manifest'])
        if batch['kind'] == 'events':
            eligible = {e['event_id'] for e in self.manifest_events(batch) if self.eligible(e)}
            items = out.get('items')
            if items is None:
                items = []
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                raise ValueError('items_must_be_list_of_objects')
            seen = {}
            for item in items:
                eid = item.get('event_id')
                if eid not in set(manifest):
                    raise ValueError('item_outside_manifest')
                if eid in seen and _json(seen[eid]) != _json(item):
                    raise ValueError('conflicting_duplicate_event_outcome')
                if type(item.get('remember')) is not bool:
                    raise ValueError('explicit_remember_or_skip_required')
                seen[eid] = item
            for field in ('proposals', 'focus', 'entities'):
                if not isinstance(out.get(field) or [], list):
                    raise ValueError(field + '_must_be_list')
            allowed = self.unresolved_for(batch)
            entities = []
            for entity in (out.get('entities') or [])[:5]:
                if not isinstance(entity, dict) or not isinstance(entity.get('impression_keys') or [], list):
                    continue
                keys = [k for k in (entity.get('impression_keys') or []) if k in allowed][:10]
                if not keys:
                    continue
                topic = str(entity.get('topic') or '')[:80]
                direction = entity.get('direction') if entity.get('direction') in ('positive', 'negative') else ''
                entities.append({'impression_keys': keys, 'topic': topic, 'direction': direction})
            missing = sorted(eligible - set(seen))
            return {'items': list(seen.values())[:20], 'missing': missing, 'proposals': (out.get('proposals') or [])[:3],
                    'focus': (out.get('focus') or [])[:2], 'entities': entities}
        keys = {c['candidate_key'] for c in manifest}
        verdicts = out.get('verdicts') or []
        if not isinstance(verdicts, list) or any(not isinstance(v, dict) or v.get('candidate_key') not in keys for v in verdicts):
            raise ValueError('verdict_outside_manifest')
        ids = [v['candidate_key'] for v in verdicts]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate_verdict')
        return {'verdicts': verdicts[:5]}

    # ---- durable, idempotent effects ----------------------------------------------------------
    def commit_key(self, key, item):
        done = json.loads(self.batch(key)['committed'])
        if item not in done:
            done.append(item)
            self.update(key, committed=_json(done))

    async def apply(self, batch, out):
        """Returns the number of real effects (0 = a successful no-change batch)."""
        key = batch['batch_key']
        done = set(json.loads(batch['committed']))
        manifest = json.loads(batch['manifest'])
        effects = 0
        if batch['kind'] == 'events':
            for item in out['items']:
                tag = 'mem:' + item['event_id']
                if tag not in done:
                    effects += 1 if await self.store_item(item) else 0
                    self.commit_key(key, tag)
            for event_id in out.get('missing', []):
                tag = 'defer:' + event_id
                if tag not in done:
                    self.defer_event(event_id)
                    self.commit_key(key, tag)
            for index, proposal in enumerate(out['proposals']):
                tag = 'prop:%d' % index
                if tag not in done:
                    effects += 1 if self.apply_proposal(proposal, manifest, '%s:prop:%d' % (key, index)) else 0
                    self.commit_key(key, tag)
            for index, focus in enumerate(out['focus']):
                tag = 'focus:%d' % index
                if tag not in done:
                    event = self.state.get_event(focus.get('event_id')) if isinstance(focus, dict) else None
                    if event and focus['event_id'] in manifest and self.eligible(event):
                        result = self.state.set_temp('current_focus', focus.get('value'), focus['event_id'],
                                                     reason='daily_batch_focus', effect_key='%s:focus:%d' % (key, index))
                        effects += 1 if result.get('accepted') else 0
                    self.commit_key(key, tag)
            ledger = impressions.Ledger(self.state)
            for index, entity in enumerate(out['entities']):
                tag = 'ent:%d' % index
                if tag not in done:
                    effects += ledger.resolve(entity['impression_keys'], entity['topic'], entity['direction'])
                    self.commit_key(key, tag)
            return effects
        ledger = impressions.Ledger(self.state)
        by_key = {c['candidate_key']: c for c in manifest}
        for index, verdict in enumerate(out['verdicts']):
            tag = 'verdict:%d' % index
            if tag in done:
                continue
            candidate = by_key[verdict['candidate_key']]
            if verdict.get('verdict') == 'promote' and verdict.get('field') in impressions.INTEREST_FIELDS:
                # contact_keys and evidence come from OUR manifest (looked up again inside propose_changes), never from the model
                current = (self.state.persona()['dynamic'].get(verdict['field']) or [])
                value = verdict.get('value')
                if isinstance(value, list) and isinstance(current, list):
                    value = list(dict.fromkeys(current + value))[:12]
                result = self.state.propose_changes({verdict['field']: value}, candidate['evidence_ids'],
                                                    '每日兴趣评审：有效接触达到提案资格', automatic=True,
                                                    contact_keys=candidate['contact_keys'],
                                                    effect_key='%s:verdict:%d' % (key, index))
                if not result.get('replayed'):
                    ledger.verdict(candidate, 'promoted' if result.get('accepted') else 'continue_observe', verdict['field'],
                                   result.get('reason', ''))
                    effects += 1 if result.get('accepted') else 0
            else:
                ledger.verdict(candidate, 'continue_observe', '', str(verdict.get('reason', ''))[:200])
            self.commit_key(key, tag)
        return effects

    def defer_event(self, event_id):
        """No explicit outcome from the model: a durable deferral, not a same-input hot loop."""
        record = self.state.get('daily_defer:' + event_id)
        count = (record or {}).get('count', 0) + 1
        self.state.put('daily_defer:' + event_id, {'count': count, 'not_before': self.now() + DEFER_SECONDS})
        if count >= EVENT_FAIL_LIMIT:
            try:
                self.state.mark_learned(event_id, 'failed', [], detail='daily_batch_no_outcome')
            except ValueError:
                pass

    async def store_item(self, item):
        """Memory write with the stable external_id of the legacy path; skipped if already durably learned or no longer
        authorized. Returns True when memory was stored."""
        eid = item['event_id']
        event = self.state.get_event(eid)
        if not self.eligible(event):
            return False
        with self.state._lock:
            row = self.state.db.execute('SELECT status FROM learning WHERE event_id=?', (eid,)).fetchone()
        if row and row['status'] in ('stored', 'processed', 'skipped'):
            return False
        summary = str(item.get('summary', '')).strip()[:600]
        if not item.get('remember') or not summary:
            self.state.mark_learned(eid, 'skipped', [], detail='daily_batch_no_memory')
            return False
        category = item.get('category', '其他')
        public = bool(item.get('public_summary') and category in PUBLIC_TOPICS
                      and event.get('conversation_id') in self.s.policy.get('public_qq_sessions', []))
        if not abstract_topic_summary(summary):
            public = False
        meta = {'unified_event_id': eid, 'origin': event.get('origin'), 'source_id': event.get('source_id'),
                'source_url': event.get('source_url'), 'source_platform': event['platform'],
                'source_chat_id': event['conversation_id'], 'chat_id': event['conversation_id'], 'public_summary': public,
                'category': category, 'knowledge_type': item.get('knowledge_type', 'inference'),
                'actual_material': event.get('material_evidence') or (event.get('metadata') or {}).get('material', 'text'),
                'learning_path': 'daily_batch'}
        if is_private_event(event):
            meta.update(is_private=True, sharing_scope='conversation', public_summary=False)
        if event.get('event_type') == 'device_voice_turn':
            meta.update(channel='device_voice', profile_id=(event.get('metadata') or {}).get('profile_id', ''))
        public_chat = event['platform'] == 'bilibili' and event.get('sharing_scope') == 'public' and not is_private_event(event)
        chat = 'bilibili:public' if public_chat else event['conversation_id']
        written = await self.s.memory('ingest_text', external_id='unified:' + eid,
                                      source_type='external' if event['platform'] == 'bilibili' else 'chat_summary', text=summary,
                                      chat_id=chat, person_ids=[], participants=[], timestamp=event.get('occurred_at', self.now()),
                                      tags=['unified', category], metadata=meta)
        if not written.get('success'):
            raise RuntimeError('memory_write_failed')
        if written.get('detail') == 'chat_filtered':
            # The Host's chat filter skipped the write ("success" with only skipped_ids): it is NOT stored memory.
            self.state.mark_learned(eid, 'skipped', [], detail='host_chat_filtered')
            return False
        # A replay of the same external_id returns skipped_ids=[existing paragraph] / reason 'exists' (Host dedup) - also stored.
        self.state.mark_learned(eid, 'stored', written.get('stored_ids') or written.get('skipped_ids') or [])
        return True

    def apply_proposal(self, proposal, manifest, effect_key):
        """A single concrete batch item can inspire an expression, preference or wish."""
        if not isinstance(proposal, dict) or proposal.get('field') not in character.LEARNABLE_FIELDS:
            return False
        ids = [e for e in (proposal.get('evidence_ids') or []) if isinstance(e, str) and e in manifest
               and self.eligible(self.state.get_event(e))]
        if not ids:
            return False
        result = self.state.propose_changes([{'field': proposal['field'], 'value': proposal.get('value'), 'evidence_ids': ids}], ids,
                                            str(proposal.get('reason') or '每日集中学习')[:300], automatic=True, effect_key=effect_key)
        return bool(result.get('accepted'))
