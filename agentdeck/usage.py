import json
import shutil
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def today(timezone):
    return datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")


def parse_usage(data, day):
    values = {}
    for row in data.get("daily", []):
        if row.get("period") != day:
            continue
        agents = row.get("agents") or ([row] if row.get("agent") in {"claude", "codex"} else [])
        for agent in agents:
            name, count = agent.get("agent"), agent.get("totalTokens")
            if name in {"claude", "codex"} and isinstance(count, int) and count >= 0:
                values[name] = values.get(name, 0) + count
    if not data.get("daily"):
        return {"claude": 0, "codex": 0}
    return values


class Usage:
    def __init__(self, timezone="Europe/Paris", command=None):
        self.timezone = timezone
        self.command = command or shutil.which("ccusage")
        self.values = {}
        self.day = None
        self.at = 0
        self.error = None

    def refresh(self):
        day = today(self.timezone)
        try:
            if not self.command:
                raise FileNotFoundError("ccusage")
            output = subprocess.check_output([self.command, "daily", "--since", day, "--until", day,
                "--timezone", self.timezone, "--json", "--by-agent", "--offline", "--no-cost"],
                timeout=45, stderr=subprocess.DEVNULL)
            self.values = parse_usage(json.loads(output), day)
            self.day, self.at, self.error = day, time.time(), None
        except (OSError, ValueError, subprocess.SubprocessError):
            self.values, self.error = {}, "ccusage unavailable"
            self.day, self.at = day, time.time()

    def current(self):
        return self.values if self.day == today(self.timezone) and time.time() - self.at < 300 else {}
