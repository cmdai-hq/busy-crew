import asyncio
import fcntl
import itertools
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .device import DEFAULT_ADDRESS, Device
from .health import code_fingerprint, health
from .model import layout
from .observe import Observer
from .render import render, visible_sessions
from .storage import load_json, state_dir, write_bytes, write_json
from .updates import MENUBAR_FAILED, UpdateCheck, install_mode, installed_version
from .usage import Usage

LOG = logging.getLogger("agentdeck")
MAX_EVENTS = 256
MAX_INBOX_FILES = 128
MAX_COMMAND_BYTES = 65536
MAX_COMMANDS_PER_POLL = 128
# Start / Pause cycles through the views; the scroll wheel steps along them without wrapping.
VIEWS = ["sessions", "tokens", "month"]
COMMANDS = {*VIEWS, "toggle", "next", "previous", "ack", "pause", "resume", "refresh"}
# Seconds between polls. A session at work, or one that just changed, is followed every second.
# At rest the poll still runs often enough for status.json to look alive to `busy-crew status`
# (5 s) and the menu (10 s), and every other poll renews the bar's lease.
ACTIVE_INTERVAL = 1
IDLE_INTERVAL = 2.5
SETTLE = 10  # Seconds at the active pace after what the collector observes changed.
LEASE_RENEWAL = 5  # The bar drops a frame 15 s after it was drawn.
WATCH_INTERVAL = 1  # At rest, how often commands and Claude's files are checked between polls.


def pace(sessions, now, changed_at):
    """Seconds until the next poll: one while a session works or waits, or shortly after a change."""
    if now - changed_at < SETTLE or any(s.state in {"working", "waiting"} for s in sessions):
        return ACTIVE_INTERVAL
    return IDLE_INTERVAL


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except Exception:
            stream.close()
            raise
        return stream


