"""Durable executor state: single-instance lock, operation ledger, dedupe, budgets.

A network write is only attempted after reserve() committed a 'sending' row.
Rows left 'sending' by a crash become 'unknown' at the next start and are never
re-sent. Counters include failed and unknown attempts. No secret, xsec_token,
Cookie or image path is stored; images are recorded by manifest id only.
"""
import fcntl, hashlib, json, os, sqlite3, threading, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
ACTIVE = ('sending', 'accepted', 'unknown')
SAFE_FAILED = ('failed', 'failed_not_sent')


class AlreadyRunning(RuntimeError):
    pass


def digest(text):
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def beijing_day(stamp):
    return datetime.fromtimestamp(stamp, TZ).strftime('%Y-%m-%d')


def beijing_hour(stamp):
    return datetime.fromtimestamp(stamp, TZ).hour


class Store:
    def __init__(self, state_dir, now=time.time):
        self.root = Path(state_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.now = now
        self._lock = threading.RLock()
        self._instance = open(self.root / 'instance.lock', 'a')
        try:
            fcntl.flock(self._instance.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._instance.close()
            raise AlreadyRunning('xhs_executor_already_running') from None
        self.db = sqlite3.connect(self.root / 'state.sqlite', isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL, day TEXT NOT NULL,
              status TEXT NOT NULL, verification TEXT NOT NULL DEFAULT 'none',
              feed_id TEXT NOT NULL DEFAULT '', comment_id TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
              content_hash TEXT NOT NULL, content TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
              image_id TEXT NOT NULL DEFAULT '', remote_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
              target_key TEXT NOT NULL, dedupe_key TEXT UNIQUE, pre_ids TEXT NOT NULL DEFAULT '[]',
              sent_at REAL, persona_version TEXT NOT NULL DEFAULT '', source_ids TEXT NOT NULL DEFAULT '[]',
              manual INTEGER NOT NULL DEFAULT 0, audience TEXT NOT NULL DEFAULT 'public', tags TEXT NOT NULL DEFAULT '[]', rev INTEGER NOT NULL DEFAULT 1, synced_rev INTEGER NOT NULL DEFAULT 0,
              draft_id TEXT NOT NULL DEFAULT '', claim_token TEXT NOT NULL DEFAULT '', reported INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS actions_day ON actions(kind, day);
            CREATE TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY, created REAL NOT NULL, outcome TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, day TEXT NOT NULL, task TEXT NOT NULL);
        ''')
        self._migrate()
        os.chmod(self.root / 'state.sqlite', 0o600)
        # Target xsec_tokens for approved-later sends live ONLY in this separate private file: never in the ledger,
        # never sent to the core, the model or QQ, and purged when they expire or the draft is finished.
        self.tokens = sqlite3.connect(self.root / 'tokens.sqlite', isolation_level=None, check_same_thread=False, timeout=10)
        self.tokens.executescript('CREATE TABLE IF NOT EXISTS draft_tokens(draft_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL);')
        os.chmod(self.root / 'tokens.sqlite', 0o600)
        # A crash between reserve and result: the write may have happened.
        self.db.execute("UPDATE actions SET status='unknown', detail='process_interrupted', rev=rev+1 WHERE status='sending'")

    # Columns added after the first deployments. CREATE TABLE IF NOT EXISTS never alters an existing table, so an
    # older state.sqlite is upgraded in place: idempotent, rows / seen marks / budgets are kept untouched.
    MIGRATIONS = (('audience', "TEXT NOT NULL DEFAULT 'public'"), ('tags', "TEXT NOT NULL DEFAULT '[]'"),
                  ('draft_id', "TEXT NOT NULL DEFAULT ''"), ('claim_token', "TEXT NOT NULL DEFAULT ''"),
                  ('reported', 'INTEGER NOT NULL DEFAULT 1'), ('retry_of', 'TEXT'))

    def _migrate(self):
        have = {row['name'] for row in self.db.execute('PRAGMA table_info(actions)')}
        for column, definition in self.MIGRATIONS:
            if column not in have:
                self.db.execute('ALTER TABLE actions ADD COLUMN %s %s' % (column, definition))
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS actions_retry_once ON actions(retry_of) WHERE retry_of IS NOT NULL')

    # --- target tokens (private file) -----------------------------------------------------------------
    # The token belongs to the NOTE, so it is keyed by feed id (the column keeps its original name `draft_id`) and is
    # shared by every draft on that note. It is never deleted because one draft finished or was refused (that would
    # strand the other pending drafts); it simply expires after a short TTL, and a newer token for the same note
    # replaces the older one.
    def put_token(self, feed_id, token, ttl=26 * 3600):
        with self._lock:
            self.tokens.execute('DELETE FROM draft_tokens WHERE expires<?', (self.now(),))
            self.tokens.execute('INSERT OR REPLACE INTO draft_tokens VALUES(?,?,?)', (feed_id, token, self.now() + ttl))

    def get_token(self, feed_id):
        with self._lock:
            self.tokens.execute('DELETE FROM draft_tokens WHERE expires<?', (self.now(),))
            row = self.tokens.execute('SELECT token FROM draft_tokens WHERE draft_id=?', (feed_id,)).fetchone()
            return row[0] if row else ''

    def is_private_feed(self, feed_id):
        """Only a note this executor itself created as self-only AND read back exactly on the account's own page.
        There is no way to register an arbitrary note: nothing else is provably private."""
        if not feed_id:
            return False
        with self._lock:
            return self.db.execute("SELECT 1 FROM actions WHERE kind='post' AND audience='self_only' AND status='accepted' "
                                   "AND verification='own_page_exact' AND remote_id=?", (feed_id,)).fetchone() is not None

    def unreported(self, limit=5):
        """Draft rows whose outcome the core has not heard yet (or can now hear as verified)."""
        return self.actions("draft_id!='' AND status!='sending' AND (reported=0 OR (reported=1 AND status='accepted' AND verification='own_page_exact'))", limit=limit)

    def mark_reported(self, action_id, level):
        with self._lock:
            self.db.execute('UPDATE actions SET reported=? WHERE id=?', (level, action_id))

    def close(self):
        with self._lock:
            self.tokens.close()
            self.db.close()
            if not self._instance.closed:
                fcntl.flock(self._instance.fileno(), fcntl.LOCK_UN)
                self._instance.close()

    # --- meta --------------------------------------------------------------------
    def get(self, key, default=None):
        with self._lock:
            row = self.db.execute('SELECT v FROM meta WHERE k=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self._lock:
            self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (key, json.dumps(value, ensure_ascii=False)))

    # --- seen / dedupe -----------------------------------------------------------
    def is_seen(self, key):
        with self._lock:
            return self.db.execute('SELECT 1 FROM seen WHERE k=?', (key,)).fetchone() is not None

    def mark_seen(self, key, outcome):
        with self._lock:
            self.db.execute('INSERT OR IGNORE INTO seen VALUES(?,?,?)', (key, self.now(), outcome[:40]))

    # --- budgets -----------------------------------------------------------------
    def model_calls_today(self):
        with self._lock:
            return self.db.execute('SELECT count(*) FROM model_calls WHERE day=?', (beijing_day(self.now()),)).fetchone()[0]

    def reserve_model_call(self, task, limit, cost=1):
        """Counted before the call (a post draft is followed by a review call, so it reserves 2);
        failures and timeouts consume budget too."""
        with self._lock:
            if self.model_calls_today() + cost > limit:
                return False
            for _ in range(cost):
                self.db.execute('INSERT INTO model_calls(created,day,task) VALUES(?,?,?)', (self.now(), beijing_day(self.now()), task))
            return True

    def image_recently_used(self, image_id, days):
        """True if a post attempt that may have reached the platform used this manifest image in the window.
        unknown and in-flight attempts occupy it too; attempts known not to have been performed do not."""
        if not image_id or days <= 0:
            return False
        with self._lock:
            return self.db.execute("SELECT 1 FROM actions WHERE kind='post' AND image_id=? AND created>? "
                                   "AND status IN ('sending','accepted','unknown')",
                                   (image_id, self.now() - days * 86400)).fetchone() is not None

    def discard(self, action_id):
        """Drop a reservation whose network write was never attempted (gated at the last moment)."""
        with self._lock:
            row = self.action(action_id)
            if self.db.execute("DELETE FROM actions WHERE id=? AND status='sending'", (action_id,)).rowcount and row:
                self._restore_retry_guard(row)

    def _restore_retry_guard(self, row):
        for _ in range(4):
            row = self.action(row.get('retry_of')) if row.get('retry_of') else None
            if not row:
                return
            if row['status'] in ('unknown', 'accepted'):
                self.db.execute("UPDATE actions SET dedupe_key=target_key WHERE id=? AND dedupe_key IS NULL "
                                "AND NOT EXISTS (SELECT 1 FROM actions a WHERE a.dedupe_key=actions.target_key)", (row['id'],))
                return

    def add_browse(self):
        self.put('browse:' + beijing_day(self.now()), self.count_browses() + 1)

    def count_actions(self, kind, day=None):
        with self._lock:
            return self.db.execute('SELECT count(*) FROM actions WHERE kind=? AND day=?', (kind, day or beijing_day(self.now()))).fetchone()[0]

    def count_browses(self):
        return self.get('browse:' + beijing_day(self.now()), 0)

    def reserve(self, kind, *, feed_id='', comment_id='', author='', content, title='', image_id='', pre_ids=(),
                limits, persona_version='', source_ids=(), manual=False, audience='public', tags=(), enforce_image_reuse=False, draft_id='', claim_token='', retry_of=''):
        """Commit the intent (and budget) before any network write. Returns (id, reason)."""
        now = self.now()
        day = beijing_day(now)
        content_hash = digest(json.dumps([kind, title, content, image_id], ensure_ascii=False))
        target = comment_id if kind == 'reply' else feed_id if kind == 'comment' else content_hash
        target_key = kind + ':' + target
        action_id = 'xhs-' + digest('%s|%s|%s' % (target_key, now, os.urandom(8).hex()))[:40]
        plural = {'post': 'daily_posts', 'comment': 'daily_comments', 'reply': 'daily_replies'}[kind]
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                reason = ''
                previous = self.action(retry_of) if retry_of else None
                if retry_of and (not previous or not draft_id or not claim_token or previous['draft_id'] != draft_id
                        or previous['target_key'] != target_key or previous['content_hash'] != content_hash or previous['claim_token'] == claim_token
                        or previous['verification'] in ('own_page_exact', 'same_thread') or previous['status'] not in ('unknown','accepted','failed','failed_not_sent')
                        or self.db.execute('SELECT 1 FROM actions WHERE retry_of=?', (retry_of,)).fetchone()):
                    self.db.execute('ROLLBACK')
                    return None, 'retry_not_authorized'
                ancestors = set()
                ancestor = previous
                for _ in range(4):
                    if not ancestor:
                        break
                    if (ancestor['draft_id'] != draft_id or ancestor['target_key'] != target_key or ancestor['content_hash'] != content_hash
                            or ancestor['verification'] in ('own_page_exact', 'same_thread')):
                        self.db.execute('ROLLBACK')
                        return None, 'retry_history_mismatch'
                    ancestors.add(ancestor['id'])
                    ancestor = self.action(ancestor['retry_of']) if ancestor.get('retry_of') else None
                row = self.db.execute('SELECT id FROM actions WHERE dedupe_key=?', (target_key,)).fetchone()
                if row and row['id'] not in ancestors:
                    reason = 'duplicate_target'
                elif kind == 'post' and enforce_image_reuse and self.image_recently_used(image_id, limits.get('image_reuse_days', 0)):
                    reason = 'image_reused'  # checked inside the reservation transaction, so it cannot race
                elif self.db.execute("SELECT count(*) FROM actions WHERE target_key=? AND status IN ('failed','failed_not_sent') AND day=?",
                                     (target_key, day)).fetchone()[0] >= 2:
                    reason = 'retry_limit'
                elif self.db.execute("SELECT 1 FROM actions WHERE kind=? AND content_hash=? AND created>? AND status NOT IN ('failed','failed_not_sent') AND (?='' OR draft_id!=?)", (kind, content_hash, now - 30 * 86400, retry_of, draft_id)).fetchone():
                    reason = 'duplicate_content'
                elif self.db.execute('SELECT count(*) FROM actions WHERE kind=? AND day=?', (kind, day)).fetchone()[0] >= limits[plural]:
                    reason = 'daily_limit'
                elif not manual and (latest := self.db.execute('SELECT max(created) FROM actions WHERE kind=?', (kind,)).fetchone()[0]) is not None \
                        and now - latest < limits['min_gap_' + kind]:
                    reason = 'min_gap'
                elif kind != 'post' and self.db.execute("SELECT count(*) FROM actions WHERE kind IN ('comment','reply') AND feed_id=? AND day=?", (feed_id, day)).fetchone()[0] >= limits['per_thread_daily']:
                    reason = 'thread_limit'
                elif kind != 'post' and author and self.db.execute("SELECT count(*) FROM actions WHERE author=? AND day=?", (author, day)).fetchone()[0] >= limits['per_author_daily']:
                    reason = 'author_limit'
                if reason:
                    self.db.execute('ROLLBACK')
                    return None, reason
                if previous:
                    self.db.execute('UPDATE actions SET dedupe_key=NULL WHERE dedupe_key=?', (target_key,))
                self.db.execute('INSERT INTO actions(id,kind,created,day,status,feed_id,comment_id,author,content_hash,content,title,image_id,'
                                'target_key,dedupe_key,pre_ids,sent_at,persona_version,source_ids,manual,audience,tags,draft_id,claim_token,reported,retry_of) '
                                "VALUES(?,?,?,?,'sending',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (action_id, kind, now, day, feed_id, comment_id, author, content_hash, content, title, image_id,
                                 target_key, target_key, json.dumps(list(pre_ids)[:200]), now, str(persona_version),
                                 json.dumps(list(source_ids)[:25]), int(manual), audience, json.dumps(list(tags)[:3], ensure_ascii=False),
                                 draft_id, claim_token, 0 if draft_id else 1, retry_of or None))
                self.db.execute('COMMIT')
                return action_id, ''
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise

    def finish(self, action_id, status, *, detail='', remote_id=None, verification=None):
        with self._lock:
            sets = ['status=?', 'detail=?', 'rev=rev+1']
            args = [status, detail[:200]]
            if remote_id:
                sets.append('remote_id=?')
                args.append(str(remote_id)[:128])
            if verification:
                sets.append('verification=?')
                args.append(verification)
            if status in SAFE_FAILED:  # definitely not performed: the target may be tried again within the retry cap
                sets.append('dedupe_key=NULL')
            self.db.execute('UPDATE actions SET ' + ', '.join(sets) + ' WHERE id=?', (*args, action_id))
            if status in SAFE_FAILED:
                self._restore_retry_guard(self.action(action_id) or {})

    def action(self, action_id):
        with self._lock:
            row = self.db.execute('SELECT * FROM actions WHERE id=?', (action_id,)).fetchone()
            return dict(row) if row else None

    def actions(self, where='1=1', args=(), limit=50):
        with self._lock:
            return [dict(r) for r in self.db.execute('SELECT * FROM actions WHERE ' + where + ' ORDER BY created DESC LIMIT ?', (*args, limit))]

    def unsynced(self, limit=5):
        return self.actions("synced_rev<rev AND status!='sending'", limit=limit)

    def mark_synced(self, action_id, rev):
        with self._lock:
            self.db.execute('UPDATE actions SET synced_rev=? WHERE id=? AND synced_rev<?', (rev, action_id, rev))

    def recent_texts(self, limit=6):
        return [r['content'] for r in self.actions("kind IN ('post','comment','reply') AND status!='failed_not_sent'", limit=limit)]
