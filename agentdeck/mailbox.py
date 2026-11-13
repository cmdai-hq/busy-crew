"""The mailbox to Busy Crew's BUSY Bar app: the crew's state goes into the app's storage on the bar.

The protocol, and the limits the bar imposes on it, are in docs/bar-app.md. In short: write only when something
changed, at most every MIN_INTERVAL seconds, whole files only (write, then rename), and back off when the bar slows.
"""
import json
import time
import urllib.error

from .render import ordered_sessions

APP_ID = "app.busycrew.crew"
APPS = "/ext/user_assets"  # One folder per installed JS app.
FOLDER = f"/ext/apps_data/js_apps/{APP_ID}"  # The app's /ext/data, as the HTTP API reaches it (firmware 1.4.1).
STATE, TEMP, ACK = FOLDER + "/state.json", FOLDER + "/state.tmp", FOLDER + "/ack.json"
SCHEMA = 1
MIN_INTERVAL = 2  # A burst of writes hung a bar's web server: never more than one every 2 s.
TOKEN_INTERVAL = 60  # Token totals move constantly; they are not worth a write more than once a minute.
HEARTBEAT = 60  # The app shows the crew as unknown after 180 s without a new seq.
ACK_INTERVAL = 2
INSTALLED_INTERVAL = 60
SLOW = 2  # A request slower than this backs off as if it failed.
MAX_BACKOFF = 30
HISTORY = 8  # Recent writes whose crew an OK press may still refer to.
UNSET = object()


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


class Mailbox:
    def __init__(self, device, clock=time.monotonic):
        self.device = device
        self.clock = clock
        # Starts from the wall clock so a restarted collector never reuses a seq the app has already seen.
        self.seq = int(time.time())
        self.written = None  # The crew part of the last file written
        self.written_tokens = None
        self.last_write = self.last_token_write = float("-inf")
        self.backoff = 0
        self.resume_at = float("-inf")
        self.error = None
        self.installed = None
        self.installed_at = float("-inf")
        self.seen_ack = UNSET
        self.ack_checked = float("-inf")
        self.history = {}  # seq → sessions presented in that write

    def _call(self, function, *args):
        """Runs one request; a failure or a slow answer backs off before the next."""
        started = self.clock()
        try:
            result = function(*args)
        except (OSError, ValueError) as exc:  # URLError and HTTPError are OSErrors.
            self._back_off(type(exc).__name__)
            raise
        if self.clock() - started > SLOW:
            self._back_off("SlowBar")
        else:
            self.backoff, self.error = 0, None
        return result

    def _back_off(self, error):
        self.error = error
        self.backoff = min(max(self.backoff * 2, MIN_INTERVAL), MAX_BACKOFF)
        self.resume_at = self.clock() + self.backoff

    def ready(self):
        return self.clock() >= self.resume_at

    def app_installed(self):
        """Whether the app is on the bar, checked once a minute; None until the bar has answered."""
        now = self.clock()
        if now - self.installed_at >= INSTALLED_INTERVAL and self.ready():
            self.installed_at = now
            try:
                names = self._call(self.device.storage_list, APPS)
                self.installed = APP_ID in (names or ())
            except (OSError, ValueError):
                pass
        return self.installed

    def sync(self, sessions, errors, tokens, month, presented):
        """Writes the state when it is due. Returns True when a write happened."""
        now = self.clock()
        if not self.ready():
            return False
        body = payload(sessions, errors, tokens, month)
        crew = (body["sessions"], body["errors"])
        counts = (body["tokens"], body["month"])
        changed = crew != self.written
        # Totals that just became known go out at once; changing ones wait for the token pace.
        known = self.written_tokens is not None and any(
            v is not None for part in self.written_tokens if part for v in part.values())
        tokens_due = counts != self.written_tokens and (not known or now - self.last_token_write >= TOKEN_INTERVAL)
        if now - self.last_write < MIN_INTERVAL or not (changed or tokens_due or now - self.last_write >= HEARTBEAT):
            return False
        seq = self.seq + 1
        data = json.dumps({**body, "seq": seq}, separators=(",", ":")).encode()
        try:
            self._call(self.device.storage_write, TEMP, data)
            self._call(self.device.storage_rename, TEMP, STATE)
        except (OSError, ValueError):
            return False
        self.seq, self.written, self.written_tokens = seq, crew, counts
        self.last_write = self.last_token_write = now
        self.history[seq] = list(presented)
        for old in sorted(self.history)[:-HISTORY]:
            del self.history[old]
        return True

    def acknowledged(self, acknowledgeable):
        """The sessions an OK press on the bar acknowledged since the last call, or []."""
        now = self.clock()
        # The first read only notes what is there, so an old ack.json is never replayed after a restart.
        if self.seen_ack is not UNSET and (not acknowledgeable or now - self.ack_checked < ACK_INTERVAL):
            return []
        if not self.ready():
            return []
        self.ack_checked = now
        try:
            raw = self._call(self.device.storage_read, ACK)
        except (OSError, ValueError):
            return []
        try:
            data = json.loads(raw) if raw is not None else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            data = None
        key = None if data is None else (data.get("run"), data.get("ack"))
        first, previous, self.seen_ack = self.seen_ack is UNSET, self.seen_ack, key
        if first or key is None or key == previous:
            return []
        return self.history.get(data.get("seq"), [])
