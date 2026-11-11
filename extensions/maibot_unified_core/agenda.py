"""Her own schedule in QQ: reminders the other person asked for or agreed to, her own "I want to bring this up
later" follow-ups, and things she said she would do (promises). The model writes it through the qq_agenda tool;
the proactive loop delivers due items. Code, not the model, enforces consent, limits and honest status."""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import content_rules, memory_time
from .reminders import parse_when

KINDS = ('reminder', 'followup', 'promise')
TIMED = ('reminder', 'followup')
OPEN = ('scheduled', 'open', 'blocked')
FINAL = ('delivered', 'sent', 'skipped', 'expired', 'cancelled', 'unknown', 'done', 'dropped')
PROMISE_STATES = ('open', 'blocked', 'done', 'dropped')
LIMITS = {'reminder': 8, 'followup': 5, 'promise': 12}
CONSENT_WINDOW = 20 * 60          # the user's words must be this recent
FOLLOWUP_HORIZON = 7 * 86400
REMINDER_LATE = 2 * 3600          # a missed reminder is still useful within two hours, then it expires
OFFER_WINDOW = 10 * 60            # a short "好呀" counts only right after her own offer to remind
NEGATIVE = re.compile(r'不要|别|不用|不需要|算了|取消|不必|没必要|免了|不想')
REQUEST = re.compile(r'提醒|叫我|喊我|叫醒|闹钟|记得.{0,6}我|催我')
OFFER = re.compile(r'提醒|叫你|喊你|叫醒|闹钟|催你')
# A short yes is consent only when it is plainly affirmative.
AFFIRM = re.compile(r'^(?:好|好的|好呀|好啊|好吧|好滴|好嘞|行|行啊|行吧|可以|可以呀|嗯|嗯嗯|要|要的|需要|拜托|麻烦你|麻烦了|ok|okay|没问题|来吧|叫吧|叫我)'
                    r'[!！~～。.呀啊哦嘛呢吧哈]*$', re.I)
CLOCK = re.compile(r'(?:今天|明天|后天|今晚|今早|明早|明晚)?(?:凌晨|早上|早晨|上午|中午|下午|晚上|夜里)?'
                   r'[零〇一二两三四五六七八九十\d]{1,3}(?:[:：]\d{1,2}|点(?:半|一刻|三刻|[零〇一二两三四五六七八九十\d]{1,3}分?)?)')
ACK = re.compile(r'收到|看到|看了|好看|喜欢|不错|可以|谢谢|满意|搞定|成功|做得|厉害|棒|能用|好用|确认')
DURATION = re.compile(r'[零〇一二两三四五六七八九十百\d.]+个?(?:小时半?|分钟|分|秒钟|秒)(?:[零〇一二两三四五六七八九十百\d.]+(?:分钟|分|秒))?|半小时|一刻钟')

RULE = ('qq_agenda是你自己的小本子，只在当前QQ会话生效：'
        '①kind=reminder：对方让你过多久/几点叫他提醒他，或你问“要不要X分钟后叫你”而对方答应了，才建；'
        'user_words必须原样抄对方要求提醒或答应的那句话，只是你自己提议、对方没答应或拒绝了就不能建；改提醒时间也要抄对方的原话。成功回执后才说“好，到时叫你”；'
        '失败就说没设好。到点系统会叫你发提醒。'
        '②kind=followup：你自己想“待会儿/晚上/明天再找对方聊某事”时记下，到点你会再想想要不要说，可以改主意。'
        '③kind=promise：你答应对方要做、要找、要学的事；做不到（没有对应工具）就把status设为blocked并写note；只有真做成了、而且对方确认收到或认可了，才设done（note写做成了什么，user_words抄对方确认的原话）。'
        'when原样传时间短语（如半小时后、明早七点、22:30），不要自己估算当前时间。改时间或取消用update/cancel。'
        '工具之外你没有计时能力，不要说“我记在心里到点叫你”。')


def _shares(a, b):
    """Do two texts share a two-character chunk (e.g. 表情包 / 表情)? Cheap topical link, not a semantic judgement."""
    a, b = _norm(a), _norm(b)
    return any(a[i:i + 2] in b for i in range(len(a) - 1))


def _norm(text):
    return re.sub(r'[\s，。！？!?,.~～…、"“”\'‘’]+', '', str(text or ''))


