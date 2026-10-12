"""Collector health published in status.json for the optional menu bar icon."""
import hashlib
from pathlib import Path

INPUTS_GRACE = 10  # The button WebSocket connects shortly after start.
SOURCES = {"claude": "Claude source error", "codex": "Codex source error", "state": "State files unreadable"}


def health(status, now, started_at):
    """First matching level wins; reasons list every problem, most severe first."""
    degraded = []
    if status.get("device_error"):
        degraded.append("Bar unreachable")
    degraded += [SOURCES[name] for name in SOURCES if name in status.get("source_errors", ())]
    if not status.get("inputs_connected") and now - started_at > INPUTS_GRACE:
        degraded.append("Bar buttons disconnected")
    update = ["New version on disk"] if status.get("code_changed") else []
    if status.get("latest_version"):
        update.append(f"Version {status['latest_version']} available")
    if status.get("menubar_update_failed"):
        update.append("Menu bar app update failed: see upgrade.log")
    paused = ["Paused by the bar selector"] if status.get("paused") else []
    level = "degraded" if degraded else "update" if update else "paused" if paused else "ok"
    return {"level": level, "reasons": degraded + update + paused}


def code_fingerprint(root):
    """Identity of Python code loaded by the collector; edits, additions and removals change it."""
    entries = []
    for path in Path(root).rglob("*"):
        if path.suffix != ".py":
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:  # Removed while scanning.
            continue
        entries.append((path.relative_to(root).as_posix(), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def menu_fingerprint(root):
    """Content identity shared with the environment of the compiled menu app."""
    return hashlib.sha256((Path(root) / "menubar" / "BusyCrewMenu.swift").read_bytes()).hexdigest()
