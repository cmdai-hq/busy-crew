"""Lifecycle regressions using isolated runtime files and simulated transports."""
import asyncio
import base64
import io
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.response import addinfourl

from agentdeck.device import APPLICATION, MAX_RESPONSE_BYTES, Device
from agentdeck.updates import UpdateCheck
from agentdeck.model import Session
from agentdeck.render import Frame, compact, render, visible_sessions
from agentdeck.service import (ACTIVE_INTERVAL, IDLE_INTERVAL, LEASE_RENEWAL, MAX_COMMANDS_PER_POLL, MAX_EVENTS,
                               MAX_INBOX_FILES, SETTLE, WATCH_INTERVAL, Service, configure_logging, pace)
from agentdeck.storage import load_json, write_json
from agentdeck.usage import Usage, UsageSnapshot, parse_periods, parse_usage, usage_output

DAY = "2026-10-04"


def session(provider="claude", state="ready", sid="a"):
    return Session(provider, sid, state, "result-1")


def service_fixture(runtime):
    service = Service.__new__(Service)
    service.runtime = runtime
    service.stop = threading.Event()
    service.wake = threading.Event()
    service.usage_running = threading.Lock()
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
    service.observer = SimpleNamespace(poll=lambda: [], errors=[], acknowledge=MagicMock(),
                                       claude_root=runtime / "claude")
    service.usage = Usage(command="fixture-ccusage")
    service.device = SimpleNamespace(draw=MagicMock(), clear=MagicMock())
    service.updates = UpdateCheck(runtime, "1.0.1", enabled=False)
    service.install_mode = None
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

    def test_month_to_date_sums_this_month_and_counts_quiet_days_as_zero(self):
        data = {"daily": [
            {"period": "2026-09-30", "agents": [{"agent": "claude", "totalTokens": 1000}]},
            {"period": "2026-10-01", "agents": [{"agent": "claude", "totalTokens": 5},
                                                {"agent": "codex", "totalTokens": 7}]},
            {"period": DAY, "agents": [{"agent": "claude", "totalTokens": 3}]}]}
        self.assertEqual(parse_periods(data, DAY), ({"claude": 3, "codex": 0}, {"claude": 8, "codex": 7}))
        self.assertEqual(parse_periods({"daily": []}, DAY), ({"claude": 0, "codex": 0}, {"claude": 0, "codex": 0}))

    def test_refresh_reads_month_to_date_in_one_report(self):
        usage = Usage(command="fixture")
        report = json.dumps({"daily": [
            {"period": "2026-10-01", "agents": [{"agent": "claude", "totalTokens": 5}]},
            {"period": DAY, "agents": [{"agent": "claude", "totalTokens": 3}]}]}).encode()
        with patch("agentdeck.usage.today", return_value=DAY), patch(
            "agentdeck.usage.usage_output", return_value=report
        ) as output:
            usage.refresh()
            state = usage.snapshot()
        command = output.call_args.args[0]
        self.assertEqual(command[command.index("--since") + 1], "2026-10-01")
        self.assertEqual(command[command.index("--until") + 1], DAY)
        self.assertEqual((state.values, state.month_values), ({"claude": 3}, {"claude": 8}))

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

    def test_start_cycles_views_and_wheel_steps_without_wrapping(self):
        seen = []
        for _ in range(3):
            self.service.command("toggle")
            seen.append(self.service.view)
        self.assertEqual(seen, ["tokens", "month", "sessions"])
        for command, expected in [("previous", "sessions"), ("next", "tokens"), ("next", "month"),
                                  ("next", "month"), ("previous", "tokens")]:
            self.service.command(command)
            self.assertEqual(self.service.view, expected)

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
        self.service.usage._state = UsageSnapshot((("claude", 10),), DAY, 1000, month=(("claude", 40),))
        writes = []
        def draw(frame):
            self.service.usage._state = UsageSnapshot(day=DAY, at=1001, error="ccusage unavailable")
            self.service.stop.set()
        self.service.device.draw.side_effect = draw
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.usage.today", return_value=DAY
        ), patch("agentdeck.usage.time.time", return_value=1001), patch(
            "agentdeck.service.write_json", side_effect=lambda path, data, **_: writes.append(data)
        ):
            self.service.run()
        self.assertEqual(writes[0]["tokens"], {"claude": 10})
        self.assertEqual(writes[0]["tokens_month"], {"claude": 40})
        self.assertEqual(writes[0]["tokens_at"], 1000)
        self.assertIsNone(writes[0]["tokens_error"])
        self.assertEqual(self.service.presented_sessions, [])

    def test_only_snapshot_writes_skip_fsync(self):
        with patch("agentdeck.storage.os.fsync") as fsync:
            write_json(self.runtime / "snapshot.json", {"a": 1}, durable=False)
            fsync.assert_not_called()
            write_json(self.runtime / "ledger.json", {"b": 1})
        self.assertEqual(fsync.call_count, 2)
        self.assertEqual(load_json(self.runtime / "snapshot.json"), {"a": 1})

    def test_status_snapshots_are_replaced_without_disk_flushes(self):
        self.service.device.draw.side_effect = lambda frame: self.service.stop.set()
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.storage.os.fsync"
        ) as fsync, patch("agentdeck.service.write_json", wraps=write_json) as write:
            self.service.run()
        self.assertTrue(any(call.args[0].name == "status.json" for call in write.call_args_list))
        fsync.assert_not_called()

    def test_lease_renewal_redraws_without_rewriting_front_png(self):
        self.service.device.draw.side_effect = lambda frame: (
            self.service.stop.set() if self.service.device.draw.call_count == 2 else None)
        clock = iter([0, 10])
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.service.time.monotonic", side_effect=lambda: next(clock, 10)
        ), patch("agentdeck.render.Frame.png", return_value=b"png") as png:
            self.service.run()
        self.assertEqual(self.service.device.draw.call_count, 2)
        png.assert_called_once()

    def test_frame_and_lock_files_are_private(self):
        lock = self.service.runtime / "service.lock"
        lock.touch(mode=0o644)  # Created by an earlier version with the default mode.
        lock.chmod(0o644)
        self.service.device.draw.side_effect = lambda frame: self.service.stop.set()
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"):
            self.service.run()
        for name in ["front.png", "service.lock"]:
            self.assertEqual((self.service.runtime / name).stat().st_mode & 0o777, 0o600, name)
        self.assertTrue((self.service.runtime / "front.png").read_bytes().startswith(b"\x89PNG"))

    def test_status_publishes_health_and_identity(self):
        self.service.inputs_connected = True
        self.service.device.draw.side_effect = lambda frame: self.service.stop.set()
        writes = []
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.service.write_json", side_effect=lambda path, data, **_: writes.append(data)
        ):
            self.service.run()
        self.assertEqual(writes[0]["pid"], os.getpid())
        self.assertIsInstance(writes[0]["started_at"], float)
        self.assertIs(writes[0]["code_changed"], False)
        self.assertEqual(writes[0]["health"], {"level": "ok", "reasons": []})

    def test_code_change_is_detected_after_recheck_interval(self):
        package = self.runtime / "package"
        package.mkdir()
        source = package / "service.py"
        source.write_text("x = 1\n")
        def draw(frame):
            if self.service.device.draw.call_count == 1:
                source.write_text("x = 22\n")
            else:
                self.service.stop.set()
        self.service.package_root = package
        self.service.inputs_connected = True
        self.service.device.draw.side_effect = draw
        clock, writes = iter([0, 31]), []
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.service.time.monotonic", side_effect=lambda: next(clock, 31)
        ), patch("agentdeck.service.write_json", side_effect=lambda path, data, **_: writes.append(data)):
            self.service.run()
        self.assertIs(writes[0]["code_changed"], False)
        self.assertIs(writes[1]["code_changed"], True)
        self.assertEqual(writes[1]["health"]["level"], "update")

    def test_fingerprint_failures_never_stop_the_collector(self):
        self.service.device.draw.side_effect = lambda frame: (
            self.service.stop.set() if self.service.device.draw.call_count == 2 else None)
        # A reinstall can remove a directory while rglob walks it.
        failures = [FileNotFoundError("vanished"), FileNotFoundError("vanished")]
        clock, writes = iter([0, 31]), []
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.service.time.monotonic", side_effect=lambda: next(clock, 31)
        ), patch("agentdeck.service.code_fingerprint", side_effect=failures), patch(
            "agentdeck.service.write_json", side_effect=lambda path, data, **_: writes.append(data)
        ):
            self.service.run()
        self.assertEqual([status["code_changed"] for status in writes], [False, False])

    def test_status_counts_only_results_the_bar_can_acknowledge(self):
        # OK acknowledges what the bar shows: nothing from the tokens view.
        self.service.observer = SimpleNamespace(poll=lambda: [session()], errors=[], acknowledge=MagicMock(),
                                                claude_root=self.runtime / "claude")
        counts = {}
        for view in ["sessions", "tokens"]:
            self.service.view, self.service.stop, writes = view, threading.Event(), []
            self.service.device.draw.side_effect = lambda frame: self.service.stop.set()
            with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
                "agentdeck.service.write_json", side_effect=lambda path, data, **_: writes.append(data)
            ):
                self.service.run()
            counts[view] = writes[0]["acknowledgeable"]
        self.assertEqual(counts, {"sessions": 1, "tokens": 0})

    def test_refresh_command_runs_ccusage_now(self):
        done = threading.Event()
        self.service.usage.refresh = done.set  # Otherwise the next periodic run is up to 120 s away.
        self.service.command("refresh")
        self.assertTrue(done.wait(2))
        self.assertEqual(self.service.last_control, "refresh")

    def test_refresh_during_a_run_starts_no_thread(self):
        self.service.usage_running.acquire()  # A run is in progress.
        try:
            with patch("agentdeck.service.threading.Thread") as thread:
                for _ in range(5):
                    self.service.command("refresh")
            thread.assert_not_called()
        finally:
            self.service.usage_running.release()

    def test_token_refreshes_never_overlap(self):
        running, release, calls = threading.Event(), threading.Event(), []
        def slow():
            calls.append("ccusage")
            running.set()
            release.wait(2)
        self.service.usage.refresh = slow
        self.service.command("refresh")
        self.assertTrue(running.wait(2))
        self.service.refresh_usage()  # Served by the run in progress: returns at once.
        release.set()
        self.assertEqual(calls, ["ccusage"])

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