class Agenda:
    def __init__(self, service, now=time.time):
        self.s = service
        self.db = service.state.db
        self.now = now
        self.db.execute('''CREATE TABLE IF NOT EXISTS qq_agenda(
          id TEXT PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
          due REAL, content TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
          note TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL, receipt TEXT NOT NULL DEFAULT '')''')
        self.db.execute('CREATE INDEX IF NOT EXISTS qq_agenda_due ON qq_agenda(status,due)')
        if 'not_before' not in {r[1] for r in self.db.execute('PRAGMA table_info(qq_agenda)')}:
            self.db.execute('ALTER TABLE qq_agenda ADD COLUMN not_before REAL')

    # ---- model-facing tool ------------------------------------------------------------------------------
    def tool(self, session, action='list', kind='', when='', content='', user_words='', item_id='', status='', note=''):
        try:
            return {'success': True, 'content': self._tool(session, str(action or 'list'), str(kind or ''), str(when or ''),
                                                           str(content or ''), str(user_words or ''), str(item_id or ''),
                                                           str(status or ''), str(note or ''))}
        except ValueError as exc:
            return {'success': False, 'error': str(exc)[:160]}

    def _tool(self, session, action, kind, when, content, user_words, item_id, status, note):
        if not self.allowed(session):
            raise ValueError('这个会话不能使用qq_agenda')
        # All checks and writes of one call succeed or fail together.
        self.db.execute('SAVEPOINT qq_agenda_tool')
        try:
            result = self._act(session, action, kind, when, content, user_words, item_id, status, note)
        except BaseException:
            self.db.execute('ROLLBACK TO qq_agenda_tool')
            self.db.execute('RELEASE qq_agenda_tool')
            raise
        self.db.execute('RELEASE qq_agenda_tool')
        return result

    def _act(self, session, action, kind, when, content, user_words, item_id, status, note):
        now = self.now()
        if action == 'list':
            return {'now': memory_time.now_line(now), 'items': self.items(session, now)}
        if action == 'create':
            return self.create(session, kind, when, content, user_words, note, now, status)
        row = self._row(session, item_id)
        if action == 'cancel':
            if row['kind'] == 'promise':
                return self._set(row, 'dropped', note or '取消', now)
            if row['status'] != 'scheduled':
                raise ValueError('这条已经不在待办里了')
            return self._set(row, 'cancelled', note, now)
        if action == 'update':
            if when:
                if row['kind'] not in TIMED or row['status'] != 'scheduled':
                    raise ValueError('只有还没到点的提醒或打算可以改时间')
                due = self._due(row['kind'], when, now)
                if row['kind'] == 'reminder':                     # moving a reminder needs the same person's say-so
                    self.consent(session, user_words, now, due, str(row.get('origin') or '').partition(':')[2])
                self.db.execute('UPDATE qq_agenda SET due=?,not_before=NULL,updated=? WHERE id=?', (due, now, row['id']))
            if content:
                if row['kind'] == 'reminder':
                    raise ValueError('提醒内容是对方定的，不能改；需要的话取消后按对方的话重建')
                self._check_text(content)
                self.db.execute('UPDATE qq_agenda SET content=?,updated=? WHERE id=?', (content[:200], now, row['id']))
            if status:
                if row['kind'] != 'promise' or status not in PROMISE_STATES:
                    raise ValueError('status只用于promise：open/blocked/done/dropped')
                if status == 'done':
                    note = self.confirmed(session, user_words, note, now, row['content'])
                return self._set(self._row(session, row['id']), status, note, now)
            if note:
                self.db.execute('UPDATE qq_agenda SET note=?,updated=? WHERE id=?', (note[:200], now, row['id']))
            return self.public(self._row(session, row['id']), now)
        raise ValueError('action只能是create/list/update/cancel')

    def allowed(self, session):
        proactive = getattr(self.s, 'qq_proactive', None)
        try:
            return bool(proactive and any(d['session'] == session for d in proactive.destinations()))
        except Exception:
            return False

    def create(self, session, kind, when, content, user_words, note, now, status=''):
        if kind not in KINDS:
            raise ValueError('kind只能是reminder/followup/promise')
        self._check_text(content)
        if kind == 'promise' and status not in ('', 'open', 'blocked'):
            raise ValueError('新记的promise只能是open或blocked；做成了再update为done')
        active = self.db.execute("SELECT count(*) FROM qq_agenda WHERE session=? AND kind=? AND status IN ('scheduled','open','blocked')",
                                 (session, kind)).fetchone()[0]
        if active >= LIMITS[kind]:
            raise ValueError('这类待办已经太多了，先完成或取消旧的')
        due, origin, evidence = None, 'self', ''
        if kind in TIMED or when:
            due = self._due(kind, when, now)
        if kind == 'reminder':
            evidence, actor = self.consent(session, user_words, now, due)
            origin = 'user:' + actor          # who agreed; kept out of the model-facing view
        status = 'scheduled' if kind in TIMED else (status or 'open')
        # A retried tool call must not create a second copy of the same item.
        for row in self.db.execute("SELECT * FROM qq_agenda WHERE session=? AND kind=? AND content=? AND status IN ('scheduled','open','blocked')",
                                   (session, kind, content[:200])):
            if (row['due'] is None and due is None) or (row['due'] is not None and due is not None and abs(row['due'] - due) <= 120):
                return {**self.public(dict(row), now), 'duplicate': True}
        ident = 'ag:' + hashlib.sha256(json.dumps([session, kind, content, due, now]).encode()).hexdigest()[:20]
        self.db.execute('INSERT INTO qq_agenda(id,session,kind,status,due,content,origin,evidence,note,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (ident, session, kind, status, due, content[:200], origin, evidence, note[:200], now, now))
        return self.public(self._row(session, ident), now)

    def consent(self, session, user_words, now, due=None, actor_required=None):
        """A reminder exists only because the other person asked for it or agreed to her offer, on record.
        Accepted: a recent message of theirs that asks to be reminded, or a short yes that directly follows her own
        offer to remind. Refused: anything negative ("不要提醒我"), or a yes with no offer of hers right before it."""
        words = _norm(user_words)
        if not words:
            raise ValueError('提醒需要对方同意：user_words抄对方要求或答应的原话')
        account = str(self.s.policy.get('qq_bot_account', ''))
        for row in self.db.execute("SELECT payload,received_at FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? "
                                   "AND origin='external_user' AND active=1 AND received_at>=? ORDER BY received_at DESC LIMIT 40",
                                   (account, session, now - CONSENT_WINDOW)):
            event = json.loads(row['payload'])
            said = str(event.get('content') or '')
            if words not in _norm(said):
                continue
            if NEGATIVE.search(said):
                raise ValueError('对方这句话是拒绝或取消，不能据此设提醒')
            actor = str(event.get('actor_id') or '')
            if actor_required is not None and actor != actor_required:
                raise ValueError('只有当初要提醒的那个人才能改这条提醒')
            stamp = float(row['received_at'])
            if REQUEST.search(said):
                if due is not None and not self._time_matches(said, stamp, due):
                    raise ValueError('对方说的时间和你要设的时间对不上，按对方说的时间设')
                return said[:200], actor
            if AFFIRM.match(said.strip()) and self._offered(session, stamp, due, now):
                return said[:200], actor
            raise ValueError('这句不是要求提醒，也不是对你刚提出的提醒建议的明确答应；不能设提醒')
        raise ValueError('没找到对方最近说过这句话；对方没要求或没答应就不能设提醒')

    def confirmed(self, session, user_words, note, now, content=''):
        """'done' is not her own say-so: it needs the other person's recent words acknowledging the result
        (e.g. "收到了，挺好看"), quoted in user_words, plus what was done in note. The acknowledgement must be about this
        promise: it, or her own line it answers, shares wording with the promise."""
        if not note.strip():
            raise ValueError('设为done需要在note写清做成了什么')
        words = _norm(user_words)
        if len(words) < 2:
            raise ValueError('设为done需要对方确认：user_words抄对方确认收到或认可的原话；没有就保持open或blocked')
        account = str(self.s.policy.get('qq_bot_account', ''))
        for row in self.db.execute("SELECT payload,received_at FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? "
                                   "AND origin='external_user' AND active=1 AND received_at>=? ORDER BY received_at DESC LIMIT 40",
                                   (account, session, now - CONSENT_WINDOW)):
            said = str(json.loads(row['payload']).get('content') or '')
            if words in _norm(said):
                if NEGATIVE.search(said) or re.search(r'没|不对|还没|假的|骗', said) or not ACK.search(said):
                    raise ValueError('对方这句不是确认收到或认可')
                mine = self._last_line(session, float(row['received_at']), 1800)
                if not (_shares(content, said) or (mine and _shares(content, mine))):
                    raise ValueError('对方这句确认和这条承诺对不上')
                return (note.strip() + '｜对方确认：' + said)[:200]
        raise ValueError('没找到对方确认完成的原话；没有就保持open或blocked')

    @staticmethod
    def _time_matches(text, anchor, due):
        """When the words name a delay or a clock time, the reminder must fall at that moment, counted from when the
        words were said (anchor), not from when the tool runs. No time named -> no check."""
        targets = []
        for phrase in DURATION.findall(text):
            try:
                targets.append(parse_when(phrase + '后', anchor))
            except ValueError:
                continue
        for phrase in CLOCK.findall(text):
            try:
                targets.append(parse_when(phrase, anchor))
            except ValueError:
                continue
        return not targets or any(abs(t - due) <= 150 for t in targets)

    def _last_line(self, session, at, window):
        """Her latest delivered line before `at`, if nobody spoke in between."""
        account = str(self.s.policy.get('qq_bot_account', ''))
        for row in self.db.execute("SELECT payload,created_at FROM activities WHERE platform='qq' AND status='succeeded' AND created_at>=? AND created_at<? "
                                   "ORDER BY created_at DESC LIMIT 20", (at - window, at)):
            a = json.loads(row['payload'])
            if a.get('conversation_id') == session and a.get('kind') in ('reply', 'proactive', 'reminder'):
                between = self.db.execute("SELECT 1 FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? AND origin='external_user' "
                                          "AND active=1 AND received_at>? AND received_at<? LIMIT 1", (account, session, float(row['created_at']), at)).fetchone()
                return '' if between else str(a.get('content') or '')
        return ''

    def _offered(self, session, at, due=None, now=None):
        """The yes answers her offer: her latest delivered line before it (within 10 minutes, nobody else speaking in
        between) offered to remind them, and when that offer named a delay, it matches the reminder being set."""
        account = str(self.s.policy.get('qq_bot_account', ''))
        mine = None
        for row in self.db.execute("SELECT payload,created_at FROM activities WHERE platform='qq' AND status='succeeded' AND created_at>=? AND created_at<? "
                                   "ORDER BY created_at DESC LIMIT 20", (at - OFFER_WINDOW, at)):
            a = json.loads(row['payload'])
            if a.get('conversation_id') == session and a.get('kind') in ('reply', 'proactive', 'reminder'):
                mine = (float(row['created_at']), str(a.get('content') or ''))
                break
        if not mine or not OFFER.search(mine[1]):
            return False
        between = self.db.execute("SELECT count(*) FROM events WHERE platform='qq' AND bot_account_id=? AND conversation_id=? AND origin='external_user' "
                                  "AND active=1 AND received_at>? AND received_at<?", (account, session, mine[0], at)).fetchone()[0]
        if between:
            return False
        if due is not None and not self._time_matches(mine[1], at, due):      # counted from their yes
            return False                      # she offered 20 minutes; a one-hour reminder is not what they agreed to
        return True

    def _due(self, kind, when, now):
        if not when:
            raise ValueError('需要when，例如半小时后、明早七点、22:30')
        due = parse_when(when, now)
        if kind == 'followup' and not 600 <= due - now <= FOLLOWUP_HORIZON:
            raise ValueError('自己的打算请定在10分钟到7天之内')
        return due

    @staticmethod
    def _check_text(content):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 200:
            raise ValueError('content需要1到200字')
        if content_rules.blocked(content):
            raise ValueError('内容触及固定话题限制')

    def _row(self, session, item_id):
        if item_id:
            row = self.db.execute('SELECT * FROM qq_agenda WHERE id=? AND session=?', (item_id, session)).fetchone()
        else:
            rows = self.db.execute("SELECT * FROM qq_agenda WHERE session=? AND status IN ('scheduled','open','blocked')", (session,)).fetchall()
            row = rows[0] if len(rows) == 1 else None
            if len(rows) > 1:
                raise ValueError('有多条待办，请先list再带item_id')
        if row is None:
            raise ValueError('找不到这条待办')
        return dict(row)

    def _set(self, row, status, note, now):
        self.db.execute('UPDATE qq_agenda SET status=?,note=?,updated=? WHERE id=?',
                        (status, (note or row.get('note') or '')[:200], now, row['id']))
        return self.public(self._row(row['session'], row['id']), now)

    @staticmethod
    def public(row, now=None):
        out = {'id': row['id'], 'kind': row['kind'], 'status': row['status'], 'content': row['content']}
        if row.get('due'):
            out['when'] = memory_time.relative(row['due'], now) or ''
        if row.get('note'):
            out['note'] = row['note']
        if str(row.get('origin') or '').startswith('user'):
            out['user_asked'] = row.get('evidence', '')[:60]
        return out

    def items(self, session, now, recent_hours=48):
        """Open items first (never crowded out), then recently settled ones, so she knows what happened to her plans."""
        rows = self.db.execute("SELECT * FROM qq_agenda WHERE session=? AND kind='promise' AND status IN ('open','blocked') "
                               "ORDER BY updated DESC LIMIT 6", (session,)).fetchall()
        rows += self.db.execute("SELECT * FROM qq_agenda WHERE session=? AND kind IN ('reminder','followup') AND status='scheduled' "
                                "ORDER BY due LIMIT 6", (session,)).fetchall()
        rows += self.db.execute("SELECT * FROM qq_agenda WHERE session=? AND status NOT IN ('scheduled','open','blocked') AND updated>=? "
                                "ORDER BY updated DESC LIMIT 4", (session, now - recent_hours * 3600)).fetchall()
        return [self.public(dict(r), now) for r in rows]

    def context(self, session, now=None):
        """Short block for QQ replies and proactive chat. Empty when nothing is on the list."""
        now = self.now() if now is None else now
        items = self.items(session, now)
        if not items:
            return ''
        return ('\n【你的小本子qq_agenda】' + json.dumps(items, ensure_ascii=False) +
                '\n只有done才算做成了；blocked表示目前做不到，不能说成在做或做好了；delivered/sent表示已经发过。')

    # ---- delivery (called by the proactive loop) --------------------------------------------------------
    def due(self, sessions, now):
        """Next due timed item among the allowed sessions; reminders first."""
        if not sessions:
            return None
        marks = ','.join('?' * len(sessions))
        row = self.db.execute(f"SELECT * FROM qq_agenda WHERE status='scheduled' AND due<=? AND (not_before IS NULL OR not_before<=?) "
                              f"AND session IN ({marks}) ORDER BY CASE kind WHEN 'reminder' THEN 0 ELSE 1 END, due LIMIT 1",
                              (now, now, *sessions)).fetchone()
        return dict(row) if row else None

    def claim(self, item_id, now):
        """Atomically move a due item out of 'scheduled' so it can never be delivered twice."""
        cur = self.db.execute("UPDATE qq_agenda SET status='sending',updated=? WHERE id=? AND status='scheduled'", (now, item_id))
        return cur.rowcount == 1

    def finish(self, item_id, status, now, receipt='', note=''):
        self.db.execute('UPDATE qq_agenda SET status=?,receipt=?,note=CASE WHEN ?<>\'\' THEN ? ELSE note END,updated=? WHERE id=?',
                        (status, receipt[:200], note, note[:200], now, item_id))

    def postpone(self, item_id, not_before, now):
        """Wait without moving the original due time, so the expiry limit still counts from the planned moment."""
        self.db.execute("UPDATE qq_agenda SET status='scheduled',not_before=?,updated=? WHERE id=?", (not_before, now, item_id))

    def seed_promise(self, session, content, status, note, marker, now=None):
        """Migration helper: idempotent, tagged so a rollback removes only its own rows."""
        now = self.now() if now is None else now
        ident = 'ag:seed:' + hashlib.sha256((marker + session + content).encode()).hexdigest()[:16]
        self.db.execute('INSERT OR IGNORE INTO qq_agenda(id,session,kind,status,due,content,origin,evidence,note,created,updated) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (ident, session, 'promise', status, None, content, marker, '', note, now, now))
        return ident
