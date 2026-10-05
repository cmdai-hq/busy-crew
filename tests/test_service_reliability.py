"""Lifecycle regressions using isolated runtime files and simulated transports."""
import asyncio
import base64
import io
import json
import logging
import queue
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.response import addinfourl

from agentdeck.device import APPLICATION, MAX_RESPONSE_BYTES, Device
from agentdeck.model import Session
from agentdeck.render import Frame, compact, render, visible_sessions
from agentdeck.service import MAX_COMMANDS_PER_POLL, MAX_EVENTS, MAX_INBOX_FILES, Service, configure_logging
from agentdeck.usage import Usage, UsageSnapshot, parse_usage, usage_output

DAY = "2026-10-04"


def session(provider="claude", state="ready", sid="a"):
    return Session(provider, sid, state, "result-1")


def service_fixture(runtime):
    service = Service.__new__(Service)
    service.runtime = runtime
    service.stop = threading.Event()
    service.events = queue.Queue(maxsize=MAX_EVENTS)
    service.events_dropped = 0
    service.view = "sessions"
    service.paused = False
    service.inputs_connected = False
    service.inputs_error = None
    service.last_control = None
    service.sessions = []
    service.presented_sessions = []
    service.presented_until = float("inf")
    service.observer = SimpleNamespace(poll=lambda: [], errors=[], acknowledge=MagicMock())
    service.usage = Usage(command="fixture-ccusage")
    service.device = SimpleNamespace(draw=MagicMock(), clear=MagicMock())
    return service


class DeviceSecurityTests(unittest.TestCase):
    def test_all_redirects_are_rejected_without_forwarding_key(self):
        for code in [301, 302, 303, 307, 308]:
            for origin, destination in [("http://busy.invalid", "http://other.invalid"),
                                        ("https://busy.invalid", "http://busy.invalid")]:
                with self.subTest(code=code, origin=origin):
                    requests = []
                    def transport(handler, request):
                        requests.append(request)
                        headers = Message()
                        headers["Location"] = destination
                        response = addinfourl(io.BytesIO(b""), headers, request.full_url, code)
                        response.msg = "Redirect"
                        return response
                    with patch.object(urllib.request.HTTPHandler, "http_open", transport), patch.object(
                        urllib.request.HTTPSHandler, "https_open", transport
                    ):
                        with self.assertRaises(urllib.error.HTTPError):
                            Device(origin, "FIXTURE_KEY").request("/api/test")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].get_header("X-api-token"), "FIXTURE_KEY")

    def test_clear_identifies_only_our_application_in_query(self):
        device = Device("http://busy.invalid")
        with patch.object(device, "request") as request:
            device.clear()
        request.assert_called_once_with("/api/display/draw?application_name=" + APPLICATION, "DELETE")

    def test_response_read_is_bounded_and_invalid_base64_is_rejected(self):
        device = Device("http://busy.invalid")
        response = io.BytesIO(b"x" * (MAX_RESPONSE_BYTES + 1))
        with patch.object(device.opener, "open", return_value=response):
            with self.assertRaisesRegex(ValueError, "size limit"):
                device.request("/api/screen")
        self.assertTrue(response.closed)
        with patch.object(device, "request", return_value=base64.b64encode(b"x" * 3456) + b"!"):
            with self.assertRaises(ValueError):
                device.screen()


