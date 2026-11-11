"""Local impression ledger: real contacts -> 1/3/5 weights -> 14-day candidate window. Zero model tokens.

A *contact* is one selection opportunity of one target. Its weight is the highest action seen for it
(read 1; like/coin/autonomous comment intent 3; favorite/follow 5). Retries, repeated pushes, new
message ids and search-cache hits address the same (platform, opportunity, target) key and so can never
create a second contact. Positive / negative / unknown direction, autonomous / commanded mode and the
execution status are stored separately and never merged into "published".
"""
from __future__ import annotations

import hashlib
import json
import time

from . import v14_2

INTEREST_FIELDS = frozenset({'interests', 'anime_preferences', 'gaming_preferences', 'food_preferences',
                             'technology_preferences'})
ACTION_WEIGHT = {'read': 1, 'like': 3, 'coin': 3, 'comment': 3, 'favorite': 5, 'follow': 5}
TARGET_TYPES = ('content', 'author', 'topic')
DIRECTIONS = ('positive', 'negative', 'unknown')
MODES = ('autonomous', 'commanded')
# failed: nothing happened; read: material read; intent_not_executed: autonomous willingness blocked by quota/queue;
# submitted_pending_moderation: platform accepted, public visibility unverified; executed: receipt verified.
EXEC_RANK = {'failed': 0, 'read': 1, 'intent_not_executed': 2, 'submitted_pending_moderation': 3, 'executed': 4}
COUNTING_EXEC = frozenset({'read', 'intent_not_executed', 'submitted_pending_moderation', 'executed'})
EXTERNAL = ('external_user', 'platform_content')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def norm(text):
    return ' '.join(str(text or '').casefold().split())[:80]


def _digest(*parts):
    return hashlib.sha256(_json(list(parts)).encode()).hexdigest()


def key_for(platform, opportunity_id, target_type, target_ref):
    return 'imp:' + _digest(str(platform), str(opportunity_id), target_type, str(target_ref))[:40]


def group_key(row):
    """Specific works group by their own reference until a nightly pass resolves a shared topic label."""
    label = norm(row['topic']) or 'ref:' + str(row['target_ref'])
    return row['target_type'], label


def _direction(a, b):
    if 'negative' in (a, b):
        return 'negative'
    return 'positive' if 'positive' in (a, b) else 'unknown'


