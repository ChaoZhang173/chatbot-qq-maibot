"""Targeted tests for the first review round (items 7-11)."""
import json, os, tempfile, unittest
from pathlib import Path
from xhs_support import FEED, OTHER, PNG, TOKEN, UID, Env, comment
from xhs_support import post_decision
from xiaohongshu import textcard
from xiaohongshu.media import MediaError, PublishableMedia
from xiaohongshu.runner import Skip


class ModelAndBrowseBudgetTests(unittest.TestCase):
    def test_post_reserves_draft_and_review_calls(self):
        env = Env(self)
        env.core_stub.decisions['post'] = {'willing': False}
        env.ex._plan('post', {})
        self.assertEqual(env.store.model_calls_today(), 2)
        env.ex._plan('reply', {})
        self.assertEqual(env.store.model_calls_today(), 3)

    def test_post_does_not_start_with_one_call_left(self):
        env = Env(self, limits={'daily_model_calls': 4})
        env.store.reserve_model_call('earlier', 4, cost=3)
        with self.assertRaises(Skip):
            env.ex._plan('post', {})
        self.assertEqual(env.store.model_calls_today(), 3)  # a refused reservation consumes nothing
        self.assertEqual(env.core_stub.payloads, [])
        self.assertEqual(env.ex._precheck('post'), 'model_daily_limit')
        # an autonomous attempt that could end in a post (select + draft + review) needs 3 calls
        env2 = Env(self, limits={'daily_model_calls': 4})
        env2.store.reserve_model_call('earlier', 4, cost=2)
        self.assertEqual(env2.ex._precheck('post'), 'model_daily_limit')
        env2.core_stub.decisions['select'] = {'action': 'post'}
        self.assertEqual(env2.ex.autonomy_step(force=True)['status'], 'nothing_to_consider')
        self.assertEqual(env2.service.writes(), [])
        self.assertEqual(env2.core_stub.of('xhs_plan'), [])

    def test_failed_model_call_still_counts(self):
        env = Env(self)
        env.core_stub.down = True
        env.ex.cfg = env.cfg
        with self.assertRaises(Exception):
            env.ex._plan('reply', {})
        self.assertEqual(env.store.model_calls_today(), 1)

    def test_failed_browse_is_counted_before_the_request(self):
        env = Env(self)
        env.service.fail_list = True
        r = env.ex.autonomy_step(force=True)
        self.assertEqual(r['status'], 'backoff')
        self.assertEqual(env.store.count_browses(), 1)


class FinalGateTests(unittest.TestCase):
    def pause_file(self, env):
        return env.store.root / 'PAUSE'

    def setup_pending_reply(self, env):
        env.ex.poll_notifications()
        env.clock.t += 120
        env.notify('cm0002', 'new message')

    def test_pause_while_drafting_prevents_the_send_and_is_not_unknown(self):
        env = Env(self)
        pause = self.pause_file(env)

        def draft(payload):
            pause.write_text('operator pause')
            return {'willing': True, 'text': '好的'}
        env.core_stub.decisions['reply'] = draft
        self.setup_pending_reply(env)
        result = env.ex.poll_notifications()['results'][0]
        self.assertTrue(result['status'].startswith('gated:'), result)
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.store.actions(), [])          # no row, no budget, no "unknown"
        self.assertEqual(env.store.count_actions('reply'), 0)
        self.assertFalse(env.store.is_seen('c:cm0002'))     # the message is still waiting
        pause.unlink()
        env.core_stub.decisions['reply'] = {'willing': True, 'text': '好的'}
        self.assertEqual(env.ex.poll_notifications()['results'][0]['status'], 'accepted')

    def test_pause_during_identity_verification_prevents_the_send(self):
        env = Env(self)
        env.core_stub.decisions['reply'] = {'willing': True, 'text': '好的'}
        self.setup_pending_reply(env)
        env.service.on_status = lambda n: self.pause_file(env).write_text('x') if n == 2 else None
        result = env.ex.poll_notifications()['results'][0]
        self.assertTrue(result['status'].startswith('gated:'), result)
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.store.actions(), [])

    def test_pause_before_publish_and_comment_writes(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'hi')]
        env.service.on_status = lambda n: self.pause_file(env).write_text('x')
        self.assertTrue(env.ex.manual_comment(FEED, TOKEN, '评论')['status'].startswith(('gated:', 'identity')))
        self.assertTrue(env.ex.publish('标题', '正文', 'card1', manual=True)['status'].startswith(('gated:', 'identity', 'paused')))
        self.assertEqual(env.service.writes(), [])

    def test_gate_checked_again_right_before_network_write(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'hi')]
        calls = []
        real_gate = env.ex.gate

        def gate(kind=None):
            calls.append(kind)
            if len(calls) == 3:  # 1: _manual, 2: before reserve, 3: after reserve / just before send
                self.pause_file(env).write_text('x')
            return real_gate(kind)
        env.ex.gate = gate
        result = env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复')
        self.assertTrue(result['status'].startswith('gated:'), result)
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.store.actions(), [])


