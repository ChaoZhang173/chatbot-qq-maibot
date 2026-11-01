"""P1：窄范围“回复后本地结束”。通过真实 MaisakaReasoningEngine._handle_tool_calls /
_handle_planner_response_actions 路径测试；planner 调用次数用同一处理函数驱动的最小循环统计。"""
import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import host_loader


def call(name, call_id="c1", **args):
    return SimpleNamespace(call_id=call_id, func_name=name, args=args)


def result(success=True, segments=1, send_ok=None, **extra):
    send_ok = [True] * segments if send_ok is None else send_ok
    return SimpleNamespace(
        success=success,
        structured_content={"msg_id": "m1", "reply_segments": ["x"] * segments,
                            "send_results": [{"success": ok} for ok in send_ok]},
        content_items=[], post_history_messages=[], metadata={}, stop_after_execution=False,
        get_history_content=lambda: "", **extra)


def plain_result():
    return SimpleNamespace(success=True, structured_content=None, content_items=[], post_history_messages=[],
                           metadata={}, stop_after_execution=False, get_history_content=lambda: "")


class Registry:
    def __init__(self, outcomes, on_invoke=None):
        self.outcomes, self.on_invoke, self.invoked, self.seen_args = outcomes, on_invoke, [], []

    async def list_tools(self, ctx):
        return []

    async def invoke(self, invocation, ctx):
        self.invoked.append(invocation.tool_name)
        self.seen_args.append(dict(getattr(invocation, "arguments", {}) or {}))
        if self.on_invoke:
            self.on_invoke(invocation.tool_name)
        return self.outcomes[invocation.tool_name]


class Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = host_loader.load("maisaka.reasoning_engine", ("maisaka.reply_termination",))
        cls.reset = staticmethod(cls.module.get_token_opt_settings.__globals__["reset_cache_for_tests"])

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"MAIBOT_TOKEN_OPT_REPLY_TERMINATION": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.reset)
        self.reset()
        self.pending = False
        self.waiting = False
        self.stopped = []
        self.target = SimpleNamespace(message_id="m1")
        queue = asyncio.Queue()
        self.runtime = SimpleNamespace(
            log_prefix="[t]", _tool_registry=None, _update_stage_status=lambda *a, **k: None, _internal_turn_queue=queue, _focus_group=None,
            _chat_history=[], is_action_tool_currently_available=lambda n: True,
            _reset_consecutive_wait_count=lambda *a: None, _end_planner_continuation=lambda: None,
            _enter_stop_state=lambda: self.stopped.append(1),
            _has_pending_messages=lambda: self.pending, _has_pending_wait_tool_call=lambda: self.waiting,
            _is_focus_mode_active_for_current_chat=lambda: False,
            find_source_message_by_id=lambda mid: self.target if mid == "m1" else None)
        self.runtime_queue = queue
        self.engine = self.module.MaisakaReasoningEngine(self.runtime)
        engine = self.engine
        for name in ("_log_tool_call_source", "_build_tool_execution_context", "_build_tool_availability_context",
                     "_append_tool_execution_result", "_append_tool_post_history_messages"):
            setattr(engine, name, mock.Mock())
        # arguments 与 tool_call.args 共用同一字典：最不利的情形，剥离 final_reply 后判定仍必须读到信号。
        engine._build_tool_invocation = lambda tc, thought: SimpleNamespace(
            tool_name=tc.func_name, call_id=tc.call_id, arguments=tc.args)
        engine._record_tool_execution_effects = mock.AsyncMock()
        engine._emit_planner_progress = mock.AsyncMock()

        async def display(*, tool_monitor_results, **kw):
            tool_monitor_results.append({})
        engine._append_tool_display_results = display
        patches = [mock.patch.object(self.module, "global_config",
                                     SimpleNamespace(capabilities=SimpleNamespace(tool_result_condense=False))),
                   mock.patch.object(self.module, "is_bot_self",
                                     lambda platform, uid: None if uid == "?" else uid == "bot")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        engine._turn_facts = self.module.TurnFacts(round_index=0, batch_external_message_ids=("m1",),
                                                   sources_verified=True)

    async def run_batch(self, calls, outcomes, on_invoke=None):
        self.runtime._tool_registry = Registry(outcomes, on_invoke)
        out = await self.engine._handle_tool_calls(calls, "", SimpleNamespace(), self.module.CycleRuntimeState())
        return out, self.runtime._tool_registry

    def reply_call(self, **extra):
        return call("reply", msg_id="m1", reply_reference="r", **extra)

    async def assert_continues(self, calls, outcomes, on_invoke=None):
        (stop, reason, *_), _ = await self.run_batch(calls, outcomes, on_invoke)
        self.assertFalse(stop, reason)
        self.assertEqual(self.stopped, [])


class TestDecisionWiring(Base):
    async def test_explicit_final_reply_stops_with_dedicated_reason(self):
        (stop, reason, *_), registry = await self.run_batch([self.reply_call(final_reply=True)], {"reply": result()})
        self.assertTrue(stop)
        self.assertEqual(reason, "reply_completed_local_stop")
        self.assertEqual(self.stopped, [1])
        end = self.module.MaisakaReasoningEngine._cycle_end_for_pause_tool(reason)
        self.assertEqual(end.reason, "reply_completed_local_stop")
        # final_reply 在工具执行前被剥离，不会流入 reply 工具（replyer/Hook）。
        self.assertEqual(registry.seen_args, [{"msg_id": "m1", "reply_reference": "r"}])

    async def test_wrapped_arguments_json_signal_is_read(self):
        wrapped = call("reply", arguments=json.dumps({"msg_id": "m1", "reply_reference": "r", "final_reply": True}))
        (stop, *_), _ = await self.run_batch([wrapped], {"reply": result()})
        self.assertTrue(stop)

    async def test_default_off_keeps_old_behaviour_even_with_signal(self):
        with mock.patch.dict(os.environ, {"MAIBOT_TOKEN_OPT_REPLY_TERMINATION": "0"}):
            self.reset()
            await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    async def test_no_signal_false_signal_or_non_bool_continue(self):
        for extra in ({}, {"final_reply": False}, {"final_reply": "true"}, {"final_reply": 1}):
            self.stopped.clear()
            await self.assert_continues([self.reply_call(**extra)], {"reply": result()})

    async def test_unknown_mode_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "opt.json")
            with open(path, "w") as handle:
                json.dump({"reply_termination": {"enabled": True, "mode": "single_step"}}, handle)
            with mock.patch.dict(os.environ, {"MAIBOT_TOKEN_OPT_CONFIG": path}):
                self.reset()
                await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    async def test_new_message_arriving_during_reply_is_not_lost(self):
        def arrive(name):
            self.pending = True  # 回复发送期间到达
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()}, arrive)

    async def test_pending_wait_or_queued_internal_trigger_continue(self):
        self.waiting = True
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})
        self.waiting = False
        self.runtime_queue.put_nowait("proactive")
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    async def test_multi_target_batch_and_private_multi_message_continue(self):
        self.engine._turn_facts = self.module.TurnFacts(round_index=0, batch_external_message_ids=("m1", "m2"),
                                                       sources_verified=True)
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    async def test_reply_with_query_in_same_batch_continues(self):
        calls = [self.reply_call(final_reply=True), call("unified_memory", "c2", query="q")]
        await self.assert_continues(calls, {"reply": result(), "unified_memory": plain_result()})

    async def test_segment_send_failure_or_partial_continues(self):
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result(success=False, send_ok=[False])})
        await self.assert_continues([self.reply_call(final_reply=True)],
                                    {"reply": result(segments=2, send_ok=[True])})  # 少一段确认
        await self.assert_continues([self.reply_call(final_reply=True)],
                                    {"reply": result(segments=2, send_ok=[True, False])})

    async def test_wait_tool_and_proactive_turn_and_later_round_continue(self):
        await self.assert_continues([call("wait", "c3", seconds=5)], {"wait": plain_result()})
        self.engine._turn_facts = self.module.TurnFacts(round_index=0, batch_external_message_ids=("m1",),
                                                       proactive_triggered=True, sources_verified=True)
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})
        self.engine._turn_facts = self.module.TurnFacts(round_index=1, batch_external_message_ids=("m1",),
                                                       sources_verified=True)
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    async def test_reply_to_other_message_continues(self):
        self.target = SimpleNamespace(message_id="m9")
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    async def test_focus_mode_continues(self):
        self.runtime._focus_group = object()
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    async def test_decision_error_falls_back_to_continue(self):
        self.runtime._has_pending_messages = mock.Mock(side_effect=RuntimeError("x"))
        await self.assert_continues([self.reply_call(final_reply=True)], {"reply": result()})

    def test_classify_batch_never_ignores_unknown_or_non_chat_items(self):
        classify = self.engine._classify_batch
        self.assertEqual(classify([chat("a"), chat("b", uid="bot")]), (["a"], True))  # 已确认的机器人自身可忽略
        for bad in (SimpleNamespace(message_id="c"), chat("d", uid="?"), chat("", mid=""),
                    chat("e", command=True), chat("f", notify=True), chat("g", command=None), chat("h", notify=None)):
            ids, verified = classify([chat("a"), bad])
            self.assertFalse(verified, bad)
            self.assertEqual(ids, ["a"])


