import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delegate.core import _Accum
from delegate.providers.base import Event, usage_tokens
from delegate.providers.grok import Grok
from delegate.providers.claude import Claude
from delegate.providers.codex import Codex
from delegate.providers.pi import Pi


class TestClaudeParser(unittest.TestCase):
    p = Claude()

    def test_tool_use(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/x/p.txt"}}]}})
        ev = self.p.parse(line)
        self.assertEqual(ev.kind, "tool_call")
        self.assertEqual(ev.name, "Write")
        self.assertEqual(ev.path, "/x/p.txt")

    def test_result(self):
        line = json.dumps({"type": "result", "subtype": "success",
                           "num_turns": 2, "total_cost_usd": 0.11})
        ev = self.p.parse(line)
        self.assertEqual(ev.kind, "end")
        self.assertEqual(ev.turns, 2)
        self.assertAlmostEqual(ev.cost_usd, 0.11)

    def test_later_end_event_replaces_turns_and_cost(self):
        acc = _Accum()
        acc.add(self.p.parse(json.dumps(
            {"type": "result", "num_turns": 2, "total_cost_usd": 0.11})))
        acc.add(self.p.parse(json.dumps(
            {"type": "result", "num_turns": 5, "total_cost_usd": 0.4})))
        self.assertEqual(acc.turns, 5)
        self.assertAlmostEqual(acc.cost_usd, 0.4)

    def test_tool_result_error(self):
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True}]}})
        ev = self.p.parse(line)
        self.assertEqual(ev.kind, "tool_result")
        self.assertIs(ev.ok, False)

    def test_non_json_ignored(self):
        self.assertIsNone(self.p.parse("not json"))

    def test_result_error_carries_message_and_http_status(self):
        line = json.dumps({
            "type": "result", "subtype": "success", "num_turns": 2,
            "total_cost_usd": 0.01, "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your session limit",
        })
        ev = self.p.parse(line)
        self.assertEqual(ev.kind, "error")
        self.assertEqual(ev.text, "You've hit your session limit (HTTP 429)")
        self.assertEqual(ev.turns, 2)
        self.assertAlmostEqual(ev.cost_usd, 0.01)

    def test_result_error_without_status_keeps_the_message(self):
        ev = self.p.parse(json.dumps({
            "type": "result", "is_error": True, "result": "boom"}))
        self.assertEqual((ev.kind, ev.text), ("error", "boom"))


class TestCodexParser(unittest.TestCase):
    p = Codex()

    def test_command_started_is_a_shell_tool_call(self):
        ev = self.p.parse(json.dumps({
            "type": "item.started",
            "item": {"id": "i-1", "type": "command_execution",
                     "command": "echo hi", "exit_code": None},
        }))
        self.assertEqual(ev.kind, "tool_call")
        self.assertEqual(ev.name, "shell")
        self.assertIsNone(ev.path)

    def test_file_change_completed_is_a_write(self):
        ev = self.p.parse(json.dumps({
            "type": "item.completed",
            "item": {"id": "i-2", "type": "file_change",
                     "changes": [{"path": "a.txt", "kind": "update"}]},
        }))
        self.assertEqual(ev.kind, "tool_call")
        self.assertEqual(ev.name, "write")
        self.assertEqual(ev.path, "a.txt")
        self.assertIs(ev.write, True)

    def test_failed_command_completed_is_a_failed_tool_result(self):
        ev = self.p.parse(json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "exit_code": 1},
        }))
        self.assertEqual(ev.kind, "tool_result")
        self.assertEqual(ev.name, "shell")
        self.assertIs(ev.ok, False)

    def test_successful_command_completed_is_silent(self):
        self.assertIsNone(self.p.parse(json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "exit_code": 0},
        })))

    def test_agent_message_is_text(self):
        ev = self.p.parse(json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }))
        self.assertEqual((ev.kind, ev.text), ("text", "done"))

    def test_turn_completed_counts_a_turn_and_usage(self):
        ev = self.p.parse(json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 3,
                      "output_tokens": 5, "reasoning_output_tokens": 2},
        }))
        self.assertEqual(ev.kind, "usage")
        self.assertEqual(ev.turns_delta, 1)
        self.assertEqual(ev.tokens, {"input": 10, "cache_read": 3,
                                     "output": 5, "reasoning": 2})
        acc = _Accum()
        acc.add(ev)
        self.assertEqual(acc.turns, 1)
        self.assertEqual(acc.usage["requests"], 1)
        self.assertEqual(acc.usage["input"], 10)

    def test_error_is_the_providers_message(self):
        ev = self.p.parse(json.dumps({
            "type": "error", "message": "You've hit your usage limit."}))
        self.assertEqual(ev.kind, "error")
        self.assertEqual(ev.text, "You've hit your usage limit.")

    def test_turn_failed_carries_the_nested_message_and_counts_the_turn(self):
        ev = self.p.parse(json.dumps({
            "type": "turn.failed",
            "error": {"message": "model is not supported"},
        }))
        self.assertEqual(ev.kind, "error")
        self.assertEqual(ev.text, "model is not supported")
        self.assertEqual(ev.turns_delta, 1)

    def test_unknown_and_non_json_lines_are_ignored(self):
        for line in (
            "", "   ", "not json", "{", "null", "[]",
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "item.updated", "item": {"type": "x"}}),
            json.dumps({"type": "item.completed", "item": "nope"}),
            json.dumps({"type": "turn.failed", "error": "nope"}),
        ):
            self.assertIsNone(self.p.parse(line), line)

    def test_accum_keeps_the_last_stream_error(self):
        acc = _Accum()
        acc.add(self.p.parse(json.dumps({"type": "error", "message": "first"})))
        acc.add(self.p.parse(json.dumps({
            "type": "turn.failed", "error": {"message": "last"}})))
        self.assertEqual(acc.error, "last")
        self.assertEqual(acc.turns, 1)


