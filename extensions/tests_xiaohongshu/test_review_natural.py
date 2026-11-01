"""Natural-language owner review, status answers, bounded model use, and the operator-only recovery of a missed explicit approval.
All ids and accounts are synthetic."""
import importlib.util, json, os, time, unittest
from pathlib import Path
from maibot_unified_core import xiaohongshu_review as review
from maibot_unified_core.service import UnifiedService
from test_review_core import FEED, OTHER, OWNER, OWNER_SESSION, UID, Base

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'xiaohongshu_recover_approval.py'


class NaturalBase(Base):
    async def pushed(self, **over):
        """A draft that really reached the owner's chat (so she has 'seen' exactly this version)."""
        did = self.submit(**over)['draft_id']
        for _ in range(6):                      # result notices of earlier drafts may be queued ahead of this push
            await self.rv.tick()
            if self.rv.db.execute('SELECT 1 FROM xhs_review_shown WHERE draft_id=?', (did,)).fetchone():
                return did
        self.fail('the draft was never pushed')

    def claim(self, did, outcome=None):
        claim = self.rv.claim({'kinds': ['reply', 'post', 'comment']})['draft']
        self.assertEqual(claim['draft_id'], did)
        if outcome:
            self.rv.report({'draft_id': did, 'claim_token': claim['claim_token'], 'outcome': outcome})
        return claim

    def answer(self, did, intent='approve', confidence=0.95, version=1, **over):
        self.llm.answers.append({'intent': intent, 'confidence': confidence, 'draft_id': did, 'version': version, **over})


class RealIncidentTests(NaturalBase):
    async def test_the_incident_phrases_are_understood_and_status_is_the_real_state(self):
        did = await self.pushed()
        reply = await self.say('好的看起来内容没什么问题，发布吧', mid='m-natural')
        self.assertIn('已批准 %s v1' % did, reply)
        row = self.rv.row(did)
        self.assertEqual((row['status'], row['approved_by'], row['approved_source']), ('approved', OWNER, 'natural:m-natural'))
        self.assertTrue(self.svc.state.get('silent_operation:%s:m-natural' % OWNER_SESSION))      # the chat model stays out of it
        self.assertIn('在等执行器领取发送', await self.say('批准发布%sv1' % did))                 # the alias on an already approved draft: no double approval
        status = await self.say('发布的怎么样了？')
        self.assertIn('已批准，在等执行器领取发送', status)
        self.assertNotIn('已发出', status)
        self.assertEqual(self.llm.prompts, [])                                                     # cheap deterministic handling, no model
        self.claim(did)
        self.assertIn('执行器正在发送', await self.say('发出去了吗'))
        self.rv.report({'draft_id': did, 'claim_token': self.rv.row(did)['claim_token'], 'outcome': 'accepted'})
        delivered = await self.say('发布的怎么样了？')
        self.assertIn('读回还没确认', delivered)
        self.assertNotIn('已发出，并', delivered)
        self.rv.report({'draft_id': did, 'claim_token': self.rv.row(did)['claim_token'], 'outcome': 'verified'})
        self.assertIn('读回确认', await self.say('发出去了吗'))

    async def test_unknown_is_reported_as_unknown_and_pending_as_not_approved_not_sent(self):
        did = await self.pushed()
        pending = await self.say('发布的怎么样了？')
        self.assertIn('还没批准，也没有发送', pending)
        self.assertIn('发布吧', pending)               # and how to approve it
        await self.say('发布吧')
        self.claim(did, 'unknown')
        unknown = await self.say('发布的怎么样了？')
        self.assertIn('结果未知，不会自动重发', unknown)
        self.assertNotIn('已发出', unknown)

    async def test_whitespace_free_and_spaced_explicit_aliases_work_without_any_context(self):
        for text in ('批准发布%sv1', '批准发布 %s v1', '小红书批准%sv1', '批准 %s V1'):
            did = self.submit(target={'feed_id': FEED, 'comment_id': 'cm' + os.urandom(3).hex(), 'author_id': OTHER, 'comment_hash': '1' * 64},
                              submit_key=os.urandom(16).hex())['draft_id']
            self.assertIn('已批准', await self.say(text % did), text)
            self.assertEqual(self.status(did), 'approved')
            self.rv.reject(did)
        did = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0777', 'author_id': OTHER, 'comment_hash': '1' * 64}, submit_key='9' * 32)['draft_id']
        self.assertIn('最新版本是 v1', await self.say('批准发布%sv2' % did))
        self.assertIn('已拒绝', await self.say('拒绝%s' % did))