def chat(name, uid="u", mid=None, command=False, notify=False):
    return SimpleNamespace(message_id=name if mid is None else mid, platform="qq", is_command=command,
                           is_notify=notify, message_info=SimpleNamespace(user_info=SimpleNamespace(user_id=uid)))


class TestRealBatchCapture(Base):
    """真实 run_loop：批次来源捕获 → TurnFacts → 终结判定 → planner 调用次数。"""

    async def run_turn(self, batch):
        runtime, engine = self.runtime, self.engine
        runtime.__dict__.update(
            _running=True, _max_internal_rounds=5, _consume_forced_turn_reason=lambda: None,
            _STATE_RUNNING="running", _STATE_WAIT="wait", _agent_state="stop",
            _start_planner_continuation=lambda: None, _log_internal_loop_cancelled=lambda: None)
        triggers = ["message"]

        async def get():
            if triggers:
                return triggers.pop()
            raise asyncio.CancelledError()
        runtime._internal_turn_queue = SimpleNamespace(get=get, empty=lambda: True)
        engine._prepare_turn_start_context = mock.AsyncMock(return_value=self.module.TurnStartContext(
            batch, batch[-1], False, False, False, "t1"))
        engine._start_cycle_round = mock.AsyncMock(return_value=(SimpleNamespace(time_records={}, cycle_id=1), "r"))
        engine._refresh_visual_placeholders_for_cycle = mock.AsyncMock()
        engine._refresh_mid_term_memory_reference_for_continuation = mock.AsyncMock()
        engine._ingest_pending_messages = mock.AsyncMock(return_value=[])
        engine._should_replace_reasoning = lambda content: False
        engine._end_planner_no_tool_cycle = mock.Mock()
        self.runtime._tool_registry = Registry({"reply": result()})
        self.planner_calls = 0
        reply_response = SimpleNamespace(content="", output_items=[], raw_messages=[],
                                         tool_calls=[self.reply_call(final_reply=True)])
        stop_response = SimpleNamespace(content="", output_items=[], raw_messages=[], tool_calls=[])

        async def planner(*, state, **kw):
            state.response = (reply_response, stop_response)[min(self.planner_calls, 1)]
            self.planner_calls += 1

        async def finalize(*, state, **kw):
            return state.cycle_end
        engine._run_planner_request = planner
        engine._finalize_cycle = finalize
        with self.assertRaises(asyncio.CancelledError):
            await engine.run_loop()
        return self.planner_calls

    async def test_single_ordinary_message_saves_the_extra_planner_call(self):
        self.assertEqual(await self.run_turn([chat("m1")]), 1)

    async def test_known_plus_unknown_source_keeps_original_flow(self):
        for unknown in (chat("m2", uid="?"), chat("m2", command=None), SimpleNamespace(message_id="m2")):
            self.assertEqual(await self.run_turn([chat("m1"), unknown]), 2)

    async def test_ordinary_plus_command_or_notice_keeps_original_flow(self):
        self.assertEqual(await self.run_turn([chat("m1"), chat("m2", command=True)]), 2)
        self.assertEqual(await self.run_turn([chat("m1"), chat("m2", notify=True)]), 2)

    async def test_command_or_notice_only_batch_is_never_terminated(self):
        self.assertEqual(await self.run_turn([chat("m1", command=True)]), 2)
        self.assertEqual(await self.run_turn([chat("m1", notify=True)]), 2)

    async def test_two_ordinary_messages_keep_original_flow(self):
        self.assertEqual(await self.run_turn([chat("m1"), chat("m2")]), 2)