class ReadbackPrecisionTests(unittest.TestCase):
    def test_reply_to_root_comment_is_exact(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'root'), comment('cm0009', 'other root')]
        r = env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复')
        self.assertEqual(r['verification'], 'own_page_exact')

    def test_reply_to_sub_comment_is_only_same_thread_never_succeeded(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'root', subs=[comment('cm0002', 'child a'), comment('cm0003', 'child b')])]
        r = env.ex.manual_reply(FEED, TOKEN, 'cm0002', '回复')
        self.assertEqual((r['status'], r['verification']), ('accepted', 'same_thread'))
        sent = env.core_stub.of('xhs_activity')[-1]['action']
        self.assertEqual(sent['status'], 'pending_visibility')
        self.assertEqual(sent['remote_id'], '')
        self.assertEqual(env.ex.status()['pending_verification'], 1)

    def test_same_thread_different_target_cannot_verify_exactly(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'root', subs=[comment('cm0002', 'child a'), comment('cm0003', 'child b')])]
        env.ex.manual_reply(FEED, TOKEN, 'cm0003', '回复一')
        # the identical text is now in the thread; replying to the other child must not read as exact either
        row = env.store.actions(limit=1)[0]
        self.assertNotEqual(row['verification'], 'own_page_exact')

    def test_reply_landing_in_another_thread_is_not_verified(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'root'), comment('cm0009', 'other root')]
        env.service.misplace_reply_under = 'cm0009'
        r = env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复')
        self.assertEqual((r['status'], r['verification']), ('accepted', 'pending'))
        self.assertEqual(env.core_stub.of('xhs_activity')[-1]['action']['status'], 'pending_visibility')

    def test_top_level_comment_must_read_back_top_level(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'root')]
        r = env.ex.manual_comment(FEED, TOKEN, '主动评论')
        self.assertEqual(r['verification'], 'own_page_exact')

    def test_post_body_must_match_whole_normalised_body(self):
        env = Env(self)
        env.service.force_suffix = ' 后面还有很多别的正文内容'
        r = env.ex.publish('标题', '短草稿', 'card1', manual=True)
        self.assertEqual((r['status'], r['verification']), ('accepted', 'pending'))
        self.assertEqual(env.core_stub.of('xhs_activity')[-1]['action']['status'], 'pending_visibility')

    def test_real_shape_title_with_trailing_whitespace_verifies_but_a_different_title_does_not(self):
        # a real note showed 'title ' (one trailing space) in user/me and in the detail
        env = Env(self, limits={'image_reuse_days': 0, 'daily_posts': 3})
        env.service.title_suffix = ' '
        r = env.ex.publish('大肥鱼游进小红书', '这是正文', 'card1', manual=True, visibility='仅自己可见')
        self.assertEqual((r['status'], r['verification']), ('accepted', 'own_page_exact'))
        # the duplicate-title guard compares the same way, so the stray space cannot hide an existing note
        self.assertEqual(env.ex.publish('大肥鱼游进小红书', '另一段正文', 'card1', manual=True, visibility='仅自己可见')['status'], 'title_exists')
        self.assertEqual(env.ex.publish('  大肥鱼游进小红书  ', '又一段正文', 'card1', manual=True, visibility='仅自己可见')['status'], 'title_exists')
        # only whitespace is tolerated: another non-empty title (or a longer one) is not a match
        for other in (' 另一个', '·'):
            env2 = Env(self, limits={'image_reuse_days': 0})
            env2.service.title_suffix = other
            r2 = env2.ex.publish('大肥鱼游进小红书', '这是正文', 'card1', manual=True, visibility='仅自己可见')
            self.assertEqual((r2['status'], r2['verification']), ('accepted', 'pending'), other)
        # and the unchanged rules still hold with the whitespace: a longer body is not an exact match
        env3 = Env(self, limits={'image_reuse_days': 0})
        env3.service.title_suffix = ' '
        env3.service.force_suffix = ' 后面还有别的正文'
        self.assertEqual(env3.ex.publish('大肥鱼游进小红书', '这是正文', 'card1', manual=True)['verification'], 'pending')

    def test_post_body_allows_known_tag_display_only(self):
        env = Env(self)
        r = env.ex.publish('标题甲', '完整 正文\n第二行', 'card1', ['AI', '游戏'], manual=True)
        self.assertEqual(r['verification'], 'own_page_exact')
        env2 = Env(self)
        env2.service.force_suffix = ' #别的话题[话题]#'
        r2 = env2.ex.publish('标题乙', '完整正文', 'card1', ['AI'], manual=True)
        self.assertEqual(r2['verification'], 'pending')


class DeferredNotificationTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.env.core_stub.decisions['reply'] = {'willing': True, 'text': '谢谢'}
        self.env.ex.poll_notifications()
        self.env.clock.t += 120

    def test_target_found_by_bounded_deep_load(self):
        env = self.env
        env.notify('cm0002', 'late comment')
        env.service.hidden_until_deep = {'cm0002'}
        r = env.ex.poll_notifications()['results'][0]
        self.assertEqual(r['status'], 'accepted')
        deep = [c for c in env.service.calls if c[1] == '/api/v1/feeds/detail' and c[2]['load_all_comments']]
        self.assertEqual(len(deep), 1)
        self.assertLessEqual(deep[0][2]['comment_config']['max_comment_items'], 100)

    def test_target_never_found_is_deferred_with_bounded_retries(self):
        env = self.env
        env.notify('cm0002', 'gone')
        env.service.comments = [c for c in env.service.comments if c['id'] != 'cm0002']
        first = env.ex.poll_notifications()['results'][0]
        self.assertEqual((first['status'], first['attempt']), ('target_not_loaded', 1))
        self.assertFalse(env.store.is_seen('c:cm0002'))
        self.assertEqual(env.ex.poll_notifications()['results'], [])       # spaced: not retried immediately
        env.clock.t += 700
        self.assertEqual(env.ex.poll_notifications()['results'][0]['attempt'], 2)
        env.clock.t += 1300
        last = env.ex.poll_notifications()['results'][0]
        self.assertEqual(last['status'], 'target_not_loaded_final')
        self.assertTrue(env.store.is_seen('c:cm0002'))
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.ex.poll_notifications()['results'], [])

    def test_deferred_target_is_answered_when_it_appears_later(self):
        env = self.env
        env.notify('cm0002', 'comes later')
        env.service.comments = [c for c in env.service.comments if c['id'] != 'cm0002']
        self.assertEqual(env.ex.poll_notifications()['results'][0]['status'], 'target_not_loaded')
        env.service.comments.append(comment('cm0002', 'comes later', when=env.clock()))
        env.clock.t += 700
        self.assertEqual(env.ex.poll_notifications()['results'][0]['status'], 'accepted')

    def test_incomplete_notification_has_a_recovery_path(self):
        env = self.env
        env.notify('cm0002', 'no token yet', feed_xsec_token='')
        first = env.ex.poll_notifications()['results'][0]
        self.assertEqual((first['status'], first['attempt']), ('incomplete_notification', 1))
        self.assertFalse(env.store.is_seen('c:cm0002'))
        env.service.notifications[0]['feed_xsec_token'] = TOKEN
        env.clock.t += 700
        self.assertEqual(env.ex.poll_notifications()['results'][0]['status'], 'accepted')

    def test_dry_run_does_not_consume_retries(self):
        env = self.env
        env.notify('cm0002', 'gone')
        env.service.comments = [c for c in env.service.comments if c['id'] != 'cm0002']
        env.ex.poll_notifications(dry_run=True)
        self.assertEqual(env.store.get('retry:c:cm0002', 0), 0)


