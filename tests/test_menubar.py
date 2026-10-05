"""Menu bar status: health model, published status and the optional menu app."""
import os
import tempfile
import unittest
from pathlib import Path

from agentdeck.health import code_fingerprint, health

STARTED = 1000.0


def status(**overrides):
    data = {"device_error": None, "source_errors": [], "inputs_connected": True,
            "paused": False, "code_changed": False, "tokens_error": None}
    data.update(overrides)
    return data


class HealthTests(unittest.TestCase):
    def test_ok_without_problems(self):
        self.assertEqual(health(status(), STARTED + 60, STARTED), {"level": "ok", "reasons": []})

    def test_degraded_reasons_and_priority(self):
        result = health(status(device_error="URLError", source_errors=["claude", "codex", "state"], paused=True),
                        STARTED + 60, STARTED)
        self.assertEqual(result["level"], "degraded")
        self.assertEqual(result["reasons"], ["Bar unreachable", "Claude source error", "Codex source error",
                                             "State files unreadable", "Paused by the bar selector"])

    def test_update_beats_paused(self):
        result = health(status(code_changed=True, paused=True), STARTED + 60, STARTED)
        self.assertEqual(result, {"level": "update", "reasons": ["New version on disk", "Paused by the bar selector"]})

    def test_buttons_grace_period(self):
        disconnected = status(inputs_connected=False)
        self.assertEqual(health(disconnected, STARTED + 5, STARTED)["level"], "ok")
        self.assertEqual(health(disconnected, STARTED + 11, STARTED),
                         {"level": "degraded", "reasons": ["Bar buttons disconnected"]})

    def test_token_error_does_not_degrade(self):
        self.assertEqual(health(status(tokens_error="ccusage unavailable"), STARTED + 60, STARTED)["level"], "ok")


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_detects_edit_add_and_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "menubar").mkdir()
            source = root / "service.py"
            source.write_text("x = 1\n")
            first = code_fingerprint(root)
            (root / "preview.png").write_bytes(b"png")
            self.assertEqual(code_fingerprint(root), first)
            (root / "menubar" / "AgentDeckMenu.swift").write_text("// app\n")
            second = code_fingerprint(root)
            self.assertNotEqual(second, first)
            stat = source.stat()
            source.write_text("x = 22\n")
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertNotEqual(code_fingerprint(root), second)


if __name__ == "__main__":
    unittest.main()
