"""The mailbox to Busy Crew's BUSY Bar app: the crew's state goes into the app's storage on the bar.

The protocol, and the limits the bar imposes on it, are in docs/bar-app.md. In short: write only when something
changed, at most every MIN_INTERVAL seconds, whole files only (write, then rename), back off when the bar slows,
and only while the app shows, by refreshing its app.json, that it is running.
"""
import json
import time

from .render import ordered_sessions

APP_ID = "app.busycrew.crew"
FOLDER = f"/ext/apps_data/js_apps/{APP_ID}"  # The app's /ext/data, as the HTTP API reaches it (firmware 1.4.1).
STATE, TEMP, APP = FOLDER + "/state.json", FOLDER + "/state.tmp", FOLDER + "/app.json"
SCHEMA = 1
MIN_INTERVAL = 2  # A burst of writes hung a bar's web server: never more than one every 2 s.
TOKEN_INTERVAL = 60  # Token totals move constantly; they are not worth a write more than once a minute.
HEARTBEAT = 60  # The app shows the crew as unknown after 180 s without a new seq.
ALIVE_TIMEOUT = 150  # The app refreshes app.json every 60 s while it runs.
# How often app.json is read: soon after OK while something can be acknowledged, quickly while the app runs (so
# Back hands the screen back to the collector), slowly otherwise.
READ_ACK, READ_RUNNING, READ_IDLE = 2, 5, 10
SLOW = 2  # A request slower than this backs off as if it failed.
MAX_BACKOFF = 30
HISTORY = 8  # Recent writes whose crew an OK press may still refer to.


def _counts(values):
    if not isinstance(values, dict):
        return None
    return {name: n if type(n) is int and n >= 0 else None for name in ("claude", "codex") for n in [values.get(name)]}


def payload(sessions, errors, tokens, month):
    """Everything the app needs to draw, without IDs, titles or text."""
    return {
        "schema": SCHEMA,
        "sessions": [[s.provider, s.state] for s in ordered_sessions(sessions)],
        "errors": [name for name in ("claude", "codex", "state") if name in errors],
        "tokens": _counts(tokens),
        "month": _counts(month),
    }


def _app_record(raw):
    """app.json as {run, beat, running, ack, seq}, or None when absent or not ours."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    if not all(type(data.get(key)) is int for key in ("run", "beat", "ack", "seq")) or type(data.get("running")) is not bool:
        return None
    return {key: data[key] for key in ("run", "beat", "running", "ack", "seq")}


class Mailbox:
    def __init__(self, device, clock=time.monotonic, wall=time.time):
        self.device = device
        self.clock = clock
        self.wall = wall
        # Starts from the wall clock so a restarted collector never reuses a seq the app has already seen.
        self.seq = int(wall())
        self.written = None  # The crew part of the last file written
        self.written_tokens = None
        self.last_write = self.last_token_write = float("-inf")
        self.backoff = 0
        self.resume_at = float("-inf")
        self.error = None
        self.app = None  # Last app.json read
        self.beat_at = float("-inf")  # When its beat last changed
        self.app_read_at = float("-inf")
        self.running = False
        self.pending_ack = []
        self.history = {}  # seq → sessions presented in that write

    # ---------- Requests ----------

    def _request(self, function, *args):
        """Runs one request. Raises on failure, after backing off; returns (result, slow)."""
        started = self.clock()
        try:
            result = function(*args)
        except (OSError, ValueError) as exc:  # URLError and HTTPError are OSErrors.
            self._back_off(type(exc).__name__)
            raise
        return result, self.clock() - started > SLOW

    def _settle(self, slow):
        """After a whole exchange: a slow request backs off; a fast exchange clears the error."""
        if slow:
            self._back_off("SlowBar")
        else:
            self.backoff, self.error = 0, None

    def _back_off(self, error):
        self.error = error
        self.backoff = min(max(self.backoff * 2, MIN_INTERVAL), MAX_BACKOFF)
        self.resume_at = self.clock() + self.backoff

    def ready(self):
        return self.clock() >= self.resume_at

    # ---------- The app's side: app.json ----------

    def app_running(self, acknowledgeable=False):
        """Whether the app runs on the bar now, from app.json; read at the pace READ_* allow."""
        now = self.clock()
        pace = READ_ACK if self.running and acknowledgeable else READ_RUNNING if self.running else READ_IDLE
        if now - self.app_read_at >= pace and self.ready():
            self.app_read_at = now
            try:
                raw, slow = self._request(self.device.storage_read, APP)
            except (OSError, ValueError):
                return self.running
            self._settle(slow)
            self._observe(_app_record(raw), now)
        if self.running and now - self.beat_at > ALIVE_TIMEOUT:
            self.running = False  # Quit without saying so: Back held, a reboot, another app.
        return self.running

    def _observe(self, record, now):
        previous, self.app = self.app, record
        if record is None:
            self.running = False
            return
        if previous is None or record["run"] != previous["run"]:
            # A launch, or the first look since the collector started. Its OK count is a baseline,
            # and its storage may be fresh (a reinstall wipes it): the crew is written again.
            self.beat_at = now
            self.written = self.written_tokens = None
        elif record["beat"] != previous["beat"] or record["ack"] != previous["ack"]:
            self.beat_at = now
            if record["ack"] != previous["ack"]:
                self.pending_ack = self.history.get(record["seq"], [])
        self.running = record["running"]

    def acknowledged(self):
        """The sessions an OK press on the bar acknowledged since the last call, or []."""
        sessions, self.pending_ack = self.pending_ack, []
        return sessions

    # ---------- The collector's side: state.json ----------

    def sync(self, sessions, errors, tokens, month, presented):
        """Writes the state when it is due. Returns True when a write happened."""
        now = self.clock()
        if self.written is not None:
            # The same crew may hold other results (a ready session replaced by another): OK must find these.
            self.history[self.seq] = list(presented)
        if not self.ready():
            return False
        body = payload(sessions, errors, tokens, month)
        crew = (body["sessions"], body["errors"])
        counts = (body["tokens"], body["month"])
        known = self.written_tokens is not None and any(
            v is not None for part in self.written_tokens if part for v in part.values())
        # Totals that just became known go out at once; changing ones wait for the token pace.
        tokens_due = counts != self.written_tokens and (not known or now - self.last_token_write >= TOKEN_INTERVAL)
        if now - self.last_write < MIN_INTERVAL or not (crew != self.written or tokens_due or now - self.last_write >= HEARTBEAT):
            return False
        seq = self.seq + 1
        data = json.dumps({**body, "seq": seq, "at": int(self.wall())}, separators=(",", ":")).encode()
        try:
            _, slow_write = self._request(self.device.storage_write, TEMP, data)
            _, slow_rename = self._request(self.device.storage_rename, TEMP, STATE)
        except (OSError, ValueError):
            return False
        self._settle(slow_write or slow_rename)
        self.seq, self.written, self.written_tokens = seq, crew, counts
        self.last_write = self.last_token_write = now
        self.history[seq] = list(presented)
        for old in sorted(self.history)[:-HISTORY]:
            del self.history[old]
        return True