class IntentTests(NaturalBase):
    async def test_reject_hold_and_change_requests(self):
        did = await self.pushed()
        self.assertIn('先不发', await self.say('先别发'))
        self.assertEqual(self.status(did), 'pending')
        self.assertIn('想怎么改', await self.say('需要修改'))
        self.assertEqual((self.status(did), self.rv.row(did)['version']), ('pending', 1))
        self.assertIn('想怎么改', await self.say('改完再发'))
        self.assertEqual(self.status(did), 'pending')
        self.assertIn('已拒绝', await self.say('不要发了'))
        self.assertEqual(self.status(did), 'rejected')

    async def test_change_request_returns_an_approved_draft_to_review(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.assertEqual(self.status(did), 'approved')
        self.assertIn('想怎么改', await self.say('需要修改'))
        self.assertEqual((self.status(did), self.rv.row(did)['approved_hash']), ('pending', ''))
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])

    async def test_sent_or_in_flight_drafts_cannot_be_cancelled_or_called_unsent(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.claim(did)
        for text in ('不要发了', '取消发布', '需要修改'):
            reply = await self.say(text)
            self.assertIn('不能', reply)
            self.assertIn('执行器正在发送', reply)
            self.assertEqual(self.status(did), 'claimed')
        self.rv.report({'draft_id': did, 'claim_token': self.rv.row(did)['claim_token'], 'outcome': 'accepted'})
        self.assertIn('已经发出或正在发送', await self.say('正文简短一点'))
        self.assertEqual(self.status(did), 'delivered')
        self.assertEqual(self.llm.prompts, [])

    async def test_a_specific_change_is_rewritten_by_her_and_needs_a_new_approval(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.llm.answers.append({'content': '短一点的回复'})
        reply = await self.say('正文简短一点')
        row = self.rv.row(did)
        self.assertEqual((row['status'], row['version'], row['approved_hash']), ('pending', 2, ''))
        self.assertIn('短一点的回复', reply)
        self.assertIn('v2', reply)
        self.assertEqual(json.loads(row['payload'])['content'], '短一点的回复')
        prompt, max_tokens = self.llm.prompts[0]
        self.assertIn('persona', prompt[1]['content'])                # her persona and the owner's wording, nothing else
        self.assertIn('正文简短一点', prompt[1]['content'])
        self.assertLessEqual(max_tokens, 700)
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])   # the previous approval never carries over
        self.assertIn('已批准 %s v2' % did, await self.say('发布吧'))    # she saw v2 (the reply delivered it), so this approves v2

    async def test_a_revision_that_did_not_reach_her_is_not_something_she_can_approve(self):
        did = await self.pushed()
        self.llm.answers.append({'content': '改写后的回复'})
        self.receipt = {'sent': False}
        await self.say('语气自然些')
        self.assertEqual(self.rv.row(did)['version'], 2)
        self.receipt = {'sent': True}
        reply = await self.say('发布吧')
        self.assertIn('没有找到你刚看过', reply)
        self.assertEqual(self.status(did), 'pending')

    async def test_revision_failures_leave_the_draft_alone(self):
        did = await self.pushed()
        for answer in ({'content': '看 https://x.example'}, {'content': ''}, {'nothing': 1}):
            self.llm.answers.append(answer)
            self.assertIn('保持原样', await self.say('正文简短一点'))
        self.assertEqual((self.status(did), self.rv.row(did)['version']), ('pending', 1))
        self.assertIn('保持原样', await self.say('语气自然些'))     # no answer left: the model is unavailable
        for _ in range(review.REVISE_DAILY):
            self.rv.spend('revise', review.REVISE_DAILY)
        calls = len(self.llm.prompts)
        self.assertIn('次数用完', await self.say('语气自然些'))
        self.assertEqual(len(self.llm.prompts), calls)

    async def test_explicit_replacement_text_uses_the_field_edit_path_without_a_model(self):
        did = await self.pushed()
        reply = await self.say('正文改成：新的正文')
        self.assertIn('已改成 v2', reply)
        self.assertEqual(json.loads(self.rv.row(did)['payload'])['content'], '新的正文')
        self.assertEqual(self.llm.prompts, [])

    async def test_an_introduction_revision_keeps_it_natural_and_ai_aware(self):
        did = await self.pushed_intro()
        self.llm.answers.append({'title': '大肥鱼来啦', 'content': '我是大肥鱼，一只爱打游戏的AI虚拟角色，今天先打个招呼。', 'tags': []})
        self.assertIn('已改成 v2', await self.say('标题活泼一点'))
        self.llm.answers.append({'title': '大肥鱼来啦', 'content': '本账号为AI虚拟角色，内容由AI生成。', 'tags': []})
        self.assertIn('保持原样', await self.say('正文正式一点'))
        self.llm.answers.append({'title': '大肥鱼来啦', 'content': '我是大肥鱼，爱打游戏，今天先打个招呼。', 'tags': []})
        self.assertIn('保持原样', await self.say('再短一点'))      # it stopped saying it is an AI character
        self.assertEqual(self.rv.row(did)['version'], 2)

    async def pushed_intro(self):
        did = self.submit('post')['draft_id']
        await self.rv.tick()
        return did