class TestPlannerCallCount(Base):
    async def planner_calls(self, first_call_args, enabled=True):
        """用真实 _handle_planner_response_actions 的“是否 break”驱动最小循环，统计 planner 调用数。"""
        engine = self.engine
        engine._should_replace_reasoning = lambda content: False
        engine._end_planner_no_tool_cycle = mock.Mock()
        self.runtime._tool_registry = Registry({"reply": result()})
        reply_response = SimpleNamespace(content="", output_items=[], raw_messages=[],
                                         tool_calls=[self.reply_call(**first_call_args)])
        stop_response = SimpleNamespace(content="", output_items=[], raw_messages=[], tool_calls=[])
        calls = 0
        for response in (reply_response, stop_response):
            calls += 1
            state = self.module.CycleRuntimeState()
            if await engine._handle_planner_response_actions(
                    response=response, cycle_detail=SimpleNamespace(time_records={}), state=state, planner_extra_lines=[]):
                return calls, state.cycle_end.reason
        return calls, state.cycle_end.reason

    async def test_allowed_path_saves_one_planner_call(self):
        self.assertEqual(await self.planner_calls({"final_reply": True}), (1, "reply_completed_local_stop"))

    async def test_baseline_without_signal_still_needs_the_stop_call(self):
        self.assertEqual(await self.planner_calls({}), (2, "planner_no_tool_end"))

    async def test_disabled_needs_the_stop_call_even_with_signal(self):
        with mock.patch.dict(os.environ, {"MAIBOT_TOKEN_OPT_REPLY_TERMINATION": "0"}):
            self.reset()
            self.assertEqual(await self.planner_calls({"final_reply": True}), (2, "planner_no_tool_end"))


class TestPureDecisionAndIdleBackoff(unittest.TestCase):
    def test_reply_local_stop_counts_like_reply_then_stop_for_idle_backoff(self):
        policy = host_loader.load("maisaka.mode_policy")
        self.assertTrue(policy.is_idle_cycle_reason("reply_completed_local_stop"))
        self.assertFalse(policy.is_idle_cycle_reason("tool_continue"))
        mod = host_loader.load("maisaka.turn_trigger.idle_backoff", ("maisaka.mode_policy",))
        timing = SimpleNamespace(no_action_backoff_base_seconds=10, no_action_backoff_cap_seconds=100,
                                 no_action_backoff_start_count=1)
        runtime = SimpleNamespace(chat_stream=SimpleNamespace(is_group_session=True), log_prefix="[t]")
        with mock.patch.object(mod, "global_config", SimpleNamespace(chat=SimpleNamespace(reply_timing=timing))):
            controller = mod.IdleBackoffController(runtime)
            for _ in range(3):
                controller.record_cycle_result("planner_no_tool_end")
            self.assertEqual(controller._count, 3)
            controller.record_cycle_result("reply_completed_local_stop")
            self.assertEqual(controller._count, 1)  # 原流程：reply 轮重置，stop 轮计 1


class TestFinalReplyParameter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = host_loader.load("maisaka.reply_termination")

    def definitions(self):
        return [{"type": "function", "function": {"name": "reply", "parameters": {"type": "object", "properties": {"msg_id": {}}}}},
                {"type": "function", "function": {"name": "wait", "parameters": {"type": "object", "properties": {}}}}]

    def test_parameter_is_opt_in_and_does_not_mutate_input(self):
        original = self.definitions()
        off = self.rt.add_final_reply_parameter(original, {"enabled": False, "mode": "explicit"})
        self.assertEqual(off, self.definitions())
        unknown_mode = self.rt.add_final_reply_parameter(original, {"enabled": True, "mode": "single_step"})
        self.assertEqual(unknown_mode, self.definitions())
        on = self.rt.add_final_reply_parameter(original, {"enabled": True, "mode": "explicit"})
        self.assertEqual(on[0]["function"]["parameters"]["properties"]["final_reply"]["type"], "boolean")
        self.assertNotIn("final_reply", on[1]["function"]["parameters"]["properties"])
        self.assertEqual(original, self.definitions())  # 原对象未被修改

    def test_unrecognised_shapes_are_left_alone(self):
        odd = [{"name": "reply"}, "text", {"function": {"name": "reply"}}]
        self.assertEqual(self.rt.add_final_reply_parameter(odd, {"enabled": True, "mode": "explicit"}), odd)

    def test_strip_removes_signal_including_wrapped_form(self):
        direct = SimpleNamespace(arguments={"msg_id": "m1", "final_reply": True})
        self.rt.strip_final_reply_argument(direct)
        self.assertEqual(direct.arguments, {"msg_id": "m1"})
        wrapped = SimpleNamespace(arguments={"arguments": json.dumps({"msg_id": "m1", "final_reply": True})})
        self.rt.strip_final_reply_argument(wrapped)
        self.assertEqual(json.loads(wrapped.arguments["arguments"]), {"msg_id": "m1"})
        self.rt.strip_final_reply_argument(SimpleNamespace())  # 没有 arguments 也不报错


if __name__ == "__main__":
    unittest.main()
