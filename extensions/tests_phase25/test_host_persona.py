"""Complete trusted Host personality reaches the shared core without learning."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from maibot_unified_core.service import UnifiedService
from maibot_unified_core.store import HOST_PERSONA_FIELDS, State


class HostPersonaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = {'allowed_qq_sessions': ['a'], 'shared_groups': [['a']],
                       'bot_accounts': {'qq': 'bot-qq', 'bilibili': '100'}}
        (self.root / 'policy.json').write_text(json.dumps(self.policy))
        self.values = {'bot.nickname': 'synthetic-role', 'bot.alias_names': ['synthetic-alias'],
                       'personality.personality': 'Stable identity fixture.',
                       'personality.reply_style': 'Stable reply style fixture.',
                       'personality.multiple_reply_style': ['Stable alternative style.'],
                       'personality.behavior_style': 'Trusted nickname reactions and proactive behavior.'}
        self.read_keys = []
        async def get(key):
            self.read_keys.append(key)
            return self.values.get(key)
        async def capability(name, **kwargs):
            return {'success': True, 'hits': []}
        host = SimpleNamespace(config=SimpleNamespace(get=get), call_capability=capability,
                               logger=SimpleNamespace(info=lambda *args: None))
        self.service = UnifiedService(host, self.root)
        self.state = State(self.root / 'state.sqlite', self.policy)
        self.service.state = self.state
        self.service.policy = self.policy

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    async def test_behavior_style_migrates_partial_snapshot_and_is_the_growth_rollback_baseline(self):
        partial = {k: v for k, v in self.values.items() if k != 'personality.behavior_style'}
        self.state.init_persona(partial, {'avoid_phrases': []})
        synced = await self.service.sync_host_persona()
        self.assertEqual(synced, {'status': 'synced', 'version': 2})
        self.assertEqual(set(self.read_keys), HOST_PERSONA_FIELDS)
        current = self.state.persona()
        self.assertEqual(current['stable'], self.values)
        self.assertEqual(current['reason'], 'trusted_host_config_revision')
        self.assertEqual(current['evidence_ids'], [])
        self.assertEqual(current['dynamic'], {'avoid_phrases': []})
        self.assertEqual(self.state.quota_used(), 0)
        context = await self.service.build_context('proactive', 'bilibili', 'video:1', 'synthetic', 'public')
        self.assertEqual(context['persona']['stable']['personality.behavior_style'], self.values['personality.behavior_style'])
        event_id = self.state.ingest({'platform': 'qq', 'bot_account_id': 'bot-qq', 'conversation_id': 'a',
            'actor_id': 'user', 'native_event_id': 'feedback-fixture', 'event_type': 'user_feedback',
            'origin': 'external_user', 'sharing_scope': 'policy', 'learning_allowed': True,
            'content': '请少用哼这个口头禅'})['event_id']
        forbidden = self.state.propose_changes({'personality.behavior_style': 'model rewrite'}, [event_id], 'untrusted change')
        self.assertFalse(forbidden['accepted'])
        growth = self.state.propose_changes({'avoid_phrases': ['哼']}, [event_id], 'explicit expression feedback')
        self.assertTrue(growth['accepted'])
        self.assertEqual(growth['version'], 3)
        with self.assertRaisesRegex(ValueError, 'stable_identity'):
            self.state.rollback_persona(1)
        rolled_back = self.state.rollback_persona(2)
        self.assertEqual(rolled_back['version'], 4)
        self.assertEqual(rolled_back['stable'], self.values)
        self.assertEqual(rolled_back['dynamic'], {'avoid_phrases': []})

    async def test_missing_behavior_style_defers_without_replacing_existing_stable_snapshot(self):
        self.state.init_persona(self.values)
        before = self.state.persona()
        self.values.pop('personality.behavior_style')
        result = await self.service.sync_host_persona()
        self.assertEqual(result, {'status': 'deferred', 'version': before['version']})
        self.assertEqual(self.state.persona(), before)
        self.assertEqual(self.state.get('host_persona_sync')['failed_fields'], ['personality.behavior_style'])


if __name__ == '__main__':
    unittest.main()
