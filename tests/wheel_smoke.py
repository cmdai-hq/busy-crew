"""Run with the installed wheel's interpreter, outside the checkout."""
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import agentdeck
from agentdeck.install import hook_command


def main():
    package = Path(agentdeck.__file__).resolve()
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Smoke test imported the checkout instead of the wheel")
    if not (package.parent / "menubar" / "BusyCrewMenu.swift").is_file():
        raise RuntimeError("The menu bar app source is missing from the wheel")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        subprocess.run([sys.executable, "-I", "-m", "agentdeck.cli", "--help"], cwd=root, check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, "-I", "-m", "agentdeck.cli", "demo", str(root / "demo")],
                       cwd=root, check=True, stdout=subprocess.DEVNULL)
        if len(list((root / "demo").glob("*.png"))) != 4:
            raise RuntimeError("Fictional previews are missing")
        shadow = root / "agentdeck"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("")
        (shadow / "hook.py").write_text('print("SHADOW_PACKAGE_LOADED")\n')
        runtime = root / "private runtime"
        payload = {"session_id": "synthetic-wheel-session", "hook_event_name": "UserPromptSubmit",
                   "prompt": "SYNTHETIC_PROMPT_MUST_NOT_PERSIST"}
        result = subprocess.run(shlex.split(hook_command(runtime)), cwd=root, check=True,
                                input=json.dumps(payload).encode(), capture_output=True)
        if result.stdout or result.stderr:
            raise RuntimeError("Isolated hook emitted output or loaded the shadow package")
        records = list((runtime / "hooks").glob("*.json"))
        if len(records) != 1 or "SYNTHETIC_PROMPT_MUST_NOT_PERSIST" in records[0].read_text():
            raise RuntimeError("Wheel hook did not produce a private status-only snapshot")
        if json.loads(records[0].read_text())["state"] != "working":
            raise RuntimeError("Wheel hook did not process its synthetic event")
    print("Installed wheel CLI, previews and isolated status hook passed")


if __name__ == "__main__":
    main()
