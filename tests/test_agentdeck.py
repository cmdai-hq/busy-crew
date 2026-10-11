import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agentdeck.hook import hook_state, update_snapshot
from agentdeck.model import Session, layout
from agentdeck.observe import Observer, pid_matches, rollout_state
from agentdeck.render import CLAWD, CODEX, MINI_CLAWD, MINI_CODEX, Frame, PALETTE, compact, render
from agentdeck.storage import write_json
from agentdeck.usage import Usage, parse_usage


def session(provider, state="working", sid="a"):
    return Session(provider, sid, state, "1")


def claude_duo_crop(state):
    expected = Frame()
    expected.sprite(CLAWD, 2, 3, "c", dim=state == "idle")
    expected.text("1", 19, 3, scale=2)
    expected.mark(state, "claude", 27 if state == "idle" else 28, 3, sleeping=state == "idle")
    color = {"working": "c", "ready": "g", "idle": "d"}[state]
    expected.dot(17, 14, color)
    expected.dot(18, 14, color)
    return [row[:35] for row in expected.rows]


class RenderingTests(unittest.TestCase):
    def test_tiles_keep_lit_pixels_and_leave_unlit_ones_transparent(self):
        from PIL import Image
        from agentdeck.render import tiles
        source = Image.new("RGB", (72, 16), (0, 0, 0))
        source.putpixel((3, 2), (220, 134, 107))
        image = tiles(source)
        self.assertEqual((image.mode, image.size), ("RGBA", (1440, 320)))
        self.assertEqual(image.getpixel((3 * 20 + 10, 2 * 20 + 10)), (220, 134, 107, 255))
        self.assertEqual(image.getpixel((3 * 20, 2 * 20)), (0, 0, 0, 0))  # The gap between tiles.
        self.assertEqual(image.getpixel((10, 10)), (0, 0, 0, 0))  # An unlit LED.

    def test_light_tiles_ink_white_pixels_and_keep_colors(self):
        from PIL import Image
        from agentdeck.render import INK_ON_LIGHT, tiles
        source = Image.new("RGB", (72, 16), (0, 0, 0))
        source.putpixel((0, 0), (223, 233, 238))  # White text.
        source.putpixel((1, 0), (120, 149, 255))  # Codex blue.
        dark, light = tiles(source), tiles(source, ground="light")
        self.assertEqual(dark.getpixel((10, 10))[:3], (223, 233, 238))
        self.assertEqual(light.getpixel((10, 10))[:3], INK_ON_LIGHT)
        self.assertEqual(light.getpixel((30, 10))[:3], (120, 149, 255))

    def test_snapshot_tiles_and_leds_are_exclusive(self):
        from agentdeck.cli import main
        with patch("sys.argv", ["busy-crew", "snapshot", "out.png", "--tiles", "light", "--leds"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main()

    def test_dimmed_codex_keeps_a_visible_face(self):
        for sprite in [CODEX, MINI_CODEX]:
            frame = Frame()
            frame.sprite(sprite, 0, 0, dim=True)
            dimmed = {}
            for y, row in enumerate(sprite):
                for x, value in enumerate(row):
                    dimmed.setdefault(value, set()).add(frame.rows[y][x])
            # Each feature must contrast with what surrounds it: eyes on the visor, mouth on the body.
            self.assertTrue(dimmed["C"].isdisjoint(dimmed["v"]), sprite)
            self.assertTrue(dimmed["m"].isdisjoint(dimmed["B"]), sprite)
            self.assertTrue(set().union(*(c for v, c in dimmed.items() if v != "0")) <= {"d", "."})

    def test_codex_mouth_keeps_its_color_when_lit(self):
        self.assertEqual(PALETTE["m"], PALETTE["C"])

    def test_single_provider_uses_entire_team(self):
        for provider in ["claude", "codex"]:
            self.assertEqual(layout([session(provider)]), ("team", provider))
            frame = render([session(provider, sid=str(i)) for i in range(6)])
            self.assertTrue(any(v != "." for row in frame.rows for v in row[:9]))
            self.assertTrue(any(v != "." for row in frame.rows for v in row[62:]))

    def test_both_working_use_duo(self):
        self.assertEqual(layout([session("claude"), session("codex")]), ("duo", None))

    def test_open_idle_and_ready_provider_sessions_keep_duo_layout(self):
        for provider, other in [("claude", "codex"), ("codex", "claude")]:
            for state in ["ready", "idle"]:
                with self.subTest(provider=provider, state=state):
                    items = [session(provider, state), session(other)]
                    self.assertEqual(layout(items), ("duo", None))

    def test_idle_and_ready_claude_keep_full_icon_while_codex_works(self):
        for state in ["ready", "idle"]:
            with self.subTest(state=state):
                frame = render([session("claude", state), session("codex")])
                self.assertEqual([row[:35] for row in frame.rows], claude_duo_crop(state))

    def test_idle_claude_keeps_its_team_tile_alongside_working_session(self):
        items = [session("claude", "idle", "idle"), session("claude", sid="working")]
        self.assertEqual(layout(items), ("team", "claude"))
        expected = Frame()
        expected.sprite(MINI_CLAWD, 25, 7, "c")
        expected.mark("working", "claude", 28, 0)
        expected.dot(28, 15, "c")
        expected.sprite(MINI_CLAWD, 37, 7, "c", dim=True)
        expected.mark("idle", "claude", 40, 0)
        self.assertEqual(render(items).rows, expected.rows)

    def test_duo_count_includes_open_idle_sessions(self):
        items = [session("claude", "idle", "idle"), session("claude", sid="working"), session("codex")]
        expected = Frame()
        expected.sprite(CLAWD, 2, 3, "c")
        expected.text("2", 19, 3, scale=2)
        expected.mark("working", "claude", 28, 3)
        for x, color in [(17, "c"), (18, "c"), (21, "d"), (22, "d")]:
            expected.dot(x, 14, color)
        self.assertEqual([row[:35] for row in render(items).rows], [row[:35] for row in expected.rows])

    def test_waiting_overrides_token_view(self):
        items = [session("claude", "waiting")]
        self.assertEqual(render(items, "tokens", {"claude": 999}).rgb(), render(items).rgb())
        self.assertEqual(render(items, "month", {"claude": 999}).rgb(), render(items).rgb())

    def test_waiting_is_visible_before_overflow(self):
        items = [session("codex", sid=str(i)) for i in range(10)] + [session("codex", "waiting", "z")]
        frame = render(items)
        self.assertEqual(frame.rows[0][6], "y")
        self.assertTrue(any(v != "." for row in frame.rows for v in row[60:]))

    def test_team_overflow_counts_idle_sessions_and_keeps_waiting_first(self):
        items = [session("claude", "idle", str(i)) for i in range(10)] + [session("claude", "waiting", "z")]
        expected = Frame()
        for x, state in [(2, "waiting"), (14, "idle"), (26, "idle"), (38, "idle"), (50, "idle")]:
            expected.sprite(MINI_CLAWD, x, 7, "c", dim=state == "idle")
            expected.mark(state, "claude", x + 3, 0)
        expected.text("+", 62, 2)
        expected.text("6", 60, 9)
        self.assertEqual(render(items).rows, expected.rows)

    def test_xpm_always_native_size_and_legal_palette(self):
        for count in [0, 1, 6, 7, 100]:
            frame = render([session("claude", sid=str(i)) for i in range(count)] + [session("codex")])
            xpm = frame.xpm().splitlines()
            width, height, colors, cpp = map(int, xpm[1].split())
            self.assertEqual((width, height, cpp), (72, 16, 1))
            self.assertLessEqual(colors, 32)
            self.assertEqual(len(xpm[2+colors:]), 16)
            self.assertTrue(all(len(row) == 72 for row in xpm[2+colors:]))
            self.assertEqual(len(frame.rgb()), 3456)

    def test_token_views_name_their_period(self):
        today, month = render([], "tokens", {"claude": 1}), render([], "month", {"claude": 1})
        self.assertNotEqual(today.rgb(), month.rgb())
        self.assertEqual(today.rows[:10], month.rows[:10])  # Same totals, only the label differs.
        self.assertTrue(any(v != "." for v in month.rows[13]))

    def test_tokens_unknown_and_compact_numbers(self):
        self.assertEqual(compact(None), "?")
        self.assertEqual(compact(14_375_421), "14.4M")
        self.assertEqual(compact(0), "0")


class HookTests(unittest.TestCase):
    def test_permission_and_question_are_waiting(self):
        self.assertEqual(hook_state({"hook_event_name": "PermissionRequest"}), "waiting")
        self.assertEqual(hook_state({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}), "waiting")

    def test_subagent_does_not_change_parent_state(self):
        self.assertIsNone(hook_state({"hook_event_name": "Stop", "agent_id": "sub-1"}))
        self.assertIsNone(hook_state({"hook_event_name": "PreToolUse", "transcript_path": "/a/subagents/b.jsonl"}))

    def test_non_attention_notification_is_ignored(self):
        self.assertIsNone(hook_state({"hook_event_name": "Notification", "notification_type": "idle_prompt"}))
        self.assertEqual(hook_state({"hook_event_name": "Notification", "notification_type": "permission_prompt"}), "waiting")

    def test_failure_is_not_green_completion(self):
        self.assertEqual(hook_state({"hook_event_name": "StopFailure"}), "unknown")
        self.assertEqual(hook_state({"hook_event_name": "PostToolUseFailure"}), "working")


def event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


class CodexEventsTests(unittest.TestCase):
    def test_other_turn_completion_does_not_finish_current(self):
        records = [event("task_started", turn_id="old"), event("task_complete", turn_id="old"),
                   event("task_started", turn_id="new")]
        self.assertEqual(rollout_state(records, "new"), (False, False))

    def test_async_question_does_not_block(self):
        records = [event("task_started", turn_id="new"), {"type": "response_item", "payload": {
            "type": "function_call", "name": "request_user_input_async", "call_id": "1"}}]
        self.assertEqual(rollout_state(records, "new"), (False, False))

    def test_synchronous_question_clears_when_answered(self):
        records = [event("task_started", turn_id="new"), {"type": "response_item", "payload": {
            "type": "function_call", "name": "request_user_input", "call_id": "1"}}]
        self.assertEqual(rollout_state(records, "new"), (False, True))
        records.append({"type": "response_item", "payload": {"type": "function_call_output", "call_id": "1"}})
        self.assertEqual(rollout_state(records, "new"), (False, False))


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.runtime = self.home / "runtime"
        self.runtime.mkdir()
        self.now = 10000
        self.observer = Observer(self.runtime, self.home, clock=lambda: self.now, alive=lambda *_: True,
                                 claude_root=self.home / ".claude", codex_root=self.home / ".codex")

    def tearDown(self):
        self.temp.cleanup()

    def test_old_completions_do_not_count_and_new_ones_can_be_acknowledged(self):
        self.assertEqual(self.observer.completion("codex", "old", "ready", "turn", 9000).state, "idle")
        self.observer.completion("codex", "live", "working", "turn")
        result = self.observer.completion("codex", "live", "ready", "turn", 10001)
        self.assertEqual(result.state, "ready")
        self.observer.acknowledge([result])
        self.assertEqual(self.observer.completion("codex", "live", "ready", "turn", 10001).state, "idle")

    def test_dead_claude_process_removed(self):
        write_json(self.home / ".claude/sessions/a.json", {
            "sessionId": "a", "pid": 123, "status": "busy", "statusUpdatedAt": 10000000})
        self.observer.alive = lambda *_: False
        self.assertEqual(self.observer.claude(), [])

    def test_claude_icon_survives_completion_and_acknowledgement_until_process_closes(self):
        native_path = self.home / ".claude/sessions/a.json"
        hook_path = self.runtime / "hooks/a.json"
        hook = update_snapshot({}, {"session_id": "a", "hook_event_name": "UserPromptSubmit"}, self.now)
        write_json(hook_path, hook)
        codex = session("codex")
        write_json(native_path, {
            "sessionId": "a", "pid": 123, "status": "busy", "statusUpdatedAt": self.now * 1000})
        with patch.object(self.observer, "codex", return_value=[codex]):
            items = self.observer.poll()
            self.assertEqual([(s.provider, s.state) for s in items], [("claude", "working"), ("codex", "working")])
            self.assertEqual([row[:35] for row in render(items).rows], claude_duo_crop("working"))

            self.now += 1
            write_json(native_path, {
                "sessionId": "a", "pid": 123, "status": "idle", "statusUpdatedAt": self.now * 1000})
            hook = update_snapshot(hook, {"session_id": "a", "hook_event_name": "Stop"}, self.now)
            write_json(hook_path, hook)
            items = self.observer.poll()
            self.assertEqual([(s.provider, s.state) for s in items], [("claude", "ready"), ("codex", "working")])
            self.assertEqual(layout(items), ("duo", None))
            self.assertEqual([row[:35] for row in render(items).rows], claude_duo_crop("ready"))

            self.observer.acknowledge(items)
            self.now += 1
            items = self.observer.poll()
            self.assertEqual([(s.provider, s.state) for s in items], [("claude", "idle"), ("codex", "working")])
            self.assertEqual(layout(items), ("duo", None))
            self.assertEqual([row[:35] for row in render(items).rows], claude_duo_crop("idle"))

            self.observer.alive = lambda *_: False
            items = self.observer.poll()
            self.assertEqual(items, [codex])
            self.assertEqual(layout(items), ("team", "codex"))

    def test_waiting_hook_then_post_tool_resume(self):
        write_json(self.home / ".claude/sessions/a.json", {
            "sessionId": "a", "pid": 123, "status": "busy", "statusUpdatedAt": 9999000})
        path = self.runtime / "hooks/a.json"
        write_json(path, {"id": "a", "state": "waiting", "at": 10000})
        self.assertEqual(self.observer.claude()[0].state, "waiting")
        write_json(path, {"id": "a", "state": "working", "at": 10001})
        self.assertEqual(self.observer.claude()[0].state, "working")

    def test_latest_root_turn_only_and_dangling_turn_excluded(self):
        root = self.home / ".codex"; root.mkdir()
        with closing(sqlite3.connect(root / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id,rollout_path,updated_at,archived,agent_path,thread_source)")
            for sid, freshness, agent_path in [("live", 10000, None), ("stale", 8000, None), ("sub", 10000, "sub")]:
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (sid, str(root/sid), freshness, 0, agent_path, "user"))
                turn = {"live": "current", "stale": "dangling", "sub": "subturn"}[sid]
                (root / sid).write_text(json.dumps(event("task_started", turn_id=turn)) + "\n")
                os.utime(root / sid, (freshness, freshness))
        with closing(sqlite3.connect(root / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,status,completed_at)")
            db.executemany("INSERT INTO thread_turns VALUES(?,?,?,?,?)", [
                ("live", "ancient", 1, "inProgress", None), ("live", "current", 2, "inProgress", None),
                ("stale", "dangling", 1, "inProgress", None), ("sub", "subturn", 1, "inProgress", None)])
        sessions = self.observer.codex()
        self.assertEqual([(s.id, s.version) for s in sessions], [("live", "current")])

    def test_source_failure_is_unknown_not_zero(self):
        with patch.object(self.observer, "codex", side_effect=sqlite3.DatabaseError()):
            self.assertEqual(self.observer.poll(), [])
            self.assertEqual(self.observer.errors, ["codex"])

    def test_invalid_pid_never_counts(self):
        self.assertFalse(pid_matches(-1))
        self.assertFalse(pid_matches(0))


class UsageTests(unittest.TestCase):
    def test_only_today_and_actual_agent_totals(self):
        data = {"daily": [{"period": "2026-10-04", "agents": [
            {"agent": "claude", "totalTokens": 13956872}, {"agent": "codex", "totalTokens": 418549}]},
            {"period": "2026-10-03", "agents": [{"agent": "claude", "totalTokens": 999}]}]}
        self.assertEqual(parse_usage(data, "2026-10-04"), {"claude": 13956872, "codex": 418549})

    def test_refresh_failure_removes_stale_totals(self):
        usage = Usage(command="/missing/ccusage")
        usage.values = {"claude": 999}
        usage.refresh()
        self.assertEqual(usage.current(), {})
        self.assertIsNotNone(usage.error)

    def test_midnight_invalidates_yesterday(self):
        usage = Usage(); usage.values = {"claude": 999}; usage.day = "yesterday"
        self.assertEqual(usage.current(), {})


if __name__ == "__main__":
    unittest.main()
