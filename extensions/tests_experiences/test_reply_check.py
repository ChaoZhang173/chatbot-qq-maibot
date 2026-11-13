"""Exercise the final factual check, failures and evidence revocation without network."""
import asyncio
import json
from unittest.mock import AsyncMock

from test_experiences import Fixture
from maibot_unified_core import experience_runtime as runtime, experience_check as check


class ReplyCheck(Fixture):
    def prepare(self):
        self.add('one', '太空蔬菜的种植介绍')
        self.incoming()
        self.good = '读了太空蔬菜的种植介绍，想知道这种菜长大之后是什么味道。'
        self.s.generate = AsyncMock(return_value={'supported': True, 'text': self.good,
                                                 'used_ids': [self.ids['one']]})

    async def test_checked_final_prose_is_bound_and_input_has_local_time(self):
        self.prepare()
        draft = '今天早上我种了太空蔬菜，当时馋坏了。你怎么还没睡？'
        result = await runtime.prepare_and_stage(self.s, 'group', 'question', {}, draft)
        self.assertEqual(result, self.good)
        self.assertTrue(runtime.before_send(self.s, self.message(result)))
        self.assertFalse(runtime.before_send(self.s, self.message(draft)))
        payload = json.loads(self.s.generate.call_args.args[0][1]['content'])
        self.assertEqual(payload['question'], '你今天干嘛了？')
        self.assertEqual(payload['evidence']['items'][0]['time'], '11-13 19:59')
        self.assertTrue(payload['evidence']['as_of_local'].endswith('+08:00'))

    async def test_actual_reply_hook_checks_prose_and_sender_rejects_bypass(self):
        from test_qq_host_adapter import load_plugin
        self.prepare()
        plugin = load_plugin().UnifiedCorePlugin()
        plugin.service = self.s
        draft = '我今天自己种了太空蔬菜。'
        result = await plugin.reply_policy_final(session_id='group', reply_message_id='question',
                                                response=draft, reply_tool_args={}, enable_splitter=True,
                                                enable_chinese_typo=True)
        self.assertEqual(result['modified_kwargs']['response'], self.good)
        self.assertFalse(result['modified_kwargs']['enable_chinese_typo'])
        self.assertTrue(result['modified_kwargs']['enable_splitter'])
        result = await plugin.pending_qq(message=self.message(draft), reply_message_id='question')
        self.assertEqual(result['action'], 'abort')

    async def test_host_sentence_segments_allow_whitespace_and_trailing_comma_trim(self):
        self.prepare()
        text = '读了太空蔬菜的种植介绍，\n\n想知道它是什么味道。'
        self.s.generate.return_value['text'] = text
        checked = await runtime.prepare_and_stage(self.s, 'group', 'question', {}, '草稿')
        self.assertTrue(runtime.is_checked_response(self.s, 'group', 'question', checked))
        for segment in ('读了太空蔬菜的种植介绍', '想知道它是什么味道。', '读了太空蔬菜的种植介绍， 想知道它是什么味道。'):
            self.assertTrue(runtime.before_send(self.s, self.message(segment)), segment)
        self.assertFalse(runtime.before_send(self.s, self.message('读了太空疏菜的种植介绍')))
        self.assertFalse(runtime.is_checked_response(self.s, 'group', 'another-turn', checked))

    async def test_unknown_fact_reference_cannot_release_original_draft(self):
        self.prepare()
        self.s.generate.return_value['used_ids'] = ['invented']
        draft = '我今天真的种了蔬菜。'
        text = await runtime.prepare_and_stage(self.s, 'group', 'question', {}, draft)
        self.assertNotEqual(text, draft)
        self.assertIn('太空蔬菜的种植介绍', text)
        self.assertNotIn('今天', text)
        self.assertTrue(runtime.before_send(self.s, self.message(text)))
        self.assertFalse(runtime.before_send(self.s, self.message(draft)))

    async def test_provider_failure_uses_only_source_backed_fallback(self):
        self.prepare()
        self.s.generate.side_effect = RuntimeError('provider unavailable')
        text = await runtime.prepare_and_stage(self.s, 'group', 'question', {}, '我昨天吃了这道菜。')
        self.assertIn('读过《太空蔬菜的种植介绍》', text)
        self.assertNotIn('吃', text)

    async def test_no_match_never_falls_back_to_denial(self):
        self.incoming()
        self.s.generate = AsyncMock(return_value={'supported': False, 'text': '', 'used_ids': []})
        text = await runtime.prepare_and_stage(self.s, 'group', 'question', {}, '今天没看什么东西。')
        self.assertEqual(text, '这段具体经历，我暂时没法确定。')
        self.assertTrue(runtime.before_send(self.s, self.message(text)))

    async def test_revoked_source_during_review_never_stages_or_sends(self):
        self.prepare()
        async def review(*args, **kwargs):
            self.s.state.db.execute('UPDATE events SET active=0 WHERE event_id=?', (self.ids['one'],))
            return {'supported': True, 'text': self.good, 'used_ids': [self.ids['one']]}
        self.s.generate.side_effect = review
        self.assertEqual(await runtime.prepare_and_stage(self.s, 'group', 'question', {}, '草稿'), '')
        self.assertFalse(runtime.before_send(self.s, self.message(self.good)))

    async def test_permission_revoked_on_disk_during_review_never_sends(self):
        self.prepare()
        async def review(*args, **kwargs):
            policy = json.loads((self.root/'policy.json').read_text())
            policy['experience_features']['recall'] = False
            (self.root/'policy.json').write_text(json.dumps(policy))
            return {'supported': True, 'text': self.good, 'used_ids': [self.ids['one']]}
        self.s.generate.side_effect = review
        self.assertEqual(await runtime.prepare_and_stage(self.s, 'group', 'question', {}, '草稿'), '')
        self.assertFalse(runtime.before_send(self.s, self.message(self.good)))

    async def test_unchecked_stage_cannot_authorize_delivery_or_followup(self):
        self.prepare()
        text = runtime.stage(self.s, 'group', 'question', {}, '我已经种好了。')
        self.assertFalse(runtime.before_send(self.s, self.message(text)))
        runtime.after_send(self.s, self.message(text), True)
        self.assertEqual(self.s.state.get(runtime.anchor_key('group','200'),{}), {})

    async def test_normal_conversation_skips_model_check(self):
        self.incoming(text='今天晚饭吃啥')
        self.s.generate = AsyncMock()
        text = await runtime.prepare_and_stage(self.s, 'group', 'question', {}, '想吃面条。')
        self.assertEqual(text, '想吃面条。')
        self.s.generate.assert_not_called()

    async def test_review_schema_fails_closed_but_group_ids_can_be_empty(self):
        evidence = {'status':'found', 'items':[{'id':'known'}]}
        for answer in ([], {}, {'supported':True, 'text':'好的', 'used_ids':[]},
                       {'supported':True, 'text':'好的', 'used_ids':[False]},
                       {'supported':'true', 'text':'好的', 'used_ids':['known']}):
            self.s.generate = AsyncMock(return_value=answer)
            self.assertIsNone(await check.correct(self.s, '候选', evidence))
        self.s.generate = AsyncMock(return_value={'supported':True, 'text':'聊聊刚才那段？', 'used_ids':[]})
        result = await check.correct(self.s, '候选', {'status':'group_context','items':[]},
                                     group_audience={'audience':{'user_id':'200'}})
        self.assertEqual(result, '聊聊刚才那段？')

    async def test_review_timeout_and_outer_cancellation_are_distinct(self):
        async def delayed(*args, **kwargs):
            await asyncio.sleep(1)
        self.s.generate = delayed
        self.assertIsNone(await check.correct(self.s, '候选', {'items':[]}, timeout=0.001))
        self.s.generate = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await check.correct(self.s, '候选', {'items':[]})
