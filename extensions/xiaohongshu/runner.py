"""Xiaohongshu executor: identity binding, notification replies, autonomy, ledgered writes.

All dependencies are injected so the whole path runs offline. Rules enforced here,
not left to the model: identity binding, switches, quotas, gaps, per-thread/author
limits, dedupe, notification baseline, backoff, pause and no automatic re-send of
an unknown write.
"""
import base64, binascii, hashlib, json, os, random, re, threading, time, uuid
from pathlib import Path
from .core_client import CoreRejected, CoreUnavailable
from .http_client import Ambiguous, NotSent, Rejected, ServiceError
from . import textcard
from .config import VISIBILITIES
from .media import MediaError
from .store import beijing_hour, digest

TEXT_CARD = 'textcard'
CARD_CAPTION = textcard.CAPTION

SAFE_ID = re.compile(r'[A-Za-z0-9_-]{6,64}')
ACTOR = re.compile(r'[A-Za-z0-9_.:-]{1,200}')
BAD_TEXT = re.compile(r'https?://|www\.|[@＠]|xsec|cookie|authorization|bearer', re.I)
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
KIND_SWITCH = {'post': 'publish_enabled', 'comment': 'comment_enabled', 'reply': 'reply_enabled'}
TEXT_LIMIT = {'comment': 300, 'reply': 300}
DEFERRED = ('daily_limit', 'min_gap')
MAX_PER_POLL = 3


MODEL_COST = {'post': 2, 'intro': 2}   # draft + review
# Budget a whole autonomous attempt needs before it starts: select + (draft [+ review]).
ATTEMPT_COST = {'post': 3, 'comment': 2, 'reply': 1}
TEXT_CARD_ID = 'textcard'
DEFERRED_DRAFT = ('draft_queue_full', 'token_store_failed')   # waiting states: the message stays unhandled for a later poll


