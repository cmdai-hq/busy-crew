"""New releases on GitHub, and the command that installs one for each way Busy Crew can be installed."""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .storage import load_json, write_json

LOG = logging.getLogger("agentdeck")
RELEASES = "https://api.github.com/repos/cmdai-hq/busy-crew/releases/latest"
CHECK_EVERY = 86400  # One request a day.
# Left by a failed detached menu bar reinstall, so the collector can report it; removed by a successful one.
MENUBAR_FAILED = "menubar-update-failed"
MAX_RESPONSE = 1_000_000


def installed_version():
    try:
        return version("busy-crew")
    except PackageNotFoundError:
        return None


def parse_version(text):
    """`1.2.3` or `v1.2.3` as a comparable tuple; anything else (pre-releases included) is None."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def fetch_latest(current, timeout=10):
    """The latest published release's version. GitHub excludes drafts and pre-releases."""
    request = urllib.request.Request(RELEASES, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"busy-crew/{current}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("Release response exceeds limit")
    release = json.loads(raw)
    if not isinstance(release, dict):
        raise ValueError("Release response is not an object")
    tag = release.get("tag_name")
    if parse_version(tag) is None:
        raise ValueError("Release tag is not a version")
    return ".".join(map(str, parse_version(tag)))


class UpdateCheck:
    """Remembers the last check in the state folder, so restarts do not ask GitHub again."""

    def __init__(self, runtime, current, enabled=True, fetch=fetch_latest):
        self.path = runtime / "update.json"
        self.current = current
        self.enabled = enabled and parse_version(current) is not None
        self.fetch = fetch
        self.latest, self.checked_at = None, None
        if self.enabled:
            try:
                state = load_json(self.path, {})
            except (OSError, ValueError):
                state = {}
            if parse_version(state.get("latest")):
                self.latest = state["latest"]
            if isinstance(state.get("checked_at"), (int, float)) and not isinstance(state["checked_at"], bool):
                self.checked_at = state["checked_at"]

    def due(self, now):
        # A clock set back would otherwise postpone the next check indefinitely.
        return self.enabled and (self.checked_at is None or not 0 <= now - self.checked_at < CHECK_EVERY)

    def check(self, now):
        try:
            self.latest = self.fetch(self.current)
        except Exception as exc:  # Offline or GitHub unavailable: keep what the last check found.
            LOG.warning("Update check failed: %s", type(exc).__name__)
        self.checked_at = now
        try:
            write_json(self.path, {"checked_at": now, "latest": self.latest}, durable=False)
        except OSError:
            LOG.warning("Update check result could not be saved")

    def loop(self, stop):
        while not stop.is_set():
            if self.due(time.time()):
                self.check(time.time())
            stop.wait(3600)

    def available(self):
        """The newer released version, if any."""
        if not self.enabled or parse_version(self.latest) is None:
            return None
        return self.latest if parse_version(self.latest) > parse_version(self.current) else None


def install_mode(prefix=None, package=None):
    """How this copy was installed: "homebrew", "uv", "checkout", or None when unknown."""
    prefix = Path(prefix or sys.prefix)
    package = Path(package or Path(__file__).parent)
    parts = prefix.parts
    # Python reports the Cellar path even when launched through Homebrew's opt link.
    if any(part in {"Cellar", "opt"} and following == "busy-crew" for part, following in zip(parts, parts[1:])):
        return "homebrew"
    if (prefix / "uv-receipt.toml").is_file():
        return "uv"
    if (package.parent / ".git").exists() and (package.parent / "pyproject.toml").is_file():
        return "checkout"
    return None


def brew_wrapper(homebrew_prefix, env_files=None):
    """The wrapper Homebrew insists on being run through (Workbrew's, for one), if any.

    HOMEBREW_FORCE_BREW_WRAPPER is resolved the way Homebrew's bin/brew does it: the environment,
    then /etc/homebrew/brew.env, the prefix's and the user's brew.env, each overriding the previous,
    and /etc's again last when HOMEBREW_SYSTEM_ENV_TAKES_PRIORITY is set. `env_files` replaces the
    (system, prefix, user) paths in tests.
    """
    keys = ("HOMEBREW_FORCE_BREW_WRAPPER", "HOMEBREW_SYSTEM_ENV_TAKES_PRIORITY")
    settings = {key: os.environ[key] for key in keys if key in os.environ}

    def load(path):
        try:
            lines = Path(path).read_text().splitlines()
        except (OSError, UnicodeError):
            return
        for line in lines:
            key, separator, value = line.strip().partition("=")
            if separator and key in keys:
                settings[key] = value

    if env_files is None:
        if os.environ.get("XDG_CONFIG_HOME"):
            user = Path(os.environ["XDG_CONFIG_HOME"]) / "homebrew"
        elif os.environ.get("HOMEBREW_XDG_CONFIG_HOME"):
            user = Path(os.environ["HOMEBREW_XDG_CONFIG_HOME"]) / "homebrew"
        else:
            user = Path.home() / ".homebrew"
        env_files = (Path("/etc/homebrew/brew.env"), Path(homebrew_prefix) / "etc/homebrew/brew.env", user / "brew.env")
    system, prefix, user_file = env_files
    load(system)
    system_first = bool(settings.get("HOMEBREW_SYSTEM_ENV_TAKES_PRIORITY"))
    load(prefix)
    load(user_file)
    if system_first:
        load(system)
    return settings.get("HOMEBREW_FORCE_BREW_WRAPPER") or None


def upgrade_commands(mode, prefix=None, env_files=None):
    """The (name, command) steps that upgrade the package, with the tool found outside PATH too.

    The menu bar app runs them with launchd's minimal PATH. Homebrew first refreshes its taps:
    it may otherwise not see a formula published minutes ago.
    """
    if mode == "homebrew":
        parts = Path(prefix or sys.prefix).parts
        keg = next(i for i, pair in enumerate(zip(parts, parts[1:]))
                   if pair[0] in {"Cellar", "opt"} and pair[1] == "busy-crew")
        homebrew_prefix = Path(*parts[:keg])
        candidates = [brew_wrapper(homebrew_prefix, env_files), shutil.which("brew"), str(homebrew_prefix / "bin" / "brew")]
        steps = [("brew update", ["update"]), ("brew upgrade", ["upgrade", "busy-crew"])]
    elif mode == "uv":
        candidates = [shutil.which("uv"), str(Path.home() / ".local/bin/uv"), "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
        steps = [("uv tool upgrade", ["tool", "upgrade", "busy-crew"])]
    else:
        raise ValueError(f"No package manager for installation mode {mode}")
    tool = next((path for path in candidates if path and os.path.isfile(path) and os.access(path, os.X_OK)), None)
    if tool is None:
        raise RuntimeError(("Homebrew" if mode == "homebrew" else "uv") + " was not found")
    return [(name, [tool, *arguments]) for name, arguments in steps]


def upgrade(runtime):
    """Upgrade the package, then let the new version refresh its installation."""
    import fcntl
    from .install import _load_manifest, _read_plist, interpreter, launch_agent_path, menubar_agent_path
    mode = install_mode()
    if mode == "checkout":
        raise RuntimeError("This copy runs from a git checkout: run `git pull --ff-only && uv sync --locked`, "
                           "then `uv run busy-crew install`")
    if mode is None:
        raise RuntimeError("Upgrade Busy Crew with the tool that installed it, then run `busy-crew install`")
    commands = upgrade_commands(mode)
    with (runtime / "upgrade.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("An update is already running") from None
        manifest = _load_manifest(runtime)
        # Keep the interpreter the installation runs, which `install --python` may have chosen.
        agent = _read_plist(launch_agent_path()) or {}
        arguments = agent.get("ProgramArguments")
        installed = arguments[0] if isinstance(arguments, list) and arguments and isinstance(arguments[0], str) else None
        python = installed or interpreter()
        install = [python, "-I", "-m", "agentdeck.cli", "install", "--python", python]
        if manifest and not manifest["hook_commands"]:
            install.append("--no-claude-hooks")  # Keep a passive installation passive.
        log_path = runtime / "upgrade.log"
        with open(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as log:
            for name, step in [*commands, ("busy-crew install", install)]:
                print("$ " + " ".join(step), file=log, flush=True)
                result = subprocess.run(step, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=1800)
                if result.returncode:
                    raise RuntimeError(f"{name} failed; see {log_path}")
            if menubar_agent_path().exists():
                # Reinstalling the app stops it and everything it started: run this step detached,
                # and record a failure where the collector reports it.
                step = [python, "-I", "-m", "agentdeck.updates", "menubar", python]
                print("$ " + " ".join(step), file=log, flush=True)
                subprocess.Popen(step, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    return mode


def reinstall_menubar(runtime, python=None):
    from .install import install_menubar
    try:
        install_menubar(python)
    except Exception:
        (runtime / MENUBAR_FAILED).touch(mode=0o600)
        raise


if __name__ == "__main__" and sys.argv[1:2] == ["menubar"] and len(sys.argv) <= 3:
    from .storage import state_dir
    reinstall_menubar(state_dir(), sys.argv[2] if len(sys.argv) == 3 else None)
