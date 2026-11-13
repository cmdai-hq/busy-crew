"""The mailbox to the bar app: what it writes, how often, and how it reads OK presses back. No device is contacted."""
import json
import unittest
import urllib.error

from agentdeck import mailbox
from agentdeck.mailbox import ACK, APP_ID, STATE, TEMP, Mailbox, payload
from agentdeck.model import Session


class FakeBar:
    """A bar's storage API in memory, with a clock the test advances."""

    def __init__(self, clock):
        self.clock = clock
        self.files = {}
        self.calls = []
        self.fail = None
        self.delay = 0
        self.apps = {APP_ID}

    def _call(self, name):
        self.calls.append(name)
        self.clock.now += self.delay
        if self.fail:
            raise self.fail

    def storage_write(self, path, data):
        self._call("write")
        self.files[path] = data

    def storage_rename(self, path, new_path):
        self._call("rename")
        self.files[new_path] = self.files.pop(path)

    def storage_read(self, path):
        self._call("read")
        return self.files.get(path)

    def storage_list(self, path):
        self._call("list")
        return set(self.apps)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def crew(*states, provider="claude"):
    return [Session(provider, f"{i:04d}", state, "v") for i, state in enumerate(states)]


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bar = FakeBar(self.clock)
        self.box = Mailbox(self.bar, clock=self.clock)

    def state(self):
        return json.loads(self.bar.files[STATE])

    def sync(self, sessions, tokens=None, errors=()):
        return self.box.sync(sessions, list(errors), tokens, None, sessions)

    def test_payload_has_no_ids_and_puts_the_most_urgent_first(self):
        body = payload(crew("ready", "waiting") + crew("working", provider="codex"), ["codex", "elsewhere"],
                       {"claude": 5, "codex": -1}, None)
        self.assertEqual(body["sessions"], [["claude", "waiting"], ["codex", "working"], ["claude", "ready"]])
        self.assertEqual(body["errors"], ["codex"])
        self.assertEqual(body["tokens"], {"claude": 5, "codex": None})
        self.assertNotIn("0000", json.dumps(body))

    def test_writes_whole_files_through_a_rename(self):
        self.assertTrue(self.sync(crew("working")))
        self.assertEqual(self.bar.calls, ["write", "rename"])
        self.assertNotIn(TEMP, self.bar.files)
        self.assertEqual(self.state()["sessions"], [["claude", "working"]])

    def test_never_writes_twice_within_the_minimum_interval(self):
        self.sync(crew("working"))
        self.clock.now += 1
        self.assertFalse(self.sync(crew("ready")))
        self.clock.now += 1
        self.assertTrue(self.sync(crew("ready")))
        self.assertEqual(self.state()["sessions"], [["claude", "ready"]])

    def test_an_unchanged_crew_is_not_rewritten_until_the_heartbeat(self):
        self.sync(crew("idle"))
        seq = self.state()["seq"]
        self.clock.now += 59
        self.assertFalse(self.sync(crew("idle")))
        self.clock.now += 1
        self.assertTrue(self.sync(crew("idle")))
        self.assertEqual(self.state()["seq"], seq + 1)

    def test_token_totals_alone_are_written_at_most_once_a_minute(self):
        self.sync(crew("working"), {"claude": 1})
        self.clock.now += 10
        self.assertFalse(self.sync(crew("working"), {"claude": 2}))
        self.clock.now += 50
        self.assertTrue(self.sync(crew("working"), {"claude": 3}))
        self.assertEqual(self.state()["tokens"]["claude"], 3)

    def test_totals_that_become_known_are_written_without_waiting(self):
        self.sync(crew("working"), {"claude": None, "codex": None})
        self.clock.now += 5
        self.assertTrue(self.sync(crew("working"), {"claude": 7, "codex": 1}))
        self.clock.now += 5
        self.assertFalse(self.sync(crew("working"), {"claude": 8, "codex": 1}))

    def test_a_crew_change_carries_the_latest_tokens(self):
        self.sync(crew("working"), {"claude": 1})
        self.clock.now += 5
        self.sync(crew("ready"), {"claude": 2})
        self.assertEqual(self.state()["tokens"]["claude"], 2)

    def test_a_failure_backs_off_and_a_success_resets(self):
        self.bar.fail = urllib.error.URLError("down")
        self.assertFalse(self.sync(crew("working")))
        self.assertEqual(self.box.error, "URLError")
        self.bar.fail = None
        self.clock.now += 1
        self.assertFalse(self.sync(crew("working")))  # Still backing off: nothing is sent.
        self.assertEqual(self.bar.calls, ["write"])
        self.clock.now += 1
        self.assertTrue(self.sync(crew("working")))
        self.assertIsNone(self.box.error)

    def test_backoff_doubles_up_to_its_ceiling(self):
        self.bar.fail = urllib.error.URLError("down")
        for _ in range(10):
            self.clock.now = self.box.resume_at
            self.sync(crew("working"))
        self.assertEqual(self.box.backoff, mailbox.MAX_BACKOFF)

    def test_a_slow_bar_backs_off_although_the_write_succeeded(self):
        self.bar.delay = 1.5  # Each request is judged on its own: 1.5 s is under SLOW.
        self.assertTrue(self.sync(crew("working")))
        self.assertIsNone(self.box.error)
        self.bar.delay = 2.5
        self.clock.now += 5
        self.sync(crew("ready"))
        self.assertEqual(self.box.error, "SlowBar")
        self.assertFalse(self.box.ready())

    def test_app_installed_is_checked_once_a_minute(self):
        self.assertTrue(self.box.app_installed())
        self.bar.apps = set()
        self.clock.now += 30
        self.assertTrue(self.box.app_installed())
        self.clock.now += 30
        self.assertFalse(self.box.app_installed())
        self.assertEqual(self.bar.calls.count("list"), 2)

    def ack(self, run, n, seq):
        self.bar.files[ACK] = json.dumps({"schema": 1, "run": run, "ack": n, "seq": seq}).encode()

    def test_an_ok_press_acknowledges_what_that_write_presented(self):
        ready = crew("ready", "working")
        self.box.sync(ready, [], None, None, ready)
        seq = self.state()["seq"]
        self.assertEqual(self.box.acknowledged(True), [])  # First read: notes there is no press yet.
        self.ack(7, 1, seq)
        self.clock.now += 2
        self.assertEqual(self.box.acknowledged(True), ready)
        self.clock.now += 2
        self.assertEqual(self.box.acknowledged(True), [])  # The same press is not replayed.

    def test_an_old_press_is_not_replayed_after_a_restart(self):
        self.ack(7, 3, 1)
        self.assertEqual(self.box.acknowledged(False), [])  # Read once at start, whatever is acknowledgeable.
        self.clock.now += 2
        self.assertEqual(self.box.acknowledged(True), [])

    def test_ack_is_only_polled_while_something_is_acknowledgeable(self):
        self.box.acknowledged(False)
        reads = self.bar.calls.count("read")
        self.clock.now += 10
        self.box.acknowledged(False)
        self.assertEqual(self.bar.calls.count("read"), reads)

    def test_a_new_launch_with_the_same_count_still_acknowledges(self):
        ready = crew("ready")
        self.box.sync(ready, [], None, None, ready)
        seq = self.state()["seq"]
        self.ack(1, 1, seq)
        self.box.acknowledged(True)
        self.ack(2, 1, seq)
        self.clock.now += 2
        self.assertEqual(self.box.acknowledged(True), ready)

    def test_an_unreadable_ack_file_is_ignored(self):
        self.box.acknowledged(True)
        self.bar.files[ACK] = b"{"
        self.clock.now += 2
        self.assertEqual(self.box.acknowledged(True), [])


if __name__ == "__main__":
    unittest.main()