class MediaRobustnessTests(unittest.TestCase):
    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(os.path.realpath(tmp.name))

    def manifest(self, root, items, **extra):
        (root / 'manifest.json').write_text(json.dumps({'public': True, 'items': items, **extra}))

    def test_missing_file_skips_only_that_entry(self):
        root = self.make() / 'media'
        root.mkdir()
        (root / 'ok.png').write_bytes(PNG)
        self.manifest(root, [{'id': 'gone', 'file': 'gone.png', 'caption': 'x', 'publishable': True},
                             {'id': 'ok', 'file': 'ok.png', 'caption': 'y', 'publishable': True}])
        self.assertEqual([i['id'] for i in PublishableMedia(root).items()], ['ok'])
        with self.assertRaises(MediaError):
            PublishableMedia(root).resolve('gone')

    def test_entry_that_is_a_directory_or_unreadable_is_skipped(self):
        root = self.make() / 'media'
        root.mkdir()
        (root / 'dir.png').mkdir()
        (root / 'ok.png').write_bytes(PNG)
        self.manifest(root, [{'id': 'dir', 'file': 'dir.png', 'caption': '', 'publishable': True},
                             {'id': 'ok', 'file': 'ok.png', 'caption': '', 'publishable': True}])
        self.assertEqual([i['id'] for i in PublishableMedia(root).items()], ['ok'])

    def test_symlinked_ancestor_directory_is_rejected(self):
        base = self.make()
        (base / 'real' / 'media').mkdir(parents=True)
        (base / 'real' / 'media' / 'ok.png').write_bytes(PNG)
        self.manifest(base / 'real' / 'media', [{'id': 'ok', 'file': 'ok.png', 'caption': '', 'publishable': True}])
        self.assertEqual(len(PublishableMedia(base / 'real' / 'media').items()), 1)
        (base / 'link').symlink_to(base / 'real')
        media = PublishableMedia(base / 'link' / 'media')
        self.assertEqual(media.items(), [])
        with self.assertRaises(MediaError):
            media.resolve('ok')

    def test_symlinked_leaf_directory_and_files_are_rejected(self):
        base = self.make()
        (base / 'real').mkdir()
        (base / 'real' / 'ok.png').write_bytes(PNG)
        self.manifest(base / 'real', [{'id': 'ok', 'file': 'ok.png', 'caption': '', 'publishable': True}])
        (base / 'leaf').symlink_to(base / 'real')
        self.assertEqual(PublishableMedia(base / 'leaf').items(), [])
        root = base / 'media'
        root.mkdir()
        outside = base / 'private-gallery.png'
        outside.write_bytes(PNG)
        (root / 'link.png').symlink_to(outside)
        self.manifest(root, [{'id': 'link', 'file': 'link.png', 'caption': '', 'publishable': True}])
        self.assertEqual(PublishableMedia(root).items(), [])

    def test_traversal_size_magic_and_manifest_rules(self):
        root = self.make() / 'media'
        root.mkdir()
        (root / 'big.png').write_bytes(PNG + b'0' * (5 * 1024 * 1024))
        (root / 'fake.png').write_bytes(b'not an image at all')
        (root / 'ok.png').write_bytes(PNG)
        self.manifest(root, [{'id': 'trav', 'file': '../x.png', 'caption': '', 'publishable': True},
                             {'id': 'abs', 'file': '/etc/hosts', 'caption': '', 'publishable': True},
                             {'id': 'big', 'file': 'big.png', 'caption': '', 'publishable': True},
                             {'id': 'fake', 'file': 'fake.png', 'caption': '', 'publishable': True},
                             {'id': 'off', 'file': 'ok.png', 'caption': '', 'publishable': False},
                             {'id': 'ok', 'file': 'ok.png', 'caption': '', 'publishable': True}])
        self.assertEqual([i['id'] for i in PublishableMedia(root).items()], ['ok'])
        (root / 'manifest.json').write_text(json.dumps({'items': [{'id': 'ok', 'file': 'ok.png', 'caption': '', 'publishable': True}]}))
        self.assertEqual(PublishableMedia(root).items(), [])  # a manifest must declare "public": true


