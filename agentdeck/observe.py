import ctypes
import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .hook import valid_snapshot
from .model import Session
from .storage import claude_dir, codex_dir, load_json, write_json

STATES = {"working", "waiting", "ready", "idle", "unknown"}
# Read once per turn: later polls read only what Codex appended (extend_turn).
MAX_ROLLOUT_SCAN = 256_000_000
MAX_RECORD = 2_000_000
CODEX_DATABASES = ("state_5.sqlite", "thread_history_1.sqlite")
# Events that change a turn's state (see rollout_details). Any other oversized record is left out.
TURN_EVENTS = {"task_started", "turn_started", "task_complete", "turn_complete", "turn_aborted", "approval_request",
               "request_user_input", "approval_response", "user_message", "thread_status_changed"}
QUESTION_TOOLS = {"request_user_input", "mcp__request_user_input"}
# The top-level header of a Codex record, anchored at its first byte so a nested object cannot pose as it:
# its kind, then its payload type and the call_id or tool name Codex writes before any content.
RECORD_KIND = re.compile(rb'\{\s*(?:"timestamp"\s*:\s*"[^"\\]*"\s*,\s*)?(?:"ordinal"\s*:\s*\d+\s*,\s*)?'
                         rb'"type"\s*:\s*"([a-z_]+)"\s*,\s*"payload"\s*:')
PAYLOAD_HEAD = re.compile(rb'\s*\{\s*"type"\s*:\s*"([a-z_]+)"\s*(?:,\s*"id"\s*:\s*"[^"\\]*"\s*)?'
                          rb'(?:,\s*"call_id"\s*:\s*"([^"\\]{1,200})"|,\s*"name"\s*:\s*"([^"\\]{1,200})")?')
DECODE = object()


class SourceError(ValueError):
    """A source read failed for a reason this module states itself: safe to log, never a path or content."""
# A finished or failed Codex conversation silent for this long is dead: it leaves the count.
CODEX_STALE = 7200
# xnu bsd/sys/proc_info.h: PROC_PIDTBSDINFO fills a 136-byte proc_bsdinfo.
PROC_PIDTBSDINFO, BSDINFO_SIZE, BSDINFO_PID, BSDINFO_START = 3, 136, 12, 120


def identifier(value):
    if not isinstance(value, str) or not 0 < len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise SourceError("Invalid session/turn identifier")
    return value


def timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise SourceError("Invalid timestamp")
    try:
        if not math.isfinite(value):
            raise SourceError("Invalid timestamp")
    except OverflowError as exc:
        raise SourceError("Invalid timestamp") from exc
    return value


def process_started_at(value):
    try:
        return datetime.strptime(" ".join(value.split()), "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


def readonly_db(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
    except Exception:
        connection.close()
        raise
    return connection


def _libproc():
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    return library


LIBPROC = _libproc()


def process_birth(pid):
    """UTC start second of a live process, queried without forking ps on macOS."""
    if LIBPROC is None:
        os.kill(pid, 0)
        output = subprocess.check_output(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                         env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}, timeout=1)
        return process_started_at(output.decode())
    info = ctypes.create_string_buffer(BSDINFO_SIZE)
    if LIBPROC.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, info, BSDINFO_SIZE) != BSDINFO_SIZE:
        raise OSError(ctypes.get_errno(), "Process information unavailable")
    # The pid field guards against a changed proc_bsdinfo layout.
    if struct.unpack_from("I", info, BSDINFO_PID)[0] != pid:
        raise SourceError("Unexpected process information layout")
    return struct.unpack_from("Q", info, BSDINFO_START)[0]


def pid_matches(pid, started=None):
    try:
        if (isinstance(pid, bool) or not isinstance(pid, int) or not 1 < pid < 2**31
                or not isinstance(started, str) or not started):
            return False
        expected = process_started_at(started)
        return expected is not None and process_birth(pid) == expected
    except (OSError, ValueError, TypeError, OverflowError, UnicodeError, subprocess.SubprocessError):
        return False


