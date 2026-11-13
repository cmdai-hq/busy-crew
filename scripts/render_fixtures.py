"""Render fixtures for the bar app's TypeScript port of agentdeck/render.py.

    uv run python scripts/render_fixtures.py          # rewrite bar-app/test/fixtures/render.json
    uv run python scripts/render_fixtures.py --check  # fail if the file is out of date

Each case is a fictional scene and the XPM that the Python renderer draws for it. The scene's sessions are
listed the way the collector writes them to the bar: most urgent first, without IDs. bar-app's
`npm test` renders every case in TypeScript and compares the XPM, so the two renderers cannot drift apart.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentdeck.model import Session  # noqa: E402
from agentdeck.render import ordered_sessions, render  # noqa: E402

FIXTURES = ROOT / "bar-app" / "test" / "fixtures" / "render.json"
STATES = ["working", "waiting", "ready", "idle", "unknown"]


def sessions(spec):
    """'claude:working,codex:ready' → fictional sessions with stable IDs."""
    result = []
    for i, item in enumerate(filter(None, spec.split(","))):
        provider, state = item.split(":")
        result.append(Session(provider, f"{i:04d}", state, "0"))
    return result


def scenes():
    yield "empty", "", "sessions", None, 0, []
    for state in STATES:
        for provider in ["claude", "codex"]:
            yield f"team one {provider} {state}", f"{provider}:{state}", "sessions", None, 0, []
            yield f"team one {provider} {state} phase 1", f"{provider}:{state}", "sessions", None, 1, []
    yield "team three mixed", "claude:ready,claude:working,claude:waiting", "sessions", None, 1, []
    yield "team six", ",".join(f"claude:{s}" for s in STATES + ["working"]), "sessions", None, 0, []
    yield "team overflow", ",".join(f"codex:{s}" for s in STATES * 2), "sessions", None, 1, []
    yield "team overflow hidden waiting", ",".join(["codex:working"] * 6 + ["codex:waiting"]), "sessions", None, 0, []
    yield "team overflow 120", ",".join(["claude:idle"] * 120), "sessions", None, 0, []
    yield "claude sleeping team", "claude:idle,claude:idle", "sessions", None, 0, []
    yield "claude sleeping duo", "claude:idle,codex:working", "sessions", None, 0, []
    yield "duo", "claude:working,claude:ready,codex:waiting,codex:idle", "sessions", None, 0, []
    yield "duo many", ",".join(["claude:ready"] * 12 + ["codex:working"] * 3), "sessions", None, 0, []
    yield "duo 100", ",".join(["claude:working"] * 100 + ["codex:ready"]), "sessions", None, 0, []
    yield "duo all states", ",".join(f"{p}:{s}" for p in ["claude", "codex"] for s in STATES), "sessions", None, 0, []
    yield "claude error", "codex:working", "sessions", None, 0, ["claude"]
    yield "both errors", "", "sessions", None, 0, ["claude", "codex"]
    yield "state error team", "claude:ready", "sessions", None, 0, ["state"]
    tokens = {"claude": 563744455, "codex": 1447757}
    yield "tokens today", "claude:working", "tokens", tokens, 0, []
    yield "tokens month", "codex:ready", "month", {"claude": 4545810701, "codex": 3942831}, 0, []
    yield "tokens unknown", "", "tokens", None, 0, []
    yield "tokens waiting wins", "claude:waiting", "tokens", tokens, 0, []
    for n in [0, 999, 1000, 999_999, 1_000_000, 1_049_999, 1_050_000, 99_949_999, 99_950_000, 999_999_999,
              10**9, 10**12 - 1, 10**12, 99 * 10**12, 999 * 10**12 + 10**12 - 1, 1000 * 10**12]:
        yield f"tokens {n}", "", "tokens", {"claude": n, "codex": n}, 0, []


def build():
    cases = []
    for name, spec, view, tokens, phase, errors in scenes():
        listed = ordered_sessions(sessions(spec))
        cases.append({
            "name": name,
            "scene": {"sessions": [{"provider": s.provider, "state": s.state} for s in listed],
                      "view": view, "tokens": tokens, "phase": phase, "errors": errors},
            "xpm": render(listed, view, tokens, phase, errors).xpm(),
        })
    return json.dumps(cases, indent=1) + "\n"


def main(argv):
    text = build()
    if "--check" in argv:
        if not FIXTURES.exists() or FIXTURES.read_text() != text:
            sys.exit(f"{FIXTURES.relative_to(ROOT)} is out of date: run scripts/render_fixtures.py")
        return
    FIXTURES.parent.mkdir(parents=True, exist_ok=True)
    FIXTURES.write_text(text)
    print(f"Wrote {len(json.loads(text))} cases to {FIXTURES.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
