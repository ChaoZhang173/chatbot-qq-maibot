"""Phase 2B/1: seed migration across restarts and 24h/72h short-lived state, including caches and the persona tool."""
import json
import unittest

from maibot_unified_core import persona as character
from maibot_unified_core.store import State
from v14_2_support import Harness, load_json, HOUR
from test_persona_style import grow, MARK


def public_event(h, oid, hours_ago, text='AI游戏新闻'):
    return h.state.ingest(h.bili_read(oid, text, author=str(oid), when=h.clock() - hours_ago * HOUR))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_seed_revision_migrates_only_absent_or_known_seeds_and_keeps_explicit_empty(self):
        h = Harness(self, seed=False)
        unified = load_json('config/unified.current.example.json')
        stable = load_json('config/persona.current.example.json')['stable']
        host = {'bot.nickname': '大肥鱼', 'bot.alias_names': ['肥鱼'], 'personality.reply_style': 'x', 'personality.multiple_reply_style': [],
                'personality.personality': stable['personality.personality'], 'personality.behavior_style': stable['personality.behavior_style']}
        # a v13 instance: legacy pre-seeded values, one grown field, one explicitly cleared field
        legacy = {k: v[0] for k, v in character.LEGACY_V13_SEEDS.items()}
        grown = '喜欢像素风肉鸽和反转悬疑番，说话偏短'
        h.state.sync_learnable_persona(host, {**legacy, 'aliases': ['蓝色大肥鱼'], 'interests': ['游戏', '动漫'], 'display_name': 'DeepSeek'}, 'v13')
        grow(h, reply_style=grown, interests=[], aliases=[], technology_preferences=['AI科技'])
        # restart + new seed revision with the v14.2 defaults
        h.state.close()
        h.state = h.service.state = State(h.root / 'state.sqlite', h.service.policy, now=h.clock)
        self.addCleanup(h.state.close)
        h.state.sync_learnable_persona(host, unified['persona_defaults'], 'v14.2')
        dynamic = h.state.persona()['dynamic']
        self.assertEqual(dynamic['reply_style'], grown)                       # grown knowledge preserved
        self.assertEqual(dynamic['interests'], [])                            # explicit [] is not "missing"
        self.assertEqual(dynamic['aliases'], [])
        self.assertEqual(dynamic['technology_preferences'], ['AI科技'])
        self.assertEqual(dynamic['reply_styles'], [])                         # known legacy scripts cleared
        self.assertEqual(dynamic['tone'], unified['persona_defaults']['tone'])  # known legacy value migrated
        self.assertEqual(dynamic['behavior_preferences'], unified['persona_defaults']['behavior_preferences'])
        # no fresh mood/focus from deployment time; the legacy unseeded values have no metadata and are not shown
        self.assertEqual(h.state.persona_view()['dynamic'].get('current_state'), None)
        self.assertEqual(h.state.persona_view()['dynamic'].get('current_focus'), None)
        # restart again with the same revision: nothing changes
        version = h.state.persona()['version']
        h.state.sync_learnable_persona(host, unified['persona_defaults'], 'v14.2')
        self.assertEqual(h.state.persona()['version'], version)


class ShortLivedStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_focus_expires_from_original_evidence_even_with_cache_and_tool(self):
        h = Harness(self)
        event = public_event(h, 11, hours_ago=71)                 # evidence is 71h old: 1h of its 72h remains
        self.assertTrue(h.state.set_temp('current_focus', '想看看新出的像素肉鸽', event['event_id'])['accepted'])
        h.service.last_message['a'] = ('hi', 'e', 'user1')
        first = await h.service.qq_context('a')
        self.assertIn('想看看新出的像素肉鸽', first)
        cached = await h.service.qq_context('a')                  # served from the 90s cache
        self.assertEqual(cached, first)
        tool = await h.service.tool('persona', {'session_id': 'a'})
        self.assertIn('current_focus', tool['content']['dynamic'])
        # a model restating the same value (even with fresh wording of the call) cannot renew it
        self.assertFalse(h.state.set_temp('current_focus', '想看看新出的像素肉鸽', event['event_id'])['accepted'])
        h.clock.advance(2 * HOUR)                                 # past expiry; the cache entry is still younger than 90s of monotonic time
        after = await h.service.qq_context('a')
        self.assertNotIn('想看看新出的像素肉鸽', after)
        self.assertNotIn('current_focus', json.loads(after.split(MARK)[1])['dynamic'])
        tool = await h.service.tool('persona', {'session_id': 'a'})
        self.assertNotIn('current_focus', tool['content']['dynamic'])  # the persona tool entry honours expiry too
        self.assertIn('current_focus', h.state.persona()['dynamic'])   # raw history keeps it (administrative view)
        # layered snapshot cache is keyed on the same signature
        h.set_policy(token_optimizations={'context_layers': {'enabled': True}})
        layers = await h.service.qq_context_layers('a')
        self.assertNotIn('想看看新出的像素肉鸽', layers['snapshot'])

    async def test_command_planner_and_command_review_prompts_use_the_ttl_aware_view(self):
        import json as _json
        from maibot_unified_core.command_planner import interpret
        from maibot_unified_core.commands import CommandQueue
        h = Harness(self)
        h.set_policy(qq_bili_commands_enabled=True, qq_command_model_planning_enabled=True)
        h.service.commands = CommandQueue(h.service)
        queue = h.service.commands
        event = public_event(h, 31, hours_ago=71)
        self.assertTrue(h.state.set_temp('current_focus', '想看看新出的像素肉鸽', event['event_id'])['accepted'])
        spec = {'kinds': ['like'], 'target_type': 'video', 'target': 'BV1gB4y1F7pV', 'text': '', 'prompt': ''}

        def persona_in(call):
            return _json.loads(call['prompt'][1]['content'])['persona']['dynamic']

        async def planner(key):
            queue.enqueue(key, 'a', 'owner', 'm' + key, {'_natural_request': '【命令】先别操作，只是问问', 'provided_targets': [], 'image_id': ''}, True)
            h.llm.queue.append({'status': 'no_action', 'reason': '没有进行修改'})
            await interpret(queue, key)
            return persona_in(h.llm.calls[-1])

        async def review(key):
            queue.enqueue(key, 'a', 'owner', 'r' + key, spec, True)
            self.assertIsNotNone(queue.claim()['job'])
            h.llm.queue.append({'topic_allowed': True, 'privacy_allowed': True, 'willing': True})
            await queue.review(key, '公开的材料')
            queue.finish(key, {'status': 'success'})            # so the next claim picks the next job
            return persona_in(h.llm.calls[-1])

        self.assertIn('current_focus', await planner('k1'))          # still valid: visible
        self.assertIn('current_focus', await review('k2'))
        h.clock.advance(2 * HOUR)                                     # now expired
        self.assertNotIn('current_focus', await planner('k3'))
        self.assertNotIn('current_focus', await review('k4'))

    async def test_state_is_24h_and_already_expired_evidence_is_rejected(self):
        h = Harness(self)
        old = public_event(h, 12, hours_ago=30)
        self.assertEqual(h.state.set_temp('current_state', '有点好奇', old['event_id'])['reason'], 'evidence_already_expired')
        fresh = public_event(h, 13, hours_ago=1)
        self.assertTrue(h.state.set_temp('current_state', '有点好奇', fresh['event_id'])['accepted'])
        self.assertIn('current_state', h.state.persona_view()['dynamic'])
        h.clock.advance(23.5 * HOUR)
        self.assertNotIn('current_state', h.state.persona_view()['dynamic'])
        # evidence that may not feed global growth cannot set a global state (private chat material)
        private = h.state.ingest(h.bili_read(14, '私下聊天', scope='conversation', when=h.clock()))
        self.assertEqual(h.state.set_temp('current_state', '私聊心情', private['event_id'])['reason'], 'global_usable_evidence_required')

    async def test_accepted_proposal_gets_expiry_from_cited_evidence_not_from_now(self):
        h = Harness(self)
        a, b = public_event(h, 21, 60, '新游戏介绍一'), public_event(h, 22, 50, '新游戏介绍二')
        result = h.state.propose_changes([{'field': 'current_focus', 'value': '追一款新游戏', 'evidence_ids': [a['event_id'], b['event_id']]}],
                                         [a['event_id'], b['event_id']], 'focus', automatic=True)
        self.assertTrue(result['accepted'], result)
        row = h.state.db.execute("SELECT evidence_at,expires_at FROM persona_temp WHERE field='current_focus'").fetchone()
        self.assertAlmostEqual(row['expires_at'] - row['evidence_at'], 72 * HOUR)
        self.assertAlmostEqual(h.clock() - row['evidence_at'], 50 * HOUR, delta=1)
