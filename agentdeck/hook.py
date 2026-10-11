"""Private status only: no network, approval decisions, or prompt persistence."""
import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .storage import load_json, state_dir, write_json

EVENTS = {"SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PostToolUseFailure", "PermissionRequest", "Notification", "Stop", "StopFailure",
          "Elicitation", "ElicitationResult"}
MAX_INPUT = 2_000_000


def hook_state(data):
    if not isinstance(data, dict):
        return None
    transcript = data.get("transcript_path", "")
    if data.get("agent_id") or (isinstance(transcript, str) and "/subagents/" in transcript):
        return None
    event = data.get("hook_event_name")
    if event == "SessionEnd":
        return "closed"
    if event == "SessionStart":
        return "idle"
    if event == "Stop":
        return "ready"
    if event == "StopFailure":
        return "unknown"
    if event in {"PermissionRequest", "Elicitation"}:
        return "waiting"
    if event == "Notification":
        return "waiting" if data.get("notification_type") == "permission_prompt" else None
    if event == "PreToolUse" and data.get("tool_name") == "AskUserQuestion":
        return "waiting"
    return "working" if event in EVENTS else None


def _interaction(data):
    event = data.get("hook_event_name")
    if event in {"Elicitation", "ElicitationResult"}:
        identifier = data.get("elicitation_id", data.get("request_id"))
        prefix = "elicitation:"
    else:
        identifier = data.get("tool_use_id")
        prefix = "tool:"
    if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
        identifier = str(identifier)
        if 0 < len(identifier) <= 200:
            return prefix + identifier
    return prefix + "unidentified"


def _tool_fingerprint(data, key=b""):
    """Keyed digest of a tool call: matches a permission to its call without storing the input."""
    if not isinstance(data.get("tool_name"), str) or not isinstance(data.get("tool_input"), dict):
        return None
    raw = json.dumps([data["tool_name"], data["tool_input"]], sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, raw, hashlib.sha256).hexdigest() if len(raw) <= MAX_INPUT else None


def fingerprint_key(runtime):
    """Per-installation secret: a stored fingerprint cannot be matched against guessed commands."""
    path = runtime / "fingerprint.key"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    fd, temporary = tempfile.mkstemp(dir=runtime, prefix=".key-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(os.urandom(32))
        try:
            os.link(temporary, path)  # Atomic: the first concurrent hook wins, the others read its key.
        except FileExistsError:
            pass
        return path.read_bytes()
    finally:
        os.unlink(temporary)


def update_snapshot(previous, data, at, key=b""):
    """Reduce per-call status under a lock; resolved IDs reject delayed PreToolUse."""
    state = hook_state(data)
    event = data.get("hook_event_name")
    if state is None:
        return previous
    snapshot = dict(previous) if valid_snapshot(previous) else {}
    pending = dict(snapshot.get("pending", {}))
    resolved = dict(snapshot.get("resolved", {}))
    calls = dict(snapshot.get("calls", {}))
    finished = dict(snapshot.get("finished", {}))
    if event in {"SessionStart", "UserPromptSubmit"}:
        if at < snapshot.get("turn_started_at", 0):
            return snapshot
        # SessionStart may run in the background after the first prompt was submitted.
        if event == "SessionStart" and snapshot.get("turn") and snapshot.get("state") in {"working", "waiting"}:
            return snapshot
        snapshot = {"schema": 2, "id": data["session_id"], "instance": uuid.uuid4().hex,
                    "pending": {}, "resolved": {}, "calls": {}}
        pending, resolved, calls, finished = {}, {}, {}, {}
        if event == "UserPromptSubmit":
            snapshot.update(turn=uuid.uuid4().hex, turn_started_at=at)
    elif at < snapshot.get("turn_started_at", 0):
        return snapshot
    elif not snapshot.get("turn") and state != "closed":
        snapshot.update(turn=uuid.uuid4().hex, turn_started_at=at)

    terminal = snapshot.get("terminal")
    if terminal and at < terminal["at"]:
        return snapshot
    if event in {"Stop", "StopFailure", "SessionEnd"} and at < snapshot.get("activity_at", 0):
        return snapshot

    interaction = _interaction(data)
    if event in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "ElicitationResult"} and interaction in resolved and interaction not in pending:
        return snapshot  # Duplicate delivery cannot reopen a completed response.
    fingerprint = _tool_fingerprint(data, key)
    if event == "PreToolUse" and fingerprint and not interaction.endswith(":unidentified"):
        calls[interaction] = fingerprint
        permission = "permission:" + fingerprint
        matches = [key for key, digest in calls.items() if digest == fingerprint and key not in resolved]
        if len(matches) > 1:
            for key in matches:
                if key in pending and pending[key]["kind"] == "permission":
                    pending[permission] = {**pending.pop(key), "ambiguous": True}
        if permission in pending:
            if len(matches) == 1:
                pending[interaction] = {**pending.pop(permission), "ambiguous": False}
            else:
                pending[permission]["ambiguous"] = True
    if event == "PermissionRequest" and interaction.endswith(":unidentified"):
        matches = [key for key, digest in calls.items() if digest == fingerprint and key not in resolved] if fingerprint else []
        if not matches and fingerprint and finished.get(fingerprint, -1) >= at:
            return snapshot
        interaction = matches[0] if len(matches) == 1 else "permission:" + (fingerprint or "unidentified")
    unidentified = "permission:unidentified"
    if event in {"PermissionRequest", "Elicitation"} or (event == "PreToolUse" and data.get("tool_name") == "AskUserQuestion"):
        if interaction not in resolved:
            kind = "elicitation" if event == "Elicitation" else ("permission" if event == "PermissionRequest" else "question")
            pending[interaction] = {"kind": kind, "at": at,
                                    "ambiguous": interaction.startswith("permission:") or interaction.endswith(":unidentified")}
        if event == "PermissionRequest" and interaction != unidentified:
            pending.pop(unidentified, None)  # The request explains its prompt notification.
    elif event == "Notification":
        if not any(key != unidentified and item["kind"] == "permission" for key, item in pending.items()):
            pending.setdefault(unidentified, {"kind": "permission", "at": at, "ambiguous": True})
    elif event in {"PostToolUse", "PostToolUseFailure", "ElicitationResult"}:
        if not interaction.endswith(":unidentified"):
            pending.pop(interaction, None)
            resolved[interaction] = at
            digest = calls.pop(interaction, None)
            if digest:
                finished[digest] = at
            if digest and digest not in calls.values():
                pending.pop("permission:" + digest, None)
        if event != "ElicitationResult" and pending.get(unidentified, {}).get("at", at) <= at:
            pending.pop(unidentified, None)  # A later tool result means the prompt was answered.
        if snapshot.pop("terminal", None):
            snapshot["generation"] = snapshot.get("generation", 0) + 1
    elif event == "PreToolUse":
        if snapshot.pop("terminal", None):
            snapshot["generation"] = snapshot.get("generation", 0) + 1
    elif event in {"Stop", "StopFailure"}:
        pending.clear()
        snapshot["terminal"] = {"version": snapshot["turn"] + ":" + str(snapshot.get("generation", 0)), "at": at,
                                "outcome": "completed" if event == "Stop" else "failed"}
    elif event == "SessionEnd":
        pending.clear()
        snapshot.pop("terminal", None)

    if state in {"working", "waiting"}:
        snapshot["activity_at"] = max(at, snapshot.get("activity_at", 0))

    if pending and state not in {"closed", "unknown", "ready"}:
        state = "waiting"
    snapshot.update(schema=2, id=data["session_id"], state=state,
                    at=max(at, snapshot.get("at", 0)), event=event,
                    pending=pending, calls=dict(list(calls.items())[-1024:]),
                    finished=dict(sorted(finished.items(), key=lambda pair: pair[1])[-128:]),
                    resolved=dict(sorted(resolved.items(), key=lambda pair: pair[1])[-128:]))
    return snapshot


