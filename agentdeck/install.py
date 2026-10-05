"""Recoverable macOS installation; never claim an advisory lock protects editors."""
import copy
import ctypes
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .hook import EVENTS
from .storage import claude_dir, codex_dir, load_json, state_dir, write_bytes

LABEL = "local.busybar.agent-deck"
MANIFEST = "installation.json"


def launch_agent_path():
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def hook_command(runtime=None):
    runtime = (runtime or state_dir()).absolute()
    return f"{shlex.quote(sys.executable)} -I -m agentdeck.hook --runtime {shlex.quote(str(runtime))}"


@contextmanager
def _installation_lock(runtime):
    import fcntl
    with (runtime / "installation.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Agent Deck installation operation is running") from exc
        yield


def _read_bytes(path):
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symbolic link: {path}")
    try:
        with path.open("rb") as source:
            raw = source.read(2_000_001)
    except FileNotFoundError:
        return None
    if len(raw) > 2_000_000:
        raise ValueError(f"Installation file exceeds 2000000 bytes: {path}")
    return raw


def _same_bytes(path, expected):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as source:
        return source.read(len(expected) + 1) == expected


def _json_bytes(data):
    raw = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    if len(raw) > 2_000_000:
        raise ValueError("Installation JSON exceeds 2000000 bytes")
    return raw