def claim_hash(claim):
    """Recompute the approval binding from the claim's own fields (same formula as the core). Any tampered field,
    other account or other target gives a different value than the hash the owner approved."""
    t = claim['target']
    raw = json.dumps([claim['bot_account_id'], claim['kind'], claim['audience'], claim['title'], claim['content'], list(claim['tags']),
                      claim['image_id'], claim['media_hash'], t['feed_id'], t['comment_id'], t['author_id'], t['comment_hash']],
                     ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def card_spec_hash(title, content):
    """Same rule as the core: a text card is bound to template version + title + first sentence."""
    line = re.split(r'[。！？!?\n]', content, maxsplit=1)[0][:60]
    return hashlib.sha256(json.dumps(['textcard-v1', title.strip()[:20], line.strip()], ensure_ascii=False).encode()).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()
RETRY_LIMIT = 3
RETRY_DELAY = 600


class Skip(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Defer(Skip):
    """Recoverable: do not mark the notification handled; retry later, a bounded number of times."""


def valid_text(text, limit):
    if not isinstance(text, str) or CONTROL.search(text):
        raise Skip('invalid_text')
    text = text.strip()
    if not 1 <= len(text) <= limit or BAD_TEXT.search(text):
        raise Skip('invalid_text')
    return text


def millis(value):
    return value / 1000 if isinstance(value, (int, float)) and value > 1e11 else float(value or 0)


def norm(text):
    return re.sub(r'\s+', '', str(text or ''))


def same_title(a, b):
    """The platform may show a title with stray leading/trailing whitespace; nothing looser than that."""
    return str(a or '').strip() == str(b or '').strip()


def body_without_tags(desc, tags):
    """Normalised note body with only the known tags' display forms removed (#tag, #tag[话题]#)."""
    text = str(desc or '')
    for tag in tags:
        text = re.sub(r'#' + re.escape(tag) + r'(?:\[话题\])?#?', '', text)
    return norm(text)


def flatten(comments):
    """[(comment, parent_id)] over top-level comments and their loaded sub-comments."""
    out = []
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        out.append((c, ''))
        for sub in c.get('subComments') or []:
            if isinstance(sub, dict):
                out.append((sub, str(c.get('id') or '')))
    return out


class Executor:
    # Human review is mandatory: nothing public is sent without a claimed, owner-approved draft. Only a test subclass
    # sets this True to exercise the low-level send mechanics; the production class and the CLI never do.
    ALLOW_DIRECT = False

    def __init__(self, cfg, http, core, store, media, now=time.time, rand=random.random):
        self.cfg, self.http, self.core, self.store, self.media = cfg, http, core, store, media
        self.now, self.rand = now, rand
        self.limits = cfg['limits']
        self._lock = threading.RLock()
        self._refs = {}      # ref -> (feed_id, xsec_token): memory only, never persisted or sent to the model
        self._tokens = {}    # feed_id -> (xsec_token, expiry): memory only, for readback

    # ---- gates ---------------------------------------------------------------------
    def apply_requests(self):
        """Operator `resume` drops a RESUME file; the single writer applies it."""
        request = self.store.root / 'RESUME'
        if request.exists():
            request.unlink(missing_ok=True)
            self.resume()

    def paused_reason(self):
        if (self.store.root / 'PAUSE').exists():
            return 'pause_file'
        return self.store.get('paused', '')

    def gate(self, kind=None):
        if not self.cfg['enabled']:
            return 'disabled'
        if kind and not self.cfg[KIND_SWITCH.get(kind, kind)]:
            return 'kind_disabled'
        reason = self.paused_reason()
        if reason:
            return 'paused:' + reason
        if self.store.get('backoff_until', 0) > self.now():
            return 'backoff'
        return ''

    def pause(self, reason='manual'):
        self.store.put('paused', reason)

    def resume(self):
        self.store.put('paused', '')
        self.store.put('backoff_until', 0)
        self.store.put('backoff_level', 0)
        self.store.put('identity', None)  # the next write re-checks the logged-in account
        return {'status': 'resumed', 'pause_file_present': (self.store.root / 'PAUSE').exists()}

    def _on_error(self, exc):
        if getattr(exc, 'challenge', False) or (isinstance(exc, Rejected) and exc.status in (401, 403)):
            self.pause('platform_challenge_or_auth')
            return 'paused'
        level = self.store.get('backoff_level', 0)
        self.store.put('backoff_level', min(level + 1, 6))
        self.store.put('backoff_until', self.now() + min(3600, 60 * 2 ** level))
        return 'backoff'

    def _ok_read(self):
        if self.store.get('backoff_level', 0):
            self.store.put('backoff_level', 0)

    # ---- identity ------------------------------------------------------------------
    def identity(self, force=False):
        """The logged-in account must equal the configured UID. Nickname never authorizes."""
        bound = self.cfg['bound_uid']
        if not bound:
            return {'ok': False, 'reason': 'unbound'}
        cached = self.store.get('identity')
        if (not force and cached and cached.get('ok') and cached.get('bound') == bound
                and self.now() - cached['at'] < self.limits['identity_ttl_seconds']):
            return cached
        try:
            status = self.http.login_status()
        except ServiceError as exc:
            self._on_error(exc)
            return {'ok': False, 'reason': 'status_unavailable'}
        self._ok_read()
        uid = status.get('user_id') if isinstance(status, dict) else None
        if not isinstance(status, dict) or status.get('is_logged_in') is not True:
            result = {'ok': False, 'reason': 'not_logged_in'}
        elif not isinstance(uid, str) or not uid:
            result = {'ok': False, 'reason': 'uid_unknown'}
        elif uid != bound:
            self.pause('identity_mismatch')
            result = {'ok': False, 'reason': 'uid_mismatch'}
        else:
            result = {'ok': True, 'reason': '', 'bound': bound, 'at': self.now()}
        self.store.put('identity', result if result['ok'] else None)
        return result

    # ---- core helpers -----------------------------------------------------------------
    def _plan(self, task, material, *, candidates=None, budget=None, conversation='xhs:global'):
        # A post draft is followed by a second (review) model call in the core: reserve both up front.
        if not self.store.reserve_model_call(task, self.limits['daily_model_calls'], cost=MODEL_COST.get(task, 1)):
            raise Skip('model_daily_limit')
        request = {'task': task, 'cycle': uuid.uuid4().hex + uuid.uuid4().hex, 'bot_account_id': self.cfg['bound_uid'],
                   'conversation_id': conversation, 'material': material, 'candidates': candidates or {},
                   'budget': budget or {}, 'history': [t[:300] for t in self.store.recent_texts(6)]}
        return self.core.plan(request)

    def _event(self, kind, **fields):
        event = {'platform': 'xiaohongshu', 'bot_account_id': self.cfg['bound_uid'], 'event_type': kind,
                 'occurred_at': min(self.now(), fields.pop('occurred_at', self.now())), **fields}
        try:
            self.core.event(event)
        except CoreUnavailable:
            self.store.put('last_event_error', self.now())  # the plan call decides availability

    # ---- read helpers ----------------------------------------------------------------
    def _detail(self, feed_id, token, deep=False):
        detail = self.http.feed_detail(feed_id, token, 100, deep=True) if deep else self.http.feed_detail(feed_id, token, self.limits['max_comments_read'])
        self._tokens[feed_id] = (token, self.now() + 6 * 3600)
        note = detail.get('note') if isinstance(detail, dict) else None
        if not isinstance(note, dict):
            raise Skip('feed_unreadable')
        meta = detail.get('comments') or {}
        roots = meta.get('list') or []
        comments = flatten(roots)
        self._last_readback_complete = (meta.get('hasMore') is False and len(comments) < 100
            and all(not c.get('subCommentHasMore') and int(c.get('subCommentCount') or 0) <= len(c.get('subComments') or []) for c in roots))
        self._last_readback_detail = (note, comments)
        return note, comments

    # ---- write flow ---------------------------------------------------------------------
    def _write(self, kind, *, fn, **reserve_args):
        """reserve -> (network write) -> classify -> verify -> sync. Never re-sends."""
        # Final gate: pause/switches may have changed while drafting or verifying identity.
        # (Live controls: PAUSE/RESUME files and stored pause state. Switches and limits come from the
        # config loaded at process start; changing them needs a service restart.)
        blocked = self.gate(kind)
        if blocked:
            raise Skip('gated:' + blocked)
        action_id, reason = self.store.reserve(kind, limits=self.limits, **reserve_args)
        if not action_id:
            raise Skip(reason)
        blocked = self.gate(kind)  # again, immediately before the network write
        if blocked:
            self.store.discard(action_id)  # never sent: not an unknown outcome, no budget consumed
            raise Skip('gated:' + blocked)
        try:
            fn()
        except NotSent as exc:
            self.store.finish(action_id, 'failed_not_sent', detail=exc.kind if exc.kind.startswith('reply_not_sent:') else 'not_sent')
        except Rejected as exc:
            self.store.finish(action_id, 'failed', detail='rejected_%s' % exc.status)
            self._on_error(exc)
        except Ambiguous as exc:
            self.store.finish(action_id, 'unknown', detail=exc.kind)
            self._on_error(exc)
        except Exception:
            self.store.finish(action_id, 'unknown', detail='unexpected_error')
        else:
            # The tool's success only means it accepted the request / saw it on this account's page.
            self.store.finish(action_id, 'accepted', detail='tool_reported_success', verification='pending')
        self.verify(action_id)
        self.sync()
        return self.store.action(action_id)

    def verify(self, action_id, force=False, deep=False):
        action = self.store.action(action_id)
        if (not action or action['status'] not in ('accepted', 'unknown')
                or action['verification'] in ('own_page_exact', 'same_thread')):
            return None
        key = 'verify_try:' + action_id
        tries = self.store.get(key, 0)
        if tries >= 6 and not force:
            return None
        self.store.put(key, tries + 1)
        try:
            found = self._readback_post(action) if action['kind'] == 'post' else self._readback_comment(action, deep=deep or tries >= 3)
        except (ServiceError, Skip):
            return None
        if found:
            exact = not found.startswith('thread:')
            # The upstream Comment structure has no reply-to field, so a reply to a sub-comment can be
            # read back in its thread but its exact target cannot be proven: tool acceptance only.
            self.store.finish(action_id, 'accepted', detail='own_page_exact_readback' if exact else 'same_thread_readback',
                              remote_id=found.replace('thread:', '', 1), verification='own_page_exact' if exact else 'same_thread')
        return found

    def _readback_post(self, action):
        uid = self.cfg['bound_uid']
        feeds = (self.http.me().get('data') or {}).get('feeds') or []
        pre = set(json.loads(action['pre_ids']))
        matches = []
        for feed in feeds:
            card = feed.get('noteCard') or {}
            if feed.get('id') in pre or not same_title(card.get('displayTitle'), action['title']) or not SAFE_ID.fullmatch(str(feed.get('id'))):
                continue
            note, _ = self._detail(feed['id'], feed.get('xsecToken', ''))
            if ((note.get('user') or {}).get('userId') == uid and same_title(note.get('title'), action['title'])
                    and norm(action['content']) == body_without_tags(note.get('desc'), json.loads(action['tags']))
                    and millis(note.get('time')) >= action['sent_at'] - 300):
                matches.append(feed['id'])
        return matches[0] if len(matches) == 1 else None

    def _readback_comment(self, action, deep=False):
        cached = self._tokens.get(action['feed_id'])
        if cached and cached[1] >= self.now():
            token = cached[0]
        else:
            # After a restart: the short-lived reference in the private token file (never the ledger).
            try:
                token = self.store.get_token(action['feed_id'])
            except Exception:
                token = ''
        if not token:
            raise Skip('token_unavailable')
        _, comments = self._detail(action['feed_id'], token, deep=deep)
        if action['kind'] == 'reply' and deep:
            target = next(((c, p) for c, p in comments if str(c.get('id')) == action['comment_id']), None)
            root_id = (target[1] or str(target[0].get('id'))) if target else ''
            root = next((c for c, p in comments if not p and str(c.get('id')) == root_id), None)
            self._last_readback_complete = bool(root and not root.get('subCommentHasMore')
                and int(root.get('subCommentCount') or 0) <= len(root.get('subComments') or []))
        pre = set(json.loads(action['pre_ids']))
        uid = self.cfg['bound_uid']
        hits = []
        for c, parent in comments:
            if (str(c.get('id')) in pre or c.get('content') != action['content'] or (c.get('userInfo') or {}).get('userId') != uid
                    or millis(c.get('createTime')) < action['sent_at'] - 300):
                continue
            if action['kind'] == 'comment' and parent:
                continue  # a top-level comment must read back as a top-level comment
            marker = ''
            if action['kind'] == 'reply':
                target = next(((cc, p) for cc, p in comments if str(cc.get('id')) == action['comment_id']), None)
                if target is None:
                    continue
                target_parent = target[1]
                if not target_parent:
                    if parent != action['comment_id']:  # direct child of the (root) target: exactly verifiable
                        continue
                else:
                    if parent != target_parent:
                        continue
                    marker = 'thread:'  # same thread only; the sub-comment target itself is not provable
            hits.append(marker + str(c.get('id')))
        return hits[0] if len(hits) == 1 else None

    def reconcile(self):
        """Re-check unknown / unverified writes by exact author+content+target+time. Never re-send."""
        for action in self.store.actions("(status='unknown' OR (status='accepted' AND verification!='own_page_exact')) AND created>?",
                                         (self.now() - 6 * 3600,), limit=5):
            self.verify(action['id'])

    def sync(self):
        for action in self.store.unsynced():
            verified = action['verification'] == 'own_page_exact' and action['status'] == 'accepted'
            status = ('succeeded' if verified else 'pending_visibility' if action['status'] == 'accepted'
                      else 'failed' if action['status'] in ('failed', 'failed_not_sent') else 'unknown')
            fid = action['feed_id'] or (action['remote_id'] if verified else '')
            conversation = 'xhs:feed:' + fid if SAFE_ID.fullmatch(fid) else 'xhs:global'
            payload = {'action_id': action['id'], 'platform': 'xiaohongshu', 'bot_account_id': self.cfg['bound_uid'],
                       'conversation_id': conversation, 'kind': action['kind'], 'status': status,
                       'content': (action['title'] + '\n' if action['title'] else '') + action['content'],
                       'remote_id': action['remote_id'] if verified else '', 'created': action['created'],
                       'verification': action['verification'], 'audience': action['audience'], 'draft_id': action['draft_id'], 'persona_version': action['persona_version'],
                       'source_ids': json.loads(action['source_ids']),
                       'target': {'feed_id': action['feed_id'], 'comment_id': action['comment_id']}}
            try:
                self.core.activity(payload)
            except CoreUnavailable:
                return False
            self.store.mark_synced(action['id'], action['rev'])
        return True

    def _precheck(self, kind, sending=None):
        """Cheap budget look before reading/modelling; reserve() remains the authority at send time.
        Drafting is not sending: with owner review the send limits (daily count, gap) do not stop a draft from being
        prepared for review; they are enforced when the approved draft is delivered."""
        if sending is None:
            sending = self.ALLOW_DIRECT
        if self.store.model_calls_today() + ATTEMPT_COST[kind] > self.limits['daily_model_calls']:
            return 'model_daily_limit'
        if not sending:
            return ''
        plural = {'post': 'daily_posts', 'comment': 'daily_comments', 'reply': 'daily_replies'}[kind]
        if self.store.count_actions(kind) >= self.limits[plural]:
            return 'daily_limit'
        latest = self.store.actions('kind=?', (kind,), limit=1)
        if latest and self.now() - latest[0]['created'] < self.limits['min_gap_' + kind]:
            return 'min_gap'
        return ''

    # ---- owner-review drafts ---------------------------------------------------------------------------
    def submit_draft(self, kind, *, content, title='', tags=(), image_id='', audience='public', purpose='general', feed_id='',
                     comment_id='', author_id='', comment_text='', token='', summary=None, plan=None, media_hash=''):
        """A persistent pending draft in the core; nothing is sent. The xsec token stays in the private token file."""
        uid = self.cfg['bound_uid']
        key = hashlib.sha256(json.dumps([uid, kind, audience, title, content, list(tags), image_id, media_hash, feed_id, comment_id],
                                        ensure_ascii=False).encode()).hexdigest()[:40]
        draft = {'bot_account_id': uid, 'kind': kind, 'audience': audience, 'purpose': purpose, 'submit_key': key, 'title': title,
                 'content': content, 'tags': list(tags), 'image_id': image_id, 'media_hash': media_hash,
                 'target': {'feed_id': feed_id, 'comment_id': comment_id, 'author_id': author_id,
                            'comment_hash': hashlib.sha256(comment_text.encode()).hexdigest() if comment_id else ''},
                 'summary': dict(summary or {}, **({'target_comment': comment_text[:300]} if comment_id else {})),
                 'persona_version': str((plan or {}).get('persona_version', '')), 'source_ids': list((plan or {}).get('source_ids', []))}
        if token and feed_id:
            # The short-lived credential reference is written BEFORE the core hears about the draft, keyed by the note:
            # if that fails there is no draft at all (no false "submitted"), and a draft can never exist without it.
            try:
                self.store.put_token(feed_id, token)
            except Exception:
                raise Skip('token_store_failed') from None
        try:
            result = self.core.draft_submit(draft)
        except CoreRejected as exc:
            raise Skip(exc.reason) from None
        return {'status': 'draft_submitted', 'draft_id': result['draft_id'], 'version': result['version']}

    def _queue_state(self, force=False):
        """Core review-queue state, cached for one short cycle so a full queue stops model spending cheaply."""
        cached = getattr(self, '_queue_cache', None)
        if not force and cached and self.now() - cached[0] < 60:
            return cached[1]
        try:
            state = self.core.draft_status()
        except CoreRejected as exc:
            raise Skip(exc.reason) from None
        self._queue_cache = (self.now(), state)
        return state

    def _kinds_ready(self):
        """Kinds whose switch is on and whose send limits allow a send right now (so an approval is not burned)."""
        out = []
        for kind, switch in KIND_SWITCH.items():
            if self.cfg[switch] and not self.gate(kind) and not self._precheck(kind, sending=True) in ('daily_limit', 'min_gap'):
                out.append(kind)
        return out

    def deliver_approved(self):
        """Claim at most one owner-approved draft and send exactly that approved content, once."""
        with self._lock:
            if self.gate():
                return {'status': self.gate()}
            kinds = self._kinds_ready()
            if not kinds:
                return {'status': 'nothing_deliverable'}
            try:
                claim = self.core.draft_claim(kinds)
            except CoreUnavailable:
                return {'status': 'core_unavailable'}
            if not claim:
                return {'status': 'no_approved_draft'}
            return self._deliver(claim)

    def _refuse(self, claim, detail):
        """Nothing was sent: tell the core so the draft returns to review (needs a fresh approval)."""
        try:
            self.core.draft_report(claim['draft_id'], claim['claim_token'], 'failed_not_sent', detail)
        except CoreUnavailable:
            pass
        return {'status': 'refused:' + detail, 'draft_id': claim['draft_id']}

    def _deliver(self, claim):
        uid = self.cfg['bound_uid']
        try:
            kind, did, ctok = claim['kind'], claim['draft_id'], claim['claim_token']
            if kind not in KIND_SWITCH or not re.fullmatch(r'[A-Za-z0-9]{6}', str(did)) or not re.fullmatch(r'[a-f0-9]{32}', str(ctok)):
                raise Skip('invalid_claim')
            # Re-derive the binding from every field of the claim before anything else: no HTTP write can follow a claim
            # whose account, text, title, tags, material, target, audience or version differs from what was approved.
            try:
                expected = claim_hash(claim)
            except (KeyError, TypeError):
                raise Skip('invalid_claim') from None
            if (claim.get('hash') != expected or type(claim.get('version')) is not int or claim['version'] < 1
                    or claim.get('approved_version') != claim['version']):
                raise Skip('hash_mismatch')
            if claim.get('bot_account_id') != uid:
                raise Skip('account_changed')
            audience = claim.get('audience')
            if audience not in ('public', 'self_only'):
                raise Skip('invalid_claim')
            content = valid_text(claim.get('content'), 900 if kind == 'post' else TEXT_LIMIT[kind])
            title = valid_text(claim.get('title'), 20) if kind == 'post' else ''
            tags = [valid_text(t, 20).replace('#', '').replace(' ', '') for t in list(claim.get('tags') or [])[:3]]
            blocked = self.gate(kind)
            if blocked:
                raise Skip('gated')
            if not self.identity(force=True)['ok']:
                raise Skip('identity_not_confirmed')
            if kind == 'post':
                return self._deliver_post(claim, title, content, tags, audience)
            return self._deliver_comment(claim, kind, content, audience)
        except Skip as skip:
            return self._refuse(claim, re.sub(r'[^a-z_]', '', skip.reason.split(':')[0])[:40] or 'refused')
        except MediaError as exc:
            return self._refuse(claim, re.sub(r'[^a-z_]', '', str(exc))[:40] or 'image_rejected')
        except ServiceError as exc:
            self._on_error(exc)
            return self._refuse(claim, 'service_error')

    def _deliver_post(self, claim, title, content, tags, audience):
        image_id = str(claim.get('image_id') or '')
        if image_id == TEXT_CARD:
            if not self.cfg['text_card']['enabled']:
                raise MediaError('text_card_disabled')
            if card_spec_hash(title, content) != claim.get('media_hash'):
                raise Skip('image_changed')
            path, generated = None, True
        else:
            path = self.media.resolve(image_id)
            if file_sha256(path) != claim.get('media_hash'):
                raise Skip('image_changed')   # the file is not the one the owner reviewed
            if self.store.image_recently_used(image_id, self.limits['image_reuse_days']):
                raise Skip('image_reused')
            generated = False
        feeds = (self.http.me().get('data') or {}).get('feeds') or []
        if any(same_title((f.get('noteCard') or {}).get('displayTitle'), title) for f in feeds):
            raise Skip('title_exists')
        if generated:
            image_id = self._card(title, content)
            path = self.media.resolve(image_id)
        visibility = '公开可见' if audience == 'public' else '仅自己可见'
        row = self._write('post', fn=lambda: self.http.publish(title, content, [path], tags, visibility), title=title, content=content,
                          image_id=image_id, pre_ids=[str(f.get('id')) for f in feeds], persona_version=claim.get('persona_version', ''),
                          source_ids=claim.get('source_ids', []), audience=audience, tags=tags, enforce_image_reuse=not generated,
                          draft_id=claim['draft_id'], claim_token=claim['claim_token'])
        return self._report_row(row)

    def _deliver_comment(self, claim, kind, content, audience):
        target = claim.get('target') or {}
        feed_id, comment_id = str(target.get('feed_id') or ''), str(target.get('comment_id') or '')
        try:
            token = self.store.get_token(feed_id) if SAFE_ID.fullmatch(feed_id) else ''
        except Exception:
            token = ''
        if not token:
            raise Skip('token_unavailable')    # never send blind
        retry_of = ''
        attempt = claim.get('delivery_attempt', 0)
        if type(attempt) is not int or not 0 <= attempt <= 2:
            raise Skip('invalid_retry_attempt')
        previous = self.store.actions('draft_id=?', (claim['draft_id'],), limit=1) if attempt else []
        if previous:
            old = previous[0]
            if old['content'] != content or old['feed_id'] != feed_id or old['comment_id'] != comment_id or old['kind'] != kind:
                raise Skip('retry_target_changed')
            try:
                history = self.store.actions('draft_id=? AND content_hash=?', (claim['draft_id'], old['content_hash']), limit=5)
                probe = {**old, 'sent_at': min(r['sent_at'] for r in history), 'pre_ids': history[-1]['pre_ids']}
                found = self._readback_comment(probe, deep=True)
                note, comments = self._last_readback_detail
            except (ServiceError, Skip):
                return self._retry_unresolved(claim, 'retry_readback_unavailable')
            if found:
                exact = not found.startswith('thread:')
                self.store.finish(old['id'], 'accepted', detail='retry_preflight_already_present', remote_id=found.removeprefix('thread:'),
                                  verification='own_page_exact' if exact else 'same_thread')
                self.core.draft_report(claim['draft_id'], claim['claim_token'], 'verified' if exact else 'accepted', 'retry_preflight_already_present')
                self.store.mark_reported(old['id'], 2)
                self.sync()
                return {'status': 'already_present', 'draft_id': claim['draft_id']}
            if not self._last_readback_complete:
                return self._retry_unresolved(claim, 'retry_readback_incomplete')
            retry_of = old['id']
        else:
            note, comments = self._detail(feed_id, token)
        author = ''
        if kind == 'comment':
            author = str(target.get('author_id') or '')   # the note author the owner's approval was bound to
            if author and (note.get('user') or {}).get('userId') != author:
                raise Skip('target_changed')
        if kind == 'reply':
            hit = next((c for c, _ in comments if str(c.get('id')) == comment_id), None)
            if hit is None:
                raise Skip('target_changed')
            author = (hit.get('userInfo') or {}).get('userId', '')
            if (author != target.get('author_id') or author == self.cfg['bound_uid']
                    or hashlib.sha256(str(hit.get('content', '')).encode()).hexdigest() != target.get('comment_hash')):
                raise Skip('target_changed')   # same comment, same author, same text as the owner saw
        fn = (lambda: self.http.reply(feed_id, token, comment_id, content)) if kind == 'reply' else (lambda: self.http.comment(feed_id, token, content))
        row = self._write(kind, fn=fn, feed_id=feed_id, comment_id=comment_id, author=author, content=content,
                          pre_ids=[str(c.get('id')) for c, _ in comments], persona_version=claim.get('persona_version', ''),
                          source_ids=claim.get('source_ids', []), audience=audience, draft_id=claim['draft_id'], claim_token=claim['claim_token'], retry_of=retry_of)
        return self._report_row(row)

    def _retry_unresolved(self, claim, detail):
        self.core.draft_report(claim['draft_id'], claim['claim_token'], 'unknown', detail)
        return {'status': detail, 'draft_id': claim['draft_id']}

    def check_requested(self):
        request = self.core.check_claim()
        if not request:
            return
        rows = self.store.actions('draft_id=?', (request['draft_id'],), limit=1)
        result = 'unavailable'
        if rows:
            action = rows[0]
            try:
                found = self._readback_post(action) if action['kind'] == 'post' else self._readback_comment(action, deep=True)
                if found:
                    exact = not found.startswith('thread:')
                    self.store.finish(action['id'], 'accepted', detail='owner_requested_readback', remote_id=found.removeprefix('thread:'),
                                      verification='own_page_exact' if exact else 'same_thread')
                    self._report_row(self.store.action(action['id']))
                    self.sync()
                    result = 'found'
                else:
                    result = 'not_found' if getattr(self, '_last_readback_complete', False) else 'incomplete'
            except (ServiceError, Skip):
                result = 'unavailable'
        self.core.check_report(request, result)

    def _report_row(self, row):
        """Tell the core what happened (accepted / unknown / failed are never reported as verified)."""
        outcome = ('verified' if row['status'] == 'accepted' and row['verification'] == 'own_page_exact'
                   else 'accepted' if row['status'] == 'accepted' else row['status'] if row['status'] in ('unknown', 'failed_not_sent') else 'failed')
        try:
            self.core.draft_report(row['draft_id'], row['claim_token'], outcome, row['detail'])
            self.store.mark_reported(row['id'], 2 if outcome == 'verified' else 1)
        except CoreUnavailable:
            pass   # report_pending() retries; the ledger already holds the truth
        return {'status': row['status'], 'verification': row['verification'], 'draft_id': row['draft_id'], 'reported': outcome}

    def report_pending(self):
        """After a crash or an unreachable core: tell the core the recorded outcome. Never re-sends anything."""
        for row in self.store.unreported():
            self._report_row(row)

    def _active_hours(self):
        return self.limits['active_start_hour'] <= beijing_hour(self.now()) < self.limits['active_end_hour']

    # ---- notifications -----------------------------------------------------------------
    def poll_notifications(self, dry_run=False):
        with self._lock:
            reason = self.gate('reply')
            if reason:
                return {'status': reason}
            ident = self.identity()
            if not ident['ok']:
                return {'status': ident['reason']}
            try:
                items = self.http.notifications(self.limits['max_notifications'])
            except ServiceError as exc:
                return {'status': self._on_error(exc)}
            self._ok_read()
            usable = [i for i in items if isinstance(i, dict) and i.get('comment_id') and SAFE_ID.fullmatch(str(i.get('comment_id')))]
            baseline = self.store.get('baseline_at')
            if baseline is None:
                # First run: everything already visible is history; reply only to later messages.
                self.store.put('baseline_at', self.now())
                for item in usable:
                    self.store.mark_seen('c:' + str(item['comment_id']), 'baseline')
                return {'status': 'baseline_established', 'history_items': len(usable)}
            results = []
            for item in usable:
                if len(results) >= MAX_PER_POLL:
                    break
                key = 'c:' + str(item['comment_id'])
                if self.store.is_seen(key) or self.store.get('retry_at:' + key, 0) > self.now():
                    continue
                results.append(self._handle_notification(item, key, baseline, dry_run))
                status = results[-1].get('status', '')
                if status in DEFERRED + DEFERRED_DRAFT + ('model_daily_limit', 'backoff', 'paused', 'core_unavailable') or status.startswith('gated:'):
                    break
            return {'status': 'polled', 'results': results}

    def _handle_notification(self, item, key, baseline, dry_run):
        author = str((item.get('from') or {}).get('user_id') or '')
        stamp = millis(item.get('time'))
        outcome = None
        if author == self.cfg['bound_uid'] or str(item.get('comment_text', '')).strip() in self.store.recent_texts(60):
            outcome = 'self_message'  # own account, or an echo of text this executor sent: never answer ourselves
        elif not stamp or stamp < baseline - 60:
            outcome = 'before_baseline'
        elif self.now() - stamp > 24 * 3600:
            outcome = 'expired'
        elif not ACTOR.fullmatch(author):
            outcome = 'invalid_author'
        elif not SAFE_ID.fullmatch(str(item.get('feed_id') or '')) or not item.get('feed_xsec_token'):
            # Recoverable: the list may deliver the post reference / token on a later poll.
            return self._defer(item, key, 'incomplete_notification', dry_run)
        if outcome:
            if not dry_run:
                self.store.mark_seen(key, outcome)
            return {'comment': item.get('comment_id'), 'status': outcome}
        wait = self._precheck('reply')
        if wait:
            return {'comment': item.get('comment_id'), 'status': wait}
        try:
            if not self.ALLOW_DIRECT and not self._queue_state()['can_submit']:
                # Review queue full: do not read the post or spend model budget; the message stays for a later poll.
                return {'comment': item.get('comment_id'), 'status': 'draft_queue_full'}
            result = self._reply_flow(item, author, dry_run)
        except Defer as defer:
            return self._defer(item, key, defer.reason, dry_run)
        except Skip as skip:
            result = {'status': skip.reason}
        except ServiceError as exc:
            return {'comment': item.get('comment_id'), 'status': self._on_error(exc)}
        except CoreUnavailable:
            self.store.put('backoff_until', self.now() + 300)
            return {'comment': item.get('comment_id'), 'status': 'core_unavailable'}
        # Waiting states (budget, pause/switch gate) leave the message unhandled for a later poll.
        if (not dry_run and result['status'] not in DEFERRED + DEFERRED_DRAFT + ('model_daily_limit',)
                and not result['status'].startswith('gated:')):
            self.store.mark_seen(key, result['status'])
        result['comment'] = item.get('comment_id')
        return result

    def _defer(self, item, key, reason, dry_run):
        """Bounded, spaced retries; only after RETRY_LIMIT is the item finally set aside."""
        out = {'comment': item.get('comment_id'), 'status': reason}
        if dry_run:
            return out
        tries = self.store.get('retry:' + key, 0) + 1
        self.store.put('retry:' + key, tries)
        if tries >= RETRY_LIMIT:
            self.store.mark_seen(key, reason + '_final')
            out['status'] = reason + '_final'
        else:
            self.store.put('retry_at:' + key, self.now() + RETRY_DELAY * tries)
        out['attempt'] = tries
        return out

    def _reply_flow(self, item, author, dry_run):
        feed_id, token, cid = item['feed_id'], item['feed_xsec_token'], str(item['comment_id'])
        note, comments = self._detail(feed_id, token)
        target = next(((c, p) for c, p in comments if str(c.get('id')) == cid), None)
        if not target:
            # Not in the first bounded load: one deeper, still bounded load (scroll + expand replies).
            detail = self.http.feed_detail(feed_id, token, self.limits['max_comments_read'] * 3, deep=True)
            comments = flatten((detail.get('comments') or {}).get('list')) if isinstance(detail, dict) else []
            target = next(((c, p) for c, p in comments if str(c.get('id')) == cid), None)
            if not target:
                raise Defer('target_not_loaded')
        if (target[0].get('userInfo') or {}).get('userId') != author:
            raise Skip('target_comment_not_verified')
        comment, parent = target
        own_post = (note.get('user') or {}).get('userId') == self.cfg['bound_uid']
        thread = [{'author': (c.get('userInfo') or {}).get('nickname', '')[:20], 'text': str(c.get('content', ''))[:200],
                   'is_me': (c.get('userInfo') or {}).get('userId') == self.cfg['bound_uid']}
                  for c, p in comments if (p or str(c.get('id'))) == (parent or cid)][:8]
        material = {'feed_title': str(note.get('title', ''))[:60], 'feed_text': str(note.get('desc', ''))[:600], 'own_post': own_post,
                    'material_scope': {'text': 'excerpt' if len(str(note.get('desc', ''))) > 600 else 'full_text', 'images_seen': False,
                                       'thread_comments_shown': len(thread), 'complete_browse': False},
                    'target_comment': str(comment.get('content', ''))[:400],
                    'target_author_nickname': str((comment.get('userInfo') or {}).get('nickname', ''))[:20], 'thread': thread}
        self._event('comment_received', event_id='xhs:comment:' + cid, conversation_id='xhs:feed:' + feed_id, actor_id=author,
                    nickname=material['target_author_nickname'], content=material['target_comment'], source_id=cid,
                    origin='external_user', occurred_at=millis(comment.get('createTime')) or self.now())
        plan = self._plan('reply', material, conversation='xhs:feed:' + feed_id)
        decision = plan['decision']
        if decision.get('willing') is not True:
            return {'status': 'model_declined'}
        text = valid_text(decision.get('text'), TEXT_LIMIT['reply'])
        if dry_run:
            return {'status': 'dry_run', 'would_reply_to': cid, 'text': text}
        if not self.ALLOW_DIRECT:
            # Review mode: a durable pending draft for the owner; nothing is sent.
            return self.submit_draft('reply', content=text, feed_id=feed_id, comment_id=cid, author_id=author,
                                     comment_text=str(comment.get('content', '')), token=token, plan=plan,
                                     summary={'feed_title': str(note.get('title', ''))[:60], 'target_author': material['target_author_nickname']})
        ident = self.identity(force=True)
        if not ident['ok']:
            raise Skip(ident['reason'])
        with self._lock:
            if self.store.is_seen('c:' + cid):
                raise Skip('already_handled')
            row = self._write('reply', fn=lambda: self.http.reply(feed_id, token, cid, text), feed_id=feed_id, comment_id=cid,
                              author=author, content=text, pre_ids=[str(c.get('id')) for c, _ in comments],
                              persona_version=plan.get('persona_version', ''), source_ids=plan.get('source_ids', []))
        return {'status': row['status'], 'verification': row['verification']}

    # ---- autonomy ----------------------------------------------------------------------
    def autonomy_step(self, dry_run=False, force=False):
        with self._lock:
            reason = self.gate('autonomy_enabled')
            if reason:
                return {'status': reason}
            if not force and (self.now() < self.store.get('next_autonomy', 0) or not self._active_hours()):
                return {'status': 'not_due'}
            ident = self.identity()
            if not ident['ok']:
                return {'status': ident['reason']}
            self.store.put('next_autonomy', self.now() + self.limits['autonomy_seconds'] * (0.8 + 0.4 * self.rand()))
            try:
                return self._autonomy(dry_run)
            except Skip as skip:
                return {'status': skip.reason}
            except ServiceError as exc:
                return {'status': self._on_error(exc)}
            except CoreUnavailable:
                self.store.put('backoff_until', self.now() + 300)
                return {'status': 'core_unavailable'}

    def _autonomy(self, dry_run):
        uid = self.cfg['bound_uid']
        feeds, self._refs = [], {}
        intro_wait = False
        if not self.ALLOW_DIRECT:
            # A full review queue stops here: no browse, no model call. An introduction already waiting for review (or sent
            # but not yet confirmed) is not drafted again every cycle only to be refused by the core.
            state = self._queue_state()
            if not state['can_submit']:
                return {'status': 'draft_queue_full'}
            intro_wait = self.cfg['visibility'] == '公开可见' and not state['intro_done'] and state['intro_active']
        if self.cfg['browse_enabled'] and self.store.count_browses() < self.limits['daily_browses']:
            interests = self.limits['interests']
            keyword = self.store.get('next_search') or (interests[int(self.rand() * len(interests)) % len(interests)] if self.rand() < 0.5 else '')
            self.store.add_browse()  # occupied before the request: a failed browse still counts
            raw = self.http.search(keyword) if keyword else self.http.list_feeds()
            self._ok_read()
            for feed in raw:
                card = feed.get('noteCard') or {}
                if (feed.get('modelType') != 'note' or not SAFE_ID.fullmatch(str(feed.get('id'))) or not feed.get('xsecToken')
                        or (card.get('user') or {}).get('userId') == uid or self.store.is_seen('f:' + feed['id']) or len(feeds) >= 8):
                    continue
                ref = 'f%d' % (len(feeds) + 1)
                self._refs[ref] = (feed['id'], feed['xsecToken'], (card.get('user') or {}).get('userId', ''))
                feeds.append({'ref': ref, 'title': str(card.get('displayTitle', ''))[:40],
                              'author': str((card.get('user') or {}).get('nickname', ''))[:20],
                              'liked': str((card.get('interactInfo') or {}).get('likedCount', ''))[:10]})
        post_ok = self.cfg['publish_enabled'] and not self._precheck('post') and not intro_wait
        if intro_wait and self.cfg['publish_enabled']:
            self.store.put('last_post_skip', {'at': self.now(), 'reason': 'intro_awaiting_review'})
        reuse_days = self.limits['image_reuse_days']
        every = self.media.items() if post_ok else []
        # Real media inside the reuse window (any attempt that may have reached the platform, unknown included) is not offered.
        images = [{'id': i['id'], 'caption': i['caption']} for i in every if not self.store.image_recently_used(i['id'], reuse_days)]
        if post_ok and self.cfg['text_card']['enabled']:
            # Rendered only after the core approved the text; the id is replaced by a unique card id at publish time.
            images.append({'id': TEXT_CARD, 'caption': CARD_CAPTION})
        if post_ok and not images:
            self.store.put('last_post_skip', {'at': self.now(), 'reason': 'media_recently_used' if every else 'no_publishable_media'})
        comment_ok = self.cfg['comment_enabled'] and not self._precheck('comment')
        feeds = feeds if comment_ok else []
        if not feeds and not images:
            return {'status': 'nothing_to_consider'}
        plan = self._plan('select', {'interests': self.limits['interests']}, candidates={'feeds': feeds, 'images': images},
                          budget={'post_allowed': bool(images), 'comment_allowed': comment_ok})
        d = plan['decision']
        self.store.put('next_search', d.get('search_keyword', ''))
        if d.get('action') == 'comment' and comment_ok and d.get('ref') in self._refs:
            return self._comment_flow(d['ref'], dry_run)
        if d.get('action') == 'post' and images:
            return self._post_flow(images, dry_run)
        return {'status': 'model_waited'}

    def _comment_flow(self, ref, dry_run):
        feed_id, token, author = self._refs[ref]
        note, comments = self._detail(feed_id, token)
        self.store.mark_seen('f:' + feed_id, 'considered')
        top = [str(c.get('content', ''))[:120] for c, p in comments if not p]
        material = {'feed_title': str(note.get('title', ''))[:60], 'feed_text': str(note.get('desc', ''))[:700], 'top_comments': top[:5],
                    'material_scope': {'text': 'excerpt' if len(str(note.get('desc', ''))) > 700 else 'full_text', 'images_seen': False,
                                       'comments_shown': len(top[:5]), 'complete_browse': False}}
        self._event('content_read', event_id='xhs:read:%s:%s' % (feed_id, digest(note.get('title', '') + note.get('desc', ''))[:12]),
                    conversation_id='xhs:feed:' + feed_id, actor_id=author or 'unknown', content=material['feed_title'] + '\n' + material['feed_text'],
                    source_id=feed_id, origin='platform_content')
        plan = self._plan('comment', material, conversation='xhs:feed:' + feed_id)
        if plan['decision'].get('willing') is not True:
            return {'status': 'model_declined'}
        text = valid_text(plan['decision'].get('text'), TEXT_LIMIT['comment'])
        if dry_run:
            return {'status': 'dry_run', 'would_comment_on': feed_id, 'text': text}
        if not self.ALLOW_DIRECT:
            # The note's author is bound into the draft (and checked again at delivery) so per-author limits keep working.
            return self.submit_draft('comment', content=text, feed_id=feed_id, author_id=author, token=token, plan=plan,
                                     summary={'feed_title': str(note.get('title', ''))[:60], 'feed_excerpt': str(note.get('desc', ''))[:200]})
        if not self.identity(force=True)['ok']:
            raise Skip('identity_not_confirmed')
        row = self._write('comment', fn=lambda: self.http.comment(feed_id, token, text), feed_id=feed_id, author=author, content=text,
                          pre_ids=[str(c.get('id')) for c, _ in comments], persona_version=plan.get('persona_version', ''),
                          source_ids=plan.get('source_ids', []))
        return {'status': row['status'], 'verification': row['verification']}

    def _post_flow(self, images, dry_run):
        task = 'post'
        if not self.ALLOW_DIRECT:
            state = self._queue_state()
            if not state['can_submit']:
                raise Skip('draft_queue_full')
            # Until a public post is really recorded as successful, the only public post she drafts is her introduction.
            if self.cfg['visibility'] == '公开可见' and not state['intro_done']:
                if state['intro_active']:
                    raise Skip('intro_draft_active')   # waiting for review or sent-but-unconfirmed: no second introduction, no model spend
                task = 'intro'
        plan = self._plan(task, {'interests': self.limits['interests']}, candidates={'images': images})
        d = plan['decision']
        if d.get('willing') is not True:
            return {'status': 'model_declined'}
        # Autonomous posts always use the configured visibility; nothing from outside can change it.
        return self.publish(d.get('title'), d.get('content'), d.get('image_id'), d.get('tags') or [], dry_run=dry_run, plan=plan,
                            purpose='intro' if d.get('purpose') == 'intro' else 'general')

    def _card(self, title, content):
        """Render the original text card from the approved title / first sentence only."""
        if not self.cfg['text_card']['enabled']:
            raise MediaError('text_card_disabled')
        line = re.split(r'[。！？!?\n]', content, maxsplit=1)[0][:60]
        try:
            return textcard.render(self.cfg['media_dir'], self.cfg['text_card']['font_path'], title, line, int(self.now()))
        except textcard.CardError as exc:
            raise MediaError(str(exc)) from None

    # ---- publish (autonomous and manual) -------------------------------------------------
    def publish(self, title, content, image_id, tags=(), *, dry_run=False, plan=None, manual=False, visibility=None, purpose='general',
                self_acceptance=False):
        """Default: a pending draft for the owner (nothing is sent). Direct sending exists only for the explicit,
        manual, private-only self-acceptance mode (and in the test subclass)."""
        with self._lock:
            reason = self.gate('post')
            if reason:
                return {'status': reason}
            if self_acceptance:
                if not manual:
                    return {'status': 'self_acceptance_is_manual_only'}
                if visibility not in (None, '仅自己可见'):
                    return {'status': 'self_acceptance_is_private_only'}   # it can never become a public bypass
                visibility = '仅自己可见'
            else:
                visibility = visibility if manual and visibility else self.cfg['visibility']
            if visibility not in VISIBILITIES:
                return {'status': 'invalid_visibility'}
            try:
                title, content = valid_text(title, 20), valid_text(content, 900)
                tags = [valid_text(t, 20).replace('#', '').replace(' ', '') for t in list(tags)[:3]]
                path = None if image_id == TEXT_CARD else self.media.resolve(image_id)
                if image_id == TEXT_CARD and not self.cfg['text_card']['enabled']:
                    raise MediaError('text_card_disabled')
            except Skip as skip:
                return {'status': skip.reason}
            except MediaError as exc:
                return {'status': 'image_rejected:' + str(exc)}
            generated = path is None  # a freshly rendered card always has a new id
            if not generated and self.store.image_recently_used(image_id, self.limits['image_reuse_days']):
                return {'status': 'image_reused'}
            if dry_run:
                return {'status': 'dry_run', 'would_publish': {'title': title, 'content': content, 'image_id': image_id, 'tags': tags,
                                                               'visibility': visibility,
                                                               'mode': 'direct' if (self.ALLOW_DIRECT or self_acceptance) else 'owner_review_draft'}}
            if not (self.ALLOW_DIRECT or self_acceptance):
                try:
                    return self.submit_draft('post', title=title, content=content, tags=tags, image_id=image_id, purpose=purpose, plan=plan,
                                             audience='public' if visibility == '公开可见' else 'self_only',
                                             media_hash=card_spec_hash(title, content) if generated else file_sha256(path))
                except Skip as skip:
                    return {'status': skip.reason}
                except CoreUnavailable:
                    return {'status': 'core_unavailable'}
                except OSError:
                    return {'status': 'image_rejected:unreadable'}
            if not self.identity(force=True)['ok']:
                return {'status': 'identity_not_confirmed'}
            try:
                feeds = (self.http.me().get('data') or {}).get('feeds') or []
            except ServiceError as exc:
                return {'status': self._on_error(exc)}
            if any(same_title((f.get('noteCard') or {}).get('displayTitle'), title) for f in feeds):
                return {'status': 'title_exists'}
            if path is None:
                try:
                    image_id = self._card(title, content)
                    path = self.media.resolve(image_id)
                except MediaError as exc:
                    return {'status': 'image_rejected:' + str(exc)}
            try:
                row = self._write('post', fn=lambda: self.http.publish(title, content, [path], tags, visibility), title=title, content=content,
                                  image_id=image_id, pre_ids=[str(f.get('id')) for f in feeds],
                                  persona_version=(plan or {}).get('persona_version', ''), source_ids=(plan or {}).get('source_ids', []),
                                  manual=manual, audience='public' if visibility == '公开可见' else 'self_only', tags=tags,
                                  enforce_image_reuse=not generated)
            except Skip as skip:
                return {'status': skip.reason}
            return {'status': row['status'], 'verification': row['verification']}

    # ---- manual single actions (coordinator acceptance) ---------------------------------
    def manual_comment(self, feed_id, token, text, *, dry_run=False, self_acceptance=False):
        with self._lock:
            return self._manual('comment', feed_id, token, '', text, dry_run, self_acceptance)

    def manual_reply(self, feed_id, token, comment_id, text, *, dry_run=False, self_acceptance=False):
        with self._lock:
            return self._manual('reply', feed_id, token, comment_id, text, dry_run, self_acceptance)

    def _manual(self, kind, feed_id, token, comment_id, text, dry_run, self_acceptance=False):
        """self_acceptance (manual only, never reachable from notifications or autonomy): acceptance on the
        account's own test note. Requires the note's author == bound UID; replying to our own comment is allowed
        only in this mode; the activity is recorded self_only so test content never reaches public memory."""
        reason = self.gate(kind)
        if reason:
            return {'status': reason}
        try:
            if not SAFE_ID.fullmatch(feed_id or '') or not token or (kind == 'reply' and not SAFE_ID.fullmatch(comment_id or '')):
                raise Skip('invalid_target')
            text = valid_text(text, TEXT_LIMIT[kind])
            if not self.identity(force=not dry_run)['ok']:
                raise Skip('identity_not_confirmed')
            note, comments = self._detail(feed_id, token)
            uid = self.cfg['bound_uid']
            if self_acceptance:
                # Direct sending without owner review is only for the account's own registered private test notes.
                if (note.get('user') or {}).get('userId') != uid:
                    raise Skip('self_acceptance_requires_own_note')
                if not self.store.is_private_feed(feed_id):
                    # Not provably private (we did not create it as self-only and read it back exactly): refuse. The
                    # same text without the option becomes an ordinary owner-review draft.
                    raise Skip('self_acceptance_requires_verified_self_only_post')
            author, hit = '', None
            if kind == 'comment':
                note_author = (note.get('user') or {}).get('userId', '')
                author = '' if note_author == uid else str(note_author or '')   # counts toward the per-author limit unless it is us
            if kind == 'reply':
                hit = next((c for c, _ in comments if str(c.get('id')) == comment_id), None)
                if not hit:
                    raise Skip('target_comment_not_found')
                author = (hit.get('userInfo') or {}).get('userId', '')
                if author == uid and not self_acceptance:
                    raise Skip('cannot_reply_to_self')
            audience = 'self_only' if self_acceptance else 'public'
            if dry_run:
                return {'status': 'dry_run', 'kind': kind, 'text': text, 'audience': audience,
                        'mode': 'direct' if (self.ALLOW_DIRECT or self_acceptance) else 'owner_review_draft'}
            if not (self.ALLOW_DIRECT or self_acceptance):
                # Default for the manual CLI too: a pending draft for the owner, never a send.
                return self.submit_draft(kind, content=text, feed_id=feed_id, comment_id=comment_id, author_id=author,
                                         comment_text=str(hit.get('content', '')) if hit else '', token=token,
                                         summary={'feed_title': str(note.get('title', ''))[:60],
                                                  'target_author': str(((hit or {}).get('userInfo') or {}).get('nickname', ''))[:20]})
            fn = (lambda: self.http.reply(feed_id, token, comment_id, text)) if kind == 'reply' else (lambda: self.http.comment(feed_id, token, text))
            row = self._write(kind, fn=fn, feed_id=feed_id, comment_id=comment_id, author=author, content=text,
                              pre_ids=[str(c.get('id')) for c, _ in comments], manual=True, audience=audience)
            return {'status': row['status'], 'verification': row['verification']}
        except Skip as skip:
            return {'status': skip.reason}
        except ServiceError as exc:
            return {'status': self._on_error(exc)}
        except CoreUnavailable:
            return {'status': 'core_unavailable'}

    # ---- scheduler ------------------------------------------------------------------------
    def tick(self, dry_run=None):
        dry = self.cfg['dry_run'] if dry_run is None else dry_run
        self.apply_requests()
        reason = self.gate()
        if reason:
            return {'status': reason}
        ident = self.identity()
        if not ident['ok']:
            return {'status': ident['reason']}
        out = {'status': 'ok'}
        try:
            if not dry:
                self.sync()
                self.reconcile()
                self.report_pending()
                self.check_requested()
                out['delivery'] = self.deliver_approved()
            if self.cfg['reply_enabled'] and self.now() >= self.store.get('next_poll', 0):
                self.store.put('next_poll', self.now() + self.limits['notification_poll_seconds'])
                out['notifications'] = self.poll_notifications(dry)
            if self.cfg['autonomy_enabled']:
                out['autonomy'] = self.autonomy_step(dry)
        except ServiceError as exc:
            out['status'] = self._on_error(exc)
        except CoreUnavailable:
            self.store.put('backoff_until', self.now() + 300)
            out['status'] = 'core_unavailable'
        return out

    def run(self, stop, log=print):
        previous = None
        while not stop.is_set():
            try:
                result = self.tick()
            except Exception as exc:  # keep the service alive; details stay out of logs
                result = {'status': 'error', 'kind': type(exc).__name__}
            if result != previous:
                log(result)
                previous = result
            stop.wait(self.limits['tick_seconds'])

    # ---- operator views -------------------------------------------------------------------
    def status(self):
        today = {k: self.store.count_actions(k) for k in ('post', 'comment', 'reply')}
        cached = self.store.get('identity')
        return {'enabled': self.cfg['enabled'], 'dry_run': self.cfg['dry_run'],
                'switches': {k: self.cfg[k] for k in ('publish_enabled', 'comment_enabled', 'reply_enabled', 'browse_enabled', 'autonomy_enabled')},
                'bound': bool(self.cfg['bound_uid']), 'identity_confirmed': bool(cached and cached.get('ok')),
                'paused': self.paused_reason() or False, 'backoff_until': self.store.get('backoff_until', 0),
                'today': today, 'model_calls_today': self.store.model_calls_today(), 'browses_today': self.store.count_browses(),
                'unknown_actions': len(self.store.actions("status='unknown'")),
                'pending_verification': len(self.store.actions("status='accepted' AND verification!='own_page_exact'")),
                'unsynced_receipts': len(self.store.unsynced(50)), 'baseline_at': self.store.get('baseline_at'),
                'last_post_skip': self.store.get('last_post_skip')}

    def doctor(self, core_socket_exists=None):
        report = {'config': 'ok', 'media_items': len(self.media.items())}
        try:
            status = self.http.login_status()
            observed = status.get('user_id') if isinstance(status, dict) else None
            report['service'] = 'reachable'
            report['logged_in'] = bool(isinstance(status, dict) and status.get('is_logged_in') is True)
            report['observed_uid'] = observed or None
            report['uid_matches_config'] = bool(observed) and observed == self.cfg['bound_uid']
            report['bound_uid_configured'] = bool(self.cfg['bound_uid'])
        except ServiceError as exc:
            report['service'] = 'unreachable:' + exc.kind
        if core_socket_exists is not None:
            report['core_socket'] = 'present' if core_socket_exists else 'missing'
        return report

    def qrcode(self, out_path):
        """Save the login QR image for a human to scan. Prints no image data and no cookie."""
        data = self.http.login_qrcode()
        img = data.get('img') if isinstance(data, dict) else None
        match = re.fullmatch(r'data:image/(png|jpeg);base64,([A-Za-z0-9+/=\s]+)', img or '')
        if not match:
            return {'saved': False, 'is_logged_in': bool(data.get('is_logged_in')) if isinstance(data, dict) else False}
        try:
            raw = base64.b64decode(match.group(2), validate=False)
        except binascii.Error:
            return {'saved': False, 'reason': 'invalid_image'}
        if not 0 < len(raw) <= 2 * 1024 * 1024 or not (raw.startswith(b'\x89PNG') or raw.startswith(b'\xff\xd8')):
            return {'saved': False, 'reason': 'invalid_image'}
        path = Path(out_path)
        if not path.is_absolute() or path.is_symlink() or not path.parent.is_dir():
            return {'saved': False, 'reason': 'invalid_output_path'}
        tmp = path.with_name(path.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        os.replace(tmp, path)
        return {'saved': True, 'path': str(path), 'is_logged_in': bool(data.get('is_logged_in')), 'timeout': str(data.get('timeout', ''))}