class TestGrokParser(unittest.TestCase):
    p = Grok()

    def test_tool_call(self):
        line = json.dumps({"type": "tool_call", "name": "search_replace",
                           "input": {"path": "a.py"}})
        ev = self.p.parse(line)
        self.assertEqual(ev.kind, "tool_call")
        self.assertEqual(ev.name, "search_replace")
        self.assertEqual(ev.path, "a.py")
        self.assertIs(ev.write, True)

    def test_thought_is_an_event_without_text(self):
        ev = self.p.parse(json.dumps({"type": "thought", "data": "reasoning chunk"}))
        self.assertEqual(ev.kind, "thought")
        self.assertIsNone(ev.text)
        self.assertEqual(ev.compact(), "")

    def test_usage_maps_token_fields(self):
        ev = self.p.parse(json.dumps({
            "type": "usage",
            "usage": {"input_tokens": 11, "output_tokens": 4,
                      "reasoning_tokens": 2, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 9},
        }))
        self.assertEqual(ev.kind, "usage")
        self.assertEqual(ev.tokens, {"input": 11, "output": 4,
                                     "reasoning": 2, "cache_read": 0})
        self.assertEqual(ev.compact(), "")
        self.assertIsNone(ev.cost_usd)
        acc = _Accum()
        acc.add(ev)
        acc.add(ev)
        self.assertIsNone(acc.cost_usd)
        self.assertIsNone(acc.turns)
        self.assertEqual(acc.usage["requests"], 2)
        self.assertEqual(acc.usage["input"], 22)

    def test_list_dir_uses_target_directory(self):
        ev = self.p.parse(json.dumps({
            "type": "tool_call", "toolName": "list_dir", "kind": "list",
            "rawInput": {"target_directory": "src"},
        }))
        self.assertEqual(ev.kind, "tool_call")
        self.assertEqual(ev.name, "list_dir")
        self.assertEqual(ev.path, "src")
        self.assertIs(ev.write, False)

    def test_read_file_raw_input_is_not_a_write(self):
        ev = self.p.parse(json.dumps({
            "type": "tool_call", "toolName": "read_file", "kind": "read",
            "rawInput": {"target_file": "src/a.py"},
        }))
        self.assertEqual(ev.kind, "tool_call")
        self.assertEqual(ev.name, "read_file")
        self.assertEqual(ev.path, "src/a.py")
        self.assertIs(ev.write, False)

    def test_write_tool_call_marks_write_and_file_path(self):
        ev = self.p.parse(json.dumps({
            "type": "tool_call", "toolName": "write", "kind": "edit",
            "rawInput": {"file_path": "src/b.py", "content": "x"},
        }))
        self.assertEqual(ev.kind, "tool_call")
        self.assertIs(ev.write, True)
        self.assertEqual(ev.path, "src/b.py")

    def test_edit_kind_and_delete_name_are_writes(self):
        edit = self.p.parse(json.dumps({
            "type": "tool_call", "toolName": "apply_patch", "kind": "edit",
            "rawInput": {"path": "src/c.py"},
        }))
        delete = self.p.parse(json.dumps({
            "type": "tool_call", "toolName": "delete_file",
            "rawInput": {"path": "src/old.py"},
        }))
        self.assertIs(edit.write, True)
        self.assertEqual(edit.path, "src/c.py")
        self.assertIs(delete.write, True)
        self.assertEqual(delete.path, "src/old.py")

    def test_end(self):
        line = json.dumps({"type": "result", "stopReason": "end_turn",
                           "num_turns": 3, "total_cost_usd": 0.2})
        ev = self.p.parse(line)
        self.assertEqual(ev.kind, "end")
        self.assertEqual(ev.stop_reason, "end_turn")
        self.assertEqual(ev.turns, 3)

    def test_failed_update(self):
        line = json.dumps({"type": "tool_call_update", "status": "failed", "name": "x"})
        ev = self.p.parse(line)
        self.assertEqual(ev.kind, "tool_result")
        self.assertIs(ev.ok, False)