class NegationAndTopicTests(NaturalBase):
    NEGATIVE = ('不批准', '不同意发布', '不通过', '没通过', '未通过', '不是说可以发布', '没同意发布', '别批准', '不能发布', '不可以发布', '不是要发布',
                '我不同意发布这一版', '不准发布')
    POSITIVE = ('没什么问题，发布吧', '不用改，可以发布', '没问题发布吧', '不错，发布吧', '好的看起来内容没什么问题，发布吧', '没毛病，可以发布')
    OTHER_TOPICS = ('需要修改代码', '拒绝别人的邀请', '修改论文', '发布会什么时候开', '发布会怎么样了', '帮我改下这段代码', '取消明天的会议',
                    '这个功能怎么发布到线上', '我想换个手机', '算了，我自己去买', '重写一下这个脚本')

    def test_negative_and_contradictory_phrases_never_read_as_approval(self):
        for text in self.NEGATIVE:
            self.assertNotEqual((review.XhsReview.classify(text) or {}).get('kind'), 'approve', text)
            self.assertFalse(review.XhsReview.screens_clean(text), text)       # the screen a model verdict also has to pass
        for text in self.POSITIVE:
            self.assertEqual(review.XhsReview.classify(text)['kind'], 'approve', text)
            self.assertTrue(review.XhsReview.screens_clean(text), text)
        self.assertEqual(review.XhsReview.classify('不批准')['kind'], 'reject')
        self.assertEqual(review.XhsReview.classify('不是说可以发布')['kind'], 'clarify')

    async def test_negative_phrases_do_not_approve_a_real_draft_and_positive_idioms_do(self):
        did = await self.pushed()
        for text in self.NEGATIVE:
            await self.rv.capture(self.msg(text))
            self.assertNotEqual(self.status(did), 'approved', text)
            if self.status(did) == 'rejected':
                self.rv.db.execute("UPDATE xhs_drafts SET status='pending' WHERE draft_id=?", (did,))
        self.assertEqual(self.llm.prompts, [])                                   # decided by the rules, no model involved
        self.assertIn('已批准', await self.say('不用改，可以发布'))
        self.assertEqual(self.status(did), 'approved')

    async def test_unrelated_chat_with_review_looking_words_passes_straight_through(self):
        did = await self.pushed()                                                  # a draft is in view, so this is the risky case
        for text in self.OTHER_TOPICS:
            self.assertFalse(await self.rv.capture(self.msg(text)), text)
        self.assertEqual((self.status(did), self.rv.row(did)['version']), ('pending', 1))
        self.assertEqual((self.llm.prompts, self.sent[1:]), ([], []))              # no model call, nothing said
        old = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        self.rv.db.execute('DELETE FROM xhs_review_shown')                         # drafts are recent, but she has seen none
        for text in self.OTHER_TOPICS:
            self.assertFalse(await self.rv.capture(self.msg(text)), text)
        self.assertEqual((self.status(did), self.status(old)), ('pending', 'pending'))

    async def test_short_review_replies_still_work_with_the_draft_in_view(self):
        did = await self.pushed()
        self.assertIn('想怎么改', await self.say('需要修改'))
        self.assertIn('先不发', await self.say('先别发'))
        self.assertIn('已拒绝', await self.say('不要发了'))
        self.assertEqual(self.status(did), 'rejected')


class EntryPointTests(NaturalBase):
    async def test_natural_text_that_starts_with_the_platform_name_is_not_swallowed_into_help(self):
        did = await self.pushed()
        self.assertIn('已批准 %s v1' % did, await self.say('小红书这篇可以发布了'))
        self.assertEqual(self.rv.row(did)['approved_source'][:8], 'natural:')
        self.assertIn('我已经撤回', await self.say('小红书这篇先别发'))
        self.assertEqual(self.status(did), 'pending')
        self.assertIn('已拒绝', await self.say('小红书这篇不要了'))
        self.assertEqual(self.status(did), 'rejected')
        again = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        await self.rv.capture(self.msg('小红书这篇不批准'))
        self.assertNotEqual(self.status(again), 'approved')                       # the strict screens still apply to approval
        self.assertFalse(await self.rv.capture(self.msg('小红书今天好多人')))       # naming the platform alone is not review

    async def test_real_legacy_command_shapes_still_use_the_command_handler(self):
        did = await self.pushed()
        self.assertIn(did, await self.say('小红书草稿'))
        self.assertIn('小红书批准', await self.say('小红书帮助'))
        self.assertIn('没看懂', await self.say('小红书批准 ' + did))               # a malformed command is still answered with the help text
        self.assertIn('已批准 %s v1' % did, await self.say('小红书批准 %s v1' % did))
        self.assertEqual(self.rv.row(did)['approved_source'][:8], 'command:')

    async def test_open_ended_edits_clearly_aimed_at_the_draft_are_understood(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.assertEqual(self.status(did), 'approved')
        seen = []
        self.llm.hook = lambda: seen.append(self.status(did))
        for version, phrase in enumerate(('把这篇正文写得更活泼一点', '这篇内容再丰富一些', '小红书这篇文案再简洁一点'), start=2):
            self.llm.answers.append({'content': '改写后的版本%d' % version})
            reply = await self.say(phrase)
            self.assertIn('已改成 v%d' % version, reply, phrase)
            self.assertEqual((self.status(did), self.rv.row(did)['version'], self.rv.row(did)['approved_hash']), ('pending', version, ''), phrase)
        self.assertEqual(seen[0], 'pending')                                      # the approval was withdrawn before the model was asked
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])

    async def test_praise_is_still_an_approval_and_a_conditional_edit_is_not(self):
        self.assertEqual(review.XhsReview.classify('这篇写得很好，内容很丰富，发布吧')['kind'], 'approve')
        self.assertEqual(review.XhsReview.classify('这篇内容再丰富一些就发布')['kind'], 'revise')
        self.assertEqual(review.XhsReview.classify('内容很丰富，发布吧')['kind'], 'approve')
        did = await self.pushed()
        self.assertIn('已批准', await self.say('这篇没问题，发布吧'))

    async def test_edit_wording_about_other_topics_falls_through(self):
        did = await self.pushed()
        for text in ('这篇论文写得更活泼一点', '这篇文章再丰富一些', '帮我把这段代码写得更简洁', '邀请函的内容再丰富一些', '把这份报告再详细一些',
                     '帮我润色一下这封邮件', '这个脚本再优化一下'):
            self.assertFalse(await self.rv.capture(self.msg(text)), text)
        self.assertEqual((self.status(did), self.rv.row(did)['version'], self.llm.prompts), ('pending', 1, []))