class Ledger:
    def __init__(self, state):
        self.state = state

    @property
    def settings(self):
        return v14_2.section(self.state.policy, 'impressions')

    def record(self, *, platform, opportunity_id, content_id, target_type, target_ref, direction, mode, execution,
               action, occurred_at, evidence_event_ids=(), topic='', scope='public'):
        platform, opportunity_id = str(platform or '').strip().lower(), str(opportunity_id or '').strip()
        content_id, target_ref = str(content_id or '').strip(), str(target_ref or '').strip()
        if (not platform or not opportunity_id or not content_id or not target_ref or target_type not in TARGET_TYPES
                or direction not in DIRECTIONS or mode not in MODES or execution not in EXEC_RANK
                or action not in ACTION_WEIGHT):
            raise ValueError('invalid_impression')
        if target_type == 'author' and action not in ('follow', 'read', 'like', 'comment'):
            raise ValueError('invalid_author_action')
        # Only completed actions (receipt / platform-accepted) or an explicitly modelled autonomous comment
        # intent may raise the effective weight. A failed or merely intended like/favorite adds nothing.
        eligible = (execution in ('executed', 'submitted_pending_moderation')
                    or (execution == 'intent_not_executed' and action == 'comment')
                    or (execution == 'read' and action == 'read'))
        weight = ACTION_WEIGHT[action] if eligible else 0
        key = key_for(platform, opportunity_id, target_type, target_ref)
        moment = float(self.state.now())
        # External material only: a bot reaction/self event is a preference signal, never evidence of the material.
        evidence = []
        for event_id in list(evidence_event_ids)[:8]:
            event = self.state.get_event(str(event_id))
            if event and event['active'] and event.get('origin') in EXTERNAL:
                evidence.append(str(event_id))
        with self.state._transaction():
            row = self.state.db.execute('SELECT * FROM impressions WHERE impression_key=?', (key,)).fetchone()
            if not row:
                self.state.db.execute('INSERT INTO impressions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (key, platform, opportunity_id, content_id, target_type, target_ref, norm(topic),
                                       direction, mode, execution, action, weight, str(scope), float(occurred_at),
                                       _json(evidence), moment, moment))
                return {'impression_key': key, 'created': True, 'weight': weight}
            best_weight, best_action = (weight, action) if weight > row['weight'] else (row['weight'], row['action'])
            # A non-eligible report (e.g. a failed favorite) never changes the recorded execution either.
            execution_now = (execution if eligible and EXEC_RANK[execution] > EXEC_RANK[row['execution']]
                             else row['execution'])
            merged = list(dict.fromkeys(json.loads(row['evidence_ids']) + evidence))[:8]
            self.state.db.execute(
                'UPDATE impressions SET direction=?,mode=?,execution=?,action=?,weight=?,topic=?,evidence_ids=?,updated_at=? '
                'WHERE impression_key=?',
                (_direction(row['direction'], direction), 'commanded' if 'commanded' in (row['mode'], mode) else 'autonomous',
                 execution_now, best_action, best_weight, row['topic'] or norm(topic), _json(merged), moment, key))
            return {'impression_key': key, 'created': False, 'weight': best_weight}

    def assign_topic(self, impression_keys, topic, evidence_event_ids):
        """Nightly entity resolution. Needs the original evidence; never adds or removes a contact."""
        label = norm(topic)
        if not label or not evidence_event_ids:
            return 0
        changed = 0
        with self.state._transaction():
            for key in impression_keys:
                row = self.state.db.execute('SELECT evidence_ids,topic FROM impressions WHERE impression_key=?', (key,)).fetchone()
                if row and not row['topic'] and set(evidence_event_ids) & set(json.loads(row['evidence_ids'])):
                    self.state.db.execute('UPDATE impressions SET topic=?,updated_at=? WHERE impression_key=?',
                                          (label, float(self.state.now()), key))
                    changed += 1
        return changed

    def unresolved(self, event_ids, now=None):
        """Contacts in the window that still lack a topic label or a direction and cite one of these materials.
        Returns {impression_key: {'row': row, 'evidence_ids': [matching event ids]}} - the only keys the nightly
        resolution may touch."""
        wanted = set(event_ids)
        found = {}
        for row in self.rows(now, only_counting=False):
            cited = [e for e in json.loads(row['evidence_ids']) if e in wanted]
            if cited and (not row['topic'] or row['direction'] == 'unknown') and row['scope'] == 'public':
                found[row['impression_key']] = {'row': row, 'evidence_ids': cited}
        return found

    def resolve(self, impression_keys, topic='', direction=''):
        """Nightly resolution of unknown entity / direction from the original material. Never adds, removes or
        re-weights a contact, so rewording or splitting labels cannot manufacture extra contacts."""
        label = norm(topic)
        changed = 0
        with self.state._transaction():
            for key in impression_keys:
                row = self.state.db.execute('SELECT topic,direction FROM impressions WHERE impression_key=?', (key,)).fetchone()
                if not row:
                    continue
                new_topic = label if label and not row['topic'] else row['topic']
                new_direction = direction if direction in ('positive', 'negative') and row['direction'] == 'unknown' else row['direction']
                if (new_topic, new_direction) != (row['topic'], row['direction']):
                    self.state.db.execute('UPDATE impressions SET topic=?,direction=?,updated_at=? WHERE impression_key=?',
                                          (new_topic, new_direction, float(self.state.now()), key))
                    changed += 1
        return changed

    def rows(self, now=None, only_counting=True):
        moment = float(self.state.now() if now is None else now)
        start = moment - self.settings['window_days'] * 86400
        with self.state._lock:
            found = self.state.db.execute('SELECT * FROM impressions WHERE occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,impression_key',
                                          (start, moment + 60)).fetchall()
        result = [dict(r) for r in found]
        if only_counting:
            result = [r for r in result if r['execution'] in COUNTING_EXEC and r['weight'] > 0 and r['mode'] == 'autonomous'
                      and r['direction'] == 'positive' and r['scope'] == 'public']
        return result

    def stats(self, rows):
        """points = sum of per-contact maxima; independent = contacts at least min_gap apart."""
        gap = self.settings['min_gap_seconds']
        ordered = sorted(rows, key=lambda r: (r['occurred_at'], r['impression_key']))
        independent, last = 0, None
        for row in ordered:
            if last is None or row['occurred_at'] - last >= gap:
                independent += 1
                last = row['occurred_at']
        return sum(r['weight'] for r in rows), independent

    def groups(self, now=None):
        grouped = {}
        for row in self.rows(now):
            grouped.setdefault(group_key(row), []).append(row)
        return grouped

    def candidates(self, now=None):
        """Proposal ELIGIBILITY only (>= points and >= 1 autonomous choice)."""
        promo = self.settings['promotion_points']
        result = []
        for (target_type, label), rows in sorted(self.groups(now).items()):
            if target_type == 'author':
                continue  # an author follow's 5 points never feed content topics
            points, independent = self.stats(rows)
            if points < promo or independent < 1:
                continue
            ckey = 'cand:' + _digest(target_type, label)[:32]
            fingerprint = _digest(sorted([r['impression_key'], r['weight'], r['execution']] for r in rows))
            with self.state._lock:
                known = self.state.db.execute('SELECT * FROM interest_candidates WHERE candidate_key=?', (ckey,)).fetchone()
            if known and (known['status'] == 'promoted' or (known['status'] == 'observing' and known['fingerprint'] == fingerprint)):
                continue
            evidence = list(dict.fromkeys(e for r in rows for e in json.loads(r['evidence_ids'])))
            result.append({'candidate_key': ckey, 'target_type': target_type, 'label': label, 'points': points,
                           'independent': independent, 'fingerprint': fingerprint,
                           'contact_keys': [r['impression_key'] for r in rows], 'evidence_ids': evidence[:12]})
        return result

    def verdict(self, candidate, verdict, field='', reason=''):
        status = 'promoted' if verdict == 'promoted' else 'observing'
        with self.state._lock:
            self.state.db.execute(
                'INSERT INTO interest_candidates VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_key) DO UPDATE SET '
                'field=excluded.field,status=excluded.status,fingerprint=excluded.fingerprint,verdict=excluded.verdict,'
                'reviewed_at=excluded.reviewed_at,reason=excluded.reason,contact_keys=excluded.contact_keys,updated_at=excluded.updated_at',
                (candidate['candidate_key'], field, candidate['label'], status, candidate['fingerprint'], verdict,
                 float(self.state.now()), str(reason)[:300], _json(candidate['contact_keys']), float(self.state.now())))