def _pi_message_end(cost, *, input=1, output=1, reasoning=0, cache_read=0):
    """One assistant message_end. The repeated transcript must not become text."""
    return json.dumps({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "full transcript repeated"},
                {"type": "toolCall", "name": "write"},
            ],
            "usage": {
                "input": input,
                "output": output,
                "cacheRead": cache_read,
                "cacheWrite": 7,
                "reasoning": reasoning,
                "totalTokens": 99,
                "cost": {"input": 0.01, "output": 0.02, "cacheRead": 0,
                         "cacheWrite": 0, "total": cost},
            },
        },
    })


def _pi_turn_end():
    """turn_end repeats the assistant usage. That usage must not be counted."""
    return json.dumps({
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "usage": {
                "input": 1000, "output": 1000, "cacheRead": 1000, "reasoning": 1000,
                "cost": {"total": 9.5},
            },
        },
        "toolResults": [{"toolName": "write", "isError": True}],
    })


class TestPiParser(unittest.TestCase):
    def test_flags(self):
        pi = Pi()
        self.assertTrue(pi.parses_stream)
        self.assertTrue(pi.answers)

    def test_two_turns_sum_cost_and_count_a_failed_write(self):
        pi = Pi()
        lines = [
            "not json",
            "{",
            json.dumps({"type": "session", "id": "s"}),
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "turn_start"}),
            json.dumps({"type": "message_end", "message": {
                "role": "system",
                "usage": {"input": 50, "cost": {"total": 4}},
            }}),
            json.dumps({"type": "message_end", "message": {"role": "user"}}),
            json.dumps({"type": "message_end", "message": {"role": "toolResult"}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "thinking_start"}}),
            json.dumps({"type": "message_update",
                        "usage": {"input": 8, "output": 8, "reasoning": 8, "cacheRead": 8,
                                  "cost": {"total": 3}},
                        "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "scratch"}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "toolcall_delta", "delta": "{\"path\":"}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "toolcall_end",
                "toolCall": {"name": "write", "arguments": {
                    "path": "out.txt", "content": "x"}},
            }}),
            json.dumps({"type": "tool_execution_start", "toolName": "write"}),
            json.dumps({"type": "tool_execution_end", "toolName": "write",
                        "isError": True}),
            json.dumps({"type": "tool_execution_end", "toolName": "bash",
                        "isError": False}),
            _pi_message_end(0.25, input=10, output=4, reasoning=2, cache_read=1),
            _pi_turn_end(),
            json.dumps({"type": "agent_end", "willRetry": True}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "Final "}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_end", "content": "Final line."}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "line."}}),
            _pi_message_end(0.5, input=6, output=3, reasoning=1, cache_read=8),
            _pi_turn_end(),
            json.dumps({"type": "agent_end", "willRetry": False}),
            "null",
            "[]",
        ]
        acc = _Accum()
        for line in lines:
            ev = pi.parse(line)
            if ev is not None:
                acc.add(ev)
        self.assertEqual(acc.turns, 2)
        self.assertAlmostEqual(acc.cost_usd, 0.75)
        self.assertEqual(acc.failed_tool_calls, 1)
        self.assertEqual(acc.counts["write"], 1)
        self.assertEqual(acc.counts["tool_call"], 1)
        self.assertEqual(acc.counts["failed_tool"], 1)
        self.assertEqual(acc.usage, {"input": 16, "output": 7, "reasoning": 3,
                                     "cache_read": 9, "requests": 2})
        self.assertEqual(acc.answer(), "Final line.")
        self.assertEqual(acc.last_kind, "turn")

    def test_turn_end_is_a_turn_and_a_bare_end_is_silent(self):
        ev = Pi().parse(_pi_turn_end())
        self.assertEqual(ev.kind, "turn")
        self.assertEqual(ev.turns_delta, 1)
        self.assertIsNone(ev.cost_usd)
        self.assertEqual(ev.compact(), "")
        self.assertEqual(Event(kind="turn", turns_delta=1).compact(), "")
        self.assertEqual(Event(kind="end", turns_delta=1).compact(), "")
        self.assertEqual(
            Event(kind="end", turns=2, cost_usd=0.5, stop_reason="end_turn").compact(),
            "  ● end turns=2 cost=$0.5000 end_turn",
        )
        acc = _Accum()
        acc.add(ev)
        acc.add(Event(kind="other", turns_delta=2))
        self.assertEqual(acc.turns, 3)

    def test_tool_paths_and_write_flag(self):
        pi = Pi()
        edit = pi.parse(json.dumps({"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_end",
            "toolCall": {"name": "edit", "arguments": {"file_path": "src/b.py"}},
        }}))
        read = pi.parse(json.dumps({"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_end",
            "toolCall": {"name": "read", "arguments": {"path": "src/a.py"}},
        }}))
        bash = pi.parse(json.dumps({"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_end",
            "toolCall": {"name": "bash", "arguments": {"command": "ls"}},
        }}))
        encoded = pi.parse(json.dumps({"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_end",
            "toolCall": {"name": "edit", "arguments": "{\"file_path\": \"src/c.py\"}"},
        }}))
        self.assertEqual((edit.name, edit.path, edit.write), ("edit", "src/b.py", True))
        self.assertEqual((read.name, read.path, read.write), ("read", "src/a.py", False))
        self.assertEqual((bash.name, bash.path, bash.write), ("bash", None, False))
        self.assertEqual((encoded.name, encoded.path, encoded.write),
                         ("edit", "src/c.py", True))

    def test_odd_lines_do_not_raise(self):
        pi = Pi()
        for line in (
            "",
            "   ",
            "not json",
            "{",
            "null",
            "[]",
            "42",
            json.dumps({"type": "agent_end", "messages": [], "willRetry": True}),
            json.dumps({"type": "message_update"}),
            json.dumps({"type": "message_update", "assistantMessageEvent": "nope"}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "thinking_start"}}),
            json.dumps({"type": "message_end"}),
            json.dumps({"type": "message_end", "message": "nope"}),
            json.dumps({"type": "tool_execution_start", "toolName": "bash"}),
            json.dumps({"type": "auto_retry_end", "success": True}),
        ):
            self.assertIsNone(pi.parse(line), line)
        ev = pi.parse(json.dumps({"type": "message_update", "assistantMessageEvent": {
            "type": "toolcall_end",
            "toolCall": {"name": "write", "arguments": [1]},
        }}))
        self.assertEqual(ev.kind, "tool_call")
        self.assertIsNone(ev.path)
        self.assertIs(ev.write, True)


