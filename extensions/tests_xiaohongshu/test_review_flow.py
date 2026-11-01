"""Production Executor behaviour with mandatory owner review (Env(strict=True)), plus the state.sqlite upgrade."""
import hashlib, json, os, sqlite3, tempfile, unittest
from pathlib import Path
from xhs_support import FEED, OTHER, PNG, SERVICE_TOKEN, TOKEN, UID, Env, comment, post_decision
from xiaohongshu.runner import Executor, claim_hash, file_sha256
from xiaohongshu.store import Store

LEGACY_SCHEMA = '''
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE actions(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL, day TEXT NOT NULL,
  status TEXT NOT NULL, verification TEXT NOT NULL DEFAULT 'none',
  feed_id TEXT NOT NULL DEFAULT '', comment_id TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL, content TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  image_id TEXT NOT NULL DEFAULT '', remote_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
  target_key TEXT NOT NULL, dedupe_key TEXT UNIQUE, pre_ids TEXT NOT NULL DEFAULT '[]',
  sent_at REAL, persona_version TEXT NOT NULL DEFAULT '', source_ids TEXT NOT NULL DEFAULT '[]',
  manual INTEGER NOT NULL DEFAULT 0, audience TEXT NOT NULL DEFAULT 'public', tags TEXT NOT NULL DEFAULT '[]',
  rev INTEGER NOT NULL DEFAULT 1, synced_rev INTEGER NOT NULL DEFAULT 0);
CREATE INDEX actions_day ON actions(kind, day);
CREATE TABLE seen(k TEXT PRIMARY KEY, created REAL NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE model_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, day TEXT NOT NULL, task TEXT NOT NULL);
'''


class StoreMigrationTests(unittest.TestCase):
    def test_old_schema_is_upgraded_in_place_and_idempotently(self):
        from xhs_support import Clock
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp)) / 'state'
            root.mkdir(mode=0o700)
            db = sqlite3.connect(root / 'state.sqlite')
            db.executescript(LEGACY_SCHEMA)
            from xiaohongshu.store import beijing_day
            day = beijing_day(clock())
            db.execute("INSERT INTO actions(id,kind,created,day,status,verification,content_hash,content,title,image_id,target_key,dedupe_key,sent_at,audience,tags) "
                       "VALUES('xhs-old','post',?,?,'accepted','pending','h','私密验收正文','大肥鱼游进小红书','card1','post:h','post:h',?,'self_only','[]')",
                       (clock(), day, clock()))
            db.execute("INSERT INTO seen VALUES('c:cm0001',?,'baseline')", (clock(),))
            db.execute("INSERT INTO model_calls(created,day,task) VALUES(?,?,'reply')", (clock(), day))
            db.execute("INSERT INTO meta VALUES('baseline_at','123.0')")
            db.commit()
            db.close()
            limits = {'daily_posts': 3, 'daily_comments': 4, 'daily_replies': 8, 'per_thread_daily': 2, 'per_author_daily': 2,
                      'min_gap_post': 3600, 'min_gap_comment': 300, 'min_gap_reply': 60}
            for round_ in range(3):                      # first upgrade, then two restarts
                store = Store(root, now=clock)
                columns = [r['name'] for r in store.db.execute('PRAGMA table_info(actions)')]
                self.assertEqual(len(columns), len(set(columns)))
                for needed in ('audience', 'tags', 'draft_id', 'claim_token', 'reported'):
                    self.assertIn(needed, columns)
                old = store.action('xhs-old')
                self.assertEqual((old['status'], old['audience'], old['title'], old['draft_id'], old['claim_token'], old['reported']),
                                 ('accepted', 'self_only', '大肥鱼游进小红书', '', '', 1))
                self.assertEqual(store.count_actions('post'), 1)               # the old row still counts against the daily quota
                self.assertEqual(store.model_calls_today(), 1)
                self.assertTrue(store.is_seen('c:cm0001'))
                self.assertEqual(store.get('baseline_at'), 123.0)
                self.assertEqual(len(store.unreported()), round_)              # the new queries work: no "no such column"
                self.assertEqual(len(store.unsynced()), 1 + round_)
                self.assertFalse(store.is_private_feed('n' * 24))              # pending readback: not provably private
                aid, reason = store.reserve('reply', feed_id='feed%d' % round_ * 6, comment_id='cm000%d' % round_, content='回复%d' % round_,
                                            limits=limits, draft_id='D0000%d' % round_, claim_token='a' * 32, manual=True)
                self.assertTrue(aid, reason)
                store.finish(aid, 'accepted')                                   # (a row still 'sending' would be 'unknown' after a restart)
                self.assertEqual(store.action(aid)['reported'], 0)             # a draft row waits to be reported
                self.assertEqual(len(store.unreported()), round_ + 1)          # the earlier rounds' draft rows are still unreported
                store.close()
            store = Store(root, now=clock)
            self.assertEqual((store.count_actions('reply'), store.count_actions('post')), (3, 1))
            self.assertEqual(store.action('xhs-old')['status'], 'accepted')
            store.close()

    def test_only_a_self_only_exactly_read_back_note_is_private(self):
        env = Env(self)
        for audience, verification, remote, expected in (('self_only', 'own_page_exact', 'a' * 24, True), ('self_only', 'pending', 'b' * 24, False),
                                                         ('public', 'own_page_exact', 'c' * 24, False), ('self_only', 'same_thread', 'd' * 24, False)):
            aid, _ = env.store.reserve('post', content='x' + remote, title='t' + remote[:8], image_id='card1', manual=True, audience=audience,
                                       limits={**env.cfg['limits'], 'daily_posts': 99})
            env.store.finish(aid, 'accepted', remote_id=remote, verification=verification)
            self.assertEqual(env.store.is_private_feed(remote), expected, (audience, verification))
        self.assertFalse(env.store.is_private_feed(''))
        self.assertFalse(env.store.is_private_feed('z' * 24))


