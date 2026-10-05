from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    provider: str
    id: str
    state: str  # working, waiting, ready, idle, unknown
    version: str


def layout(sessions, errors=()):
    """Every observed session stays visible until its source removes it."""
    if set(errors) & {"claude", "codex"}:
        return "duo", None
    providers = {s.provider for s in sessions} | (set(errors) & {"claude", "codex"})
    return ("duo", None) if len(providers) != 1 else ("team", next(iter(providers)))


ORDER = {"waiting": 0, "working": 1, "ready": 2, "unknown": 3, "idle": 4}


def aggregate(sessions):
    return min((s.state for s in sessions), key=ORDER.get, default="idle")