class UsageContractTests(unittest.TestCase):
    def test_valid_empty_and_missing_family_are_distinct(self):
        self.assertEqual(parse_usage({"daily": []}, DAY), {"claude": 0, "codex": 0})
        self.assertEqual(parse_usage({"daily": [{"period": DAY, "agents": [
            {"agent": "claude", "totalTokens": 0}]}]}, DAY), {"claude": 0})

    def test_malformed_schema_and_boolean_count_are_rejected(self):
        cases = [None, [], {}, {"daily": None}, {"daily": [None]},
                 {"daily": [{"period": DAY}]},
                 {"daily": [{"period": "invalid", "agents": []}]},
                 {"daily": [{"period": DAY, "agent": [], "agents": None}]},
                 {"daily": [{"period": DAY, "agents": [None]}]},
                 {"daily": [{"period": DAY, "agents": [{"agent": "claude", "totalTokens": True}]}]}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_usage(data, DAY)

    def test_malformed_report_becomes_unknown_and_next_refresh_recovers(self):
        usage = Usage(command="fixture")
        reports = [b'{"daily":[null]}', json.dumps({"daily": [{"period": DAY, "agents": [
            {"agent": "claude", "totalTokens": 321}]}]}).encode()]
        with patch("agentdeck.usage.today", return_value=DAY), patch(
            "agentdeck.usage.usage_output", side_effect=reports
        ):
            usage.refresh()
            self.assertEqual(usage.current(), {})
            self.assertEqual(usage.error, "ccusage unavailable")
            usage.refresh()
            self.assertEqual(usage.current(), {"claude": 321})
            self.assertIsNone(usage.error)

    def test_snapshot_is_immutable_and_values_do_not_alias_state(self):
        usage = Usage(command="fixture")
        usage._state = UsageSnapshot((("claude", 123),), DAY, 1000)
        with patch("agentdeck.usage.today", return_value=DAY), patch("agentdeck.usage.time.time", return_value=1001):
            state = usage.snapshot()
        exposed = state.values
        exposed["claude"] = 0
        usage._state = UsageSnapshot(day=DAY, at=1002, error="ccusage unavailable")
        self.assertEqual(state.values, {"claude": 123})
        self.assertEqual(state.at, 1000)
        self.assertIsNone(state.error)

    def test_date_failure_still_publishes_unavailable_and_current_unknown(self):
        usage = Usage(command="fixture")
        usage.values = {"claude": 123}
        with patch("agentdeck.usage.today", side_effect=ValueError("fixture")):
            usage.refresh()
            self.assertEqual(usage.current(), {})
        self.assertEqual(usage.values, {})
        self.assertEqual(usage.error, "ccusage unavailable")

    def test_output_timeout_and_size_limit_kill_reap_and_close_process(self):
        for oversized in [False, True]:
            with self.subTest(oversized=oversized):
                process = MagicMock()
                process.returncode = None
                process.poll.side_effect = lambda: process.returncode
                process.wait.side_effect = lambda **kwargs: setattr(process, "returncode", 0)
                selector = MagicMock()
                selector.__enter__.return_value = selector
                selector.select.return_value = [(None, None)] if oversized else []
                with patch("agentdeck.usage.subprocess.Popen", return_value=process), patch(
                    "agentdeck.usage.selectors.DefaultSelector", return_value=selector
                ), patch("agentdeck.usage.os.read", return_value=b"12345"):
                    with self.assertRaises(ValueError if oversized else subprocess.TimeoutExpired):
                        usage_output(["fixture"], max_bytes=4)
                process.kill.assert_called_once()
                process.wait.assert_called_once()
                process.stdout.close.assert_called_once()

    def test_successful_output_is_reaped_and_closed_without_kill(self):
        process = MagicMock()
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode
        process.wait.side_effect = lambda **kwargs: setattr(process, "returncode", 0)
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.select.return_value = [(None, None)]
        with patch("agentdeck.usage.subprocess.Popen", return_value=process), patch(
            "agentdeck.usage.selectors.DefaultSelector", return_value=selector
        ), patch("agentdeck.usage.os.read", side_effect=[b"{}", b""]):
            self.assertEqual(usage_output(["fixture"]), b"{}")
        process.kill.assert_not_called()
        process.wait.assert_called_once()
        process.stdout.close.assert_called_once()


class ServiceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.service = service_fixture(self.runtime)

    def tearDown(self):
        self.temp.cleanup()

    def test_malformed_inbox_does_not_block_following_valid_command(self):
        inbox = self.runtime / "inbox"
        inbox.mkdir()
        (inbox / "invalid.json").write_text('["ack"]')
        (inbox / "valid.json").write_text('{"command":"tokens"}')
        self.service.read_commands(inbox)
        self.assertEqual(self.service.view, "tokens")
        self.assertEqual(list(inbox.iterdir()), [])

    def test_inbox_and_event_processing_are_bounded(self):
        inbox = self.runtime / "inbox"
        inbox.mkdir()
        for index in range(MAX_INBOX_FILES + 1):
            (inbox / f"{index}.json").write_text('{"command":"sessions"}')
        self.service.read_commands(inbox)
        self.assertEqual(len(list(inbox.iterdir())), 1)
        for _ in range(MAX_EVENTS + 10):
            self.service.enqueue("tokens")
        self.assertEqual(self.service.events.qsize(), MAX_EVENTS)
        self.assertGreater(self.service.events_dropped, 0)
        for path in inbox.iterdir():
            path.unlink()
        self.service.read_commands(inbox)
        self.assertEqual(self.service.events.qsize(), MAX_EVENTS - MAX_COMMANDS_PER_POLL)

    def test_ack_only_targets_last_presented_results(self):
        previous = session(sid="presented")
        self.service.sessions = [session(sid="new")]
        self.service.presented_sessions = [previous]
        self.service.command("ack")
        self.service.observer.acknowledge.assert_called_once_with([previous])

    def test_ack_ignores_expired_or_paused_display(self):
        self.service.presented_sessions = [session()]
        self.service.presented_until = 1000
        with patch("agentdeck.service.time.time", return_value=1001):
            self.service.command("ack")
        self.service.presented_until = float("inf")
        self.service.paused = True
        self.service.command("ack")
        self.service.observer.acknowledge.assert_not_called()

    def test_failed_acknowledgement_does_not_stop_next_control(self):
        self.service.presented_sessions = [session()]
        self.service.observer.acknowledge.side_effect = OSError("fixture-private-details")
        inbox = self.runtime / "inbox"
        inbox.mkdir()
        self.service.enqueue("ack")
        self.service.enqueue("tokens")
        with self.assertLogs("agentdeck", "WARNING") as logs:
            self.service.read_commands(inbox)
        self.assertEqual(self.service.view, "tokens")
        self.assertTrue(self.service.presented_sessions)
        self.assertNotIn("fixture-private-details", "\n".join(logs.output))

    def test_usage_worker_recovers_from_unexpected_refresh_error(self):
        refresh = MagicMock(side_effect=[RuntimeError("fixture"), None])
        unavailable = MagicMock()
        self.service.usage = SimpleNamespace(refresh=refresh, unavailable=unavailable)
        class Stop:
            calls = 0
            def is_set(self):
                return self.calls >= 2
            def wait(self, timeout):
                self.calls += 1
        self.service.stop = Stop()
        self.service.usage_loop()
        self.assertEqual(refresh.call_count, 2)
        unavailable.assert_called_once()

    def test_input_worker_startup_failure_reports_only_exception_type(self):
        async def fail():
            raise RuntimeError("fixture-secret-url")
        with patch.object(self.service, "inputs_loop", fail), self.assertLogs("agentdeck", "WARNING") as logs:
            self.service.inputs_worker()
        self.assertEqual(self.service.inputs_error, "RuntimeError")
        self.assertFalse(self.service.inputs_connected)
        self.assertNotIn("fixture-secret-url", "\n".join(logs.output))

    def test_normal_websocket_close_and_errors_back_off_and_reset_connection(self):
        for failure in [False, True]:
            with self.subTest(failure=failure):
                self.service.stop.clear()
                self.service.device.address = "http://fixture.invalid"
                self.service.device.token = None
                waits, states = [], []
                service = self.service
                class FakeBar:
                    def __init__(self, **kwargs):
                        pass
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *args):
                        pass
                    async def stream_status_ws(self):
                        if failure:
                            raise OSError("fixture")
                        yield {}
                async def wait(delay):
                    waits.append(delay)
                    states.append(service.inputs_connected)
                    if len(waits) == 3:
                        service.stop.set()
                with patch("busylib.AsyncBusyBar", FakeBar), patch.object(service, "retry_wait", wait):
                    asyncio.run(service.inputs_loop())
                self.assertEqual(waits, [1, 2, 4])
                self.assertEqual(states, [False, False, False])
                self.assertFalse(service.inputs_connected)

    def test_status_uses_one_usage_snapshot_despite_refresh_during_draw(self):
        self.service.view = "tokens"
        self.service.usage._state = UsageSnapshot((("claude", 10),), DAY, 1000)
        writes = []
        def draw(frame):
            self.service.usage._state = UsageSnapshot(day=DAY, at=1001, error="ccusage unavailable")
            self.service.stop.set()
        self.service.device.draw.side_effect = draw
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.usage.today", return_value=DAY
        ), patch("agentdeck.usage.time.time", return_value=1001), patch(
            "agentdeck.service.write_json", side_effect=lambda path, data: writes.append(data)
        ):
            self.service.run()
        self.assertEqual(writes[0]["tokens"], {"claude": 10})
        self.assertEqual(writes[0]["tokens_at"], 1000)
        self.assertIsNone(writes[0]["tokens_error"])
        self.assertEqual(self.service.presented_sessions, [])

    def test_setup_failure_still_clears_and_releases_instance_lock(self):
        with patch("agentdeck.service.signal.signal", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                self.service.run()
        self.service.device.clear.assert_called_once()
        self.assertTrue(self.service.stop.is_set())
        # A second instance can acquire the same lock after setup failed.
        second = service_fixture(self.runtime)
        second.device.draw.side_effect = lambda frame: second.stop.set()
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"):
            second.run()

    def test_log_rotation_is_bounded_private_and_reuses_handler(self):
        handler = configure_logging(self.runtime)
        self.assertIs(configure_logging(self.runtime), handler)
        handler.maxBytes = 256
        logger = logging.getLogger("agentdeck")
        try:
            for _ in range(30):
                logger.warning("Fixture log record with a bounded size")
        finally:
            logger.removeHandler(handler)
            handler.close()
        files = list(self.runtime.glob("service.log*"))
        self.assertLessEqual(len(files), 4)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in files))


