import asyncio
import fcntl
import logging
import queue
import signal
import threading
import time
from dataclasses import asdict

from .device import Device
from .model import layout
from .observe import Observer
from .render import render
from .storage import read_json, state_dir, write_json
from .usage import Usage

LOG = logging.getLogger("agentdeck")


class Service:
    def __init__(self, config=None):
        self.runtime = state_dir()
        self.config = config or read_json(self.runtime / "config.json", {})
        self.device = Device(self.config.get("address", "http://10.0.4.20"), self.config.get("token"))
        self.observer = Observer(self.runtime)
        self.usage = Usage(self.config.get("timezone", "Europe/Paris"), self.config.get("ccusage"))
        self.view = "sessions"
        self.paused = False
        self.inputs_connected = False
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.sessions = []
        self.last_control = None

    def command(self, command):
        if command in {"sessions", "tokens"}:
            self.view = command
        elif command == "toggle":
            self.view = "tokens" if self.view == "sessions" else "sessions"
        elif command == "ack":
            self.observer.acknowledge(self.sessions)
        elif command == "pause":
            self.paused = True
        elif command == "resume":
            self.paused = False
        self.last_control = command

    def usage_loop(self):
        while not self.stop.is_set():
            self.usage.refresh()
            self.stop.wait(120)

    async def inputs_loop(self):
        from busylib import AsyncBusyBar
        from busylib.features.input_events import ButtonEvent, EncoderEvent, SelectorEvent, input_events
        while not self.stop.is_set():
            try:
                async with AsyncBusyBar(addr=self.device.address, token=self.device.token, max_retries=0) as bar:
                    async for message in bar.stream_status_ws():
                        self.inputs_connected = True
                        if self.stop.is_set():
                            return
                        if not isinstance(message, dict):
                            continue
                        for event in input_events(message):
                            if isinstance(event, ButtonEvent) and event.is_press:
                                self.events.put({"start": "toggle", "back": "sessions", "ok": "ack"}[event.button])
                            elif isinstance(event, EncoderEvent) and event.delta:
                                self.events.put("tokens" if event.delta > 0 else "sessions")
                            elif isinstance(event, SelectorEvent):
                                self.events.put("pause" if event.position in {"off", "settings"} else "resume")
            except Exception:
                self.inputs_connected = False
                await asyncio.sleep(5)

    def run(self):
        lock = (self.runtime / "service.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Agent Deck is already running")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        inbox = self.runtime / "inbox"
        inbox.mkdir(exist_ok=True, mode=0o700)
        threading.Thread(target=self.usage_loop, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(self.inputs_loop()), daemon=True).start()
        LOG.info("Service started")
        last_frame, last_draw, last_error = None, 0, None
        try:
            while not self.stop.is_set():
                for path in sorted(inbox.glob("*.json")):
                    data = read_json(path, {})
                    self.events.put(data.get("command"))
                    path.unlink(missing_ok=True)
                while not self.events.empty():
                    self.command(self.events.get_nowait())
                self.sessions = self.observer.poll()
                tokens = self.usage.current()
                frame = render(self.sessions, self.view, tokens, int(time.time()) % 2, self.observer.errors)
                xpm = frame.xpm()
                now = time.monotonic()
                device_error = None
                try:
                    if self.paused:
                        if last_frame is not None:
                            self.device.clear()
                            last_frame = None
                    elif xpm != last_frame or now - last_draw >= 5:
                        self.device.draw(frame)
                        last_frame, last_draw = xpm, now
                        frame.save(self.runtime / "front.png")
                except Exception as exc:
                    device_error = type(exc).__name__
                    # Failed frames retry next poll and the device lease expires after 15s.
                    last_frame = None
                error = device_error or (",".join(self.observer.errors) if self.observer.errors else None)
                if error != last_error:
                    LOG.warning("Source/device status: %s", error or "recovered")
                    last_error = error
                mode, provider = layout(self.sessions)
                write_json(self.runtime / "status.json", {
                    "at": time.time(), "sessions": [asdict(s) for s in self.sessions],
                    "layout": mode, "provider": provider, "view": self.view,
                    "tokens": tokens, "tokens_at": self.usage.at, "tokens_error": self.usage.error,
                    "source_errors": self.observer.errors, "device_error": device_error,
                    "inputs_connected": self.inputs_connected, "paused": self.paused,
                    "last_control": self.last_control})
                self.stop.wait(1)
        finally:
            self.stop.set()
            try:
                self.device.clear()
            except Exception:
                pass
            (self.runtime / "status.json").unlink(missing_ok=True)
            lock.close()
            LOG.info("Service stopped")