def configure_logging(runtime):
    LOG.setLevel(logging.INFO)
    path = str((runtime / "service.log").resolve())
    for handler in LOG.handlers:
        if isinstance(handler, PrivateRotatingHandler) and handler.baseFilename == path:
            return handler
    handler = PrivateRotatingHandler(path, maxBytes=1_048_576, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    return handler


class Service:
    # Code this process loaded; the menu bar offers a reload when it changes on disk.
    package_root = Path(__file__).parent

    def __init__(self, config=None):
        self.runtime = state_dir()
        self.config = config if config is not None else load_json(self.runtime / "config.json", {})
        self.device = Device(self.config.get("address", DEFAULT_ADDRESS), self.config.get("token"))
        self.observer = Observer(self.runtime, claude_root=self.config.get("claude_dir"),
                                 codex_root=self.config.get("codex_dir"))
        self.usage = Usage(self.config.get("timezone", "Europe/Paris"), self.config.get("ccusage"))
        self.updates = UpdateCheck(self.runtime, installed_version(), self.config.get("update_check", True) is not False)
        self.install_mode = install_mode()
        self.view = "sessions"
        self.paused = False
        self.inputs_connected = False
        self.inputs_error = None
        self.events = queue.Queue(maxsize=MAX_EVENTS)
        self.events_dropped = 0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.usage_running = threading.Lock()
        self.sessions = []
        self.presented_sessions = []
        self.presented_until = 0
        self.last_control = None

    def enqueue(self, command):
        if not isinstance(command, str) or command not in COMMANDS:
            return False
        try:
            self.events.put_nowait(command)
        except queue.Full:
            # Preserve the latest physical intent without blocking the socket.
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events_dropped += 1
            try:
                self.events.put_nowait(command)
            except queue.Full:
                self.events_dropped += 1
                return False
        self.wake.set()
        return True

    def command(self, command):
        if not isinstance(command, str) or command not in COMMANDS:
            return
        if command in VIEWS:
            self.view = command
        elif command == "toggle":
            self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
        elif command in {"next", "previous"}:
            step = 1 if command == "next" else -1
            self.view = VIEWS[min(max(VIEWS.index(self.view) + step, 0), len(VIEWS) - 1)]
        elif command == "ack":
            if not self.paused and time.time() < self.presented_until and self.presented_sessions:
                try:
                    self.observer.acknowledge(self.presented_sessions)
                except (OSError, ValueError) as exc:
                    LOG.warning("Acknowledgement could not be persisted: %s", type(exc).__name__)
        elif command == "refresh":
            # A run in progress already covers this request. Otherwise run ccusage off the main
            # loop so the bar never waits on it; a burst of requests starts one thread at most.
            if self.usage_running.acquire(blocking=False):
                try:
                    threading.Thread(target=self.refresh_usage, args=(True,), daemon=True).start()
                except RuntimeError:
                    self.usage_running.release()
                    LOG.warning("Token refresh could not start")
        elif command == "pause":
            self.paused = True
        elif command == "resume":
            self.paused = False
        self.last_control = command

    def _fingerprint(self):
        try:
            return code_fingerprint(self.package_root)
        except OSError:  # A reinstall can remove a directory mid-scan; retried next interval.
            return None

    def refresh_usage(self, locked=False):
        """Run ccusage once; a request made while a run is in progress joins that run."""
        if not locked and not self.usage_running.acquire(blocking=False):
            return
        try:
            self.usage.refresh()
        except Exception as exc:
            # A worker failure cannot silently disable all future refreshes.
            self.usage.unavailable()
            LOG.warning("Token refresh failed: %s", type(exc).__name__)
        finally:
            self.usage_running.release()

    def usage_loop(self):
        while not self.stop.is_set():
            self.refresh_usage()
            self.stop.wait(120)

    async def retry_wait(self, delay):
        deadline = time.monotonic() + delay
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.2, remaining))

    async def inputs_loop(self):
        from busylib import AsyncBusyBar
        from busylib.features.input_events import ButtonEvent, EncoderEvent, SelectorEvent, input_events
        delay = 1
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                async with AsyncBusyBar(addr=self.device.address, token=self.device.token, max_retries=0) as bar:
                    async for message in bar.stream_status_ws():
                        self.inputs_connected = True
                        self.inputs_error = None
                        if self.stop.is_set():
                            return
                        if not isinstance(message, dict):
                            continue
                        for event in input_events(message):
                            if isinstance(event, ButtonEvent) and event.is_press:
                                self.enqueue({"start": "toggle", "back": "sessions", "ok": "ack"}[event.button])
                            elif isinstance(event, EncoderEvent) and event.delta:
                                self.enqueue("next" if event.delta > 0 else "previous")
                            elif isinstance(event, SelectorEvent):
                                self.enqueue("pause" if event.position in {"off", "settings"} else "resume")
            except Exception as exc:
                self.inputs_error = type(exc).__name__
                LOG.debug("Input stream unavailable: %s", type(exc).__name__)
            finally:
                self.inputs_connected = False
            if self.stop.is_set():
                return
            if time.monotonic() - started >= 30:
                delay = 1
            await self.retry_wait(delay)
            delay = min(delay * 2, 30)

    def inputs_worker(self):
        try:
            asyncio.run(self.inputs_loop())
        except Exception as exc:
            self.inputs_error = type(exc).__name__
            LOG.warning("Input worker stopped: %s", self.inputs_error)
        finally:
            self.inputs_connected = False

    def read_commands(self, inbox):
        for path in itertools.islice(inbox.glob("*.json"), MAX_INBOX_FILES):
            try:
                data = load_json(path, {}, max_bytes=MAX_COMMAND_BYTES)
                if not self.enqueue(data.get("command")):
                    LOG.warning("Discarded invalid runtime command")
            except (OSError, ValueError, TypeError):
                LOG.warning("Discarded unreadable runtime command")
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    LOG.warning("Could not remove runtime command")
        for _ in range(MAX_COMMANDS_PER_POLL):
            try:
                command = self.events.get_nowait()
            except queue.Empty:
                break
            self.command(command)

    def news(self, inbox):
        """What a CLI or menu command, a Claude hook or a Claude session changes on disk: a few stats."""
        marks = []
        for directory in (inbox, self.runtime / "hooks"):
            try:
                marks.append(directory.stat().st_mtime_ns)
            except OSError:
                marks.append(None)
        # Claude Code rewrites its session files in place, which leaves their directory unchanged.
        try:
            with os.scandir(self.observer.claude_root / "sessions") as entries:
                marks += sorted((entry.name, entry.stat().st_mtime_ns) for entry in entries
                                if entry.name.endswith(".json"))
        except OSError:
            marks.append(None)
        return marks

    def rest(self, due, inbox):
        """Wait until `due` (monotonic), or less when a button, a command or a Claude file arrives."""
        watched = self.news(inbox)
        while not self.stop.is_set():
            remaining = due - time.monotonic()
            if remaining <= 0:
                return
            if self.wake.wait(min(WATCH_INTERVAL, remaining)):
                self.wake.clear()
                return
            if self.news(inbox) != watched:
                return

    def run(self):
        lock_path = self.runtime / "service.lock"
        lock = os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")
        lock_path.chmod(0o600)  # A lock created by an earlier version kept the default mode.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise SystemExit("Busy Crew is already running")
        handler = None
        try:
            handler = configure_logging(self.runtime)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: (self.stop.set(), self.wake.set()))
            inbox = self.runtime / "inbox"
            inbox.mkdir(exist_ok=True, mode=0o700)
            threading.Thread(target=self.usage_loop, daemon=True).start()
            threading.Thread(target=self.inputs_worker, daemon=True).start()
            if self.updates.enabled:
                threading.Thread(target=self.updates.loop, args=(self.stop,), daemon=True).start()
            LOG.info("Service started")
            last_frame, last_draw, last_error = None, 0, None
            observed, changed_at = None, 0
            started_at, code_changed, checked_at = time.time(), False, None
            fingerprint = self._fingerprint()
            while not self.stop.is_set():
                now = time.monotonic()
                self.read_commands(inbox)
                self.sessions = self.observer.poll()
                usage = self.usage.snapshot()
                tokens, month = usage.values, usage.month_values
                frame = render(self.sessions, self.view, month if self.view == "month" else tokens,
                               int(time.time()) % 2, self.observer.errors)
                xpm = frame.xpm()
                if checked_at is None:
                    checked_at = now
                elif not code_changed and now - checked_at >= 30:
                    current, checked_at = self._fingerprint(), now
                    if fingerprint is None:
                        fingerprint = current
                    elif current is not None:
                        code_changed = current != fingerprint
                device_error = None
                try:
                    if self.paused:
                        self.presented_sessions = []
                        if last_frame is not None:
                            self.device.clear()
                            last_frame = None
                    else:
                        changed = xpm != last_frame
                        if changed or now - last_draw >= LEASE_RENEWAL:
                            drawn_at = time.time()
                            self.device.draw(frame)
                            self.presented_until = drawn_at + 15
                            last_frame, last_draw = xpm, now
                            if changed:  # A lease renewal resends the same pixels.
                                write_bytes(self.runtime / "front.png", frame.png(), durable=False)
                        self.presented_sessions = visible_sessions(self.sessions, self.view, self.observer.errors)
                except Exception as exc:
                    device_error = type(exc).__name__
                    # Failed frames retry next poll and the device lease expires after 15s.
                    last_frame = None
                    self.presented_sessions = []
                error = device_error or (self.observer.error_summary() if self.observer.errors else None)
                if error != last_error:
                    LOG.warning("Source/device status: %s", error or "recovered")
                    last_error = error
                mode, provider = layout(self.sessions, self.observer.errors)
                status = {
                    "at": time.time(), "sessions": [asdict(s) for s in self.sessions],
                    "layout": mode, "provider": provider, "view": self.view,
                    "tokens": tokens, "tokens_month": month, "tokens_at": usage.at, "tokens_error": usage.error,
                    "source_errors": self.observer.errors, "device_error": device_error,
                    "inputs_connected": self.inputs_connected, "paused": self.paused,
                    "inputs_error": self.inputs_error,
                    "events_dropped": self.events_dropped, "last_control": self.last_control,
                    "pid": os.getpid(), "started_at": started_at, "code_changed": code_changed,
                    "version": self.updates.current, "latest_version": self.updates.available(),
                    "install_mode": self.install_mode,
                    "menubar_update_failed": (self.runtime / MENUBAR_FAILED).exists(),
                    # What OK would acknowledge now, so the menu never offers a silent no-op.
                    "acknowledgeable": sum(s.state == "ready" for s in self.presented_sessions)}
                status["health"] = health(status, time.time(), started_at)
                write_json(self.runtime / "status.json", status, durable=False)
                current = (self.sessions, self.view, self.paused, self.observer.errors, device_error)
                if current != observed:
                    observed, changed_at = current, now
                due = now + pace(self.sessions, now, changed_at)
                if last_frame is not None:  # Displayed: renew the lease on time even at rest.
                    due = min(due, last_draw + LEASE_RENEWAL)
                self.rest(due, inbox)
        finally:
            self.stop.set()
            self.presented_sessions = []
            self.presented_until = 0
            try:
                self.device.clear()
            except Exception:
                pass
            try:
                (self.runtime / "status.json").unlink(missing_ok=True)
            except OSError:
                LOG.warning("Could not remove runtime status")
            finally:
                lock.close()
                LOG.info("Service stopped")
                if handler is not None:
                    LOG.removeHandler(handler)
                    handler.close()
