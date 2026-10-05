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

from .device import DEFAULT_ADDRESS, Device
from .model import layout
from .observe import Observer
from .render import render, visible_sessions
from .storage import load_json, state_dir, write_json
from .usage import Usage

LOG = logging.getLogger("agentdeck")
MAX_EVENTS = 256
MAX_INBOX_FILES = 128
MAX_COMMAND_BYTES = 65536
MAX_COMMANDS_PER_POLL = 128
COMMANDS = {"sessions", "tokens", "toggle", "ack", "pause", "resume"}


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
    def __init__(self, config=None):
        self.runtime = state_dir()
        self.config = config if config is not None else load_json(self.runtime / "config.json", {})
        self.device = Device(self.config.get("address", DEFAULT_ADDRESS), self.config.get("token"))
        self.observer = Observer(self.runtime, claude_root=self.config.get("claude_dir"),
                                 codex_root=self.config.get("codex_dir"))
        self.usage = Usage(self.config.get("timezone", "Europe/Paris"), self.config.get("ccusage"))
        self.view = "sessions"
        self.paused = False
        self.inputs_connected = False
        self.inputs_error = None
        self.events = queue.Queue(maxsize=MAX_EVENTS)
        self.events_dropped = 0
        self.stop = threading.Event()
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
        return True

    def command(self, command):
        if not isinstance(command, str) or command not in COMMANDS:
            return
        if command in {"sessions", "tokens"}:
            self.view = command
        elif command == "toggle":
            self.view = "tokens" if self.view == "sessions" else "sessions"
        elif command == "ack":
            if not self.paused and time.time() < self.presented_until and self.presented_sessions:
                try:
                    self.observer.acknowledge(self.presented_sessions)
                except (OSError, ValueError) as exc:
                    LOG.warning("Acknowledgement could not be persisted: %s", type(exc).__name__)
        elif command == "pause":
            self.paused = True
        elif command == "resume":
            self.paused = False
        self.last_control = command

    def usage_loop(self):
        while not self.stop.is_set():
            try:
                self.usage.refresh()
            except Exception as exc:
                # A worker failure cannot silently disable all future refreshes.
                self.usage.unavailable()
                LOG.warning("Token refresh failed: %s", type(exc).__name__)
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
                                self.enqueue("tokens" if event.delta > 0 else "sessions")
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

    def run(self):
        lock = (self.runtime / "service.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise SystemExit("Agent Deck is already running")
        handler = None
        try:
            handler = configure_logging(self.runtime)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop.set())
            inbox = self.runtime / "inbox"
            inbox.mkdir(exist_ok=True, mode=0o700)
            threading.Thread(target=self.usage_loop, daemon=True).start()
            threading.Thread(target=self.inputs_worker, daemon=True).start()
            LOG.info("Service started")
            last_frame, last_draw, last_error = None, 0, None
            while not self.stop.is_set():
                self.read_commands(inbox)
                self.sessions = self.observer.poll()
                usage = self.usage.snapshot()
                tokens = usage.values
                frame = render(self.sessions, self.view, tokens, int(time.time()) % 2, self.observer.errors)
                xpm = frame.xpm()
                now = time.monotonic()
                device_error = None
                try:
                    if self.paused:
                        self.presented_sessions = []
                        if last_frame is not None:
                            self.device.clear()
                            last_frame = None
                    else:
                        if xpm != last_frame or now - last_draw >= 5:
                            drawn_at = time.time()
                            self.device.draw(frame)
                            self.presented_until = drawn_at + 15
                            last_frame, last_draw = xpm, now
                            frame.save(self.runtime / "front.png")
                        self.presented_sessions = visible_sessions(self.sessions, self.view, self.observer.errors)
                except Exception as exc:
                    device_error = type(exc).__name__
                    # Failed frames retry next poll and the device lease expires after 15s.
                    last_frame = None
                    self.presented_sessions = []
                error = device_error or (",".join(self.observer.errors) if self.observer.errors else None)
                if error != last_error:
                    LOG.warning("Source/device status: %s", error or "recovered")
                    last_error = error
                mode, provider = layout(self.sessions, self.observer.errors)
                write_json(self.runtime / "status.json", {
                    "at": time.time(), "sessions": [asdict(s) for s in self.sessions],
                    "layout": mode, "provider": provider, "view": self.view,
                    "tokens": tokens, "tokens_at": usage.at, "tokens_error": usage.error,
                    "source_errors": self.observer.errors, "device_error": device_error,
                    "inputs_connected": self.inputs_connected, "paused": self.paused,
                    "inputs_error": self.inputs_error,
                    "events_dropped": self.events_dropped, "last_control": self.last_control})
                self.stop.wait(1)
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
