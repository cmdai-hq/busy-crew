from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    provider: str
    id: str
    state: str  # working, waiting, ready, idle, unknown
    version: str


def layout(sessions):
    """Every observed session stays visible until its source removes it."""
    providers = {s.provider for s in sessions}
    return ("duo", None) if len(providers) != 1 else ("team", next(iter(providers)))


ORDER = {"waiting": 0, "working": 1, "ready": 2, "unknown": 3, "idle": 4}


def aggregate(sessions):
    return min((s.state for s in sessions), key=ORDER.get, default="idle")