class WithdrawalBeforeAwaitTests(NaturalBase):
    async def test_hold_really_revokes_an_approval_and_is_honest_when_it_is_too_late(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.assertEqual(self.status(did), 'approved')
        self.assertIn('我已经撤回', await self.say('先别发'))
        row = self.rv.row(did)
        self.assertEqual((row['status'], row['approved_hash'], row['approved_source']), ('pending', '', ''))
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])            # the executor cannot get it
        await self.say('发布吧')
        self.claim(did)
        for outcome, said in ((None, '执行器正在发送'), ('accepted', '读回还没确认')):
            if outcome:
                self.rv.report({'draft_id': did, 'claim_token': self.rv.row(did)['claim_token'], 'outcome': outcome})
            reply = await self.say('先别发')
            self.assertIn('不能撤回', reply)
            self.assertIn(said, reply)
            self.assertNotIn('我已经撤回', reply)
        other = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        self.rv.db.execute("UPDATE xhs_drafts SET status='verified' WHERE draft_id=?", (did,))   # finished: only the new draft is in play
        self.assertIn('已批准 %s' % other, await self.say('发布吧'))
        self.claim(other, 'unknown')
        unknown = await self.say('先别发')
        self.assertIn('结果未知', unknown)
        self.assertIn('不能撤回', unknown)

    async def test_a_specific_change_request_revokes_the_approval_before_the_model_is_asked(self):
        did = await self.pushed()
        await self.say('发布吧')
        seen = []
        self.llm.hook = lambda: (seen.append(self.status(did)), seen.append(self.rv.claim({'kinds': ['reply']})['draft']))
        self.llm.answers.append({'content': '短一点'})
        await self.say('正文简短一点')
        self.assertEqual(seen, ['pending', None])                                   # while the model worked, nothing was claimable
        self.assertEqual((self.status(did), self.rv.row(did)['version']), ('pending', 2))

    async def test_if_the_rewrite_fails_or_times_out_the_draft_stays_unapproved(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.assertEqual(self.status(did), 'approved')
        self.assertIn('保持原样', await self.say('正文简短一点'))                      # no model answer at all
        self.assertEqual((self.status(did), self.rv.row(did)['version']), ('pending', 1))
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])
        self.assertIn('已批准', await self.say('发布吧'))                               # she has to approve again, explicitly
        import asyncio

        def hang():
            raise asyncio.TimeoutError()
        self.llm.hook = hang
        self.assertIn('保持原样', await self.say('语气自然些'))
        self.assertEqual(self.status(did), 'pending')
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])


class ContextTests(NaturalBase):
    async def test_two_shown_drafts_are_ambiguous_and_nothing_is_picked(self):
        first = await self.pushed()
        second = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        reply = await self.say('发布吧')
        self.assertIn(first, reply)
        self.assertIn(second, reply)
        self.assertIn('哪一篇', reply)
        self.assertEqual((self.status(first), self.status(second)), ('pending', 'pending'))
        self.assertIn('哪一篇', await self.say('不要发了'))
        self.assertEqual((self.status(first), self.status(second)), ('pending', 'pending'))
        self.assertIn('v1', await self.say('小红书批准 %s v1' % first))      # an explicit id still works
        self.assertEqual((self.status(first), self.status(second)), ('approved', 'pending'))
        self.assertIn('已批准 %s' % second, await self.say('发布吧'))        # now exactly one is anchored

    async def test_only_the_draft_she_actually_saw_is_approved(self):
        seen = await self.pushed()
        unseen = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        self.assertIn('已批准 %s' % seen, await self.say('可以发布'))
        self.assertEqual((self.status(seen), self.status(unseen)), ('approved', 'pending'))

    async def test_no_review_in_progress_means_ordinary_conversation(self):
        for text in ('发布会什么时候开', '今天天气不错', '发布吧'):
            self.assertFalse(await self.rv.capture(self.msg(text)), text)
        self.assertEqual(self.sent, [])
        self.submit()                                            # a draft exists but she has not seen it
        for text in ('今天天气不错', '晚上吃什么', '这个番好看吗'):
            self.assertFalse(await self.rv.capture(self.msg(text)), text)
        reply = await self.say('发布吧')                           # a clear review phrase with no fresh reference: asked, not guessed
        self.assertIn('没有找到你刚看过', reply)
        self.assertTrue(all(self.status(r['draft_id']) == 'pending' for r in self.rv.recent_drafts()))

    async def test_unrelated_chat_while_a_draft_is_in_view_is_not_hijacked(self):
        did = await self.pushed()
        for text in ('今天天气不错', '晚上吃什么', '这个番好看吗'):
            self.answer(did, 'none')
            self.assertFalse(await self.rv.capture(self.msg(text)), text)
        self.assertEqual(self.status(did), 'pending')
        self.assertEqual(self.sent[1:], [])                       # nothing was said to her

    async def test_negation_questions_conditions_and_hypotheticals_never_approve(self):
        did = await self.pushed()
        for text in ('可以发布吗？', '如果没问题就发布', '假如我说发布吧你会发吗', '“发布吧”是什么意思', '要是发布了会怎样',
                     '改完再发', '先别发', '别发布吧', '不要发布', '发布前要不要再看看？', '你说：发布吧'):
            await self.rv.capture(self.msg(text))
            self.assertIn(self.status(did), ('pending', 'rejected'), text)
            self.assertNotEqual(self.rv.row(did)['approved_source'], 'natural', text)
            if self.status(did) == 'rejected':
                self.rv.db.execute("UPDATE xhs_drafts SET status='pending' WHERE draft_id=?", (did,))
        self.assertEqual(self.status(did), 'pending')
        self.assertIn('已批准', await self.say('没问题，发布吧'))

    async def test_stale_quoted_forwarded_and_other_peoples_messages_cannot_authorize(self):
        did = await self.pushed()
        quote = {'type': 'reply', 'data': {'target_message_id': 'x', 'text': '小红书批准 %s v1' % did}}
        forward = {'type': 'forward', 'data': {'text': '批准发布%sv1' % did}}
        self.assertFalse(await self.rv.capture(self.msg('发布吧', age=400)))                              # stale
        self.assertFalse(await self.rv.capture(self.msg('', extra_parts=[quote])))                         # a quote only, nothing typed
        self.assertFalse(await self.rv.capture(self.msg('', extra_parts=[forward])))
        self.assertFalse(await self.rv.capture(self.msg('好的', extra_parts=[quote, forward])))            # typed 'ok' + quoted approval
        for kw in ({'actor': 'someone-else'}, {'group': True, 'session': 'gs', 'actor': 'owner-grp'}, {'notify': True}, {'actor': 'qq-bot'}):
            self.assertFalse(await self.rv.capture(self.msg('发布吧', **kw)), kw)
        self.assertEqual(self.status(did), 'pending')

    async def test_the_owner_may_reply_to_the_draft_message_with_her_own_words(self):
        did = await self.pushed()
        quote = {'type': 'reply', 'data': {'target_message_id': 'x', 'text': '小红书有一份新草稿等你审'}}
        reply = await self.say('可以发布', extra_parts=[quote])           # the normal QQ way: a reply component AND her typed words
        self.assertIn('已批准 %s v1' % did, reply)
        self.assertEqual(self.status(did), 'approved')


