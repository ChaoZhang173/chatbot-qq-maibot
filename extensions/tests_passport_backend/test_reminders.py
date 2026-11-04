import asyncio
import unittest
from datetime import datetime

from maibot_unified_core.reminders import Reminders, parse_when, TZ
from maibot_unified_core.voice_scope import CURRENT
from maibot_unified_core.store import State
from maibot_unified_core.private_access import PrivateAccess
from .test_identity import IdentityCase


class TimeTests(unittest.TestCase):
    def test_relative_and_china_clock(self):
        now = datetime(2026, 11, 4, 21, 10, tzinfo=TZ).timestamp()
        for text, seconds in [("半小时后",1800), ("过二十分钟",1200), ("两小时后",7200),
                              ("一个半小时后",5400), ("一小时半后",5400), ("两小时二十分钟后",8400)]:
            self.assertEqual(parse_when(text, now), now + seconds, text)
        for text, stamp in [("晚上十点半","2026-11-04T22:30:00+08:00"),
                            ("明早七点","2026-11-05T07:00:00+08:00"),
                            ("22:30","2026-11-04T22:30:00+08:00"),
                            ("今晚十二点","2026-11-05T00:00:00+08:00"),
                            ("2026-11-06 08:00","2026-11-06T08:00:00+08:00")]:
            self.assertEqual(datetime.fromtimestamp(parse_when(text, now), TZ).isoformat(), stamp)
        for text in ("七点", "今天晚上八点", "过去20分钟", "0秒后", "99999小时后", "25:00", "下次有空"):
            with self.assertRaises(ValueError, msg=text):
                parse_when(text, now)