def record_json(line):
    if len(line) > MAX_RECORD:
        raise SourceError("Rollout record exceeds size limit")
    try:
        record = json.loads(line)
    except RecursionError as exc:
        raise SourceError("Rollout nesting exceeds parser limit") from exc
    if not isinstance(record, dict) or not isinstance(record.get("payload", {}), dict):
        raise SourceError("Invalid rollout record")
    return record


def triage(head):
    """What a Codex record needs, from its first bytes: None to leave it out, a stand-in, or DECODE.

    Only turn events, questions and their answers change a turn's state. Messages, reasoning, tool
    outputs and token counts are conversation content: they are left out without being decoded. A
    record whose top-level header cannot be read is decoded, so a malformed record still fails.
    """
    kind = RECORD_KIND.match(head[:4096])
    if not kind:
        return DECODE
    kind_name = kind.group(1).decode()
    if kind_name not in {"event_msg", "response_item"}:
        return None
    payload = PAYLOAD_HEAD.match(head[:4096], kind.end())
    if not payload:
        return DECODE
    payload_type, call_id, name = payload.group(1).decode(), payload.group(2), payload.group(3)
    if kind_name == "event_msg":
        if payload_type == "user_message":  # It ends a wait; its text is not needed.
            return {"type": kind_name, "payload": {"type": payload_type}}
        return DECODE if payload_type in TURN_EVENTS else None
    if payload_type == "function_call_output":  # It may answer a question: keep its call_id only.
        return {"type": kind_name, "payload": {"type": payload_type, "call_id": call_id.decode()}} if call_id else DECODE
    if payload_type == "function_call":
        return None if name and name.decode() not in QUESTION_TOOLS else DECODE
    return None


def oversized_record(head):
    """Stand-in for a record over MAX_RECORD, or None to leave it out; fails when it would need decoding."""
    outcome = triage(head)
    if outcome is DECODE:
        raise SourceError("Rollout record exceeds size limit")
    return outcome


def read_turn(path, turn_id):
    """Read backwards to a verified selected-turn boundary, with explicit limits."""
    return read_turn_tail(path, turn_id)[0]


def is_boundary(record, turn_id):
    payload = record.get("payload", {})
    return (record.get("type") == "event_msg" and payload.get("type") in {"task_started", "turn_started"}
            and payload.get("turn_id") == turn_id)


class BoundaryUnavailable(SourceError):
    """The selected turn's start is not in the scanned tail; `end` is where complete lines stop."""

    def __init__(self, end):
        super().__init__("Selected turn boundary unavailable")
        self.end = end


def read_turn_tail(path, turn_id):
    """The selected turn's records, and the offset just past the last complete line read."""
    records, buffer, terminated, oversized, head, complete_end = [], b"", False, False, b"", 0
    with path.open("rb") as handle:
        position = handle.seek(0, 2)
        floor = max(0, position - MAX_ROLLOUT_SCAN)
        while position > floor:
            length = min(65536, position - floor)
            position -= length
            handle.seek(position)
            chunk = handle.read(length)
            if oversized:
                # Reading backwards, only the start of an oversized record is kept: it holds its kind.
                start = chunk.rfind(b"\n")
                if start < 0:
                    head = (chunk + head)[:4096]
                    continue
                stand_in = oversized_record(chunk[start + 1:] + head)
                if stand_in:
                    records.append(stand_in)
                oversized, chunk = False, chunk[:start + 1]
            buffer = chunk + buffer
            if not terminated:
                # Codex may still be appending the last record: parse only complete lines.
                end = buffer.rfind(b"\n")
                if end < 0:
                    buffer = b""
                    continue
                buffer, terminated, complete_end = buffer[:end], True, position + end + 1
            lines = buffer.split(b"\n")
            buffer = lines.pop(0)
            for line in reversed(lines):
                if not line:
                    continue
                outcome = triage(line)
                if outcome is not DECODE:
                    if outcome:
                        records.append(outcome)
                    continue
                if len(line) > MAX_RECORD:
                    raise SourceError("Rollout record exceeds size limit")
                record = record_json(line)
                records.append(record)
                if is_boundary(record, turn_id):
                    return list(reversed(records)), complete_end
            if len(buffer) > MAX_RECORD:
                oversized, head, buffer = True, buffer[:4096], b""
        if floor == 0 and (oversized or len(buffer) > MAX_RECORD):
            oversized_record(head if oversized else buffer)  # The file starts with it: never the boundary.
        elif floor == 0 and buffer:
            outcome = triage(buffer)
            record = record_json(buffer) if outcome is DECODE else outcome
            if record:
                records.append(record)
                if is_boundary(record, turn_id):
                    return list(reversed(records)), complete_end
    raise BoundaryUnavailable(complete_end)