class StrictBase(unittest.TestCase):
    def setUp(self):
        self.env = Env(self, strict=True)
        self.env.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢你的留言'}

    def drafted(self, env=None, text='new message', cid='cm0002'):
        env = env or self.env
        env.ex.poll_notifications()                  # baseline
        env.clock.t += 120
        env.notify(cid, text)
        return env.ex.poll_notifications()['results'][0]

    def approved_reply(self, env=None):
        env = env or self.env
        result = self.drafted(env)
        env.core_stub.approve(result['draft_id'])
        return result['draft_id']


class ReviewModeTests(StrictBase):
    def test_a_reply_becomes_a_pending_draft_and_nothing_is_sent(self):
        env = self.env
        result = self.drafted()
        self.assertEqual((result['status'], result['comment']), ('draft_submitted', 'cm0002'))
        self.assertEqual(env.service.writes(), [])
        (draft,) = env.core_stub.drafts.values()
        self.assertEqual((draft['kind'], draft['content'], draft['audience'], draft['bot_account_id']), ('reply', '谢谢你的留言', 'public', UID))
        target = draft['target']
        self.assertEqual((target['feed_id'], target['comment_id'], target['author_id']), (FEED, 'cm0002', OTHER))
        self.assertEqual(target['comment_hash'], hashlib.sha256(b'new message').hexdigest())
        self.assertEqual(draft['summary']['target_comment'], 'new message')
        self.assertTrue(env.store.is_seen('c:cm0002'))
        self.assertEqual(env.ex.poll_notifications()['results'], [])
        self.assertEqual(len(env.core_stub.drafts), 1)
        # the note's xsec token stays in the private token file only
        self.assertNotIn(TOKEN, json.dumps(env.core_stub.payloads))
        self.assertNotIn(TOKEN.encode(), env.db_bytes())
        self.assertIn(TOKEN.encode(), env.db_bytes(include_tokens=True))
        self.assertEqual(oct((env.store.root / 'tokens.sqlite').stat().st_mode & 0o777), '0o600')

    def test_dry_run_is_not_a_substitute_for_a_persistent_draft(self):
        self.drafted()                                            # baseline + one real draft
        env = Env(self, strict=True)
        env.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢'}
        env.ex.poll_notifications()
        env.clock.t += 120
        env.notify('cm0002', 'hello')
        result = env.ex.poll_notifications(dry_run=True)['results'][0]
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual((env.core_stub.drafts, env.service.writes()), ({}, []))

    def test_manual_commands_and_publish_default_to_drafts(self):
        env = self.env
        env.service.comments = [comment('cm0001', 'hi', user=OTHER, when=env.clock())]
        for call in (lambda: env.ex.manual_comment(FEED, TOKEN, '主动评论'), lambda: env.ex.manual_reply(FEED, TOKEN, 'cm0001', '手动回复'),
                     lambda: env.ex.publish('标题甲', '正文甲', 'card1', ['AI'], manual=True),
                     lambda: env.ex.publish('标题乙', '正文乙', 'card1', manual=True, visibility='仅自己可见')):
            self.assertEqual(call()['status'], 'draft_submitted')
        self.assertEqual(env.service.writes(), [])
        kinds = sorted((d['kind'], d['audience']) for d in env.core_stub.drafts.values())
        self.assertEqual(kinds, [('comment', 'public'), ('post', 'public'), ('post', 'self_only'), ('reply', 'public')])
        post = next(d for d in env.core_stub.drafts.values() if d['audience'] == 'public' and d['kind'] == 'post')
        self.assertEqual(post['media_hash'], file_sha256(env.root / 'media' / 'a-card.png'))   # the real file is bound by hash

    def test_the_cli_class_never_allows_direct_sending(self):
        self.assertFalse(Executor.ALLOW_DIRECT)
        self.assertFalse(type(self.env.ex).ALLOW_DIRECT)


