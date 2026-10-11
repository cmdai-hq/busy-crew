import json
import os
import tempfile
from pathlib import Path


STATE_ROOT = "~/Library/Application Support/"
# Installations made before 1.0 keep their folder: the LaunchAgent and hooks pin it.
LEGACY_STATE = STATE_ROOT + "BusybarAgentDeck"


def default_state():
    legacy = Path(LEGACY_STATE).expanduser()
    return legacy if legacy.is_dir() else Path(STATE_ROOT + "BusyCrew").expanduser()


def state_dir():
    configured = os.environ.get("BUSY_CREW_STATE") or os.environ.get("AGENT_DECK_STATE")
    path = Path(configured).expanduser() if configured else default_state()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def claude_dir(home=None):
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str((home or Path.home()) / ".claude"))).expanduser().absolute()


def codex_dir(home=None):
    return Path(os.environ.get("CODEX_HOME", str((home or Path.home()) / ".codex"))).expanduser().absolute()


def load_json(path, default=None, *, max_bytes=2_000_000, require_mapping=True):
    """Read a bounded JSON file; only absence returns the default."""
    try:
        with path.open("rb") as source:
            raw = source.read(max_bytes + 1)
    except FileNotFoundError:
        return default
    if len(raw) > max_bytes:
        raise ValueError(f"JSON file exceeds {max_bytes} bytes: {path}")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"Invalid JSON file: {path}") from exc
    if require_mapping and not isinstance(data, dict):
        raise ValueError(f"JSON object required: {path}")
    return data


def read_json(path, default=None):
    try:
        return load_json(path, default, require_mapping=False)
    except (OSError, ValueError):
        return default


def write_json(path, data, durable=True):
    write_bytes(path, json.dumps(data, ensure_ascii=False).encode(), durable)


def write_bytes(path, data, durable=True):
    """Replace an application-owned file with private, complete contents.

    Snapshots rewritten every poll skip fsync: the atomic rename already
    prevents torn reads, and a crash only loses a value about to be replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            if durable:
                output.flush()
                os.fsync(output.fileno())
        os.replace(temporary, path)
        if durable:
            # The rename itself must survive a crash, not only the file contents.
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