class SnapshotTests(NaturalBase):
    async def test_an_edit_during_the_send_cannot_make_the_newer_version_look_seen(self):
        did = self.submit()['draft_id']
        self.on_send = lambda: (self.rv.edit(did, '正文', '发送途中被改过'), setattr(self, 'on_send', None))
        self.assertEqual((await self.rv.tick())['status'], 'sent')             # the push carried v1 ...
        self.assertEqual(self.rv.row(did)['version'], 2)                         # ... and v2 appeared while it was in flight
        shown = self.rv.db.execute('SELECT version FROM xhs_review_shown WHERE draft_id=?', (did,)).fetchall()
        self.assertEqual([r['version'] for r in shown], [1])                    # only the version that was really sent is recorded
        self.assertIn('没有找到你刚看过', await self.say('发布吧'))
        self.assertEqual(self.status(did), 'pending')

    async def test_the_same_holds_for_a_view_reply(self):
        did = await self.pushed()
        await self.say('小红书修改 %s 正文：第二版' % did)                        # v2 shown by this reply
        self.on_send = lambda: (self.rv.edit(did, '正文', '第三版'), setattr(self, 'on_send', None))
        await self.say('小红书草稿 ' + did)                                       # shows v2; v3 lands during the send
        versions = [r['version'] for r in self.rv.db.execute('SELECT version FROM xhs_review_shown WHERE draft_id=? ORDER BY id', (did,))]
        self.assertEqual(versions, [1, 2, 2])
        self.assertEqual(self.rv.row(did)['version'], 3)
        self.assertIn('没有找到你刚看过', await self.say('发布吧'))

    async def test_a_failed_or_unknown_push_or_view_is_not_seen(self):
        did = self.submit()['draft_id']
        self.receipt = {'sent': False}
        self.assertEqual((await self.rv.tick())['status'], 'not_delivered')
        self.assertEqual(self.rv.db.execute('SELECT count(*) FROM xhs_review_shown').fetchone()[0], 0)
        self.receipt = {'sent': True}
        self.send_error = True
        await self.rv.capture(self.msg('小红书草稿 ' + did))
        self.send_error = False
        self.receipt = {'sent': False}
        await self.rv.capture(self.msg('小红书草稿 ' + did))
        self.assertEqual(self.rv.db.execute('SELECT count(*) FROM xhs_review_shown').fetchone()[0], 0)
        self.receipt = {'sent': True}
        self.assertIn('没有找到你刚看过', await self.say('发布吧'))
        self.assertEqual(self.status(did), 'pending')
        await self.say('小红书草稿 ' + did)                                       # a delivered view finally counts
        self.assertIn('已批准', await self.say('发布吧'))

    async def test_a_listing_is_not_a_view_and_a_failed_edit_does_not_count(self):
        did = self.submit()['draft_id']
        await self.say('小红书草稿')
        await self.say('小红书修改 %s 正文：看 https://x.example' % did)
        self.assertEqual(self.rv.db.execute('SELECT count(*) FROM xhs_review_shown').fetchone()[0], 0)

    async def test_a_push_confirmed_before_this_feature_existed_still_counts_as_seen(self):
        did = self.submit()['draft_id']
        await self.rv.tick()
        self.rv.db.execute('DELETE FROM xhs_review_shown')                     # the deployed v1 only has the confirmed outbox row (sent=2)
        self.assertEqual(self.rv.db.execute('SELECT sent FROM xhs_review_outbox').fetchone()[0], 2)
        self.assertIn('已批准 %s v1' % did, await self.say('好的，发布吧'))      # no need to look again or to approve explicitly
        # an undelivered push never backfills
        other = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        self.rv.db.execute('DELETE FROM xhs_review_shown')
        self.receipt = {'sent': False}
        await self.rv.tick()
        self.receipt = {'sent': True}
        self.assertEqual([r['draft_id'] for r in self.rv.anchors(OWNER_SESSION, OWNER)], [])
        self.assertEqual(self.status(other), 'pending')