class DeliveryTests(StrictBase):
    def test_approved_draft_is_sent_once_with_the_approved_content_and_reported(self):
        env = self.env
        did = self.approved_reply()
        result = env.ex.deliver_approved()
        self.assertEqual((result['status'], result['verification'], result['reported']), ('accepted', 'own_page_exact', 'verified'))
        _, path, body = env.service.writes()[0]
        self.assertEqual((path, body), ('/api/v1/feeds/comment/reply',
                                        {'feed_id': FEED, 'xsec_token': TOKEN, 'comment_id': 'cm0002', 'content': '谢谢你的留言'}))
        self.assertEqual(env.core_stub.reports, [(did, 'verified')])
        activity = env.core_stub.of('xhs_activity')[-1]['action']
        self.assertEqual((activity['draft_id'], activity['status'], activity['audience']), (did, 'succeeded', 'public'))
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')
        env.ex.tick()
        self.assertEqual(len(env.service.writes()), 1)             # never a second send

    def test_tick_delivers_approved_drafts(self):
        env = self.env
        self.approved_reply()
        out = env.ex.tick()
        self.assertEqual(out['delivery']['status'], 'accepted')

    def test_tampered_claims_never_reach_http(self):
        cases = {'content': lambda c: c.update(content='恶意内容'), 'tags': lambda c: c.update(tags=['x']),
                 'audience': lambda c: c.update(audience='self_only'), 'account': lambda c: c.update(bot_account_id='u' + 'z' * 11),
                 'feed': lambda c: c['target'].update(feed_id='z' * 24), 'comment': lambda c: c['target'].update(comment_id='cm9999'),
                 'author': lambda c: c['target'].update(author_id='evil'), 'comment_hash': lambda c: c['target'].update(comment_hash='0' * 64),
                 'media_hash': lambda c: c.update(media_hash='f' * 64), 'image': lambda c: c.update(image_id='evil'),
                 'title': lambda c: c.update(title='标题'), 'version': lambda c: c.update(version=2),
                 'approved_version': lambda c: c.update(approved_version=9), 'hash': lambda c: c.update(hash='0' * 64),
                 'kind': lambda c: c.update(kind='comment')}
        for name, tamper in cases.items():
            env = Env(self, strict=True)
            env.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢你的留言'}
            did = self.approved_reply(env)
            env.core_stub.tamper = tamper
            result = env.ex.deliver_approved()
            self.assertEqual(result['status'], 'refused:hash_mismatch', name)
            self.assertEqual(env.service.writes(), [], name)
            self.assertEqual(env.core_stub.reports[-1], (did, 'failed_not_sent'), name)
        env = Env(self, strict=True)
        env.core_stub.decisions['reply'] = {'willing': True, 'text': 'x'}
        self.approved_reply(env)
        env.core_stub.tamper = lambda c: c.pop('target')
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:invalid_claim')
        self.assertEqual(env.service.writes(), [])

    def test_missing_expired_or_unreadable_token_is_never_sent_blind(self):
        for how in ('deleted', 'expired', 'error'):
            env = Env(self, strict=True)
            env.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢'}
            self.approved_reply(env)
            if how == 'deleted':
                env.store.tokens.execute('DELETE FROM draft_tokens')
            elif how == 'expired':
                env.clock.t += 30 * 3600
            else:
                env.store.get_token = lambda feed_id: (_ for _ in ()).throw(RuntimeError('db locked'))
            self.assertEqual(env.ex.deliver_approved()['status'], 'refused:token_unavailable', how)
            self.assertEqual(env.service.writes(), [], how)

    def test_token_reference_failure_means_no_draft_and_no_false_success(self):
        env = self.env
        env.ex.poll_notifications()
        env.clock.t += 120
        env.notify('cm0002', 'hello')
        real = env.store.put_token
        env.store.put_token = lambda *a, **k: (_ for _ in ()).throw(OSError('disk full'))
        result = env.ex.poll_notifications()['results'][0]
        self.assertEqual(result['status'], 'token_store_failed')
        self.assertEqual(env.core_stub.drafts, {})                 # the core never heard of it
        self.assertFalse(env.store.is_seen('c:cm0002'))            # still waiting, not silently dropped
        env.store.put_token = real
        self.assertEqual(env.ex.poll_notifications()['results'][0]['status'], 'draft_submitted')

    def test_target_changed_since_the_owner_saw_it_is_refused(self):
        for how in ('edited', 'author', 'deleted', 'self'):
            env = Env(self, strict=True)
            env.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢'}
            self.approved_reply(env)
            target = next(c for c in env.service.comments if c['id'] == 'cm0002')
            if how == 'edited':
                target['content'] = 'edited after approval'
            elif how == 'author':
                target['userInfo']['userId'] = 'u' + 'c' * 11
            elif how == 'deleted':
                env.service.comments.remove(target)
            else:
                target['userInfo']['userId'] = UID
            self.assertEqual(env.ex.deliver_approved()['status'], 'refused:target_changed', how)
            self.assertEqual(env.service.writes(), [], how)

    def test_switches_pause_limits_and_account_are_rechecked_at_delivery(self):
        env = self.env
        did = self.approved_reply()
        env.ex.cfg = {**env.cfg, 'reply_enabled': False}
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')    # reply is not even asked for: the approval is not burned
        self.assertEqual(env.core_stub.drafts[did]['status'], 'approved')
        env.ex.cfg = env.cfg
        (env.store.root / 'PAUSE').write_text('x')
        self.assertTrue(env.ex.deliver_approved()['status'].startswith('paused'))
        self.assertEqual(env.core_stub.drafts[did]['status'], 'approved')
        (env.store.root / 'PAUSE').unlink()
        env.service.uid = OTHER                                   # the logged-in account is no longer the bound one
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:identity_not_confirmed')
        self.assertEqual(env.service.writes(), [])
        limited = Env(self, strict=True, limits={'daily_replies': 0})
        limited.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢'}
        drafted = self.drafted(limited)                           # drafting is not blocked by an exhausted send quota...
        self.assertEqual(drafted['status'], 'draft_submitted')
        limited.core_stub.approve(drafted['draft_id'])
        self.assertEqual(limited.ex.deliver_approved()['status'], 'no_approved_draft')   # ...the send limit still is enforced
        self.assertEqual(limited.core_stub.drafts[drafted['draft_id']]['status'], 'approved')
        self.assertEqual(limited.service.writes(), [])

    def test_reviewed_comments_keep_the_note_author_and_the_per_author_limit(self):
        env = Env(self, strict=True, limits={'per_author_daily': 1})
        env.service.note_author = OTHER
        other_note = 'g' * 24                                   # a second note by the same author
        self.assertEqual(env.ex.manual_comment(FEED, TOKEN, '评论一')['status'], 'draft_submitted')
        self.assertEqual(env.ex.manual_comment(other_note, TOKEN, '评论二')['status'], 'draft_submitted')
        first, second = list(env.core_stub.drafts)
        for did in (first, second):
            self.assertEqual(env.core_stub.drafts[did]['target']['author_id'], OTHER)    # the author is part of what the owner approves
            env.core_stub.approve(did)
        self.assertEqual(env.ex.deliver_approved()['status'], 'accepted')
        self.assertEqual(env.store.actions('kind=?', ('comment',))[0]['author'], OTHER)   # counted against the original limit
        env.clock.t += 400                                        # past min_gap_comment: only the author limit can stop it now
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:author_limit')
        self.assertEqual(len(env.service.writes()), 1)

    def two_reply_drafts_on_one_note(self, env):
        # different texts: identical text to the same account would (rightly) trip the duplicate-content guard
        env.core_stub.decisions['reply'] = lambda payload: {'willing': True, 'text': '谢谢' + payload['material']['target_comment']}
        env.ex.poll_notifications()
        env.clock.t += 120
        env.notify('cm0002', 'first')
        env.notify('cm0003', 'second')
        results = env.ex.poll_notifications()['results']
        self.assertEqual([r['status'] for r in results], ['draft_submitted'] * 2)
        drafts = [env.core_stub.drafts[r['draft_id']] for r in results]
        self.assertEqual({d['target']['feed_id'] for d in drafts}, {FEED})
        self.assertEqual(len({d['target']['comment_id'] for d in drafts}), 2)
        for d in drafts:
            env.core_stub.approve(d['draft_id'])
        return drafts

    def test_drafts_on_one_note_share_its_token_and_each_can_be_approved_and_sent(self):
        env = Env(self, strict=True)
        drafts = self.two_reply_drafts_on_one_note(env)
        self.assertEqual(env.ex.deliver_approved()['status'], 'accepted')
        self.assertEqual(env.store.get_token(FEED), TOKEN)           # finishing one does not remove what the other needs
        env.clock.t += 120                                            # past min_gap_reply
        self.assertEqual(env.ex.deliver_approved()['status'], 'accepted')
        sent = [w[2] for w in env.service.writes()]
        self.assertEqual({w['comment_id'] for w in sent}, {d['target']['comment_id'] for d in drafts})
        self.assertEqual({w['xsec_token'] for w in sent}, {TOKEN})
        self.assertEqual(env.store.get_token(FEED), TOKEN)           # only the short TTL ends it

    def test_a_refused_draft_does_not_strand_the_other_draft_on_the_same_note(self):
        env = Env(self, strict=True)
        drafts = self.two_reply_drafts_on_one_note(env)
        first = next(c for c in env.service.comments if c['id'] == drafts[0]['target']['comment_id'])
        first['content'] = 'edited after approval'                  # the first draft's target changed: it is refused
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:target_changed')
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.store.get_token(FEED), TOKEN)
        second = env.ex.deliver_approved()
        self.assertEqual(second['status'], 'accepted')               # the other draft still has its token
        self.assertEqual(env.service.writes()[0][2]['comment_id'], drafts[1]['target']['comment_id'])
        env.clock.t += 27 * 3600                                      # the TTL, not any completion, is what removes it
        self.assertEqual(env.store.get_token(FEED), '')

    def test_the_note_author_is_checked_again_when_a_comment_is_delivered(self):
        env = Env(self, strict=True)
        env.service.note_author = OTHER
        env.ex.manual_comment(FEED, TOKEN, '评论')
        (did,) = env.core_stub.drafts
        env.core_stub.approve(did)
        env.service.note_author = 'u' + 'c' * 11                  # the note is no longer by the author the owner saw
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:target_changed')
        self.assertEqual(env.service.writes(), [])

    def test_a_changed_image_file_is_not_sent(self):
        env = self.env
        env.ex.publish('今日小发现', '一些想法', 'card1', manual=True)
        (did,) = env.core_stub.drafts
        env.core_stub.approve(did)
        (env.root / 'media' / 'a-card.png').write_bytes(PNG + b'changed')     # a different file under the same material id
        self.assertEqual(env.ex.deliver_approved()['status'], 'refused:image_changed')
        self.assertEqual(env.service.writes(), [])

    def test_an_approved_post_is_published_as_approved(self):
        env = self.env
        env.ex.publish('今日小发现', '一些想法', 'card1', ['AI'], manual=True)
        (did,) = env.core_stub.drafts
        env.core_stub.approve(did)
        result = env.ex.deliver_approved()
        self.assertEqual((result['status'], result['verification'], result['reported']), ('accepted', 'own_page_exact', 'verified'))
        _, path, body = env.service.writes()[0]
        self.assertEqual((path, body['title'], body['content'], body['tags'], body['visibility']), ('/api/v1/publish', '今日小发现', '一些想法', ['AI'], '公开可见'))
        self.assertEqual(env.core_stub.reports, [(did, 'verified')])

    def test_unknown_outcome_is_reported_and_never_resent(self):
        env = self.env
        did = self.approved_reply()
        env.service.fail = 'ambiguous_lost_before'
        result = env.ex.deliver_approved()
        self.assertEqual((result['status'], result['reported']), ('unknown', 'unknown'))
        env.service.fail = None
        env.store.put('backoff_until', 0)
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')
        env.restart()
        env.ex.tick()
        self.assertEqual(len(env.service.writes()), 1)
        self.assertEqual(env.core_stub.reports, [(did, 'unknown')])

    def test_unreachable_core_report_is_retried_after_restart_without_resending(self):
        env = self.env
        did = self.approved_reply()
        env.core_stub.report_down = True
        env.ex.deliver_approved()
        self.assertEqual(env.core_stub.reports, [])
        self.assertEqual(len(env.store.unreported()), 1)
        env.restart()
        env.core_stub.report_down = False
        env.ex.report_pending()
        self.assertEqual(env.core_stub.reports, [(did, 'verified')])
        self.assertEqual(env.store.unreported(), [])
        self.assertEqual(len(env.service.writes()), 1)

    def test_late_strict_readback_after_a_restart_is_reported_as_verified_only_then(self):
        env = self.env
        env.service.comments = [comment('cm0009', 'other root', user=OTHER, when=env.clock())]
        env.service.misplace_reply_under = 'cm0009'              # accepted, but it does not read back under the exact target yet
        did = self.approved_reply()
        result = env.ex.deliver_approved()
        self.assertEqual((result['status'], result['verification'], result['reported']), ('accepted', 'pending', 'accepted'))
        self.assertEqual(env.core_stub.reports, [(did, 'accepted')])
        env.restart()                                            # the readback arrives after a restart
        other = next(c for c in env.service.comments if c['id'] == 'cm0009')
        moved = other['subComments'].pop()
        next(c for c in env.service.comments if c['id'] == 'cm0002')['subComments'].append(moved)
        env.ex.reconcile()
        env.ex.report_pending()
        self.assertEqual(env.core_stub.reports, [(did, 'accepted'), (did, 'verified')])
        env.ex.report_pending()
        self.assertEqual(len(env.core_stub.reports), 2)
        self.assertEqual(len(env.service.writes()), 1)           # still exactly one send