class TestCostAccumulation(unittest.TestCase):
    def test_usage_cost_sums_and_end_cost_replaces(self):
        acc = _Accum()
        acc.add(Event(kind="usage", cost_usd=0.25, tokens={"input": 1}))
        acc.add(Event(kind="usage", cost_usd=0.5))
        self.assertAlmostEqual(acc.cost_usd, 0.75)
        acc.add(Event(kind="end", turns_delta=1))
        acc.add(Event(kind="end", turns_delta=1))
        self.assertEqual(acc.turns, 2)
        self.assertAlmostEqual(acc.cost_usd, 0.75)
        acc.add(Event(kind="end", turns=9, cost_usd=1.0))
        self.assertEqual(acc.turns, 9)
        self.assertAlmostEqual(acc.cost_usd, 1.0)
        acc.add(Event(kind="end", turns_delta=1))
        self.assertEqual(acc.turns, 10)
        self.assertAlmostEqual(acc.cost_usd, 1.0)


class TestUsageTokens(unittest.TestCase):
    fields = (("in", "input"), ("out", "output"))

    def test_maps_ints_and_skips_bool_and_non_int(self):
        self.assertEqual(
            usage_tokens({"in": 4, "out": 2, "other": 9}, self.fields),
            {"input": 4, "output": 2},
        )
        self.assertEqual(usage_tokens({"in": True, "out": 3}, self.fields), {"output": 3})
        self.assertIsNone(usage_tokens({"in": 1.5, "out": "2"}, self.fields))
        self.assertIsNone(usage_tokens({}, self.fields))
        self.assertIsNone(usage_tokens(None, self.fields))


if __name__ == "__main__":
    unittest.main()