class PresentationContractTests(unittest.TestCase):
    def test_tokens_ack_has_no_results_unless_waiting_overrides_tokens(self):
        items = [session("claude", sid=str(i)) for i in range(7)]
        self.assertEqual(visible_sessions(items, "tokens"), [])
        items.append(session("claude", "waiting", "waiting"))
        shown = visible_sessions(items, "tokens")
        self.assertEqual(len(shown), 5)
        self.assertEqual(shown[0].state, "waiting")

    def test_overflow_ack_excludes_hidden_results(self):
        items = [session("claude", sid=str(i)) for i in range(7)]
        self.assertEqual(len(visible_sessions(items)), 5)

    def test_duo_ack_uses_aggregate_or_only_individual_ready_dots(self):
        items = [session("claude", sid=str(i)) for i in range(7)] + [session("codex", "working")]
        self.assertEqual(sum(s.provider == "claude" for s in visible_sessions(items)), 7)
        items.append(session("claude", "working", "working"))
        self.assertEqual(sum(s.state == "ready" for s in visible_sessions(items)), 3)
        self.assertEqual(sum(s.state == "ready" for s in visible_sessions(items, errors=["claude"])), 3)

    def test_source_error_uses_unknown_count_and_duo_selection(self):
        items = [session("codex", sid=str(i)) for i in range(7)]
        frame = render(items, errors=["claude"])
        expected = Frame()
        expected.text("?", 19, 3, scale=2)
        self.assertEqual([row[19:25] for row in frame.rows[:10]], [row[19:25] for row in expected.rows[:10]])
        self.assertEqual(len(visible_sessions(items, errors=["claude"])), 7)

    def test_zzz_only_when_all_open_claude_sessions_are_idle(self):
        idle = [session("claude", "idle", str(i)) for i in range(2)]
        sleeping = render(idle)
        for added in [session("claude", "ready", "new"), session("claude", "working", "new")]:
            self.assertNotEqual(sleeping.rows, render(idle + [added]).rows)
        self.assertTrue(any(v == "d" for row in sleeping.rows for v in row))
        duo = render(idle + [session("codex", "working")])
        expected = Frame()
        expected.text("zzz", 27, 3, "d")
        self.assertEqual([row[27:35] for row in duo.rows[3:7]], [row[27:35] for row in expected.rows[3:7]])

    def test_token_units_never_clip_even_for_enormous_valid_counts(self):
        missed = []
        original = Frame.dot
        def record(frame, x, y, color):
            if not (0 <= x < 72 and 0 <= y < 16):
                missed.append((x, y))
            return original(frame, x, y, color)
        with patch.object(Frame, "dot", record):
            for count in [0, 999, 1000, 999999, 99950000, 100000000, 10**10, 10**12, 10**1000]:
                render([], "tokens", {"claude": count, "codex": count})
        self.assertEqual(missed, [])
        self.assertEqual(compact(14_375_421), "14.4M")
        self.assertEqual(compact(10**10), "10.0G")


if __name__ == "__main__":
    unittest.main()