def contacts_qualify(state, contact_keys, evidence_ids, field, value=None):
    """Adapted source gate: validated contact evidence for ONE interest field.

    Requires the ledger contacts to exist in the window, share one group, be autonomous, positive,
    counted, publicly scoped, reach the points threshold with >= 1 autonomous choice, and each carry
    its own active external material usable for global growth. The material ids cited by the proposal
    must come from those contacts. No other path may use this."""
    if field not in INTEREST_FIELDS or not contact_keys or not isinstance(contact_keys, (list, tuple)):
        return False
    ledger = Ledger(state)
    keys = list(dict.fromkeys(str(k) for k in contact_keys))
    in_window = {r['impression_key']: r for r in ledger.rows()}
    rows = [in_window.get(k) for k in keys]
    if any(r is None for r in rows) or len({group_key(r) for r in rows}) != 1 or rows[0]['target_type'] == 'author':
        return False
    points, independent = ledger.stats(rows)
    if points < ledger.settings['promotion_points'] or independent < 1:
        return False
    # Model-supplied keys are only looked up, never trusted: the proposed value must name the contacts' own
    # (resolved) topic, so keys for one subject cannot justify another. Unresolved works need a nightly label first.
    label = group_key(rows[0])[1]
    if label.startswith('ref:'):
        return False
    items = value if isinstance(value, list) else [value]
    if not any(isinstance(x, str) and (label in norm(x) or norm(x) in label) and norm(x) for x in items):
        return False
    allowed_ids = set()
    for row in rows:
        valid = []
        for event_id in json.loads(row['evidence_ids']):
            event = state.get_event(event_id)
            if (event and event['active'] and event.get('learning_allowed') and event.get('origin') in EXTERNAL
                    and state.growth_evidence_allowed(event)):
                valid.append(event_id)
        if not valid:
            return False
        allowed_ids.update(valid)
    return bool(evidence_ids) and set(evidence_ids) <= allowed_ids