class ImageReuseWindowTests(unittest.TestCase):
    def env(self, **limits):
        return Env(self, limits={'daily_posts': 3, **limits})

    def test_reused_real_media_is_blocked_before_publish_and_not_offered(self):
        env = self.env()
        self.assertEqual(env.ex.publish('标题一', '正文一', 'card1', manual=True)['status'], 'accepted')
        self.assertEqual(env.ex.publish('标题二', '正文二', 'card1', manual=True)['status'], 'image_reused')
        self.assertEqual(env.ex.publish('标题二', '正文二', 'card1', dry_run=True, manual=True)['status'], 'image_reused')
        self.assertEqual(len(env.service.writes()), 1)
        # autonomous candidates: the only manifest image is inside the window, so nothing is offered
        env.clock.t += 7200
        env.core_stub.decisions['select'] = {'action': 'post'}
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'nothing_to_consider')
        self.assertEqual(env.store.get('last_post_skip')['reason'], 'media_recently_used')
        self.assertEqual(env.core_stub.of('xhs_plan'), [])

    def test_unknown_attempt_occupies_the_image_but_unsent_does_not(self):
        env = self.env()
        env.service.fail = 'ambiguous_lost_before'
        self.assertEqual(env.ex.publish('标题一', '正文一', 'card1', manual=True)['status'], 'unknown')
        env.service.fail = None
        env.store.put('backoff_until', 0)
        self.assertEqual(env.ex.publish('标题二', '正文二', 'card1', manual=True)['status'], 'image_reused')
        env2 = self.env()
        env2.service.fail = 'notsent'
        self.assertEqual(env2.ex.publish('标题一', '正文一', 'card1', manual=True)['status'], 'failed_not_sent')
        env2.service.fail = None
        self.assertEqual(env2.ex.publish('标题二', '正文二', 'card1', manual=True)['status'], 'accepted')

    def test_window_expires_and_zero_disables(self):
        env = self.env(image_reuse_days=30)
        env.ex.publish('标题一', '正文一', 'card1', manual=True)
        env.clock.t += 29 * 86400
        self.assertEqual(env.ex.publish('标题二', '正文二', 'card1', manual=True)['status'], 'image_reused')
        env.clock.t += 2 * 86400
        self.assertEqual(env.ex.publish('标题二', '正文二', 'card1', manual=True)['status'], 'accepted')
        off = self.env(image_reuse_days=0)
        off.ex.publish('标题一', '正文一', 'card1', manual=True)
        self.assertEqual(off.ex.publish('标题二', '正文二', 'card1', manual=True)['status'], 'accepted')

    def test_reservation_itself_enforces_the_window(self):
        env = self.env()
        first, _ = env.store.reserve('post', title='a', content='b', image_id='card1', limits=env.cfg['limits'], enforce_image_reuse=True)
        self.assertTrue(first)
        second, reason = env.store.reserve('post', title='c', content='d', image_id='card1', limits=env.cfg['limits'], enforce_image_reuse=True)
        self.assertEqual((second, reason), (None, 'image_reused'))
        other, _ = env.store.reserve('post', title='e', content='f', image_id='card2', limits=env.cfg['limits'], enforce_image_reuse=True, manual=True)
        self.assertTrue(other)

    def test_autonomous_post_skips_a_used_image_and_offers_the_rest(self):
        env = self.env()
        (env.root / 'media' / 'b-card.png').write_bytes(PNG)
        (env.root / 'media' / 'manifest.json').write_text(json.dumps({'public': True, 'items': [
            {'id': 'card1', 'file': 'a-card.png', 'caption': 'one', 'publishable': True},
            {'id': 'card2', 'file': 'b-card.png', 'caption': 'two', 'publishable': True}]}))
        env.ex.publish('标题一', '正文一', 'card1', manual=True)
        env.clock.t += 7200
        env.core_stub.decisions.update(select={'action': 'post'}, post=post_decision(image_id='card2'))
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'accepted')
        offered = env.core_stub.of('xhs_plan')[0]['candidates']['images']
        self.assertEqual([i['id'] for i in offered], ['card2'])