class ModelBoundTests(NaturalBase):
    TEXT = '嗯，就按这个版本吧，没毛病'

    async def test_a_model_verdict_bound_to_the_shown_draft_can_approve_once(self):
        did = await self.pushed()
        self.answer(did)
        self.assertIn('已批准 %s v1' % did, await self.say(self.TEXT))
        prompt, max_tokens = self.llm.prompts[0]
        self.assertLessEqual(max_tokens, 120)
        self.assertNotIn('谢谢你的留言', prompt[1]['content'])               # the model sees the owner's words and ids, not the draft body
        self.assertLess(len(prompt[1]['content']), 400)
        self.assertEqual(self.status(did), 'approved')

    async def test_bad_or_unbound_or_unsure_verdicts_do_nothing(self):
        did = await self.pushed()
        for verdict in ({'intent': 'approve', 'confidence': 0.95, 'draft_id': 'ZZZZZZ', 'version': 1},       # another draft
                        {'intent': 'approve', 'confidence': 0.95, 'draft_id': did, 'version': 2},             # another version
                        {'intent': 'approve', 'confidence': 0.5, 'draft_id': did, 'version': 1},              # unsure
                        {'intent': 'approve', 'confidence': 0.95, 'draft_id': did},                           # schema
                        {'intent': 'publish_now', 'confidence': 1, 'draft_id': did, 'version': 1},            # unknown intent
                        {'intent': 'approve', 'confidence': 'high', 'draft_id': did, 'version': 1},
                        {'intent': 'approve', 'confidence': 0.99, 'draft_id': did, 'version': 1, 'extra': 'x'}):
            self.llm.answers.append(verdict)
            await self.rv.capture(self.msg('好的，就这样吧，没毛病'))        # nothing the rules can read: the model is asked, and its verdict is checked
            self.assertEqual(self.status(did), 'pending', verdict)
        self.llm.answers.append({'intent': 'approve', 'confidence': 0.99, 'draft_id': did, 'version': 1})
        await self.rv.capture(self.msg('嗯，就按这个版本吧？'))          # a question: the deterministic screens veto a model "approve"
        self.assertEqual(self.status(did), 'pending')

    async def test_the_model_budget_is_bounded(self):
        did = await self.pushed()
        for _ in range(review.CLASSIFY_DAILY):
            self.rv.spend('classify', review.CLASSIFY_DAILY)
        self.answer(did)
        before = len(self.llm.prompts)
        await self.rv.capture(self.msg(self.TEXT))
        self.assertEqual((len(self.llm.prompts), self.status(did)), (before, 'pending'))

    async def test_an_edit_while_the_model_is_thinking_voids_the_pending_approval(self):
        did = await self.pushed()
        self.answer(did)
        self.llm.hook = lambda: self.rv.edit(did, '正文', '想的时候被改了')
        reply = await self.say(self.TEXT)
        self.assertNotIn('已批准', reply)
        self.assertEqual((self.status(did), self.rv.row(did)['version']), ('pending', 2))

    async def test_a_newer_owner_message_while_the_model_is_thinking_voids_it(self):
        did = await self.pushed()
        self.answer(did)
        self.llm.hook = lambda: self.rv.bump(OWNER_SESSION)             # what any newer owner message does on arrival
        reply = await self.say(self.TEXT)
        self.assertIn('又发了新消息', reply)
        self.assertEqual(self.status(did), 'pending')

    async def test_a_rejection_while_the_model_is_thinking_wins(self):
        did = await self.pushed()
        self.answer(did)
        self.llm.hook = lambda: self.rv.reject(did)
        reply = await self.say(self.TEXT)
        self.assertNotIn('已批准', reply)
        self.assertEqual(self.status(did), 'rejected')

    async def test_a_second_message_arriving_during_the_model_call_is_a_real_concurrent_interleaving(self):
        did = await self.pushed()
        self.answer(did)

        async def other_message():
            await self.say('先别发')                                        # a newer owner message processed in the middle
        self.llm.hook = other_message
        reply = await self.say(self.TEXT)
        self.assertEqual(self.status(did), 'pending')
        self.assertIn('又发了新消息', reply)

    async def test_when_the_model_is_down_a_review_like_phrase_is_asked_not_guessed(self):
        did = await self.pushed()
        reply = await self.say('就这样，发出去，别拖了')               # no deterministic pattern, model unavailable
        self.assertIn('没有当作批准', reply)
        self.assertEqual(self.status(did), 'pending')

    async def test_replayed_and_deduplicated_messages(self):
        did = await self.pushed()
        first = await self.say('发布吧', mid='same-id')
        self.assertIn('已批准', first)
        count = len(self.sent)
        await self.svc.ingest_qq(self.msg('发布吧', mid='same-id'))
        self.assertEqual(len(self.sent), count)                          # the same inbound message again: swallowed
        self.assertEqual(self.status(did), 'approved')
        other = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        self.answer(other, 'none')
        self.assertFalse(await self.rv.capture(self.msg('嗯，好的', mid='chat-id')))
        self.assertIsNone(self.rv.db.execute('SELECT 1 FROM xhs_review_msgs WHERE message_id=?', (OWNER_SESSION + ':chat-id',)).fetchone())

    async def test_a_naturally_approved_draft_still_passes_every_claim_gate(self):
        did = await self.pushed()
        await self.say('发布吧')
        self.rv.db.execute("UPDATE xhs_drafts SET approval_expires=1 WHERE draft_id=?", (did,))
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])          # expired approvals are never handed out
        did2 = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})
        await self.say('发布吧')
        self.unbind()                                                              # the owner binding disappears after approval
        self.assertIsNone(self.rv.claim({'kinds': ['reply']})['draft'])
        self.bind()
        self.rv.reject(did2)                                                       # (back in review it would otherwise be a second anchor)
        did3 = await self.pushed(target={'feed_id': FEED, 'comment_id': 'cm0003', 'author_id': OTHER, 'comment_hash': '3' * 64})
        await self.say('发布吧')
        claim = self.rv.claim({'kinds': ['reply']})['draft']
        self.assertEqual((claim['draft_id'], claim['approved_version']), (did3, 1))
        self.assertEqual(sorted(op for op in UnifiedService.XHS_OPS if 'approve' in op or 'recover' in op), [])   # still no executor approval API


