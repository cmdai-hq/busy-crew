import json
import os
import tempfile
from pathlib import Path


def state_dir():
    path = Path(os.environ.get("AGENT_DECK_STATE", "~/Library/Application Support/BusybarAgentDeck")).expanduser()
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


def write_json(path, data):
    write_bytes(path, json.dumps(data, ensure_ascii=False).encode())


def write_bytes(path, data):
    """Replace an application-owned file with private, complete contents."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        # The rename itself must survive a crash, not only the file contents.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