class ReminderTests(IdentityCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.device = "dev-reminder"
        self.pid = self.add_profile("小花", self.device, address="小花")
        self.route = await self.svc.voice.ensure_session(self.svc.voice.binding(self.device))
        self.now = datetime(2026, 11, 4, 12, 0, tzinfo=TZ).timestamp()
        self.rem = Reminders(self.svc, now=lambda:self.now)
        self.svc.state.put("passport_notify_seen:" + self.device, self.now)
        self.scope = {"device_id":self.device,"route":self.route,"message":("提醒我","test-request-1","user")}
        self.prompts = []
        async def generate(messages, **kw):
            self.prompts.append(messages)
            return {"reply":"小花，午觉的时间到了，先伸个懒腰，我在这儿陪你慢慢醒过来。", "mood":"gentle"}
        self.svc.generate = generate

    def tool(self, *args, **kw):
        token = CURRENT.set(self.scope)
        try:
            return self.rem.tool(*args, **kw)
        finally:
            CURRENT.reset(token)

    async def rpc(self, op, job=None, **kw):
        return await self.rem.rpc({"op":"reminder_" + op,"device_id":self.device,
                                   **({k:job[k] for k in ("id","token")} if job else {}), **kw})

    def create(self):
        result = self.tool("create", "半小时后", "午睡叫醒")
        self.assertTrue(result["success"], result)
        return result["reminder"]

    async def test_durable_idempotent_schedule_update_and_cancel(self):
        first = self.create()
        self.assertEqual(self.create()["id"], first["id"])
        self.svc.state.close()
        self.svc.state = State(self.root / "state.sqlite", self.svc.policy)
        self.svc.private_access = PrivateAccess(self.svc)
        self.rem = Reminders(self.svc, now=lambda:self.now)
        self.assertEqual(len(self.tool("list")["reminders"]), 1)
        result = self.tool("update", "明早七点", reminder_id=first["id"])
        self.assertEqual(result["reminder"]["time"], "2026-11-05T07:00:00+08:00")
        self.assertTrue(self.tool("cancel", reminder_id=first["id"])["success"])
        self.now += 86400
        self.assertIsNone((await self.rpc("claim"))["job"])

    async def test_generated_persona_speech_and_actual_receipt(self):
        self.create()
        self.assertIsNone((await self.rpc("claim"))["job"])
        self.now += 1800
        job = (await self.rpc("claim"))["job"]
        reply = await self.rpc("prepare", job)
        self.assertIn("小花", reply["reply"])
        self.assertIn("persona", self.prompts[0][1]["content"])
        self.assertIn("午睡叫醒", self.prompts[0][1]["content"])
        self.assertEqual(len(self.events()), 0)  # internal timer never impersonates a user utterance
        await self.rpc("arm", job)
        self.assertIsNone((await self.rpc("claim"))["job"])
        self.assertEqual((await self.rpc("report", job, outcome="delivered"))["status"], "delivered")
        await self.rpc("report", job, outcome="delivered")
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["origin"], "bot_self")
        self.assertIsNone((await self.rpc("claim"))["job"])

    async def test_crash_before_speech_retries_but_after_arm_never_replays(self):
        self.create(); self.now += 1800
        job = (await self.rpc("claim"))["job"]
        self.now += 121
        retried = (await self.rpc("claim"))["job"]
        self.assertNotEqual(job["token"], retried["token"])
        with self.assertRaises(ValueError):
            await self.rpc("prepare", job)
        await self.rpc("prepare", retried)
        await self.rpc("arm", retried)
        self.now += 121
        self.assertIsNone((await self.rpc("claim"))["job"])
        self.assertEqual(self.tool("list")["reminders"][0]["status"], "unknown")

    async def test_busy_defers_without_changing_requested_time_and_offline_expires(self):
        first = self.create(); self.now += 1800
        job = (await self.rpc("claim"))["job"]
        await self.rpc("report", job, outcome="defer")
        self.assertEqual(self.tool("list")["reminders"][0]["time"], first["time"])
        self.assertIsNone((await self.rpc("claim"))["job"])
        self.now += 301
        self.assertIsNone((await self.rpc("claim"))["job"])
        self.assertEqual(self.tool("list")["reminders"][0]["status"], "missed")

    async def test_cancel_during_generation_wins_and_other_profile_cannot_get_it(self):
        first = self.create(); self.now += 1800
        job = (await self.rpc("claim"))["job"]
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*a, **kw):
            entered.set(); await release.wait()
            return {"reply":"小花，醒醒啦。"}
        self.svc.generate = slow
        task = asyncio.create_task(self.rpc("prepare", job))
        await entered.wait()
        self.assertTrue(self.tool("cancel", reminder_id=first["id"])["success"])
        release.set()
        with self.assertRaises(ValueError):
            await task
        other = self.add_profile("另一个人", "dev-other")
        self.edit(lambda r:r["devices"].update({self.device:{"profile_id":other,"enabled":True}}))
        self.assertIsNone((await self.rpc("claim"))["job"])
        with self.assertRaises(ValueError):
            await self.rpc("prepare", job)

    async def test_requires_voice_scope_and_capable_connected_device(self):
        self.assertFalse(self.rem.tool("create", "半小时后", "叫醒")["success"])
        self.svc.state.put("passport_notify_seen:" + self.device, 0)
        self.assertFalse(self.tool("create", "半小时后", "叫醒")["success"])

    async def test_connection_heartbeat_does_not_claim_due_reminder(self):
        self.create()
        self.now += 1800
        self.assertEqual(await self.rpc("seen"), {"supported": True})
        self.assertEqual(self.tool("list")["reminders"][0]["status"], "pending")
        self.assertTrue(self.tool("create", "十分钟后", "喝水")["success"])
        self.assertIsNotNone((await self.rpc("claim"))["job"])

    async def test_failure_receipt_survives_list_but_successful_retry_replaces_it(self):
        self.svc.state.put("passport_notify_seen:" + self.device, 0)
        self.assertFalse(self.tool("create", "十分钟后", "叫醒")["success"])
        self.tool("list")
        self.assertIn("不会自动叫你", self.rem.failed_voice_reply(self.scope["reminder_receipt"]))
        await self.rpc("seen")
        self.assertTrue(self.tool("create", "十分钟后", "叫醒")["success"])
        self.assertIsNone(self.rem.failed_voice_reply(self.scope["reminder_receipt"]))

    async def test_admin_push_is_idempotent_and_not_a_user_message(self):
        self.svc.reminders = self.rem
        request = {"op":"reminder_admin_create", "device_id":self.device, "content":"测试叫醒", "delay_seconds":20, "request_id":"test-wake-once"}
        with self.assertRaises(PermissionError):
            await self.svc.dispatch(request, admin=False)
        first = await self.svc.dispatch(request, admin=True)
        second = await self.svc.dispatch(request, admin=True)
        self.assertEqual(first["reminder"]["id"], second["reminder"]["id"])
        self.assertEqual(len(self.events()), 0)