def _exchange(first, second):
    """Atomically retain the displaced file, including a late external edit.

    Both paths must be on the same filesystem. Unsupported filesystems fail
    without falling back to a replacement that would discard the old inode.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(first), os.fsencode(second), 2)  # RENAME_SWAP
    else:  # Allows isolated regression tests on Linux; install itself is macOS-only.
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise RuntimeError("Atomic settings exchange is unavailable on this platform")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(first), -100, os.fsencode(second), 2)  # RENAME_EXCHANGE
    function.restype = ctypes.c_int
    if function(*args) != 0:
        code = ctypes.get_errno()
        raise OSError(code, "Atomic settings exchange failed", str(second))


def _recovery_dir(path, runtime):
    # A custom runtime can live on a different volume. Exchange still requires
    # a same-volume staging file; keep that recovery directory private too.
    directory = runtime / "recovery"
    if path.parent.stat().st_dev != runtime.stat().st_dev:
        directory = path.parent / ".agent-deck-recovery"
    if directory.is_symlink():
        raise ValueError(f"Refusing a symbolic recovery directory: {directory}")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    return directory


@dataclass
class FileChange:
    path: Path
    before: bytes | None
    after: bytes | None
    runtime: Path

    def rollback(self):
        if self.before is not None:
            _replace_preserving(self.path, self.before, self.after, self.runtime)
            return
        _remove_preserving(self.path, self.after, self.runtime)


def _remove_preserving(path, expected, runtime):
    if expected is None:
        return None
    if _read_bytes(path) != expected:
        raise RuntimeError(f"File changed; retry the operation: {path}")
    recovery = _recovery_dir(path, runtime)
    fd, filename = tempfile.mkstemp(prefix=path.name + "-", suffix=".backup", dir=recovery)
    os.close(fd)
    preserved = Path(filename)
    os.rename(path, preserved)
    if not _same_bytes(preserved, expected):
        try:
            os.link(preserved, path, follow_symlinks=False)
        except FileExistsError:
            pass
        raise RuntimeError(f"Concurrent edit during removal; recovery: {preserved}")
    preserved.chmod(0o600)
    return FileChange(path, expected, None, runtime)


def _replace_preserving(path, after, expected, runtime):
    """Commit only an expected version, keeping the actual displaced inode.

    Atomic exchange closes the previous check/replace loss window. External
    writers can still edit after a commit, so this is conflict recovery rather
    than a promise to serialize arbitrary editors.
    """
    if _read_bytes(path) != expected:
        raise RuntimeError(f"File changed; retry the operation: {path}")
    if after == expected:
        return None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    recovery = _recovery_dir(path, runtime)
    fd, temporary = tempfile.mkstemp(prefix=path.name + "-", suffix=".backup", dir=recovery)
    staged = Path(temporary)
    committed = False
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(after)
            output.flush()
            os.fsync(output.fileno())
        if expected is None:
            # Exclusive creation prevents losing a file created after preflight.
            os.link(staged, path)
            staged.unlink()
            committed = True
        else:
            _exchange(staged, path)
            committed = True
            if not _same_bytes(staged, expected):
                try:
                    _exchange(staged, path)
                except OSError as exc:
                    raise RuntimeError(f"Concurrent edit could not be restored; recovery: {recovery}") from exc
                # staged now retains the proposed version or another late edit.
                write_bytes(recovery / (staged.name + ".initial"), expected)
                raise RuntimeError(f"Concurrent edit detected; original restored, recovery: {recovery}")
            staged.chmod(0o600)
        return FileChange(path, expected, after, runtime)
    finally:
        if not committed:
            staged.unlink(missing_ok=True)


def _load_manifest(runtime):
    manifest = load_json(runtime / MANIFEST, {})
    if not manifest:
        return {}
    if manifest.get("version") != 1 or not isinstance(manifest.get("hook_commands"), list):
        raise ValueError("Unsupported Agent Deck installation manifest")
    if any(not isinstance(command, str) for command in manifest["hook_commands"]):
        raise ValueError("Invalid hook commands in installation manifest")
    settings = manifest.get("hook_settings")
    if not isinstance(settings, str) or not Path(settings).is_absolute():
        raise ValueError("Invalid Claude settings path in installation manifest")
    return manifest


def _legacy_commands(plist):
    # Explicit migration only for the interpreter in our own LaunchAgent.
    # A shell command merely mentioning the module is never a migration match.
    if not plist or plist.get("Label") != LABEL:
        return set()
    args = plist.get("ProgramArguments", [])
    if not isinstance(args, list) or not args or not all(isinstance(arg, str) for arg in args):
        return set()
    if args[1:] not in [["-m", "agentdeck.cli", "run"], ["-I", "-m", "agentdeck.cli", "run"]]:
        return set()
    return {f"{shlex.quote(args[0])} -m agentdeck.hook"}


def _plan_one_hooks(path, enabled, owned, runtime):
    before = _read_bytes(path)
    data = load_json(path, {})
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"Claude hooks must be an object: {path}")
    updated = copy.deepcopy(data)
    changed_hooks = copy.deepcopy(hooks)
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"Claude hook groups must be lists: {path}")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Invalid Claude hook group: {path}")
            if any(not isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError(f"Invalid Claude hook handler: {path}")
            if any(handler.get("type") == "command" and not isinstance(handler.get("command"), str)
                   for handler in group["hooks"]):
                raise ValueError(f"Invalid Claude command hook: {path}")
        filtered = []
        for group in groups:
            remaining = [handler for handler in group["hooks"] if not (
                handler.get("type") == "command" and handler.get("command") in owned)]
            if remaining or not group["hooks"]:
                replacement = copy.deepcopy(group)
                replacement["hooks"] = remaining
                filtered.append(replacement)
        if filtered:
            changed_hooks[event] = filtered
        else:
            changed_hooks.pop(event, None)
    if enabled:
        command = hook_command(runtime)
        for event in sorted(EVENTS):
            changed_hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": command, "timeout": 2}]})
    if changed_hooks or "hooks" in data:
        updated["hooks"] = changed_hooks
    if updated == data:
        return None
    return path, before, _json_bytes(updated)


def _hook_plans(enabled, runtime, manifest, old_plist=None, root=None):
    target = (root or claude_dir()) / "settings.json"
    owned = set(manifest.get("hook_commands", []))
    old_target = Path(manifest.get("hook_settings", str(target)))
    if not manifest:
        owned.update(_legacy_commands(old_plist))
    new_manifest = {"version": 1, "hook_settings": str(target),
                    "hook_commands": [hook_command(runtime)] if enabled else []}
    if not enabled and not owned:
        # A first passive installation has no reason to inspect or rewrite
        # another application's settings (including symlinks or broken JSON).
        return [], new_manifest
    plans = []
    if old_target != target:
        old_plan = _plan_one_hooks(old_target, False, owned, runtime)
        if old_plan:
            plans.append(old_plan)
    plan = _plan_one_hooks(target, enabled, owned, runtime)
    if plan:
        plans.append(plan)
    return plans, new_manifest


def _recorded_root(manifest):
    # Later hook operations follow the installed settings file, not the caller's environment.
    return Path(manifest["hook_settings"]).parent if manifest else None


def _apply_plans(plans, runtime, changes):
    for path, before, after in plans:
        change = _replace_preserving(path, after, before, runtime)
        if change:
            changes.append(change)


def _rollback(changes):
    errors = []
    for change in reversed(changes):
        try:
            change.rollback()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(str(exc))
    return errors


def set_hooks(enabled):
    runtime = state_dir()
    with _installation_lock(runtime):
        manifest = _load_manifest(runtime)
        old_plist = _read_plist(launch_agent_path())
        plans, updated = _hook_plans(enabled, runtime, manifest, old_plist, _recorded_root(manifest))
        manifest_path = runtime / MANIFEST
        before = _read_bytes(manifest_path)
        changes = []
        try:
            _apply_plans(plans, runtime, changes)
            if updated != manifest:
                _apply_plans([(manifest_path, before, _json_bytes(updated))], runtime, changes)
        except Exception:
            errors = _rollback(changes)
            if errors:
                raise RuntimeError("Hook update rollback incomplete; recovery copies retained: " + "; ".join(errors))
            raise


def _read_plist(path):
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        data = plistlib.loads(raw)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise ValueError(f"Invalid LaunchAgent: {path}") from exc
    if not isinstance(data, dict) or data.get("Label") != LABEL:
        raise ValueError(f"LaunchAgent is not owned by Agent Deck: {path}")
    return data


def _preflight(address, timezone, *, imports=True):
    if sys.platform != "darwin":
        raise RuntimeError("Agent Deck installation is supported only on macOS")
    launchctl = shutil.which("launchctl")
    if not launchctl:
        raise RuntimeError("launchctl is unavailable")
    if not Path(sys.executable).is_file() or not os.access(sys.executable, os.X_OK):
        raise RuntimeError("Python interpreter is unavailable")
    if address is not None:
        _validate_address(address)
    if timezone is not None:
        ZoneInfo(timezone)
    # Import only: this does not instantiate a service or contact an appliance.
    if imports:
        subprocess.run([sys.executable, "-I", "-c", "import agentdeck.cli, busylib; from PIL import Image"],
                       check=True, capture_output=True, timeout=10)
    return launchctl


def _validate_address(address):
    if not isinstance(address, str):
        raise ValueError("BUSY Bar address must be an HTTP or HTTPS origin")
    parsed = urlsplit(address)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("BUSY Bar address must be an HTTP or HTTPS origin")
    parsed.port  # Validate the port too.


def _loaded(launchctl, target):
    result = subprocess.run([launchctl, "print", target], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        return True
    # 113 is launchctl's "Could not find specified service"; its wording varies by release.
    output = (result.stdout + result.stderr).lower()
    if result.returncode == 113 or "could not find service" in output or "service not found" in output:
        return False
    raise RuntimeError("Unable to determine the LaunchAgent state")


def _stop(launchctl, target):
    result = subprocess.run([launchctl, "bootout", target], capture_output=True, text=True, timeout=10)
    if _loaded(launchctl, target):
        raise RuntimeError(f"LaunchAgent did not stop (launchctl exit {result.returncode})")


def _bootstrap(launchctl, domain, path):
    subprocess.run([launchctl, "bootstrap", domain, str(path)], check=True, capture_output=True, timeout=10)
    if not _loaded(launchctl, domain + "/" + LABEL):
        raise RuntimeError("LaunchAgent was not loaded after bootstrap")


def install(address=None, timezone=None, hooks=True):
    launchctl = _preflight(address, timezone)
    runtime = state_dir()
    with _installation_lock(runtime):
        config_path = runtime / "config.json"
        config_before = _read_bytes(config_path)
        config = load_json(config_path, {})
        config = dict(config)
        config["address"] = address if address is not None else config.get("address", "http://busybar.local")
        config["timezone"] = timezone if timezone is not None else config.get("timezone", "Europe/Paris")
        _validate_address(config["address"])
        ZoneInfo(config["timezone"])
        if config.get("token") is not None and not isinstance(config["token"], str):
            raise ValueError("BUSY token must be a string")
        config.setdefault("ccusage", shutil.which("ccusage"))
        roots = {}
        for key, variable, default in [("claude_dir", "CLAUDE_CONFIG_DIR", claude_dir),
                                        ("codex_dir", "CODEX_HOME", codex_dir)]:
            value = os.environ.get(variable, config.get(key, str(default())))
            if not isinstance(value, str) or not value:
                raise ValueError(f"Invalid source directory: {key}")
            roots[key] = str(Path(value).expanduser().absolute())
        config.update(roots)
        path = launch_agent_path()
        plist_before = _read_bytes(path)
        old_plist = _read_plist(path)
        manifest = _load_manifest(runtime)
        hook_plans, updated = _hook_plans(hooks, runtime, manifest, old_plist, Path(config["claude_dir"]))
        manifest_path = runtime / MANIFEST
        manifest_before = _read_bytes(manifest_path)
        service = {"Label": LABEL, "ProgramArguments": [sys.executable, "-I", "-m", "agentdeck.cli", "run"],
                   "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                   "WorkingDirectory": str(runtime),
                   "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "AGENT_DECK_STATE": str(runtime), "CLAUDE_CONFIG_DIR": config["claude_dir"],
                        "CODEX_HOME": config["codex_dir"], "PYTHONUNBUFFERED": "1"},
                   "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
        domain = "gui/" + str(os.getuid())
        target = domain + "/" + LABEL
        was_loaded = _loaded(launchctl, target)
        changes, start_attempted = [], False
        try:
            _apply_plans([(config_path, config_before, _json_bytes(config))] + hook_plans, runtime, changes)
            if was_loaded:
                _stop(launchctl, target)
            _apply_plans([(path, plist_before, plistlib.dumps(service)),
                          (manifest_path, manifest_before, _json_bytes(updated))], runtime, changes)
            start_attempted = True
            _bootstrap(launchctl, domain, path)
        except Exception as exc:
            rollback_errors = []
            try:
                if start_attempted and _loaded(launchctl, target):
                    _stop(launchctl, target)
            except Exception as stop_error:
                rollback_errors.append(str(stop_error))
            rollback_errors.extend(_rollback(changes))
            if was_loaded and old_plist and not rollback_errors:
                try:
                    if not _loaded(launchctl, target):
                        _bootstrap(launchctl, domain, path)
                except Exception as restart_error:
                    rollback_errors.append(str(restart_error))
            if rollback_errors:
                raise RuntimeError("Installation failed; rollback incomplete. Recovery copies retained: " + "; ".join(rollback_errors)) from exc
            raise
    print("Installed LaunchAgent and Claude status hooks" if hooks else "Installed LaunchAgent with passive sources")


def uninstall():
    launchctl = _preflight(None, None, imports=False)
    runtime = state_dir()
    with _installation_lock(runtime):
        path = launch_agent_path()
        plist_before = _read_bytes(path)
        _read_plist(path)
        manifest = _load_manifest(runtime)
        plans, updated = _hook_plans(False, runtime, manifest, _read_plist(path), _recorded_root(manifest))
        domain = "gui/" + str(os.getuid())
        target = domain + "/" + LABEL
        was_loaded = _loaded(launchctl, target)
        changes = []
        try:
            if was_loaded:
                _stop(launchctl, target)  # Abort before deleting anything if still loaded.
            _apply_plans(plans, runtime, changes)
            manifest_path = runtime / MANIFEST
            if manifest:
                _apply_plans([(manifest_path, _read_bytes(manifest_path), _json_bytes(updated))], runtime, changes)
            change = _remove_preserving(path, plist_before, runtime)
            if change:
                changes.append(change)
        except Exception:
            errors = _rollback(changes)
            # A failed uninstall leaves the previous installation running, as before.
            if was_loaded and not errors:
                try:
                    if not _loaded(launchctl, target):
                        _bootstrap(launchctl, domain, path)
                except Exception as restart_error:
                    errors.append(str(restart_error))
            if errors:
                raise RuntimeError("Uninstall cleanup incomplete; recovery copies retained: " + "; ".join(errors))
            raise
    print("Stopped service and removed only Agent Deck hooks. Private runtime data retained.")
