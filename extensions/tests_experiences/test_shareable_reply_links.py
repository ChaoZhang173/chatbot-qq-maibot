"""Reply provenance stays resolvable when several authors share one chat window."""
import copy

from test_experiences import Fixture
from maibot_unified_core import shareable_experience as sharing


class ReplyLinks(Fixture):
    def test_group_reply_targets_original_author_not_latest_neighbor(self):
        original = self.add('original', '我还没带耳机。', platform='qq', session='group',
                            actor='201', origin='external_user', kind='message_received',
                            scope='policy', stamp=self.now-80)
        other = self.add('neighbor', '我在吃饭。', platform='qq', session='group',
                         actor='202', origin='external_user', kind='message_received',
                         scope='policy', stamp=self.now-60,
                         metadata={'reply_to': original['source_id']})
        self.s.state.record_activity({'action_id':'reply', 'platform':'qq', 'bot_account_id':'900',
            'conversation_id':'group', 'kind':'reply', 'status':'succeeded', 'receipt_verified':True,
            'content':'那就等有耳机再听。', 'reply_to':original['source_id'], 'created_at':self.now-10})
        item = sharing.collect(self.s, self.now-100, self.now)[0]
        records = item['records']
        self.assertEqual(records[1]['source_id'], other['event_id'])
        self.assertEqual(records[1]['reply_to_source_id'], original['event_id'])
        self.assertEqual(records[-1]['reply_to_source_id'], original['event_id'])
        self.assertEqual(records[-1]['reply_relation'], 'verified')
        self.assertTrue(sharing.valid_refs(self.s, item['source_refs']))

    def test_missing_target_is_unresolved_not_assigned_to_neighbor(self):
        self.conversation()
        item = sharing.collect(self.s, self.now-100, self.now)[0]
        records = copy.deepcopy(item['records'])
        records[-1]['reply_to'] = 'older-message-outside-window'
        source = self.s.state.get_event(records[0]['source_id'])
        sharing.bind_reply_links(records, [source])
        self.assertEqual(records[-1]['reply_to_source_id'], '')
        self.assertEqual(records[-1]['reply_relation'], 'unresolved')

    def test_model_window_truncation_cannot_leave_verified_dangling_target(self):
        original = self.add('old-visible', '原先的问题。', platform='qq', session='group',
                            actor='201', origin='external_user', kind='message_received',
                            scope='policy', stamp=self.now-100)
        for i in range(4):
            self.add('new-'+str(i), '另一个人的话题。', platform='qq', session='group', actor='202',
                     origin='external_user', kind='message_received', scope='policy', stamp=self.now-80+i)
        self.s.state.record_activity({'action_id':'later-reply', 'platform':'qq', 'bot_account_id':'900',
            'conversation_id':'group', 'kind':'reply', 'status':'succeeded', 'receipt_verified':True,
            'content':'回复原先的问题。', 'reply_to':original['source_id'], 'created_at':self.now-10})
        records = sharing.collect(self.s, self.now-120, self.now)[0]['records']
        self.assertEqual(len(records), 4)
        self.assertEqual(records[-1]['reply_to_source_id'], '')
        self.assertEqual(records[-1]['reply_relation'], 'unresolved')

    def test_ambiguous_or_hidden_alias_never_resolves(self):
        records = [{'source_id':'event-a'}, {'source_id':'event-b'},
                   {'source_id':'sent', 'reply_to':'duplicate'}]
        sharing.bind_reply_links(records, [{'event_id':'event-a', 'source_id':'duplicate'},
                                           {'event_id':'event-b', 'source_id':'duplicate'}])
        self.assertEqual(records[-1]['reply_relation'], 'unresolved')
        records[-1]['reply_to'] = 'hidden-native'
        sharing.bind_reply_links(records, [{'event_id':'hidden-event', 'source_id':'hidden-native'}])
        self.assertEqual(records[-1]['reply_to_source_id'], '')
        self.assertEqual(records[-1]['reply_relation'], 'unresolved')

    def test_no_reference_stays_unspecified(self):
        records = [{'source_id':'event'}, {'source_id':'sent', 'reply_to':''}]
        sharing.bind_reply_links(records, [{'event_id':'event', 'source_id':'native'}])
        self.assertEqual(records[-1]['reply_to_source_id'], '')
        self.assertEqual(records[-1]['reply_relation'], 'unspecified')