def valid_snapshot(value):
    """Reject incompatible local snapshots instead of poisoning later hook writes."""
    def valid_time(number):
        try:
            return isinstance(number, (float, int)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0
        except OverflowError:
            return False

    def valid_id(identifier):
        return isinstance(identifier, str) and 0 < len(identifier) <= 200 and not any(ord(c) < 32 for c in identifier)

    if not isinstance(value, dict) or value.get("schema") != 2:
        return False
    if not valid_id(value.get("id")) or not isinstance(value.get("state"), str) or value["state"] not in {"working", "waiting", "ready", "idle", "unknown", "closed"}:
        return False
    if not valid_time(value.get("at")) or not valid_time(value.get("turn_started_at", 0)) or not valid_time(value.get("activity_at", 0)):
        return False
    if "turn" in value and not valid_id(value["turn"]):
        return False
    generation = value.get("generation", 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        return False
    pending, resolved = value.get("pending"), value.get("resolved")
    if not isinstance(pending, dict) or not isinstance(resolved, dict) or len(pending) > 1024 or len(resolved) > 128:
        return False
    calls = value.get("calls", {})
    if not isinstance(calls, dict) or len(calls) > 1024 or any(not isinstance(key, str) or not isinstance(digest, str) or len(digest) != 64 for key, digest in calls.items()):
        return False
    finished = value.get("finished", {})
    if not isinstance(finished, dict) or len(finished) > 128 or any(not isinstance(digest, str) or len(digest) != 64 or not valid_time(at) for digest, at in finished.items()):
        return False
    for key, item in pending.items():
        if not isinstance(key, str) or not isinstance(item, dict) or not isinstance(item.get("kind"), str) or item["kind"] not in {"permission", "question", "elicitation"} or not valid_time(item.get("at")) or not isinstance(item.get("ambiguous", False), bool):
            return False
    if any(not isinstance(key, str) or not valid_time(at) for key, at in resolved.items()):
        return False
    terminal = value.get("terminal")
    return terminal is None or (isinstance(terminal, dict) and isinstance(terminal.get("outcome"), str) and terminal["outcome"] in {"completed", "failed"}
                               and valid_id(terminal.get("version")) and valid_time(terminal.get("at")))


def main(argv=None):
    at = time.time()  # Before stdin/locking: earlier invocations cannot overwrite a new turn.
    try:
        import fcntl

        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--runtime", type=Path)
        args, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return
        data = json.loads(raw)
        state = hook_state(data)
        sid = data.get("session_id") if isinstance(data, dict) else None
        if not state or not isinstance(sid, str) or not 0 < len(sid) <= 200:
            return
        runtime = args.runtime.expanduser().resolve() if args.runtime else state_dir()
        directory = runtime / "hooks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(sid.encode()).hexdigest()
        path = directory / (key + ".json")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(directory / (key + ".lock"), flags, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                previous = load_json(path, {})
            except (OSError, ValueError):
                previous = {}
            write_json(path, update_snapshot(previous, data, at, fingerprint_key(runtime)))
    except (Exception, SystemExit):
        pass


if __name__ == "__main__":
    main()