class RecoveryTests(NaturalBase):
    def event(self, did, content=None, version=1, **over):
        created = self.rv.row(did)['created']
        e = {'event_id': 'qq:rec-' + os.urandom(3).hex(), 'platform': 'qq', 'bot_account_id': 'qq-bot', 'conversation_id': OWNER_SESSION,
             'actor_id': OWNER, 'nickname': 'owner', 'occurred_at': created + 1, 'event_type': 'message_received',
             'content': content if content is not None else '批准发布%sv%d' % (did, version), 'source_id': 'rec-' + os.urandom(3).hex(),
             'origin': 'external_user', 'sharing_scope': 'conversation', 'learning_allowed': False, 'metadata': {'is_private': True}}
        e.update(over)
        return self.svc.state.ingest(e)['event_id'], e

    def recover(self, event_id, did, version=1, now=None):
        return review.recover_explicit_approval(self.root, event_id, did, version, now=now if now is not None else self.rv.row(did)['created'] + 30)

    async def test_the_missed_explicit_approval_is_recovered_with_its_original_provenance(self):
        did = self.submit()['draft_id']
        event_id, e = self.event(did)
        result = self.recover(event_id, did)
        self.assertEqual((result['ok'], result['reason']), (True, 'recovered'))
        row = self.rv.row(did)
        self.assertEqual((row['status'], row['approved_by'], row['approved_session'], row['approved_at'], row['approval_expires'], row['approved_source']),
                         ('approved', OWNER, OWNER_SESSION, e['occurred_at'], e['occurred_at'] + review.APPROVAL_TTL, 'event:' + event_id))
        self.assertEqual((row['approved_version'], row['approved_hash']), (1, row['content_hash']))
        source = self.svc.state.get_event(event_id)                              # the evidence itself is untouched
        self.assertEqual((source['content'], source['origin'], source['active']), (e['content'], 'external_user', True))
        claim = self.rv.claim({'kinds': ['reply']})['draft']                     # and the normal gates still apply to what follows
        self.assertEqual((claim['draft_id'], claim['approved_version']), (did, 1))
        again = self.recover(event_id, did)                                       # repeating it is harmless, even after the claim
        self.assertEqual((again['ok'], again['reason']), (True, 'already_recovered'))
        self.assertEqual(self.rv.row(did)['status'], 'claimed')
        other = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        self.assertEqual(self.recover(event_id, other)['reason'], 'event_already_consumed')
        self.assertEqual(self.status(other), 'pending')

    async def test_evidence_that_is_insufficient_is_refused_with_a_bounded_reason(self):
        did = self.submit()['draft_id']
        row = self.rv.row(did)

        def refused(reason, event_id, version=1, now=None):
            # earlier cases' messages are not "newer evidence" for this one: only this event is in play
            self.svc.state.db.execute("UPDATE events SET active=0 WHERE event_id!=? AND event_type='message_received'", (event_id,))
            result = self.recover(event_id, did, version, now)
            self.assertEqual((result['ok'], result['reason']), (False, reason))
            self.assertEqual(self.status(did), 'pending')
        refused('event_not_found', 'evt:' + 'f' * 64)
        refused('invalid_arguments', 'not-an-event')
        e1, _ = self.event(did, actor_id='someone-else')
        refused('not_the_owner', e1)
        e2, _ = self.event(did, conversation_id='other-session')
        refused('not_the_owner_private_chat', e2)
        e3, _ = self.event(did, conversation_id='gs')
        refused('not_the_owner_private_chat', e3)
        e4, _ = self.event(did, origin='platform_content')
        refused('event_not_an_active_owner_message', e4)
        e5, _ = self.event(did)
        self.svc.state.db.execute('UPDATE events SET active=0 WHERE event_id=?', (e5,))
        refused('event_not_an_active_owner_message', e5)
        e6, _ = self.event(did, sharing_scope='policy', metadata={'is_private': False})
        refused('event_not_private', e6)
        e7, _ = self.event(did, bot_account_id='other-bot')
        refused('wrong_platform_or_account', e7)
        e8, _ = self.event(did, content='可以发布吧')
        refused('not_an_explicit_approval_of_this_draft', e8)                      # a generic message is never an approval
        e9, _ = self.event(did, content='批准发布ZZZZZZv1')
        refused('not_an_explicit_approval_of_this_draft', e9)
        e10, _ = self.event(did, content='批准发布%sv9' % did)
        refused('not_an_explicit_approval_of_this_draft', e10)
        e11, _ = self.event(did)
        refused('source_too_old', e11, now=row['created'] + 1 + review.RECOVERY_MAX_AGE + 5)
        e12, _ = self.event(did, occurred_at=row['created'] - 10)
        refused('event_outside_the_draft_window', e12)
        e13, _ = self.event(did, occurred_at=row['expires'] + 5)
        refused('event_outside_the_draft_window', e13, now=row['expires'] + 20)
        e14, e14_raw = self.event(did)
        self.rv.db.execute('INSERT INTO xhs_review_msgs(message_id,created) VALUES(?,?)', (OWNER_SESSION + ':' + e14_raw['source_id'], time.time()))
        refused('message_already_handled_live', e14)
        e15, _ = self.event(did)
        refused('source_in_the_future', e15, now=row['created'] - 600)

    async def test_newer_owner_withdrawal_or_change_evidence_is_never_overridden(self):
        created = None
        for later in ('先别发', '改一下正文', '小红书拒绝 {id}', '不要发了', '需要修改', '算了', '{id} 这篇我再看看', '假如我说发布吧'):
            did = self.submit(target={'feed_id': FEED, 'comment_id': 'cm' + os.urandom(3).hex(), 'author_id': OTHER, 'comment_hash': '1' * 64},
                              submit_key=os.urandom(16).hex())['draft_id']
            event_id, e = self.event(did)
            self.event(did, content=later.format(id=did), occurred_at=e['occurred_at'] + 5)       # a newer message from the owner
            result = self.recover(event_id, did)
            self.assertEqual((result['ok'], result['reason']), (False, 'newer_owner_withdrawal_or_change'), later)
            self.assertEqual(self.status(did), 'pending', later)
            self.rv.reject(did)                                                                       # keep the review queue below its cap

    async def test_later_chat_status_questions_or_the_same_approval_do_not_block_recovery(self):
        did = self.submit()['draft_id']
        event_id, e = self.event(did)
        self.event(did, content='发布的怎么样了？', occurred_at=e['occurred_at'] + 5)
        self.event(did, content='今天天气不错', occurred_at=e['occurred_at'] + 6)
        self.event(did, content='批准发布%sv1' % did, occurred_at=e['occurred_at'] + 7)           # the same approval repeated
        self.assertTrue(self.recover(event_id, did)['ok'])
        self.assertEqual(self.status(did), 'approved')
        # messages BEFORE the source event (the natural "发布吧" that was missed too) are not "newer"
        did2 = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        self.event(did2, content='先别发', occurred_at=self.rv.row(did2)['created'] + 1)
        event2, _ = self.event(did2, occurred_at=self.rv.row(did2)['created'] + 2)
        self.assertTrue(self.recover(event2, did2)['ok'])

    async def test_the_draft_must_be_the_unchanged_pending_version(self):
        did = self.submit()['draft_id']
        event_id, _ = self.event(did)
        await self.say('小红书修改 %s 正文：改过了' % did)                          # v2: the old v1 approval is no longer about this text
        result = self.recover(event_id, did, 1)
        self.assertEqual(result['reason'], 'draft_not_pending_at_that_version')
        self.assertEqual((self.status(did), self.rv.row(did)['version']), ('pending', 2))
        did2 = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0002', 'author_id': OTHER, 'comment_hash': '2' * 64})['draft_id']
        e2, _ = self.event(did2)
        self.rv.db.execute("UPDATE xhs_drafts SET payload=replace(payload,'谢谢你的留言','被改过') WHERE draft_id=?", (did2,))   # payload no longer matches its hash
        self.assertEqual(self.recover(e2, did2)['reason'], 'draft_hash_mismatch')
        did3 = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0003', 'author_id': OTHER, 'comment_hash': '3' * 64})['draft_id']
        e3, _ = self.event(did3)
        self.rv.reject(did3)
        self.assertEqual(self.recover(e3, did3)['reason'], 'draft_not_pending_at_that_version')
        did4 = self.submit(target={'feed_id': FEED, 'comment_id': 'cm0004', 'author_id': OTHER, 'comment_hash': '4' * 64})['draft_id']
        e4, _ = self.event(did4)
        self.write_policy(xhs_bot_account='u' + 'z' * 11)                             # the account changed since the draft was written
        self.assertEqual(self.recover(e4, did4)['reason'], 'draft_for_another_account')

    async def test_owner_configuration_is_rechecked_by_the_recovery(self):
        did = self.submit()['draft_id']
        event_id, _ = self.event(did)
        self.unbind()
        self.assertEqual(self.recover(event_id, did)['reason'], 'owner_binding_not_verified')
        self.bind()
        self.write_policy(xhs_review_owner_session=None, xhs_review_owner_actor=None)
        self.assertEqual(self.recover(event_id, did)['reason'], 'review_not_configured')
        self.write_policy(xhs_review_owner_session=OWNER_SESSION, xhs_review_owner_actor=OWNER)
        self.assertTrue(self.recover(event_id, did)['ok'])

    async def test_an_intro_gate_applies_to_a_recovered_public_post_too(self):
        did = self.submit('post', audience='self_only', purpose='general', title='私密')['draft_id']
        self.rv.db.execute("UPDATE xhs_drafts SET audience='public' WHERE draft_id=?", (did,))
        row = self.rv.row(did)
        self.rv.db.execute('UPDATE xhs_drafts SET content_hash=? WHERE draft_id=?', (review.row_hash(dict(row), json.loads(row['payload'])), did))
        event_id, _ = self.event(did)
        self.assertEqual(self.recover(event_id, did)['reason'], 'intro_not_done')

    async def test_it_is_operator_only_and_has_no_socket_operation(self):
        source = (Path(review.__file__).parent / 'service.py').read_text(encoding='utf-8')
        self.assertNotIn('recover_explicit_approval', source)
        for op in ('xhs_draft_recover', 'xhs_draft_approve'):
            self.assertNotIn(op, UnifiedService.XHS_OPS)
            with self.assertRaises(ValueError):
                await self.svc.dispatch({'op': op}, admin=True)
        spec = importlib.util.spec_from_file_location('xhs_recover_script', SCRIPT)
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        did = self.submit()['draft_id']
        event_id, _ = self.event(did)
        out = []
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = script.main(['--core-root', str(self.root), '--event-id', event_id, '--draft-id', did, '--version', '1'])
        self.assertEqual(code, 1 if self.rv.row(did)['status'] != 'approved' else 0)
        result = json.loads(buf.getvalue())
        self.assertEqual(sorted(result), ['approval_expires', 'approved_at', 'draft_id', 'ok', 'reason', 'source_event_id', 'version'] if result['ok'] else ['ok', 'reason'])


if __name__ == '__main__':
    unittest.main()
