import json
import os
import selectors
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

MAX_USAGE_BYTES = 2_000_000


def today(timezone):
    return datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")


def parse_usage(data, day):
    """Read ccusage's unified daily schema; incompatible reports are unknown."""
    if not isinstance(data, dict) or not isinstance(data.get("daily"), list):
        raise ValueError("Invalid ccusage daily report")
    values = {}
    for row in data["daily"]:
        if not isinstance(row, dict) or not isinstance(row.get("period"), str):
            raise ValueError("Invalid ccusage daily row")
        if len(row["period"]) != 10 or date.fromisoformat(row["period"]).isoformat() != row["period"]:
            raise ValueError("Invalid ccusage daily date")
        agents = row.get("agents")
        if agents is None and isinstance(row.get("agent"), str) and row["agent"] in {"claude", "codex"}:
            agents = [row]
        if not isinstance(agents, list):
            raise ValueError("Invalid ccusage agent breakdown")
        for agent in agents:
            if not isinstance(agent, dict) or not isinstance(agent.get("agent"), str):
                raise ValueError("Invalid ccusage agent")
            name, count = agent["agent"], agent.get("totalTokens")
            if type(count) is not int or count < 0:
                raise ValueError("Invalid ccusage token count")
            if row["period"] == day and name in {"claude", "codex"}:
                values[name] = values.get(name, 0) + count
    # A valid empty daily report establishes no usage; an absent agent in a
    # nonempty report does not establish that its data source was available.
    return values if data["daily"] else {"claude": 0, "codex": 0}


def usage_output(command, timeout=45, max_bytes=MAX_USAGE_BYTES):
    """Bound both stdout buffering and process lifetime without a shell."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(command, timeout)
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    if process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, command)
                    return bytes(output)
                if len(output) + len(chunk) > max_bytes:
                    raise ValueError("ccusage output exceeds limit")
                output.extend(chunk)
    finally:
        try:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        finally:
            process.stdout.close()


@dataclass(frozen=True)
class UsageSnapshot:
    totals: tuple = ()
    day: str | None = None
    at: float = 0
    error: str | None = None

    @property
    def values(self):
        return dict(self.totals)


class Usage:
    def __init__(self, timezone="Europe/Paris", command=None):
        ZoneInfo(timezone)  # Reject an invalid timezone before starting workers.
        self.timezone = timezone
        self.command = command or shutil.which("ccusage")
        self._state = UsageSnapshot()
        self._lock = threading.Lock()

    # Retain the public API while publishing each refresh as one immutable state.
    @property
    def values(self):
        return self._state.values

    @values.setter
    def values(self, values):
        with self._lock:
            self._state = replace(self._state, totals=tuple(values.items()))

    @property
    def day(self):
        return self._state.day

    @day.setter
    def day(self, day):
        with self._lock:
            self._state = replace(self._state, day=day)

    @property
    def at(self):
        return self._state.at

    @at.setter
    def at(self, at):
        with self._lock:
            self._state = replace(self._state, at=at)

    @property
    def error(self):
        return self._state.error

    @error.setter
    def error(self, error):
        with self._lock:
            self._state = replace(self._state, error=error)

    def unavailable(self):
        try:
            day = today(self.timezone)
        except Exception:
            day = None
        state = UsageSnapshot(day=day, at=time.time(), error="ccusage unavailable")
        with self._lock:
            self._state = state

    def refresh(self):
        try:
            day = today(self.timezone)
            if not self.command:
                raise FileNotFoundError("ccusage")
            output = usage_output([self.command, "daily", "--since", day, "--until", day,
                "--timezone", self.timezone, "--json", "--by-agent", "--offline", "--no-cost"])
            values = parse_usage(json.loads(output), day)
            state = UsageSnapshot(tuple(values.items()), day, time.time())
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            self.unavailable()
            return
        with self._lock:
            self._state = state

    def snapshot(self):
        with self._lock:
            state = self._state
        try:
            fresh = state.day == today(self.timezone) and 0 <= time.time() - state.at < 300
        except Exception:
            fresh = False
        if not fresh:
            return replace(state, totals=())
        return state

    def current(self):
        return self.snapshot().values
