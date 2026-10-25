"""Real per-event policy state, synthetic QQ inputs, no network or model calls."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import State
from test_qq_host_adapter import load_plugin


class ReplyPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.policy={'qq_bot_account':'bot','allowed_qq_sessions':['a','b'],
                     'qq_group_sessions':{'group-a':'a','group-b':'b'},'shared_groups':[['a','b']],
                     'qq_reply_policy':{'enabled':True,'casual_probability':0.7}}
        (self.root/'policy.json').write_text(json.dumps(self.policy))
        async def get_id(platform,actor):return 'native-'+actor
        self.service=UnifiedService(types.SimpleNamespace(person=types.SimpleNamespace(get_id=get_id)),self.root)
        self.service.reload_policy();self.service.state=State(self.root/'state.sqlite',self.policy)
        self.service.state.init_persona({'bot.nickname':'大小姐','bot.alias_names':['小麦']},{'interests':['AI','游戏','美食']})

    def tearDown(self):self.service.state.close();self.tmp.cleanup()

    @staticmethod
    def message(mid='external-1',text='今天下雨了',actor='user',session='a',**kw):
        return {'message_id':mid,'platform':'qq','account_id':'bot','session_id':session,'scope':'group',
                'timestamp':'1000','message_info':{'user_info':{'user_id':actor},
                    'group_info':{'group_id':'group-'+session},'additional_config':{}},
                'raw_message':[{'type':'text','data':text}],**kw}

    @staticmethod
    def call(mid,token='call-1',name='reply'):
        return {'item_type':'FunctionCallItem','meta':{'item_id':'item-'+token},
                'tool_call':{'call_id':token,'func_name':name,'args':{'msg_id':mid,'reply_reference':'适当回应'},'extra_content':None}}

    async def test_ordinary_draw_is_exact_boundary_and_sticky_across_replay_restart(self):
        self.service.qq_reply_draw=lambda *_:0.7
        message=self.message()
        blocked=self.service.record_qq_reply_eligibility(message)
        self.assertFalse(blocked['eligible'])
        self.service.qq_reply_draw=lambda *_:0.0
        self.assertEqual(self.service.record_qq_reply_eligibility(message),blocked)
        allowed=self.service.record_qq_reply_eligibility(self.message(mid='next-event'))
        self.assertTrue(allowed['eligible'])
        self.service.state.close();self.service.state=State(self.root/'state.sqlite',self.policy)
        self.assertEqual(self.service.record_qq_reply_eligibility(message),blocked)
        self.assertEqual(self.service.filter_qq_planner_output('a',[self.call('external-1')]),[])

    async def test_about_bot_mentions_reply_and_shared_interests_bypass_casual_draw(self):
        self.service.qq_reply_draw=lambda *_:self.fail('priority message must not be randomly sampled')
        examples=[self.message('at',is_at=True),self.message('name',text='大小姐刚才说得有道理'),
                  self.message('alias',text='小麦怎么看'),self.message('interest',text='今天这个游戏很好玩'),
                  self.message('ai',text='DeepSeek的新模型有什么变化')]
        at=self.message('component-at');at['raw_message'].append({'type':'at','data':{'target_user_id':'bot'}});examples.append(at)
        reply=self.message('component-reply');reply['raw_message'].append({'type':'reply','data':{'target_message_sender_id':'bot'}});examples.append(reply)
        for message in examples:self.assertTrue(self.service.record_qq_reply_eligibility(message)['eligible'])
        self.assertFalse(self.service.record_qq_reply_eligibility(self.message('self',actor='bot',text='大小姐游戏'))['eligible'])
        self.assertFalse(self.service.record_qq_reply_eligibility(self.message('notify',is_notify=True,text='大小姐'))['eligible'])

    async def test_casual_message_is_retained_with_images_and_cannot_be_sent_or_rerolled(self):
        self.service.qq_reply_draw=lambda *_:0.99
        message=self.message();picture={'type':'image','hash':'image-ref','binary_data_base64':'AA=='}
        message['raw_message'].append(picture)
        plugin=load_plugin().UnifiedCorePlugin();plugin.service=self.service
        result=await plugin.receive_qq(message=message)
        self.assertEqual(result,{'action':'continue'})
        self.assertIs(message['raw_message'][1],picture)
        self.assertEqual(len(self.service.state.recent_events()),1)
        self.assertEqual(self.service.filter_qq_planner_output('a',[self.call('external-1')]),[])
        final=await plugin.reply_policy_final(session_id='a',reply_message_id='external-1',response='不应发出',
              reply_tool_args={},skip_post_process=False,enable_splitter=True,enable_chinese_typo=True)
        self.assertEqual(final['modified_kwargs']['response'],'')
        self.assertTrue(final['modified_kwargs']['enable_splitter'])

    async def test_only_one_logical_reply_claim_and_final_guard_preserve_tools_and_kwargs(self):
        self.service.qq_reply_draw=lambda *_:0.1
        mid='ROBOT1.0_a-long-native-message-id-12345'
        self.service.record_qq_reply_eligibility(self.message(mid))
        alias=self.service.qq_display_message_id(mid)
        memory=self.call(mid,'memory-call','unified_memory');reason={'item_type':'ReasoningItem','meta':{},'text_parts':['考虑上下文']}
        plugin=load_plugin().UnifiedCorePlugin();plugin.service=self.service
        planned=await plugin.reply_policy_plan(session_id='a',output_items=[reason,memory,self.call(alias),self.call(alias,'duplicate')],item_schema_version=1,prompt_tokens=91)
        output=planned['modified_kwargs']['output_items']
        self.assertEqual(len(output),3);self.assertIs(output[0],reason);self.assertIs(output[1],memory)
        self.assertEqual(planned['modified_kwargs']['prompt_tokens'],91)
        args=output[-1]['tool_call']['args']
        self.assertEqual(args['_unified_reply_token'],'call-1')
        self.assertEqual(self.service.filter_qq_planner_output('a',[self.call(alias,'new-round')]),[])
        self.assertFalse(self.service.guard_qq_reply('a',mid,{'_unified_reply_token':'different-call'}))
        self.assertTrue(self.service.guard_qq_reply('a',mid,args))
        self.assertFalse(self.service.guard_qq_reply('a',mid,args))
        self.assertFalse(self.service.guard_qq_reply('a','bot-own-message',{}))
        self.service.state.close();self.service.state=State(self.root/'state.sqlite',self.policy)
        self.assertEqual(self.service.filter_qq_planner_output('a',[self.call(mid,'after-restart')]),[])

    async def test_cross_session_unknown_targets_and_own_loops_are_denied_disabled_scope_untouched(self):
        self.service.qq_reply_draw=lambda *_:0.1
        self.service.record_qq_reply_eligibility(self.message())
        self.assertEqual(self.service.filter_qq_planner_output('b',[self.call('external-1')]),[])
        self.assertEqual(self.service.filter_qq_planner_output('a',[self.call('own-generated-id')]),[])
        source=[self.call('external-1')]
        self.assertIs(self.service.filter_qq_planner_output('unauthorized-session',source),source)
        self.assertTrue(self.service.guard_qq_reply('unauthorized-session','external-1',{}))
        self.policy['qq_reply_policy']['enabled']=False
        (self.root/'policy.json').write_text(json.dumps(self.policy))
        self.assertIs(self.service.filter_qq_planner_output('a',source),source)

    async def test_sampling_is_message_based_and_ascii_interest_is_not_a_substring(self):
        draws=[self.service.qq_reply_draw('a','sample-'+str(i)) for i in range(1000)]
        self.assertGreater(sum(x<0.7 for x in draws),620)
        self.assertLess(sum(x<0.7 for x in draws),780)
        self.assertEqual(draws[0],self.service.qq_reply_draw('a','sample-0'))
        self.assertNotEqual(draws[0],self.service.qq_reply_draw('b','sample-0'))
        self.assertFalse(self.service.qq_term_in_text('AI','rainfall'))
        self.assertTrue(self.service.qq_term_in_text('AI','聊聊AI进展'))


if __name__=='__main__':unittest.main()