def extend_turn(path, start):
    """Records Codex appended after `start`, a line boundary, and the offset past the last complete line.

    Reading forward only what is new keeps a long turn cheap: its start can be far behind.
    """
    records, data, base, end, skipping, head = [], b"", start, start, False, b""
    with path.open("rb") as handle:
        handle.seek(start)
        while chunk := handle.read(65536):
            data += chunk
            while (newline := data.find(b"\n")) >= 0:
                line, data = data[:newline], data[newline + 1:]
                end = base + newline + 1
                base = end
                if skipping:
                    # The newline that ends an oversized record: only its start, kept, says what it was.
                    skipping, record = False, oversized_record(head)
                elif not line:  # Skipped, as when reading backwards.
                    continue
                elif len(line) > MAX_RECORD:
                    record = oversized_record(line[:4096])
                else:
                    outcome = triage(line)
                    record = record_json(line) if outcome is DECODE else outcome
                if record:
                    records.append(record)
            if not skipping and len(data) > MAX_RECORD:
                skipping, head = True, data[:4096]
            if skipping:
                base, data = base + len(data), b""
    return records, end


def rollout_details(records, turn_id):
    current, terminal, completed_at = None, None, 0
    pending, explicit_wait, boundary, superseded = set(), False, False, False
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("payload", {}), dict):
            raise SourceError("Invalid rollout record")
        payload = record.get("payload", {})
        kind = payload.get("type")
        if record.get("type") == "event_msg":
            if kind in {"task_started", "turn_started"}:
                current = identifier(payload.get("turn_id"))
                root = payload.get("root_turn_id")
                if root is not None:
                    identifier(root)
                if boundary and current != turn_id and root in {None, current}:
                    superseded = True
                boundary |= current == turn_id
            if kind in {"task_complete", "turn_complete", "turn_aborted"} and payload.get("turn_id") == turn_id:
                terminal = "aborted" if kind == "turn_aborted" else ("failed" if payload.get("error") else "completed")
                completed_at = timestamp(payload.get("completed_at") or 0)
                pending.clear()
                explicit_wait = False
            if current == turn_id and terminal is None:
                if kind in {"approval_request", "request_user_input"}:
                    explicit_wait = True
                elif kind in {"approval_response", "user_message"}:
                    explicit_wait = False
                elif kind == "thread_status_changed":
                    flags = payload.get("active_flags", [])
                    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
                        raise SourceError("Invalid active flags")
                    explicit_wait = bool(set(flags) & {"waitingOnApproval", "waitingOnUserInput"})
        elif current == turn_id and terminal is None and record.get("type") == "response_item":
            if kind == "function_call" and payload.get("name") in {"request_user_input", "mcp__request_user_input"}:
                pending.add(identifier(payload.get("call_id")))
            elif kind == "function_call_output":
                pending.discard(payload.get("call_id"))
    return {"terminal": terminal, "waiting": bool(pending or explicit_wait),
            "completed_at": completed_at, "boundary": boundary, "superseded": superseded}