class SelfAcceptanceTests(StrictBase):
    def test_private_acceptance_is_direct_but_can_never_be_public(self):
        env = self.env
        self.assertEqual(env.ex.publish('私密验收', '正文', 'card1', manual=True, self_acceptance=True, visibility='公开可见')['status'],
                         'self_acceptance_is_private_only')
        self.assertEqual(env.ex.publish('私密验收', '正文', 'card1', self_acceptance=True)['status'], 'self_acceptance_is_manual_only')
        self.assertEqual(env.service.writes(), [])
        result = env.ex.publish('私密验收', '正文', 'card1', manual=True, self_acceptance=True)
        self.assertEqual((result['status'], result['verification']), ('accepted', 'own_page_exact'))
        self.assertEqual(env.service.writes()[0][2]['visibility'], '仅自己可见')
        self.assertEqual(env.core_stub.of('xhs_activity')[-1]['action']['audience'], 'self_only')
        self.assertTrue(env.store.is_private_feed('n' * 24))

    def test_free_comments_only_on_a_note_we_proved_private_everything_else_is_reviewed_or_refused(self):
        env = self.env
        env.service.comments = [comment('cm0001', 'my root', user=UID, when=env.clock())]
        # not provable: refused, and the same text without the option is an ordinary owner-review draft
        self.assertEqual(env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复自己', self_acceptance=True)['status'],
                         'self_acceptance_requires_verified_self_only_post')
        self.assertEqual(env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复自己')['status'], 'cannot_reply_to_self')
        env.ex.publish('私密验收', '正文', 'card1', manual=True, self_acceptance=True)
        private = 'n' * 24
        sent = env.ex.manual_reply(private, TOKEN, 'cm0001', '回复自己', self_acceptance=True)
        self.assertEqual((sent['status'], sent['verification']), ('accepted', 'own_page_exact'))
        self.assertEqual(len(env.service.writes()), 2)
        self.assertEqual(env.core_stub.of('xhs_activity')[-1]['action']['audience'], 'self_only')
        # another feed of ours is still not private, however it is phrased
        self.assertEqual(env.ex.manual_comment(FEED, TOKEN, '想直接评论', self_acceptance=True)['status'], 'self_acceptance_requires_verified_self_only_post')
        self.assertEqual(env.ex.manual_comment(FEED, TOKEN, '想直接评论')['status'], 'draft_submitted')
        self.assertEqual(len(env.service.writes()), 2)

    def test_a_private_test_post_that_was_not_read_back_proves_nothing(self):
        env = self.env
        env.service.hide_publish = True
        env.ex.publish('私密验收', '正文', 'card1', manual=True, self_acceptance=True)
        self.assertFalse(env.store.is_private_feed('n' * 24))
        env.service.comments = [comment('cm0001', 'my root', user=UID, when=env.clock())]
        self.assertEqual(env.ex.manual_reply('n' * 24, TOKEN, 'cm0001', '回复', self_acceptance=True)['status'],
                         'self_acceptance_requires_verified_self_only_post')

    def test_there_is_no_way_to_register_an_arbitrary_note(self):
        self.assertFalse(hasattr(Executor, 'register_private_feed'))
        self.assertFalse(hasattr(Store, 'add_private_feed'))


class QueueEconomyTests(unittest.TestCase):
    def strict(self, **over):
        env = Env(self, strict=True, **over)
        env.core_stub.decisions.update(select={'action': 'post'}, intro={**post_decision(), 'purpose': 'intro', 'title': '我是大肥鱼'},
                                       post=post_decision())
        return env

    def plans(self, env):
        return [p['task'] for p in env.core_stub.of('xhs_plan')]

    def test_the_first_public_post_is_drafted_as_the_introduction(self):
        env = self.strict()
        env.core_stub.intro_done = False
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'draft_submitted')
        self.assertEqual(self.plans(env), ['select', 'intro'])
        (draft,) = env.core_stub.drafts.values()
        self.assertEqual((draft['kind'], draft['purpose'], draft['audience'], draft['image_id']), ('post', 'intro', 'public', 'card1'))
        self.assertEqual(env.service.writes(), [])
        done = self.strict()
        self.assertEqual(done.ex.autonomy_step(force=True)['status'], 'draft_submitted')
        self.assertEqual(self.plans(done), ['select', 'post'])
        self.assertEqual(next(iter(done.core_stub.drafts.values()))['purpose'], 'general')

    def test_an_autonomous_comment_draft_binds_the_note_author(self):
        env = self.strict()
        env.service.note_author = OTHER
        env.service.feeds = [{'modelType': 'note', 'id': FEED, 'xsecToken': TOKEN,
                              'noteCard': {'displayTitle': 'title', 'user': {'userId': OTHER, 'nickname': 'n'}, 'interactInfo': {'likedCount': '1'}}}]
        env.core_stub.decisions.update(select={'action': 'comment', 'ref': 'f1'}, comment={'willing': True, 'text': '有意思的想法'})
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'draft_submitted')
        (draft,) = env.core_stub.drafts.values()
        self.assertEqual((draft['kind'], draft['target']['feed_id'], draft['target']['author_id']), ('comment', FEED, OTHER))
        self.assertEqual(env.service.writes(), [])

    def test_a_full_review_queue_spends_no_model_budget_and_no_browse(self):
        env = self.strict()
        env.core_stub.can_submit = False
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'draft_queue_full')
        self.assertEqual((self.plans(env), env.store.count_browses(), env.store.model_calls_today()), ([], 0, 0))
        env.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢'}
        env.ex.poll_notifications()
        env.clock.t += 120
        env.notify('cm0002', 'hello')
        result = env.ex.poll_notifications()['results'][0]
        self.assertEqual(result['status'], 'draft_queue_full')
        self.assertFalse(env.store.is_seen('c:cm0002'))            # the message waits for room in the queue
        self.assertEqual((self.plans(env), env.store.model_calls_today()), ([], 0))
        env.core_stub.can_submit = True
        env.clock.t += 120
        self.assertEqual(env.ex.poll_notifications()['results'][0]['status'], 'draft_submitted')

    def test_a_waiting_or_unchecked_introduction_is_not_drafted_again(self):
        env = self.strict()
        env.core_stub.intro_done, env.core_stub.intro_active = False, True
        for _ in range(3):
            self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'nothing_to_consider')
            env.clock.t += 6 * 3600
        self.assertEqual(self.plans(env), [])
        self.assertEqual(env.store.model_calls_today(), 0)
        self.assertEqual(env.store.get('last_post_skip')['reason'], 'intro_awaiting_review')
        self.assertEqual(env.core_stub.drafts, {})
        # a private-visibility configuration is not held back by the public introduction
        private = self.strict(visibility='仅自己可见')
        private.core_stub.intro_done, private.core_stub.intro_active = False, True
        self.assertEqual(private.ex.autonomy_step(force=True)['status'], 'draft_submitted')
        self.assertEqual(next(iter(private.core_stub.drafts.values()))['audience'], 'self_only')

    def test_drafting_is_not_blocked_by_an_exhausted_post_quota_but_sending_is(self):
        env = self.strict(limits={'daily_posts': 0})
        env.core_stub.intro_done = False
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'draft_submitted')
        (did,) = env.core_stub.drafts
        env.core_stub.approve(did)
        self.assertEqual(env.ex.deliver_approved()['status'], 'no_approved_draft')   # the quota is enforced where it counts
        self.assertEqual(env.core_stub.drafts[did]['status'], 'approved')              # and the approval is kept for later
        self.assertEqual(env.service.writes(), [])

    def test_queue_state_is_asked_once_per_cycle(self):
        env = self.strict()
        env.ex.autonomy_step(force=True)
        self.assertEqual(len(env.core_stub.of('xhs_draft_status')), 1)


if __name__ == '__main__':
    unittest.main()