class SelfOnlyAcceptanceTests(unittest.TestCase):
    """Manual-only acceptance on the account's own test note: self-reply allowed, always self_only."""
    def audience_sent(self, env):
        return env.core_stub.of('xhs_activity')[-1]['action']['audience']

    def test_self_reply_needs_the_explicit_option(self):
        env = Env(self)
        env.private_feed()   # self-acceptance only works on a note we created as self-only and read back exactly
        env.service.comments = [comment('cm0001', 'my root comment', user=UID, when=env.clock())]
        self.assertEqual(env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复自己')['status'], 'cannot_reply_to_self')
        self.assertEqual(env.service.writes(), [])
        r = env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复自己', self_acceptance=True)
        self.assertEqual((r['status'], r['verification']), ('accepted', 'own_page_exact'))
        _, path, body = env.service.writes()[0]
        self.assertEqual((path, body['comment_id']), ('/api/v1/feeds/comment/reply', 'cm0001'))
        self.assertEqual(env.store.actions(limit=1)[0]['audience'], 'self_only')
        self.assertEqual(self.audience_sent(env), 'self_only')

    def test_note_must_be_authored_by_the_bound_account(self):
        env = Env(self)
        env.service.note_author = OTHER
        env.service.comments = [comment('cm0001', 'my root comment', user=UID, when=env.clock())]
        self.assertEqual(env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复', self_acceptance=True)['status'], 'self_acceptance_requires_own_note')
        self.assertEqual(env.ex.manual_comment(FEED, TOKEN, '评论', self_acceptance=True)['status'], 'self_acceptance_requires_own_note')
        self.assertEqual(env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复')['status'], 'cannot_reply_to_self')
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(env.store.actions(), [])

    def test_acceptance_comment_and_reply_to_others_are_self_only_too(self):
        env = Env(self)
        env.private_feed()
        env.service.comments = [comment('cm0001', 'someone else', user=OTHER, when=env.clock())]
        self.assertEqual(env.ex.manual_comment(FEED, TOKEN, '验收评论', self_acceptance=True)['status'], 'accepted')
        self.assertEqual(self.audience_sent(env), 'self_only')
        self.assertEqual(env.ex.manual_reply(FEED, TOKEN, 'cm0001', '验收回复', self_acceptance=True)['status'], 'accepted')
        self.assertEqual(self.audience_sent(env), 'self_only')
        self.assertEqual({r['audience'] for r in env.store.actions()}, {'self_only'})

    def test_ordinary_manual_actions_stay_public(self):
        env = Env(self)
        env.service.comments = [comment('cm0001', 'someone else', user=OTHER, when=env.clock())]
        env.ex.manual_reply(FEED, TOKEN, 'cm0001', '普通回复')
        self.assertEqual(self.audience_sent(env), 'public')

    def test_dry_run_reports_the_audience_and_sends_nothing(self):
        env = Env(self)
        env.private_feed()
        env.service.comments = [comment('cm0001', 'mine', user=UID, when=env.clock())]
        r = env.ex.manual_reply(FEED, TOKEN, 'cm0001', '回复', dry_run=True, self_acceptance=True)
        self.assertEqual((r['status'], r['audience']), ('dry_run', 'self_only'))
        self.assertEqual(env.service.writes(), [])
        self.assertEqual(len(env.store.actions()), 1)    # only the proof row of the private note itself

    def test_notifications_and_autonomy_still_refuse_our_own_account(self):
        env = Env(self)
        env.core_stub.decisions['reply'] = {'willing': True, 'text': '回复'}
        env.ex.poll_notifications()
        env.clock.t += 120
        env.notify('cm0002', '我自己的评论', user=UID)
        self.assertEqual(env.ex.poll_notifications()['results'][0]['status'], 'self_message')
        self.assertEqual((env.service.writes(), env.core_stub.of('xhs_plan')), ([], []))
        # autonomy never offers a note authored by the bound account
        (env.root / 'media' / 'manifest.json').write_text(json.dumps({'public': True, 'items': []}))
        env.service.feeds = [{'modelType': 'note', 'id': FEED, 'xsecToken': TOKEN,
                              'noteCard': {'displayTitle': 'mine', 'user': {'userId': UID, 'nickname': 'me'}, 'interactInfo': {}}}]
        self.assertEqual(env.ex.autonomy_step(force=True)['status'], 'nothing_to_consider')
        self.assertEqual((env.service.writes(), env.core_stub.of('xhs_plan')), ([], []))

    def test_cli_option_exists_only_for_comment_and_reply(self):
        from xiaohongshu import cli
        parser = cli.parser()
        args = parser.parse_args(['reply', '--feed-id', 'x', '--comment-id', 'y', '--text', 't', '--self-only-acceptance'])
        self.assertTrue(args.self_only_acceptance)
        self.assertFalse(parser.parse_args(['comment', '--feed-id', 'x', '--text', 't']).self_only_acceptance)
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(['poll-once', '--self-only-acceptance'])


class TextCardCleanupTests(unittest.TestCase):
    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(os.path.realpath(tmp.name))

    def test_prune_only_deletes_own_plain_card_files(self):
        base = self.make()
        root = base / 'media'
        root.mkdir()
        victim = base / 'victim.png'
        victim.write_bytes(PNG)
        linked_target = base / 'linked-target.png'
        linked_target.write_bytes(PNG)
        (root / 'card-aaaaaaaaaaaa.png').symlink_to(linked_target)
        (root / 'card-bbbbbbbbbbbb.png').mkdir()
        (root / 'keep-me.png').write_bytes(PNG)
        old = [('../victim.png', 'bad-traversal'), ('card-aaaaaaaaaaaa.png', 'bad-symlink'), ('card-bbbbbbbbbbbb.png', 'bad-dir'),
               ('keep-me.png', 'bad-name'), ('/etc/hosts', 'bad-abs'), ('card-0000000000zz.png', 'bad-hex')]
        items = [{'id': i, 'file': f, 'caption': '', 'publishable': True, 'source': 'generated_text_card'} for f, i in old]
        good = []
        for n in range(textcard.KEEP_CARDS):
            name = 'card-%012x.png' % n
            (root / name).write_bytes(PNG)
            good.append({'id': 'card-%012x' % n, 'file': name, 'caption': '', 'publishable': True, 'source': 'generated_text_card'})
        (root / 'manifest.json').write_text(json.dumps({'public': True, 'items': items + good}))
        textcard._register(root, 'card-ffffffffffff', 'card-ffffffffffff.png')
        manifest = json.loads((root / 'manifest.json').read_text())
        ids = [i['id'] for i in manifest['items']]
        for bad_id in ('bad-traversal', 'bad-symlink', 'bad-dir', 'bad-name', 'bad-abs', 'bad-hex'):
            self.assertNotIn(bad_id, ids)                  # bad entries leave the manifest...
        self.assertTrue(victim.exists())                   # ...but nothing outside the directory is touched
        self.assertTrue(linked_target.exists())
        self.assertTrue((root / 'card-aaaaaaaaaaaa.png').is_symlink())
        self.assertTrue((root / 'card-bbbbbbbbbbbb.png').is_dir())
        self.assertTrue((root / 'keep-me.png').exists())
        self.assertFalse((root / 'card-000000000000.png').exists())   # the oldest genuine card is pruned
        self.assertTrue((root / ('card-%012x.png' % (textcard.KEEP_CARDS - 1))).exists())
        self.assertEqual(sum(1 for i in manifest['items'] if i.get('source') == 'generated_text_card'), textcard.KEEP_CARDS)

    def test_render_rejects_symlinked_ancestor_and_leaf(self):
        base = self.make()
        (base / 'real' / 'media').mkdir(parents=True)
        (base / 'link').symlink_to(base / 'real')
        font = base / 'font.ttf'
        font.write_bytes(b'x')
        with self.assertRaises(textcard.CardError) as ctx:
            textcard.render(base / 'link' / 'media', str(font), '标题')
        self.assertEqual(str(ctx.exception), 'media_dir_symlink_rejected')
        (base / 'leaf').symlink_to(base / 'real' / 'media')
        with self.assertRaises(textcard.CardError):
            textcard.render(base / 'leaf', str(font), '标题')
        self.assertEqual(list((base / 'real' / 'media').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