def rollout_state(records, turn_id):
    details = rollout_details(records, turn_id)
    return (False, False) if details["superseded"] else (details["terminal"] == "completed", details["waiting"])


class Observer:
    def __init__(self, runtime, home=None, clock=time.time, alive=pid_matches, claude_root=None, codex_root=None):
        self.runtime = Path(runtime)
        self.home = home or Path.home()
        self.claude_root = Path(claude_root) if claude_root is not None else claude_dir(self.home)
        self.codex_root = Path(codex_root) if codex_root is not None else codex_dir(self.home)
        self.clock, self.alive, self.started = clock, alive, clock()
        self.previous, self.instances, self.tail_cache = {}, {}, {}
        self.errors, self.state_errors = [], set()  # Ledgers that could not be read intact.
        self.causes = {}  # Provider -> why it failed, for the log; never a path or content.
        self.acks = self._load_state("acknowledged.json")
        self.results = self._load_state("completions.json")
        self.results_dirty = False
        for key, version in list(self.acks.items()):
            try:
                self._state_key(key)
                identifier(version)
            except ValueError:
                self.state_errors.add("acknowledged.json")
                self.acks.pop(key)
        for key, result in list(self.results.items()):
            try:
                self._state_key(key)
                if not isinstance(result, dict):
                    raise SourceError("Invalid pending result")
                identifier(result.get("version"))
                timestamp(result.get("completed_at"))
                if result.get("instance") is not None:
                    identifier(result["instance"])
                if self.acks.get(key) == result["version"]:
                    self.results.pop(key)
                    self.results_dirty = True
            except ValueError:
                self.state_errors.add("completions.json")
                self.results.pop(key)
        self.ready = {key: result["version"] for key, result in self.results.items()}
        # A Codex home alone (e.g. CLI-only) does not establish the desktop databases.
        self.known_sources = {"claude": (self.claude_root / "sessions").exists(),
                              "codex": all((self.codex_root / name).exists() for name in CODEX_DATABASES)}

    @staticmethod
    def _state_key(key):
        provider, separator, sid = key.partition(":")
        if provider not in {"claude", "codex"} or not separator:
            raise SourceError("Invalid persisted provider")
        identifier(sid)

    def _load_state(self, name):
        try:
            return load_json(self.runtime / name, {})
        except (OSError, ValueError):
            self.state_errors.add(name)
            return {}

    def _error(self, provider, exc=None):
        if provider not in self.errors:
            self.errors.append(provider)
        if exc is not None and provider not in self.causes:
            # Only this module's own messages are logged; any other error may carry a path or a value.
            self.causes[provider] = str(exc) if isinstance(exc, SourceError) else type(exc).__name__

    def error_summary(self):
        return ", ".join(p + (f" ({self.causes[p]})" if p in self.causes else "") for p in self.errors)

    def _save_results(self):
        self.results_dirty = True
        write_json(self.runtime / "completions.json", self.results)
        self.results_dirty = False
        self.state_errors.discard("completions.json")

    def _save_acks(self, acks):
        write_json(self.runtime / "acknowledged.json", acks)
        self.acks = acks
        self.state_errors.discard("acknowledged.json")

    def _forget(self, key):
        changed = self.results.pop(key, None) is not None
        self.ready.pop(key, None)
        self.previous.pop(key, None)
        self.instances.pop(key, None)
        if self.acks.pop(key, None) is not None:
            self._save_acks(self.acks)
        if changed:
            self._save_results()

    def acknowledge(self, sessions):
        acknowledgements, keys = dict(self.acks), []
        for session in sessions:
            key = session.provider + ":" + session.id
            if session.state == "ready" and self.ready.get(key) == session.version:
                acknowledgements[key] = session.version
                keys.append(key)
        if keys:
            self._save_acks(acknowledgements)
            for key in keys:
                self.results.pop(key, None)
                self.ready.pop(key, None)
            self._save_results()

    def completion(self, provider, sid, state, version, completed_at=0, instance=None):
        key = provider + ":" + sid
        previous = self.previous.get(key)
        result = self.results.get(key)
        if result and instance is not None and result.get("instance") != instance:
            self._forget(key)
            result, previous = None, None
        if state in {"working", "waiting", "unknown"}:
            if result:
                self.results.pop(key)
                self.ready.pop(key, None)
                self._save_results()
        elif state == "ready":
            fresh = completed_at >= self.started or (previous and previous[0] in {"working", "waiting"} and previous[1] == version)
            if result or fresh:
                if self.acks.get(key) != version:
                    new_result = {"version": version, "completed_at": completed_at, "instance": instance}
                    if result != new_result:
                        self.results[key] = new_result
                        self.ready[key] = version
                        self._save_results()
                    state = "ready"
                else:
                    state = "idle"
            else:
                state = "idle"
        elif state == "idle" and result and self.acks.get(key) != result["version"]:
            state, version = "ready", result["version"]
        self.previous[key] = (state, version)
        return Session(provider, sid, state, version)

    def _files(self, path, provider):
        if not path.exists():
            if self.known_sources[provider] or any(key.startswith(provider + ":") for key in self.results):
                raise SourceError("Previously available source is absent")
            return []
        self.known_sources[provider] = True
        if not path.is_dir():
            raise SourceError("Source directory incompatible")
        return sorted(p for p in path.iterdir() if p.suffix == ".json")

    def _hooks(self):
        hooks = {}
        directory = self.runtime / "hooks"
        if not directory.exists():
            return hooks
        for path in sorted(directory.iterdir()):
            if path.suffix != ".json":
                continue
            try:
                data = load_json(path)
                if data is None:  # A concurrent hook cleanup removed the listed file.
                    continue
                sid = identifier(data.get("id"))
                timestamp(data.get("at"))
                if data.get("state") not in STATES | {"closed"}:
                    raise SourceError("Invalid hook state")
                if data.get("schema") == 2 and not valid_snapshot(data):
                    raise SourceError("Invalid hook snapshot")
                if data.get("schema") == 2 and data.get("turn"):
                    identifier(data["turn"])
                elif data.get("schema") is None:
                    data = {key: data[key] for key in ("id", "state", "at")}
                elif data.get("schema") != 2:
                    raise SourceError("Unsupported hook schema")
                hooks[sid] = data
            except (OSError, ValueError, TypeError) as exc:
                self._error("claude", exc)
        return hooks

    def claude(self):
        hooks, result, seen = self._hooks(), [], set()
        files = self._files(self.claude_root / "sessions", "claude")
        for path in files:
            try:
                data = load_json(path)
                if data is None:  # Native session cleanup raced directory enumeration.
                    continue
                sid = identifier(data.get("sessionId"))
                key = "claude:" + sid
                seen.add(key)
                if data.get("kind") not in {None, "interactive", "headless"}:
                    self._forget(key)
                    continue
                pid, started = data.get("pid"), data.get("procStart")
                if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
                    raise SourceError("Invalid native PID")
                if self.alive is pid_matches and (not isinstance(started, str) or not started):
                    raise SourceError("Native process birth unavailable")
                if not self.alive(pid, started):
                    self._forget(key)
                    continue
                instance = str(pid) + ":" + str(started or "")
                old_instance = self.instances.get(key) or self.results.get(key, {}).get("instance")
                changed_instance = old_instance is not None and old_instance != instance
                if changed_instance:
                    self._forget(key)
                self.instances[key] = instance
                native_at = timestamp(data.get("statusUpdatedAt", data.get("updatedAt", 0))) / 1000
                state = {"busy": "working", "idle": "idle", "waiting": "waiting", "needs_input": "waiting"}.get(data.get("status"), "unknown")
                version, completed_at = "native:" + str(native_at), 0
                hook = hooks.get(sid, {})
                hook_at = hook.get("at", 0)
                born = process_started_at(started)
                instance_valid = hook_at >= born if born is not None else (not changed_instance or hook_at >= native_at)
                if 0 <= self.clock() - hook_at < 86400 and instance_valid:
                    if hook.get("state") == "closed" and hook_at >= native_at:
                        self._forget(key)
                        continue
                    if hook.get("turn"):
                        version = hook["turn"] + ":" + str(hook.get("generation", 0))
                    pending = hook.get("pending", {})
                    active_pending = [item for item in pending.values() if
                                      (state == "working" and (item["kind"] != "permission" or item["at"] >= native_at)) or
                                      item["at"] >= native_at]
                    terminal = hook.get("terminal")
                    if any(item.get("ambiguous") and item["at"] >= native_at for item in pending.values()):
                        state = "unknown"
                    elif active_pending:
                        state = "waiting"
                    elif terminal and terminal["outcome"] == "failed":
                        state = "unknown"
                    elif state == "idle" and terminal and native_at >= hook.get("turn_started_at", 0):
                        state, version, completed_at = "ready", terminal["version"], terminal["at"]
                    elif hook.get("schema") != 2 and hook_at >= native_at:
                        # Legacy hook snapshots remain readable while new hooks load.
                        candidate = hook.get("state", state)
                        if candidate in {"waiting", "unknown"}:
                            state = candidate
                        elif candidate == "ready" and state == "idle":
                            state, version, completed_at = "ready", "legacy-stop:" + str(hook_at), hook_at
                    elif state == "idle" and hook.get("turn_started_at", 0) > native_at and hook.get("state") == "working":
                        state = "working"
                result.append(self.completion("claude", sid, state, version, completed_at, instance))
            except (OSError, ValueError, TypeError) as exc:
                self._error("claude", exc)
        # An invalid file may conceal an existing session; retain its ledger for recovery.
        if "claude" not in self.errors:
            for key in list(set(self.previous) | set(self.results) | set(self.acks)):
                if key.startswith("claude:") and key not in seen:
                    self._forget(key)
        return result

    def _turn_details(self, sid, path, stat, version):
        """The selected turn's details, reading only what Codex appended since the last poll when it can."""
        identity = (str(path.resolve()), stat.st_dev, stat.st_ino, version)
        change = (stat.st_mtime_ns, stat.st_size)
        cached = self.tail_cache.get(sid)
        if cached and cached["identity"] == identity and cached["change"] == change:
            return cached["outcome"]
        try:
            if cached and cached["identity"] == identity and cached["end"] is not None and stat.st_size >= cached["end"]:
                more, end = extend_turn(path, cached["end"])
                if cached["records"] is not None:
                    records = cached["records"] + more
                else:  # Still waiting for the turn's start to appear.
                    start = next((index for index, record in enumerate(more) if is_boundary(record, version)), None)
                    records = None if start is None else more[start:]
            else:
                records, end = read_turn_tail(path, version)
            outcome = BoundaryUnavailable(end) if records is None else rollout_details(records, version)
        except BoundaryUnavailable as exc:
            # Later polls read only what is appended, looking for the start: a long turn is not rescanned.
            records, end, outcome = None, exc.end, exc
        except ValueError as exc:
            # An unreadable rollout stays so until it changes: don't rescan it every poll.
            records, end, outcome = None, None, exc
        self.tail_cache[sid] = {"identity": identity, "change": change, "records": records, "end": end, "outcome": outcome}
        return outcome

    def codex(self):
        state_db, history_db = (self.codex_root / name for name in CODEX_DATABASES)
        if not state_db.exists() or not history_db.exists():
            if self.known_sources["codex"] or any(key.startswith("codex:") for key in self.results):
                raise SourceError("Codex databases unavailable or incompatible")
            return []
        self.known_sources["codex"] = True
        result, now, seen = [], self.clock(), set()
        with closing(readonly_db(state_db)) as state, closing(readonly_db(history_db)) as history:
            threads = state.execute("""SELECT id, rollout_path, updated_at FROM threads
                WHERE archived=0 AND agent_path IS NULL AND thread_source='user'
                AND updated_at > ? ORDER BY updated_at DESC LIMIT 200""", (now - CODEX_STALE,)).fetchall()
            threads = list(threads)
            ids = {row["id"] for row in threads}
            # Results and acknowledgements die with their conversation: archived, or silent too
            # long. Saved identities beyond the page are checked in bounded batches, not presumed dead.
            old = [key for key in set(self.results) | set(self.acks) if key.startswith("codex:") and key[6:] not in ids]
            for offset in range(0, len(old), 200):
                keys = old[offset:offset + 200]
                placeholders = ",".join("?" for _ in keys)
                rows = state.execute(f"""SELECT id, rollout_path, updated_at FROM threads WHERE id IN ({placeholders})
                    AND archived=0 AND agent_path IS NULL AND thread_source='user' AND updated_at > ?""",
                    [key[6:] for key in keys] + [now - CODEX_STALE]).fetchall()
                eligible = {row["id"]: row for row in rows}
                for key in keys:
                    sid = key[6:]
                    if sid not in eligible:
                        self._forget(key)
                    elif key in self.results:
                        threads.append(eligible[sid])
            for thread in threads:
                sid = None
                try:
                    sid = identifier(thread["id"])
                    seen.add(sid)
                    turn = history.execute("SELECT turn_id, status, completed_at FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1", (sid,)).fetchone()
                    if turn is None:
                        if "codex:" + sid in self.results:
                            raise SourceError("Previously observed turn unavailable")
                        continue
                    version, status = identifier(turn["turn_id"]), turn["status"]
                    completed_at = turn["completed_at"] or 0
                    if isinstance(completed_at, str):
                        completed_at = float(completed_at)
                    completed_at = timestamp(completed_at)
                    if not isinstance(thread["rollout_path"], str):
                        raise SourceError("Invalid rollout path")
                    path = Path(thread["rollout_path"])
                    stat = path.stat()
                    details = self._turn_details(sid, path, stat, version)
                    if isinstance(details, Exception):
                        raise details.with_traceback(None)
                    completed_at = details["completed_at"] or completed_at
                    freshness = max(stat.st_mtime, timestamp(thread["updated_at"]))
                    if details["superseded"] or details["terminal"] in {"failed", "aborted"} or status in {"failed", "interrupted", "cancelled", "canceled"}:
                        state_name = "unknown"
                    elif details["terminal"] == "completed" or status == "completed":
                        state_name = "ready"
                    elif status == "inProgress":
                        if now - freshness > 900:
                            continue
                        state_name = "waiting" if details["waiting"] else "working"
                    else:
                        state_name = "unknown"
                    # A finished turn ages from its completion, so a later rename cannot revive it.
                    # The last write stands in when no completion time is recorded or a newer turn started.
                    last = completed_at if completed_at and not details["superseded"] else freshness
                    if state_name in {"ready", "unknown"} and now - last > CODEX_STALE:
                        self._forget("codex:" + sid)
                        continue
                    session = self.completion("codex", sid, state_name, version, completed_at)
                    if session.state != "idle":
                        result.append(session)
                except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                    self._error("codex", exc)
            for sid in set(self.tail_cache) - seen:
                self.tail_cache.pop(sid, None)
            for key in list(self.previous):
                if key.startswith("codex:") and key[6:] not in seen:
                    self.previous.pop(key, None)
        return result

    def poll(self):
        sessions, self.errors, self.causes = [], ["state"] if self.state_errors else [], {}
        if self.results_dirty:
            try:
                self._save_results()
            except (OSError, ValueError):
                self._error("state")
        for provider, collect in [("claude", self.claude), ("codex", self.codex)]:
            try:
                sessions.extend(collect())
            except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                self._error(provider, exc)
        return sessions
