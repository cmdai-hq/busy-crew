"""The mailbox to the bar app: what it writes, how often, and how it follows the app. No device is contacted."""
import json
import unittest
import urllib.error

from agentdeck import mailbox
from agentdeck.mailbox import APP, STATE, TEMP, Mailbox, payload
from agentdeck.model import Session


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeBar:
    """A bar's storage API in memory, on the test's clock."""

    def __init__(self, clock):
        self.clock = clock
        self.files = {}
        self.calls = []
        self.fail = None
        self.delays = {}

    def _call(self, name):
        self.calls.append(name)
        self.clock.now += self.delays.get(name, 0)
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


def crew(*states, provider="claude", version="v"):
    return [Session(provider, f"{i:04d}", state, version) for i, state in enumerate(states)]


class MailboxTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.bar = FakeBar(self.clock)
        self.box = Mailbox(self.bar, clock=self.clock, wall=lambda: 1_800_000_000)

    def state(self):
        return json.loads(self.bar.files[STATE])

    def sync(self, sessions, tokens=None, errors=()):
        return self.box.sync(sessions, list(errors), tokens, None, sessions)

    def app(self, run=1, beat=0, running=True, ack=0, seq=0):
        self.bar.files[APP] = json.dumps(
            {"schema": 1, "run": run, "beat": beat, "running": running, "ack": ack, "seq": seq}).encode()

    def later(self, seconds):
        self.clock.now += seconds

    # ---------- state.json ----------

    def test_payload_has_no_ids_and_puts_the_most_urgent_first(self):
        body = payload(crew("ready", "waiting") + crew("working", provider="codex"), ["codex", "elsewhere"],
                       {"claude": 5, "codex": -1}, None)
        self.assertEqual(body["sessions"], [["claude", "waiting"], ["codex", "working"], ["claude", "ready"]])
        self.assertEqual(body["errors"], ["codex"])
        self.assertEqual(body["tokens"], {"claude": 5, "codex": None})
        self.assertNotIn("0000", json.dumps(body))

    def test_writes_whole_files_through_a_rename_with_the_time(self):
        self.assertTrue(self.sync(crew("working")))
        self.assertEqual(self.bar.calls, ["write", "rename"])
        self.assertNotIn(TEMP, self.bar.files)
        self.assertEqual(self.state()["sessions"], [["claude", "working"]])
        self.assertEqual(self.state()["at"], 1_800_000_000)

    def test_never_writes_twice_within_the_minimum_interval(self):
        self.sync(crew("working"))
        self.later(1)
        self.assertFalse(self.sync(crew("ready")))
        self.later(1)
        self.assertTrue(self.sync(crew("ready")))
        self.assertEqual(self.state()["sessions"], [["claude", "ready"]])

    def test_an_unchanged_crew_is_not_rewritten_until_the_heartbeat(self):
        self.sync(crew("idle"))
        seq = self.state()["seq"]
        self.later(59)
        self.assertFalse(self.sync(crew("idle")))
        self.later(1)
        self.assertTrue(self.sync(crew("idle")))
        self.assertEqual(self.state()["seq"], seq + 1)

    def test_token_totals_alone_are_written_at_most_once_a_minute(self):
        self.sync(crew("working"), {"claude": 1})
        self.later(10)
        self.assertFalse(self.sync(crew("working"), {"claude": 2}))
        self.later(50)
        self.assertTrue(self.sync(crew("working"), {"claude": 3}))
        self.assertEqual(self.state()["tokens"]["claude"], 3)

    def test_totals_that_become_known_are_written_without_waiting(self):
        self.sync(crew("working"), {"claude": None, "codex": None})
        self.later(5)
        self.assertTrue(self.sync(crew("working"), {"claude": 7, "codex": 1}))
        self.later(5)
        self.assertFalse(self.sync(crew("working"), {"claude": 8, "codex": 1}))

    def test_a_crew_change_carries_the_latest_tokens(self):
        self.sync(crew("working"), {"claude": 1})
        self.later(5)
        self.sync(crew("ready"), {"claude": 2})
        self.assertEqual(self.state()["tokens"]["claude"], 2)

    def test_a_failure_backs_off_and_a_success_resets(self):
        self.bar.fail = urllib.error.URLError("down")
        self.assertFalse(self.sync(crew("working")))
        self.assertEqual(self.box.error, "URLError")
        self.bar.fail = None
        self.later(1)
        self.assertFalse(self.sync(crew("working")))  # Still backing off: nothing is sent.
        self.assertEqual(self.bar.calls, ["write"])
        self.later(1)
        self.assertTrue(self.sync(crew("working")))
        self.assertIsNone(self.box.error)

    def test_backoff_doubles_up_to_its_ceiling(self):
        self.bar.fail = urllib.error.URLError("down")
        for _ in range(10):
            self.clock.now = self.box.resume_at
            self.sync(crew("working"))
        self.assertEqual(self.box.backoff, mailbox.MAX_BACKOFF)

    def test_a_slow_write_is_reported_even_when_the_rename_is_fast(self):
        self.bar.delays = {"write": 2.5}
        self.assertTrue(self.sync(crew("working")))
        self.assertEqual(self.box.error, "SlowBar")
        self.assertFalse(self.box.ready())

    # ---------- app.json ----------

    def test_no_app_json_means_no_app(self):
        self.assertFalse(self.box.app_running())

    def test_a_running_app_is_followed_until_its_beat_stops(self):
        self.app(beat=0)
        self.assertTrue(self.box.app_running())
        self.later(60)
        self.app(beat=1)
        self.assertTrue(self.box.app_running())
        self.later(mailbox.ALIVE_TIMEOUT + 1)  # Back held, or the bar rebooted: no goodbye.
        self.assertFalse(self.box.app_running())

    def test_an_app_that_quits_says_so(self):
        self.app()
        self.assertTrue(self.box.app_running())
        self.app(running=False)
        self.later(mailbox.READ_RUNNING)
        self.assertFalse(self.box.app_running())

    def test_app_json_is_read_slowly_while_the_app_does_not_run(self):
        self.box.app_running()
        self.later(mailbox.READ_IDLE - 1)
        self.box.app_running()
        self.assertEqual(self.bar.calls.count("read"), 1)

    def test_a_new_launch_rewrites_the_crew_at_once(self):
        self.app(run=1)
        self.box.app_running()
        self.sync(crew("working"))
        self.app(run=2)  # Reinstalled: the app's storage was wiped.
        self.later(mailbox.READ_RUNNING)
        self.box.app_running()
        self.later(mailbox.MIN_INTERVAL)
        self.assertTrue(self.sync(crew("working")))

    def test_an_ok_press_acknowledges_what_that_write_presented(self):
        self.app(ack=4)
        self.box.app_running(True)
        ready = crew("ready", "working")
        self.sync(ready)
        self.app(ack=5, seq=self.state()["seq"])
        self.later(mailbox.READ_ACK)
        self.box.app_running(True)
        self.assertEqual(self.box.acknowledged(), ready)
        self.assertEqual(self.box.acknowledged(), [])  # Once.

    def test_the_ok_count_of_a_launch_is_a_baseline(self):
        self.app(run=1, ack=3)
        self.box.app_running(True)
        self.sync(crew("ready"))
        self.app(run=2, ack=3, seq=self.state()["seq"])
        self.later(mailbox.READ_ACK)
        self.box.app_running(True)
        self.assertEqual(self.box.acknowledged(), [])

    def test_ok_reaches_results_that_replaced_others_without_a_new_write(self):
        self.app()
        self.box.app_running(True)
        self.box.sync(crew("ready", version="r1"), [], None, None, crew("ready", version="r1"))
        seq = self.state()["seq"]
        self.later(5)
        newer = crew("ready", version="r2")  # Same picture, another result: nothing is written.
        self.assertFalse(self.box.sync(newer, [], None, None, newer))
        self.app(ack=1, seq=seq)
        self.later(mailbox.READ_ACK)
        self.box.app_running(True)
        self.assertEqual(self.box.acknowledged(), newer)

    def test_a_malformed_app_json_is_no_app(self):
        for raw in [b"{", b"[]", json.dumps({"schema": 1, "run": 1, "beat": 0, "running": True, "ack": 0,
                                             "seq": [1]}).encode()]:
            self.bar.files[APP] = raw
            self.later(mailbox.READ_IDLE)
            self.assertFalse(self.box.app_running(True), raw)


if __name__ == "__main__":
    unittest.main()
