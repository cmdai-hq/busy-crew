"""Hook retention fixtures use synthetic sessions and the real hook writer."""
import fcntl
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdeck import hook, observe
from agentdeck.observe import Observer
from agentdeck.storage import load_json, write_json


class HookCleanupTests(unittest.TestCase):
    """Exercise retention and concurrent writers without touching real agent state."""

    def setUp(self):
        """Create private fixture roots with a controllable clock and live synthetic PIDs."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.claude = self.root / "claude"
        self.now = 200000.0
        self.observer = Observer(self.runtime, self.root, clock=lambda: self.now,
                                 alive=lambda *_: True, claude_root=self.claude,
                                 codex_root=self.root / "codex", monotonic=lambda: self.now)

    def emit(self, sid, at, event="UserPromptSubmit", **extra):
        """Run the real hook reducer and locked writer with a synthetic stdin event."""
        data = {"session_id": sid, "hook_event_name": event, **extra}
        with patch.object(hook.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(data).encode()))), \
             patch.object(hook.time, "time", return_value=at):
            hook.main(["--runtime", str(self.runtime)])
        return self.runtime / "hooks" / (hashlib.sha256(sid.encode()).hexdigest() + ".json")

    def native(self, sid, state="busy"):
        """Register a synthetic native session independently of its hook snapshot."""
        write_json(self.claude / "sessions" / (sid + ".json"),
                   {"sessionId": sid, "pid": 123, "status": state,
                    "statusUpdatedAt": self.now * 1000})

    def test_expired_snapshot_is_deleted_even_for_registered_session(self):
        """Registration cannot preserve a hook past its maximum retention period."""
        old = self.emit("old", self.now - 86401)
        recent = self.emit("recent", self.now)
        self.native("old")
        self.native("recent")
        self.assertEqual(len(self.observer.poll()), 2)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertNotIn(old.name, self.observer.hook_cache)
        self.assertFalse(self.observer.errors)

    def test_orphan_grace_preserves_recent_and_registered_snapshots(self):
        """Only absent sessions past grace qualify before maximum retention expires."""
        orphan = self.emit("orphan", self.now - 3601)
        recent = self.emit("recent", self.now - 3599)
        live = self.emit("live", self.now - 7200)
        future = self.emit("future", self.now + 1)
        self.native("live")
        self.observer.poll()
        self.assertFalse(orphan.exists())
        for path in (recent, live, future):
            self.assertTrue(path.exists())
        self.assertFalse(self.observer.errors)

    def test_cleanup_retains_snapshots_at_exact_age_boundaries(self):
        """Cleanup requires strictly more than either retention threshold."""
        expired = self.emit("expired", self.now - 86401)
        boundary = self.emit("boundary", self.now - 86400)
        usable = self.emit("usable", self.now - 86399)
        orphan_boundary = self.emit("orphan-boundary", self.now - 3600)
        self.native("expired")
        self.native("boundary")
        self.native("usable")
        self.observer.poll()
        self.assertFalse(expired.exists())
        self.assertTrue(boundary.exists())
        self.assertTrue(usable.exists())
        self.assertTrue(orphan_boundary.exists())

    def test_cleanup_runs_only_once_an_hour(self):
        """Intervening polls populate caches without rereading cleanup candidates."""
        self.observer.poll()
        stale = self.emit("stale", self.now - 86401)
        self.now += 3599
        with patch.object(self.observer, "_hook", wraps=self.observer._hook) as read:
            self.observer.poll()
            self.observer.poll()
        self.assertEqual(read.call_count, 1)  # Cache population only, no cleanup reread.
        self.assertTrue(stale.exists())
        self.now += 1
        self.observer.poll()
        self.assertFalse(stale.exists())
        self.assertNotIn(stale.name, self.observer.hook_cache)

    def test_wall_clock_adjustment_cannot_bypass_hourly_cadence(self):
        """Retention uses wall time, while elapsed cleanup cadence ignores clock jumps."""
        elapsed = [0.0]
        self.observer.monotonic = lambda: elapsed[0]
        self.observer.poll()
        stale = self.emit("stale", self.now - 86401)
        self.now += 86400  # Wall time jumps without an hour of elapsed time.
        self.observer.poll()
        self.assertTrue(stale.exists())
        elapsed[0] += 3600
        self.observer.poll()
        self.assertFalse(stale.exists())

    def test_retention_matrix_with_independent_hook_and_registry_errors(self):
        """Check all age boundaries across registry availability and hook corruption."""
        ages = (-1, 0, 3599, 3600, 3601, 86400, 86401)
        # Expected retention from the policy, indexed by the seven ages above.
        uncertain_or_present = (True, True, True, True, True, True, False)
        proven_absent = (True, True, True, True, False, False, False)
        policies = {
            "initially_absent": uncertain_or_present,
            "empty": proven_absent,
            "live": uncertain_or_present,
            "malformed": uncertain_or_present,
            "missing_identity": uncertain_or_present,
            "disappeared": uncertain_or_present,
            "unreadable": uncertain_or_present,
            "incompatible": uncertain_or_present,
            "vanished_record": uncertain_or_present,
        }
        registry_errors = {"malformed", "missing_identity", "disappeared", "unreadable", "incompatible"}
        case = 0
        for registry, expected_retention in policies.items():
            for age, retain in zip(ages, expected_retention):
                for hook_health in ("healthy", "malformed_json", "invalid_identity"):
                    with self.subTest(registry=registry, age=age, hook_health=hook_health):
                        case += 1
                        home = self.root / str(case)
                        self.runtime, self.claude = home / "runtime", home / "claude"
                        self.now = 200000.0
                        sessions = self.claude / "sessions"
                        if registry != "initially_absent":
                            sessions.mkdir(parents=True)
                        if registry in {"live", "vanished_record"}:
                            self.native("candidate")
                        elif registry == "malformed":
                            (sessions / "broken.json").write_text("{broken")
                        elif registry == "missing_identity":
                            write_json(sessions / "broken.json", {"pid": 123})
                        self.observer = Observer(self.runtime, home, clock=lambda: self.now,
                                                 monotonic=lambda: 0, alive=lambda *_: True,
                                                 claude_root=self.claude, codex_root=home / "codex")
                        if registry == "disappeared":
                            sessions.rmdir()
                        elif registry == "incompatible":
                            sessions.rmdir()
                            sessions.write_text("not a directory")
                        candidate = self.emit("candidate", self.now - age)
                        if hook_health == "malformed_json":
                            self.emit("broken-hook", self.now).write_text("{broken")
                        elif hook_health == "invalid_identity":
                            (self.runtime / "hooks" / "-broken.json").write_text(json.dumps(
                                {"id": "bad\ud800", "state": "idle", "at": self.now - 86401}))
                        iterdir, load = Path.iterdir, observe.load_json

                        def list_files(path):
                            """Inject enumeration failure while leaving hook files accessible."""
                            if registry == "unreadable" and path == sessions:
                                raise PermissionError
                            return iterdir(path)

                        def read_file(path, *args, **kwargs):
                            """Race a native record removal between listing and reading."""
                            if registry == "vanished_record" and path == sessions / "candidate.json":
                                path.unlink()
                            return load(path, *args, **kwargs)

                        with patch.object(Path, "iterdir", list_files), \
                             patch.object(observe, "load_json", side_effect=read_file):
                            self.observer.poll()
                        self.assertEqual(candidate.exists(), retain)
                        self.assertEqual(bool(self.observer.errors), hook_health != "healthy" or registry in registry_errors)
                        if not retain:
                            self.assertNotIn(candidate.name, self.observer.hook_cache)

    def test_busy_writer_is_skipped_without_removing_its_lock(self):
        """A contended real flock defers deletion and keeps the writer's inode stable."""
        stale = self.emit("busy", self.now - 86401)
        lock_path = stale.with_suffix(".lock")
        inode = lock_path.stat().st_ino
        with lock_path.open("a") as writer:
            fcntl.flock(writer, fcntl.LOCK_EX)
            self.observer.poll()  # Real contention with the hook's separate open description.
            self.assertTrue(stale.exists())
            self.assertIn(stale.name, self.observer.hook_cache)
            self.assertFalse(self.observer.errors)
        self.now += 3600
        self.observer.poll()
        self.assertFalse(stale.exists())
        self.assertEqual(lock_path.stat().st_ino, inode)
        self.assertEqual(lock_path.stat().st_size, 0)
        self.emit("busy", self.now)
        self.assertTrue(stale.exists())
        self.assertEqual(lock_path.stat().st_ino, inode)

    def test_snapshot_rewritten_before_lock_acquisition_is_rechecked(self):
        """An eligible cached snapshot cannot authorize deletion of a newer write."""
        stale = self.emit("rewrite", self.now - 86401)
        flock = fcntl.flock

        def rewrite(lock, operation):
            """Inject a real hook write just before cleanup acquires its lock."""
            if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                self.emit("rewrite", self.now, "PreToolUse", tool_name="AskUserQuestion", tool_use_id="question")
            return flock(lock, operation)

        with patch.object(observe.fcntl, "flock", side_effect=rewrite):
            self.observer.poll()
        self.assertTrue(stale.exists())
        self.assertEqual(load_json(stale)["at"], self.now)
        self.native("rewrite")
        self.assertEqual(self.observer.poll()[0].state, "waiting")
        self.assertFalse(self.observer.errors)

    def test_vanished_candidate_is_removed_from_cache(self):
        """A disappearing candidate is harmless and cannot linger in the cache."""
        stale = self.emit("vanish", self.now - 86401)
        read = self.observer._hook
        calls = 0

        def vanish(path):
            """Remove the candidate between its cached read and locked recheck."""
            nonlocal calls
            calls += 1
            if calls == 2:  # Initial cached read, then locked recheck.
                path.unlink()
            return read(path)

        with patch.object(self.observer, "_hook", side_effect=vanish):
            self.observer.poll()
        self.assertNotIn(stale.name, self.observer.hook_cache)
        self.assertFalse(self.observer.errors)

    def test_unreadable_registry_cannot_establish_orphanhood(self):
        """Registry corruption blocks orphan decisions but still permits age expiry."""
        orphan = self.emit("orphan", self.now - 7200)
        expired = self.emit("expired", self.now - 86401)
        sessions = self.claude / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "broken.json").write_text("{broken")
        self.observer.poll()
        self.assertTrue(orphan.exists())
        self.assertFalse(expired.exists())
        self.assertEqual(self.observer.errors, ["claude"])

    def test_disappeared_registry_still_allows_age_expiry(self):
        """A previously available registry cannot block expiry or prove orphanhood."""
        self.native("live")
        self.observer.poll()
        sessions = self.claude / "sessions"
        (sessions / "live.json").unlink()
        sessions.rmdir()
        expired = self.emit("expired", self.now - 86401)
        orphan = self.emit("orphan", self.now - 7200)
        self.now += 3600
        self.assertEqual(self.observer.poll(), [])
        self.assertFalse(expired.exists())
        self.assertTrue(orphan.exists())
        self.assertIn("claude:live", self.observer.previous)
        self.assertEqual(self.observer.errors, ["claude"])
        self.assertEqual(self.observer.error_summary(), "claude (Previously available source is absent)")
        self.native("live")
        self.assertEqual(self.observer.poll()[0].state, "working")
        self.assertFalse(self.observer.errors)

    def test_initially_absent_registry_cannot_establish_orphanhood(self):
        """No registry is initially normal, but cannot prove absence of a hooked session."""
        expired = self.emit("expired", self.now - 86401)
        orphan = self.emit("orphan", self.now - 7200)
        self.assertEqual(self.observer.poll(), [])
        self.assertFalse(expired.exists())
        self.assertTrue(orphan.exists())
        self.assertFalse(self.observer.errors)
        # A successful read of an existing empty registry can now prove absence.
        (self.claude / "sessions").mkdir(parents=True)
        self.now += 3600
        self.assertEqual(self.observer.poll(), [])
        self.assertFalse(orphan.exists())
        self.assertFalse(self.observer.errors)

    def test_registry_enumeration_failure_still_allows_age_expiry(self):
        """Unreadable registry directories disable absence decisions, not age expiry."""
        self.native("live")
        expired = self.emit("expired", self.now - 86401)
        orphan = self.emit("orphan", self.now - 7200)
        iterdir = Path.iterdir

        def unreadable(path):
            """Simulate registry permissions without changing fixture filesystem access."""
            if path == self.claude / "sessions":
                raise PermissionError
            return iterdir(path)

        with patch.object(Path, "iterdir", unreadable):
            self.assertEqual(self.observer.poll(), [])
        self.assertFalse(expired.exists())
        self.assertTrue(orphan.exists())
        self.assertEqual(self.observer.errors, ["claude"])
        self.assertEqual(self.observer.error_summary(), "claude (PermissionError)")
        self.assertNotIn(expired.name, self.observer.hook_cache)

    def test_incompatible_registry_still_allows_age_expiry(self):
        """A file in place of the registry stays an error while expired hooks are pruned."""
        self.claude.mkdir()
        (self.claude / "sessions").write_text("not a directory")
        expired = self.emit("expired", self.now - 86401)
        orphan = self.emit("orphan", self.now - 7200)
        self.assertEqual(self.observer.poll(), [])
        self.assertFalse(expired.exists())
        self.assertTrue(orphan.exists())
        self.assertEqual(self.observer.errors, ["claude"])
        self.assertEqual(self.observer.error_summary(), "claude (Source directory incompatible)")

    def test_unreadable_snapshot_is_retained_and_reported(self):
        """Snapshot corruption remains visible and never proves eligibility to delete."""
        broken = self.emit("broken", self.now - 86401)
        broken.write_text("{broken")
        self.observer.poll()
        self.assertTrue(broken.exists())
        self.assertEqual(self.observer.errors, ["claude"])

    def test_unencodable_hook_identity_cannot_block_other_expiry(self):
        """Malformed Unicode metadata must fail validation before hashing or persistence."""
        expired = self.emit("expired", self.now - 86401)
        broken = self.runtime / "hooks" / "-broken.json"
        broken.write_text(json.dumps({"id": "bad\ud800", "state": "idle", "at": self.now - 86401}))
        self.observer.poll()
        self.assertFalse(expired.exists())
        self.assertTrue(broken.exists())
        self.assertEqual(self.observer.errors, ["claude"])
        self.assertEqual(self.observer.error_summary(), "claude (Invalid session/turn identifier)")

    def test_corrupt_hook_does_not_disable_orphan_cleanup(self):
        """Neither fresh nor cached hook errors obscure a complete registry scan."""
        broken = self.emit("broken", self.now - 7200)
        broken.write_text("{broken")
        orphan = self.emit("orphan", self.now - 7200)
        live = self.emit("live", self.now - 7200)
        recent = self.emit("recent", self.now)
        self.native("live")
        self.observer.poll()
        self.assertFalse(orphan.exists())
        for path in (broken, live, recent):
            self.assertTrue(path.exists())
        self.assertEqual(self.observer.errors, ["claude"])
        # The corrupt hook's cached error persists on the next cleanup pass.
        another = self.emit("another", self.now - 7200)
        self.now += 3600
        self.observer.poll()
        self.assertFalse(another.exists())
        self.assertTrue(broken.exists())
        self.assertEqual(self.observer.errors, ["claude"])

    def test_cleanup_failure_keeps_display_and_reports_degradation(self):
        """Deletion failure preserves native activity and recovers on a later pass."""
        stale = self.emit("live", self.now - 86401)
        self.native("live")
        with patch.object(Path, "unlink", side_effect=PermissionError):
            self.assertEqual(self.observer.poll()[0].state, "working")
        self.assertTrue(stale.exists())
        self.assertEqual(self.observer.errors, ["claude"])
        self.now += 3600
        self.observer.poll()
        self.assertFalse(stale.exists())
        self.assertFalse(self.observer.errors)

    def test_result_ledger_failure_cannot_skip_age_expiry(self):
        """A failed result-ledger write cannot prevent independently eligible cleanup."""
        self.emit("live", self.now)
        self.now += 1
        live = self.emit("live", self.now, "Stop")
        self.native("live", "idle")
        self.assertEqual(self.observer.poll()[0].state, "ready")
        (self.claude / "sessions/live.json").unlink()
        expired = self.emit("expired", self.now - 86401)
        self.now += 3600

        def fail_ledger(path, data, **kwargs):
            """Fail only result persistence, keeping the hook filesystem writable."""
            if path == self.runtime / "completions.json":
                raise PermissionError
            return write_json(path, data, **kwargs)

        with patch.object(observe, "write_json", side_effect=fail_ledger):
            self.assertEqual(self.observer.poll(), [])
        self.assertFalse(expired.exists())
        self.assertTrue(live.exists())  # Exactly at grace, so unrelated metadata is retained.
        self.assertEqual(self.observer.errors, ["claude"])
        self.assertNotIn(expired.name, self.observer.hook_cache)
        self.observer.poll()
        self.assertFalse(self.observer.errors)

    def test_only_canonical_names_are_deleted(self):
        """Unrelated filenames cannot borrow the canonical writer's per-session lock."""
        canonical = self.emit("old", self.now - 86401)
        other = canonical.with_name("unrelated.json")
        canonical.rename(other)
        self.observer.poll()
        self.assertTrue(other.exists())
        self.assertFalse(self.observer.errors)

    def test_legacy_snapshot_uses_same_retention_and_lock(self):
        """Legacy status snapshots expire without removing their synchronization inode."""
        legacy = self.emit("legacy", self.now - 86401)
        write_json(legacy, {"id": "legacy", "state": "ready", "at": self.now - 86401})
        self.observer.poll()
        self.assertFalse(legacy.exists())
        self.assertTrue(legacy.with_suffix(".lock").exists())
        self.assertFalse(self.observer.errors)

    def test_resumed_session_cannot_reuse_acknowledged_result_after_cleanup(self):
        """Prompt and tool resumptions get fresh result identities after snapshot expiry."""
        for resume_event in ("UserPromptSubmit", "PreToolUse"):
            with self.subTest(event=resume_event):
                sid = resume_event
                path = self.emit(sid, self.now)
                self.now += 1
                self.emit(sid, self.now, "Stop")
                self.native(sid, "idle")
                first = next(s for s in self.observer.poll() if s.id == sid)
                self.assertEqual(first.state, "ready")
                self.observer.acknowledge([first])
                self.now += 86401
                self.observer.poll()
                self.assertFalse(path.exists())
                self.assertEqual(load_json(self.runtime / "acknowledged.json")["claude:" + sid], first.version)
                self.emit(sid, self.now, resume_event)
                self.native(sid)
                self.observer.poll()
                self.now += 1
                self.emit(sid, self.now, "Stop")
                self.native(sid, "idle")
                second = next(s for s in self.observer.poll() if s.id == sid)
                self.assertEqual(second.state, "ready")
                self.assertNotEqual(second.version, first.version)
                self.observer.acknowledge([first])
                self.assertEqual(next(s for s in self.observer.poll() if s.id == sid), second)