class SimulatedClock:
    """Monotonic time that moves only while the collector waits; arrivals happen on the way."""

    def __init__(self, arrivals=()):
        self.now, self.arrivals = 0.0, sorted(arrivals, key=lambda arrival: arrival[0])

    def monotonic(self):
        return self.now


class SimulatedWake(threading.Event):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def wait(self, timeout=None):
        target = self.clock.now + timeout
        while not self.is_set() and self.clock.arrivals and self.clock.arrivals[0][0] <= target:
            moment, action = self.clock.arrivals.pop(0)
            self.clock.now = max(self.clock.now, moment)
            action()
        if not self.is_set():
            self.clock.now = target
        return self.is_set()


def gaps(times):
    return [round(b - a, 6) for a, b in zip(times, times[1:])]


class PaceTests(unittest.TestCase):
    """The loop's rhythm, on a simulated clock: no device and no real waiting."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.service = service_fixture(self.runtime)
        (self.runtime / "claude" / "sessions").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def simulate(self, states, until, arrivals=()):
        """Run the collector until `until`; `states(now)` lists the observed session states."""
        clock = SimulatedClock(arrivals)
        self.polls, self.draws, self.writes = [], [], []
        self.service.wake = SimulatedWake(clock)

        def poll():
            self.polls.append(clock.now)
            if clock.now >= until:
                self.service.stop.set()
            return [session(state=state, sid=str(i)) for i, state in enumerate(states(clock.now))]

        def write(path, data, **_):
            if path.name == "status.json":
                self.writes.append(clock.now)

        self.service.observer.poll = poll
        self.service.device.draw.side_effect = lambda frame: self.draws.append(clock.now)
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), patch(
            "agentdeck.service.code_fingerprint", return_value=()
        ), patch("agentdeck.service.time.monotonic", clock.monotonic), patch(
            "agentdeck.service.write_json", side_effect=write
        ):
            self.service.run()

    def test_pace_follows_work_waits_and_recent_changes(self):
        now = 100
        for state, expected in [("working", ACTIVE_INTERVAL), ("waiting", ACTIVE_INTERVAL),
                                ("ready", IDLE_INTERVAL), ("idle", IDLE_INTERVAL), ("unknown", IDLE_INTERVAL)]:
            with self.subTest(state=state):
                self.assertEqual(pace([session(state=state)], now, now - SETTLE), expected)
        self.assertEqual(pace([], now, now - SETTLE), IDLE_INTERVAL)
        self.assertEqual(pace([session(state="idle")], now, now - SETTLE + 0.5), ACTIVE_INTERVAL)

    def test_working_session_is_followed_every_second(self):
        self.simulate(lambda now: ["working", "idle"], until=30)
        self.assertEqual(set(gaps(self.polls)), {ACTIVE_INTERVAL})

    def test_rest_slows_down_but_keeps_lease_and_status_fresh(self):
        self.simulate(lambda now: ["idle", "ready"], until=60)
        settled = [t for t in self.polls if t >= SETTLE]
        self.assertEqual(set(gaps([t for t in self.polls if t <= SETTLE])), {ACTIVE_INTERVAL})
        self.assertEqual(set(gaps(settled)), {IDLE_INTERVAL})
        self.assertLessEqual(max(gaps(self.draws)), LEASE_RENEWAL)
        # `busy-crew status` takes a status older than 5 s for a dead collector, the menu 10 s.
        self.assertLessEqual(max(gaps(self.writes)), IDLE_INTERVAL)

    def test_session_that_starts_working_restores_the_fast_pace(self):
        native = self.runtime / "claude" / "sessions" / "1.json"
        self.simulate(lambda now: ["working"] if now >= 31.3 else ["idle"], until=45,
                      arrivals=[(31.3, lambda: native.write_text("{}"))])
        first = next(t for t in self.polls if t >= 31.3)
        self.assertLessEqual(first - 31.3, WATCH_INTERVAL)
        self.assertEqual(set(gaps([t for t in self.polls if t >= first])), {ACTIVE_INTERVAL})

    def test_claude_write_during_a_poll_is_picked_up_within_a_second(self):
        native, written = self.runtime / "claude" / "sessions" / "1.json", []

        def states(now):
            if now >= 30 and not written:  # Claude writes while the collector is busy with this poll.
                written.append(now)
                native.write_text("{}")
            return ["idle"]

        self.simulate(states, until=45)
        self.assertLessEqual(next(t for t in self.polls if t > written[0]) - written[0], WATCH_INTERVAL)

    def test_button_wakes_the_collector_at_once_without_breaking_the_lease(self):
        self.simulate(lambda now: ["idle"], until=60, arrivals=[(33.7, lambda: self.service.enqueue("next"))])
        self.assertIn(33.7, self.polls)
        self.assertEqual(self.service.view, "tokens")
        self.assertLessEqual(max(gaps(self.draws)), LEASE_RENEWAL)

    def test_cli_command_and_claude_hook_wake_the_collector_within_a_second(self):
        def command():
            write_json(self.runtime / "inbox" / "command.json", {"command": "month"})

        def hook():
            write_json(self.runtime / "hooks" / "a.json", {"id": "a", "state": "waiting", "at": 0})

        (self.runtime / "hooks").mkdir()
        self.simulate(lambda now: ["idle"], until=80, arrivals=[(31.2, command), (55.4, hook)])
        for moment in [31.2, 55.4]:
            with self.subTest(moment=moment):
                self.assertLessEqual(next(t for t in self.polls if t >= moment) - moment, WATCH_INTERVAL)
        self.assertEqual(self.service.view, "month")
        self.assertLessEqual(max(gaps(self.draws)), LEASE_RENEWAL)


class PresentationContractTests(unittest.TestCase):
    def test_tokens_ack_has_no_results_unless_waiting_overrides_tokens(self):
        items = [session("claude", sid=str(i)) for i in range(7)]
        self.assertEqual(visible_sessions(items, "tokens"), [])
        self.assertEqual(visible_sessions(items, "month"), [])
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
