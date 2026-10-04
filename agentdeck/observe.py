import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .model import Session
from .storage import read_json


def readonly_db(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def pid_matches(pid, started=None):
    try:
        if int(pid) <= 1:
            return False
        os.kill(int(pid), 0)
        if started:
            # Claude stores procStart with UTC ps output, regardless of macOS timezone.
            actual = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                             env={**os.environ, "TZ": "UTC"}, timeout=1).decode().strip()
            return " ".join(actual.split()) == " ".join(str(started).split())
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def read_tail(path, size=2_000_000):
    with path.open("rb") as handle:
        total = handle.seek(0, 2)
        handle.seek(max(0, total - size))
        if total > size:
            handle.readline()  # skip a possibly partial first record
        for line in handle:
            try:
                yield json.loads(line)
            except (ValueError, UnicodeError):
                continue


def rollout_state(records, turn_id):
    """Only inspect the selected root turn; async questions do not block work."""
    current = None
    completed = False
    pending = set()
    explicit_wait = False
    for record in records:
        payload = record.get("payload", {})
        kind = payload.get("type")
        if record.get("type") == "event_msg":
            if kind == "task_started":
                current = payload.get("turn_id")
            if kind == "task_complete" and payload.get("turn_id") == turn_id:
                completed = True
            if current == turn_id:
                if kind in {"approval_request", "request_user_input"}:
                    explicit_wait = True
                elif kind in {"approval_response", "user_message"}:
                    explicit_wait = False
                elif kind == "thread_status_changed":
                    flags = payload.get("active_flags", [])
                    explicit_wait = bool(set(flags) & {"waitingOnApproval", "waitingOnUserInput"})
        elif current == turn_id and record.get("type") == "response_item":
            if kind == "function_call" and payload.get("name") in {"request_user_input", "mcp__request_user_input"}:
                pending.add(payload.get("call_id"))
            elif kind == "function_call_output":
                pending.discard(payload.get("call_id"))
    return completed, bool(pending or explicit_wait)


class Observer:
    def __init__(self, runtime, home=None, clock=time.time, alive=pid_matches):
        self.runtime = runtime
        self.home = home or Path.home()
        self.clock = clock
        self.alive = alive
        self.started = clock()
        self.previous = {}
        self.ready = {}
        self.acks = read_json(runtime / "acknowledged.json", {})
        self.tail_cache = {}
        self.errors = []

    def acknowledge(self, sessions):
        from .storage import write_json
        for session in sessions:
            if session.state == "ready":
                self.acks[session.provider + ":" + session.id] = session.version
        write_json(self.runtime / "acknowledged.json", self.acks)

    def completion(self, provider, sid, state, version, completed_at=0):
        key = provider + ":" + sid
        previous = self.previous.get(key)
        if state in {"working", "waiting"}:
            self.ready.pop(key, None)
        elif state in {"ready", "idle"}:
            if (previous and previous[0] in {"working", "waiting"}) or completed_at >= self.started:
                self.ready[key] = version
            if key in self.ready and self.acks.get(key) != self.ready[key]:
                state = "ready"
                version = self.ready[key]
            elif state == "ready":
                state = "idle"
        self.previous[key] = (state, version)
        return Session(provider, sid, state, version)

    def claude(self):
        hooks = {}
        for path in (self.runtime / "hooks").glob("*.json"):
            data = read_json(path, {})
            if data.get("id"):
                hooks[data["id"]] = data
        result = []
        # Native registry is authoritative for identity and lifetime: dead PIDs never count.
        for path in (self.home / ".claude/sessions").glob("*.json"):
            data = read_json(path, {})
            sid = data.get("sessionId")
            if not sid or data.get("kind") not in {None, "interactive", "headless"}:
                continue
            if not self.alive(data.get("pid", -1), data.get("procStart")):
                continue
            native_at = data.get("statusUpdatedAt", data.get("updatedAt", 0)) / 1000
            state = {"busy": "working", "idle": "idle", "waiting": "waiting", "needs_input": "waiting"}.get(data.get("status"), "unknown")
            version = str(native_at)
            hook = hooks.get(sid, {})
            if hook.get("at", 0) >= native_at and self.clock() - hook.get("at", 0) < 24 * 3600:
                if hook.get("state") == "closed":
                    continue
                state = hook.get("state", state)
                version = str(hook["at"])
            result.append(self.completion("claude", sid, state, version,
                                          hook.get("at", 0) if state == "ready" else 0))
        return result

    def codex(self):
        root = self.home / ".codex"
        state_db, history_db = root / "state_5.sqlite", root / "thread_history_1.sqlite"
        if not state_db.exists() or not history_db.exists():
            return []
        result = []
        now = self.clock()
        with readonly_db(state_db) as state, readonly_db(history_db) as history:
            threads = state.execute("""SELECT id, rollout_path, updated_at FROM threads
                WHERE archived=0 AND agent_path IS NULL AND thread_source='user'
                AND updated_at > ? ORDER BY updated_at DESC LIMIT 200""", (now - 86400,)).fetchall()
            for thread in threads:
                turn = history.execute("""SELECT * FROM thread_turns WHERE thread_id=?
                    ORDER BY rollout_ordinal DESC LIMIT 1""", (thread["id"],)).fetchone()
                if turn is None:
                    continue
                sid, version = thread["id"], turn["turn_id"]
                status = turn["status"]
                path = Path(thread["rollout_path"])
                completed, waiting = False, False
                if path.exists():
                    stat = path.stat()
                    key = (stat.st_mtime_ns, stat.st_size, version)
                    cached = self.tail_cache.get(sid)
                    if not cached or cached[0] != key:
                        cached = (key, rollout_state(read_tail(path), version))
                        self.tail_cache[sid] = cached
                    completed, waiting = cached[1]
                    freshness = max(stat.st_mtime, thread["updated_at"])
                else:
                    freshness = thread["updated_at"]
                if status == "inProgress" and not completed:
                    # Old dangling turns exist in real Codex databases; never call them running.
                    if now - freshness > 900:
                        continue
                    state_name = "waiting" if waiting else "working"
                elif status == "completed" or completed:
                    state_name = "ready"
                else:
                    continue
                completed_at = turn["completed_at"] or 0
                if isinstance(completed_at, str):
                    try:
                        completed_at = float(completed_at)
                    except ValueError:
                        completed_at = 0
                session = self.completion("codex", sid, state_name, version, completed_at)
                if session.state != "idle":
                    result.append(session)
        return result

    def poll(self):
        sessions, self.errors = [], []
        for name, collect in [("claude", self.claude), ("codex", self.codex)]:
            try:
                sessions.extend(collect())
            except (OSError, ValueError, TypeError, sqlite3.Error):
                self.errors.append(name)
                # A failed source yields an explicit unknown marker, not a false zero.
                sessions.append(Session(name, "source-unavailable", "unknown", "error"))
        return sessions
