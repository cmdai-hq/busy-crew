"""Controlled lifecycle fixtures: never observe real Claude/Codex data or processes."""
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agentdeck import hook, observe
from agentdeck.model import layout
from agentdeck.observe import Observer, pid_matches, read_turn, rollout_state, readonly_db
from agentdeck.storage import load_json, write_json
from agentdeck.service import Service


def event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def function(kind, **payload):
    return {"type": "response_item", "payload": {"type": kind, **payload}}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.claude = self.root / "custom-claude"
        self.codex = self.root / "custom-codex"
        self.now = 200000.0
        self.snapshot = {}
        self.observer = self.new_observer()

    def tearDown(self):
        self.temp.cleanup()

    def new_observer(self):
        return Observer(self.runtime, self.root, clock=lambda: self.now, alive=lambda *_: True,
                        claude_root=self.claude, codex_root=self.codex)

    def native(self, state="busy", sid="a", pid=123, born=None):
        data = {"sessionId": sid, "pid": pid, "status": state, "statusUpdatedAt": self.now * 1000}
        if born is not None:
            data["procStart"] = datetime.fromtimestamp(born, timezone.utc).strftime("%a %b %d %H:%M:%S %Y")
        write_json(self.claude / "sessions" / (sid + ".json"), data)

    def emit(self, kind, at=None, **extra):
        self.snapshot = hook.update_snapshot(self.snapshot, {"session_id": "a", "hook_event_name": kind, **extra}, self.now if at is None else at)
        write_json(self.runtime / "hooks" / "a.json", self.snapshot)
        return self.snapshot

    def codex_fixture(self, records, status="inProgress", completed=None, turn="turn"):
        self.codex.mkdir(exist_ok=True)
        self.rollout = self.codex / "rollout.jsonl"
        self.rollout.write_text("".join(json.dumps(row) + "\n" for row in records))
        os.utime(self.rollout, (self.now, self.now))
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id,rollout_path,updated_at,archived,agent_path,thread_source)")
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", ("c", str(self.rollout), self.now, 0, None, "user"))
            db.commit()
        with closing(sqlite3.connect(self.codex / "thread_history_1.sqlite")) as db:
            db.execute("CREATE TABLE thread_turns(thread_id,turn_id,rollout_ordinal,status,completed_at)")
            db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?)", ("c", turn, 1, status, completed))
            db.commit()

    def test_busy_to_idle_without_stop_remains_idle(self):
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "working")
        self.now += 1
        self.native("idle")
        self.assertEqual(self.observer.poll()[0].state, "idle")
        self.assertFalse(self.observer.results)

    def test_stop_does_not_finish_native_busy_continuation(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_fast_stop_survives_newer_native_idle_and_restart(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.now += 1
        self.native("idle")
        result = self.observer.poll()[0]
        self.assertEqual(result.state, "ready")
        self.now += 1
        restarted = self.new_observer()
        self.assertEqual(restarted.poll(), [result])
        restarted.acknowledge([result])
        self.now += 1
        self.emit("Stop")  # Same result, delayed duplicate delivery.
        self.native("idle")
        self.assertEqual(restarted.poll()[0].state, "idle")

    def test_ack_then_actual_continuation_has_new_result_identity(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        first = self.observer.poll()[0]
        self.observer.acknowledge([first])
        self.now += 1
        self.emit("PreToolUse", tool_use_id="continued", tool_name="Bash")
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "working")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        second = self.observer.poll()[0]
        self.assertEqual(second.state, "ready")
        self.assertNotEqual(second.version, first.version)
        self.observer.acknowledge([first])  # Old presentation may not ack this result.
        self.assertEqual(self.observer.poll()[0], second)

    def test_stop_failure_is_unknown(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("StopFailure")
        self.native("idle")
        self.assertEqual(self.observer.poll()[0].state, "unknown")

    def test_parallel_unrelated_tool_result_preserves_question(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("PreToolUse", tool_name="AskUserQuestion", tool_use_id="question")
        self.now += 1
        self.emit("PostToolUse", tool_name="Bash", tool_use_id="other")
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        self.now += 1
        self.emit("PostToolUse", tool_name="AskUserQuestion", tool_use_id="question")
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "working")
        # The earlier PreToolUse callback finishes writing after its result.
        self.emit("PreToolUse", at=self.now - 2, tool_name="AskUserQuestion", tool_use_id="question")
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_stale_stop_cannot_end_new_activity_or_new_turn(self):
        self.emit("UserPromptSubmit")
        started = self.now
        self.now += 2
        self.emit("PreToolUse", tool_name="Bash", tool_use_id="work")
        self.emit("Stop", at=started + 1)
        self.assertNotIn("terminal", self.snapshot)
        self.now += 1
        self.emit("UserPromptSubmit")
        latest = self.snapshot["turn"]
        self.emit("Stop", at=started + 2)
        self.assertEqual(self.snapshot["turn"], latest)
        self.assertNotIn("terminal", self.snapshot)

    def test_duplicate_completed_tool_events_do_not_replay_acknowledged_stop(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("PreToolUse", tool_name="Bash", tool_use_id="old")
        self.now += 1
        self.emit("PostToolUse", tool_name="Bash", tool_use_id="old")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        first = self.observer.poll()[0]
        self.observer.acknowledge([first])
        for kind in ["PreToolUse", "PostToolUse"]:
            self.now += 1
            self.emit(kind, tool_name="Bash", tool_use_id="old")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        self.assertEqual(self.observer.poll()[0].state, "idle")
        self.assertEqual(self.snapshot["terminal"]["version"], first.version)

    def test_official_permission_without_tool_id_matches_pretool_fingerprint(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        tool = {"tool_name": "Bash", "tool_input": {"command": "private command"}}
        self.emit("PreToolUse", tool_use_id="permitted", **tool)
        self.now += 1
        self.emit("PermissionRequest", **tool)  # Official PermissionRequest has no tool_use_id.
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        self.assertNotIn("private command", json.dumps(self.snapshot))
        self.now += 1
        self.emit("PostToolUse", tool_use_id="permitted", **tool)
        # Native status clock may remain unchanged when the permission is accepted.
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_permission_notification_is_resolved_with_its_tool_call(self):
        tool = {"tool_name": "Bash", "tool_input": {"command": "private command"}}
        for order in [("PermissionRequest", "Notification"), ("Notification", "PermissionRequest"), ("Notification",)]:
            with self.subTest(order=order):
                call = "-".join(order)
                self.now += 10
                self.emit("UserPromptSubmit")
                self.now += 1
                self.emit("PreToolUse", tool_use_id=call, **tool)
                self.now += 1
                for kind in order:
                    self.emit(kind, **(tool if kind == "PermissionRequest" else {"notification_type": "permission_prompt"}))
                self.native()
                # A notification explained by an identified request is not ambiguous.
                self.assertEqual(self.observer.poll()[0].state, "waiting" if len(order) == 2 else "unknown")
                self.now += 1
                self.emit("PostToolUse", tool_use_id=call, **tool)
                self.assertEqual(self.observer.poll()[0].state, "working")
                self.assertFalse(self.snapshot["pending"])

    def test_ambiguous_permission_older_than_native_update_does_not_override_it(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Notification", notification_type="permission_prompt")
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "unknown")
        self.now += 1
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_identical_parallel_permissions_are_unknown_until_all_calls_resolve(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        tool = {"tool_name": "Bash", "tool_input": {"command": "same"}}
        self.emit("PreToolUse", tool_use_id="first", **tool)
        self.emit("PreToolUse", tool_use_id="second", **tool)
        self.now += 1
        self.emit("PermissionRequest", **tool)
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "unknown")
        self.now += 1
        self.emit("PostToolUse", tool_use_id="first", **tool)
        self.assertEqual(self.observer.poll()[0].state, "unknown")
        self.now += 1
        self.emit("PostToolUse", tool_use_id="second", **tool)
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_permission_before_pretool_write_recovers_unique_correlation(self):
        self.emit("UserPromptSubmit")
        self.now += 2
        tool = {"tool_name": "Bash", "tool_input": {"command": "same"}}
        self.emit("PermissionRequest", **tool)
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "unknown")
        self.emit("PreToolUse", at=self.now - 1, tool_use_id="first", **tool)
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        self.now += 1
        self.emit("PostToolUse", tool_use_id="first", **tool)
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_delayed_permission_write_after_matching_result_does_not_reopen_wait(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        tool = {"tool_name": "Bash", "tool_input": {"command": "same"}}
        self.emit("PreToolUse", tool_use_id="first", **tool)
        self.now += 2
        self.emit("PostToolUse", tool_use_id="first", **tool)
        self.emit("PermissionRequest", at=self.now - 1, **tool)
        self.native()
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_new_native_process_does_not_reuse_previous_stop(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle", born=self.now - 10)
        self.assertEqual(self.observer.poll()[0].state, "ready")
        self.now += 2
        self.native("idle", pid=456, born=self.now)
        self.assertEqual(self.observer.poll()[0].state, "idle")
        self.assertFalse(self.observer.results)

    def test_dead_or_removed_session_clears_results_and_ack(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        first = self.observer.poll()[0]
        self.observer.acknowledge([first])
        (self.claude / "sessions/a.json").unlink()
        self.assertEqual(self.observer.poll(), [])
        self.assertFalse(self.observer.acks)
        self.assertFalse(self.observer.previous)

    def test_invalid_native_records_isolate_valid_provider_and_recover(self):
        self.codex_fixture([event("task_started", turn_id="turn")])
        bad = self.claude / "sessions/bad.json"
        bad.parent.mkdir(parents=True)
        for raw in ("[]", "null", "{", '{"sessionId":"bad","pid":true}', '{"sessionId":"bad","pid":123,"statusUpdatedAt":false}'):
            bad.write_text(raw)
            self.assertEqual([(s.provider, s.state) for s in self.observer.poll()], [("codex", "working")])
            self.assertEqual(self.observer.errors, ["claude"])
        bad.unlink()
        self.native()
        self.assertEqual({s.provider for s in self.observer.poll()}, {"claude", "codex"})
        self.assertFalse(self.observer.errors)

    def test_removed_file_between_listing_and_reading_is_not_a_crash(self):
        self.native()
        native = self.claude / "sessions/a.json"
        original = load_json

        def vanish(path, *args, **kwargs):
            if path == native:
                native.unlink()
            return original(path, *args, **kwargs)

        with patch("agentdeck.observe.load_json", side_effect=vanish):
            self.assertEqual(self.observer.poll(), [])
        self.assertFalse(self.observer.errors)

    def test_corrupt_pending_ledger_and_hook_are_explicit_and_repairable(self):
        write_json(self.runtime / "completions.json", {"claude:a": {"version": "turn", "completed_at": [], "instance": None}})
        observer = self.new_observer()
        self.native()
        self.assertEqual(observer.poll()[0].state, "working")
        self.assertIn("state", observer.errors)
        self.emit("UserPromptSubmit")
        broken = {**self.snapshot, "pending": [], "turn_started_at": []}
        write_json(self.runtime / "hooks/a.json", broken)
        self.assertEqual(self.observer.poll()[0].state, "working")
        self.assertIn("claude", self.observer.errors)
        repaired = hook.update_snapshot(broken, {"session_id": "a", "hook_event_name": "UserPromptSubmit"}, self.now)
        self.assertTrue(hook.valid_snapshot(repaired))
        write_json(self.runtime / "hooks/a.json", repaired)
        self.observer.poll()
        self.assertFalse(self.observer.errors)

    def test_state_error_clears_once_each_corrupt_ledger_is_rewritten(self):
        (self.runtime).mkdir(parents=True, exist_ok=True)
        (self.runtime / "completions.json").write_text("{broken")
        (self.runtime / "acknowledged.json").write_text("{broken")
        self.observer = self.new_observer()
        self.native("idle")
        self.observer.poll()
        self.assertEqual(self.observer.errors, ["state"])
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        result = self.observer.poll()[0]
        self.assertEqual(result.state, "ready")
        self.assertEqual(self.observer.poll()[0], result)
        self.assertEqual(self.observer.errors, ["state"])  # acknowledged.json is still corrupt.
        self.observer.acknowledge([result])
        self.assertEqual(self.observer.poll()[0].state, "idle")
        self.assertFalse(self.observer.errors)

    def test_malformed_legacy_nested_fields_are_not_interpreted(self):
        self.native("idle")
        write_json(self.runtime / "hooks/a.json", {"id": "a", "state": "ready", "at": self.now,
                                                    "pending": [], "terminal": [], "turn": [], "turn_started_at": []})
        self.assertEqual(self.observer.poll()[0].state, "ready")
        self.assertFalse(self.observer.errors)

    def test_ack_write_failure_leaves_result_available_for_retry(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        result = self.observer.poll()[0]
        with patch("agentdeck.observe.write_json", side_effect=OSError("fixture denied")):
            with self.assertRaises(OSError):
                self.observer.acknowledge([result])
        self.assertFalse(self.observer.acks)
        self.assertEqual(self.observer.poll()[0], result)
        self.observer.acknowledge([result])
        self.assertEqual(self.observer.poll()[0].state, "idle")

    def service_with_presentation(self, sessions):
        service = Service.__new__(Service)
        service.observer = self.observer
        service.presented_sessions = sessions
        service.presented_until = float("inf")
        service.paused = False
        return service

    def test_service_ack_real_filesystem_failure_keeps_presentation_and_retry(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        result = self.observer.poll()[0]
        service = self.service_with_presentation([result])
        blocked = self.runtime / "acknowledged.json"
        blocked.mkdir()  # Atomic file replacement fails on an existing directory.
        with self.assertLogs("agentdeck", level="WARNING"):
            service.command("ack")
        self.assertEqual(self.observer.poll()[0], result)
        self.assertFalse(self.observer.acks)
        blocked.rmdir()
        service.command("ack")
        self.assertEqual(self.observer.poll()[0].state, "idle")

    def test_partial_ack_cleanup_failure_cannot_replay_after_hook_expiry_restart(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        result = self.observer.poll()[0]
        service = self.service_with_presentation([result])

        def fail_cleanup(path, data):
            if path.name == "completions.json":
                raise OSError("fixture cleanup failure")
            return write_json(path, data)

        with patch("agentdeck.observe.write_json", side_effect=fail_cleanup), self.assertLogs("agentdeck", level="WARNING"):
            service.command("ack")
        self.assertEqual(load_json(self.runtime / "acknowledged.json")["claude:a"], result.version)
        self.assertIn("claude:a", load_json(self.runtime / "completions.json"))
        self.now += 86401  # The hook no longer supplies a terminal state.
        restarted = self.new_observer()
        self.assertEqual(restarted.poll()[0].state, "idle")
        self.assertFalse(restarted.results)

    def test_completion_write_failure_is_retried_before_restart(self):
        self.emit("UserPromptSubmit")
        self.now += 1
        self.emit("Stop")
        self.native("idle")
        with patch("agentdeck.observe.write_json", side_effect=OSError("fixture denied")):
            self.assertEqual(self.observer.poll(), [])
        self.assertTrue(self.observer.results_dirty)
        result = self.observer.poll()[0]
        self.assertFalse(self.observer.results_dirty)
        self.now += 1
        self.assertEqual(self.new_observer().poll(), [result])

    def test_extreme_number_and_deep_rollout_nesting_remain_provider_errors(self):
        bad = self.claude / "sessions/bad.json"
        bad.parent.mkdir(parents=True)
        bad.write_text('{"sessionId":"bad","pid":123,"statusUpdatedAt":' + '1' * 1000 + '}')
        self.assertEqual(self.observer.poll(), [])
        self.assertEqual(self.observer.errors, ["claude"])
        bad.unlink()
        self.codex_fixture([event("task_started", turn_id="turn")])
        with self.rollout.open("a") as stream:
            stream.write('{"payload":' + '[' * 1100 + '0' + ']' * 1100 + '}\n')
        self.assertEqual(self.observer.poll(), [])
        self.assertEqual(self.observer.errors, ["codex"])

    def test_missing_previously_available_sources_do_not_fabricate_sessions(self):
        self.native()
        self.observer.poll()
        (self.claude / "sessions/a.json").unlink()
        (self.claude / "sessions").rmdir()
        self.assertEqual(self.observer.poll(), [])
        self.assertEqual(self.observer.errors, ["claude"])
        self.assertEqual(layout([], self.observer.errors), ("duo", None))

    def test_hook_runtime_argument_is_independent_of_parent_environment(self):
        supplied = {"session_id": "a", "hook_event_name": "UserPromptSubmit", "prompt": "must not persist"}
        with patch.object(hook.sys, "stdin", type("Input", (), {"buffer": io.BytesIO(json.dumps(supplied).encode())})()), \
             patch.dict(os.environ, {"AGENT_DECK_STATE": str(self.root / "wrong-runtime")}), \
             patch.object(hook.time, "time", return_value=self.now):
            hook.main(["--runtime", str(self.runtime)])
        files = list((self.runtime / "hooks").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("must not persist", files[0].read_text())
        self.assertFalse((self.root / "wrong-runtime").exists())
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_custom_environment_roots_supported_without_default_sources(self):
        self.native()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)}):
            observer = Observer(self.runtime, self.root, clock=lambda: self.now, alive=lambda *_: True)
            self.assertEqual(observer.poll()[0].provider, "claude")
        self.assertFalse((self.root / ".claude").exists())
        self.assertFalse((self.root / ".codex").exists())

    def test_codex_terminal_error_and_abort_do_not_turn_green(self):
        for terminal, extra in [("task_complete", {"error": {"message": "failed"}}), ("turn_aborted", {})]:
            with self.subTest(terminal=terminal):
                records = [event("task_started", turn_id="turn"), event(terminal, turn_id="turn", **extra)]
                self.assertEqual(rollout_state(records, "turn"), (False, False))
        self.codex_fixture([event("task_started", turn_id="turn"), event("task_complete", turn_id="turn", error={"message": "failed"})], "completed", self.now)
        self.assertEqual(self.observer.poll()[0].state, "unknown")

    def test_large_rollout_keeps_question_then_resolves_matching_answer(self):
        records = [event("task_started", turn_id="turn")]
        records += [function("function_call_output", call_id=str(i), output="x" * 16000) for i in range(160)]
        records.append(function("function_call", name="request_user_input", call_id="question"))
        self.codex_fixture(records)
        self.assertGreater(self.rollout.stat().st_size, 2_000_000)
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        with self.rollout.open("a") as stream:
            stream.write(json.dumps(function("function_call_output", call_id="unrelated")) + "\n")
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        with self.rollout.open("a") as stream:
            stream.write(json.dumps(function("function_call_output", call_id="question")) + "\n")
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_missing_boundary_or_corrupt_rollout_is_unknown_count_not_working(self):
        self.codex_fixture([function("function_call", name="request_user_input", call_id="q")])
        self.assertEqual(self.observer.poll(), [])
        self.assertEqual(self.observer.errors, ["codex"])
        self.rollout.write_text(json.dumps(event("task_started", turn_id="turn")) + "\n" + '{"type":"event_msg","payload":null}\n')
        self.assertEqual(self.observer.poll(), [])
        self.assertEqual(self.observer.errors, ["codex"])
        self.rollout.write_text(json.dumps(event("task_started", turn_id="turn")) + "\n")
        self.assertEqual(self.observer.poll()[0].state, "working")
        self.assertFalse(self.observer.errors)

    def test_record_still_being_appended_is_not_a_provider_error(self):
        self.codex_fixture([event("task_started", turn_id="turn")])
        with self.rollout.open("a") as stream:
            stream.write('{"type":"event_msg","payload":{"type":"task_comp')
        self.assertEqual(self.observer.poll()[0].state, "working")
        self.assertFalse(self.observer.errors)
        with self.rollout.open("a") as stream:
            stream.write('lete","turn_id":"turn"}}\n')
        os.utime(self.rollout, (self.now, self.now + 1))
        self.assertEqual(self.observer.poll()[0].state, "ready")

    def test_codex_home_without_desktop_databases_is_not_a_source_error(self):
        self.codex.mkdir()  # For example, created by the Codex CLI alone.
        observer = self.new_observer()
        self.native()
        self.assertEqual([s.provider for s in observer.poll()], ["claude"])
        self.assertFalse(observer.errors)

    def oversized_line(self, kind, payload_type, length, **payload):
        """One JSON record of exactly `length` bytes, padded inside its payload."""
        record = {"timestamp": "2026-10-07T01:47:19.940Z", "type": kind,
                  "payload": {"type": payload_type, **payload, "output": ""}}
        base = len(json.dumps(record))
        record["payload"]["output"] = "x" * (length - base)
        line = json.dumps(record)
        self.assertEqual(len(line), length)
        return line

    def test_oversized_tool_output_is_left_out_of_the_turn(self):
        big = self.oversized_line("response_item", "custom_tool_call_output", 3_430_000, call_id="c")
        self.codex_fixture([event("task_started", turn_id="turn")], "completed", self.now)
        with self.rollout.open("a") as stream:
            stream.write(big + "\n" + json.dumps(event("task_complete", turn_id="turn")) + "\n")
        self.assertEqual(self.observer.poll()[0].state, "ready")
        self.assertFalse(self.observer.errors)

    def test_oversized_record_split_across_read_chunks_is_still_recognized(self):
        complete = json.dumps(event("task_complete", turn_id="turn"))
        # The newline before the big record lands 3 bytes before a 64 KiB read boundary.
        big = self.oversized_line("response_item", "function_call_output", 65536 * 40 - len(complete), call_id="c")
        self.codex_fixture([event("task_started", turn_id="turn")], "completed", self.now)
        with self.rollout.open("a") as stream:
            stream.write(big + "\n" + complete + "\n")
        size = self.rollout.stat().st_size
        newline_before_big = size - len(complete) - 1 - len(big) - 1 - 1
        self.assertEqual((size - newline_before_big) % 65536, 3)
        self.assertEqual(self.observer.poll()[0].state, "ready")

    def test_oversized_turn_event_or_function_call_still_fails_the_read(self):
        for kind, payload_type in [("event_msg", "task_complete"), ("response_item", "function_call")]:
            with self.subTest(payload_type=payload_type):
                big = self.oversized_line(kind, payload_type, 2_100_000, turn_id="turn", call_id="c")
                path = self.root / ("rollout-" + payload_type)
                path.write_text(json.dumps(event("task_started", turn_id="turn")) + "\n" + big + "\n")
                with self.assertRaisesRegex(ValueError, "exceeds size limit"):
                    read_turn(path, "turn")

    def test_nested_header_cannot_make_an_oversized_turn_event_skippable(self):
        record = {"metadata": {"type": "response_item", "payload": {"type": "message"}},
                  "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn", "output": "x" * 2_100_000}}
        path = self.root / "nested"
        path.write_text(json.dumps(event("task_started", turn_id="turn")) + "\n" + json.dumps(record) + "\n")
        with self.assertRaisesRegex(ValueError, "exceeds size limit"):
            read_turn(path, "turn")

    def test_oversized_answer_still_clears_the_pending_question(self):
        answer = self.oversized_line("response_item", "function_call_output", 2_100_000, call_id="question")
        self.codex_fixture([event("task_started", turn_id="turn"),
                            function("function_call", name="request_user_input", call_id="question")])
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        with self.rollout.open("a") as stream:
            stream.write(answer + "\n")
        self.assertEqual(self.observer.poll()[0].state, "working")

    def test_claude_source_failure_logs_its_cause(self):
        (self.runtime / "hooks").mkdir(parents=True)
        (self.runtime / "hooks" / "a.json").write_text('{"id": 3}')
        self.native()
        self.observer.poll()
        self.assertEqual(self.observer.error_summary(), "claude (Invalid session/turn identifier)")

    def test_log_cause_keeps_only_this_module_messages(self):
        self.observer._error("codex", ValueError("could not convert string to float: 'private text'"))
        self.observer._error("claude", ValueError("Invalid JSON file: /Users/someone/.claude/sessions/a.json"))
        self.assertEqual(self.observer.error_summary(), "codex (ValueError), claude (ValueError)")
        self.observer.causes, self.observer.errors = {}, []
        self.observer._error("codex", observe.SourceError("Rollout record exceeds size limit"))
        self.assertEqual(self.observer.error_summary(), "codex (Rollout record exceeds size limit)")

    def test_unreadable_rollout_is_not_rescanned_until_it_changes(self):
        self.codex_fixture([function("function_call", name="request_user_input", call_id="q")])
        with patch("agentdeck.observe.read_turn", wraps=read_turn) as reader:
            for _ in range(3):
                self.assertEqual(self.observer.poll(), [])
                self.assertEqual(self.observer.errors, ["codex"])
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(self.observer.error_summary(), "codex (Selected turn boundary unavailable)")
            with self.rollout.open("a") as stream:
                stream.write(json.dumps(event("task_started", turn_id="turn")) + "\n")
            self.assertEqual(self.observer.poll()[0].state, "working")
            self.assertEqual(reader.call_count, 2)

    def test_rollout_scan_limit_is_explicit(self):
        path = self.root / "rollout"
        path.write_text(json.dumps(event("task_started", turn_id="turn")) + "\n" + json.dumps(function("function_call_output", call_id="1", output="x" * 1000)) + "\n")
        with patch("agentdeck.observe.MAX_ROLLOUT_SCAN", 100):
            with self.assertRaisesRegex(ValueError, "boundary"):
                read_turn(path, "turn")

    def test_pending_codex_completion_survives_restart_until_ack_or_archive(self):
        self.codex_fixture([event("task_started", turn_id="turn"), event("task_complete", turn_id="turn")], "completed", self.now + 1)
        self.now += 1
        first = self.observer.poll()[0]
        self.assertEqual(first.state, "ready")
        self.now += 3600
        observer = self.new_observer()
        self.assertEqual(observer.poll(), [first])
        observer.acknowledge([first])
        self.assertEqual(observer.poll(), [])
        self.assertIn("codex:c", observer.acks)
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET archived=1")
            db.commit()
        self.assertEqual(observer.poll(), [])
        self.assertFalse(observer.acks)
        self.assertFalse(observer.tail_cache)
        self.assertFalse(observer.previous)

    def test_silent_codex_completion_dies_after_two_hours(self):
        self.codex_fixture([event("task_started", turn_id="turn"), event("task_complete", turn_id="turn")], "completed", self.now + 1)
        self.now += 1
        self.assertEqual(self.observer.poll()[0].state, "ready")
        self.now += 7201
        self.assertEqual(self.new_observer().poll(), [])
        self.assertFalse(load_json(self.runtime / "completions.json"))

    def test_pending_codex_result_beyond_the_recent_page_is_kept(self):
        self.codex_fixture([event("task_started", turn_id="turn"), event("task_complete", turn_id="turn")], "completed", self.now + 1)
        self.now += 1
        first = self.observer.poll()[0]
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as db:
            db.executemany("INSERT INTO threads VALUES(?,?,?,?,?,?)",
                           [(f"other{i}", str(self.rollout), self.now, 0, None, "user") for i in range(200)])
            db.commit()
        self.assertEqual(self.observer.poll(), [first])
        self.assertIn("codex:c", self.observer.results)

    def test_touched_old_codex_conversation_does_not_revive_its_result(self):
        self.codex_fixture([event("task_started", turn_id="turn"), event("task_complete", turn_id="turn")], "completed", self.now + 1)
        self.now += 1
        self.assertEqual(self.observer.poll()[0].state, "ready")
        self.now += 7201
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET updated_at=?", (self.now,))  # For example, a rename.
            db.commit()
        os.utime(self.rollout, (self.now, self.now))
        self.assertEqual(self.observer.poll(), [])
        self.assertFalse(self.observer.results)

    def test_failed_codex_turn_dies_after_two_hours(self):
        self.codex_fixture([event("task_started", turn_id="turn"), event("turn_aborted", turn_id="turn")], "interrupted", self.now)
        self.assertEqual(self.observer.poll()[0].state, "unknown")
        self.now += 7201
        self.assertEqual(self.observer.poll(), [])

    def test_new_turn_and_rotated_same_size_rollout_invalidate_cache(self):
        self.codex_fixture([event("task_started", turn_id="turn"), function("function_call", name="request_user_input", call_id="q")])
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        temporary = self.codex / "replacement"
        temporary.write_text(json.dumps(event("task_started", turn_id="next")) + "\n")
        temporary.replace(self.rollout)
        with closing(sqlite3.connect(self.codex / "thread_history_1.sqlite")) as db:
            db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?)", ("c", "next", 2, "inProgress", None))
            db.commit()
        self.assertEqual(self.observer.poll()[0].state, "working")
        self.assertEqual(self.observer.poll()[0].version, "next")

    def test_new_root_turn_in_rollout_clears_old_ready_before_sqlite_catches_up(self):
        self.codex_fixture([event("task_started", turn_id="turn"), event("task_complete", turn_id="turn")], "completed", self.now)
        previous = self.observer.poll()[0]
        self.assertEqual(previous.state, "ready")
        self.now += 1
        with self.rollout.open("a") as stream:
            stream.write(json.dumps(event("task_started", turn_id="next", root_turn_id="next")) + "\n")
        uncertain = self.observer.poll()[0]
        self.assertEqual(uncertain.state, "unknown")
        self.assertFalse(self.observer.results)
        self.assertFalse(load_json(self.runtime / "completions.json"))
        self.assertFalse(self.observer.errors)
        self.observer.acknowledge([previous])
        self.assertFalse(self.observer.acks)
        with closing(sqlite3.connect(self.codex / "thread_history_1.sqlite")) as db:
            db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?)", ("c", "next", 2, "inProgress", None))
            db.commit()
        active = self.observer.poll()[0]
        self.assertEqual((active.state, active.version), ("working", "next"))

    def test_subagent_started_event_does_not_supersede_root_completion(self):
        records = [event("task_started", turn_id="turn", root_turn_id="turn"),
                   event("task_complete", turn_id="turn"),
                   event("task_started", turn_id="child", root_turn_id="turn")]
        self.assertEqual(rollout_state(records, "turn"), (True, False))

    def test_sqlite_connections_are_closed_each_poll_without_gc(self):
        self.codex_fixture([event("task_started", turn_id="turn")])
        connections = []

        def tracked(path):
            connection = readonly_db(path)
            connections.append(connection)
            return connection

        with patch("agentdeck.observe.readonly_db", side_effect=tracked):
            for _ in range(10):
                self.observer.poll()
        self.assertEqual(len(connections), 20)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_pid_identity_rejects_bool_missing_birth_and_reuse(self):
        for pid, born in [(True, "birth"), (123, None), (-1, "birth"), (0, "birth"), (2**40, "birth")]:
            self.assertFalse(pid_matches(pid, born))
        with patch("agentdeck.observe.process_birth", return_value=2):
            self.assertFalse(pid_matches(123, "Thu Jan 1 00:00:01 1970"))
            self.assertTrue(pid_matches(123, "Thu Jan 1 00:00:02 1970"))
            self.assertFalse(pid_matches(123, "not a process start"))

    def test_ps_fallback_reads_utc_birth_when_libproc_is_absent(self):
        with patch("agentdeck.observe.LIBPROC", None), patch("agentdeck.observe.os.kill"), patch(
                "agentdeck.observe.subprocess.check_output", return_value=b"Thu Jan  1 00:00:02 1970\n"):
            self.assertFalse(pid_matches(123, "Thu Jan 1 00:00:01 1970"))
            self.assertTrue(pid_matches(123, "Thu Jan 1 00:00:02 1970"))

    @unittest.skipUnless(observe.LIBPROC, "libproc is macOS only")
    def test_libproc_identifies_processes_without_forking(self):
        started = subprocess.check_output(["/bin/ps", "-p", str(os.getpid()), "-o", "lstart="],
                                          env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}).decode()
        child = subprocess.Popen(["/bin/sleep", "0"])
        child.wait()
        with patch("agentdeck.observe.subprocess.check_output", side_effect=AssertionError("forked ps")):
            self.assertTrue(pid_matches(os.getpid(), started))
            later = datetime.fromtimestamp(observe.process_started_at(started) + 1, timezone.utc)
            self.assertFalse(pid_matches(os.getpid(), later.strftime("%a %b %d %H:%M:%S %Y")))
            self.assertFalse(pid_matches(child.pid, started))


if __name__ == "__main__":
    unittest.main()
