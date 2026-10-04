"""No network, no hook decisions, no prompt storage; always exit successfully."""
import hashlib
import json
import os
import sys
import time

from .storage import state_dir, write_json

EVENTS = {"SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PostToolUseFailure", "PermissionRequest", "Notification", "Stop", "StopFailure",
          "Elicitation", "ElicitationResult"}


def hook_state(data):
    if data.get("agent_id") or "/subagents/" in data.get("transcript_path", ""):
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
    if event in EVENTS:
        return "working"
    return None


def main():
    try:
        data = json.loads(sys.stdin.buffer.read(2_000_000))
        state = hook_state(data)
        sid = data.get("session_id")
        if state and isinstance(sid, str) and len(sid) <= 200:
            key = hashlib.sha256(sid.encode()).hexdigest()
            write_json(state_dir() / "hooks" / (key + ".json"), {
                "id": sid, "state": state, "at": time.time(), "pid": os.getppid(),
                "event": data.get("hook_event_name")})
    except Exception:
        pass


if __name__ == "__main__":
    main()
