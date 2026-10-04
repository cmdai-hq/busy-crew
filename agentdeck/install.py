import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .hook import EVENTS
from .storage import read_json, state_dir, write_json

LABEL = "local.busybar.agent-deck"


def launch_agent_path():
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def hook_command():
    return f"{shlex.quote(sys.executable)} -m agentdeck.hook"


def set_hooks(enabled):
    path = Path.home() / ".claude/settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to replace malformed existing configuration.
    before = path.read_bytes() if path.exists() else b"{}"
    data = json.loads(before)
    hooks = data.setdefault("hooks", {})
    command = hook_command()
    for event in sorted(EVENTS):
        groups = hooks.get(event, [])
        filtered = []
        for group in groups:
            group = dict(group)
            group["hooks"] = [h for h in group.get("hooks", []) if not (
                h.get("type") == "command" and h.get("command", "").endswith(" -m agentdeck.hook"))]
            if group["hooks"]:
                filtered.append(group)
        if enabled:
            filtered.append({"hooks": [{"type": "command", "command": command, "timeout": 2}]})
        if filtered:
            hooks[event] = filtered
        else:
            hooks.pop(event, None)
    after = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    if before != after:
        backup = state_dir() / ("claude-settings-" + str(time.time_ns()) + ".json")
        backup.write_bytes(before); backup.chmod(0o600)
        # Avoid clobbering edits made while we were preparing the change.
        if path.exists() and path.read_bytes() != before:
            raise RuntimeError("Claude settings changed; retry installation")
        write_json(path, data)


def install(address, timezone, hooks=True):
    runtime = state_dir()
    config = read_json(runtime / "config.json", {})
    config.update(address=address, timezone=timezone, ccusage=shutil.which("ccusage"))
    write_json(runtime / "config.json", config)
    if hooks:
        set_hooks(True)
    path = launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    service = {
        "Label": LABEL, "ProgramArguments": [sys.executable, "-m", "agentdeck.cli", "run"],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                 "AGENT_DECK_STATE": str(runtime), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(runtime / "service.log"), "StandardErrorPath": str(runtime / "service.log")}
    domain = "gui/" + str(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path.write_bytes(plistlib.dumps(service)); path.chmod(0o600)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    print("Installed LaunchAgent and Claude status hooks" if hooks else "Installed LaunchAgent")


def uninstall():
    subprocess.run(["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + LABEL],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    launch_agent_path().unlink(missing_ok=True)
    set_hooks(False)
    print("Stopped service and removed only Agent Deck hooks. Private runtime data retained.")
