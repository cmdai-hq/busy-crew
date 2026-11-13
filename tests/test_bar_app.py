"""The bar app's TypeScript renderer is checked against fixtures drawn by agentdeck/render.py."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RenderFixturesTest(unittest.TestCase):
    def test_fixtures_match_the_python_renderer(self):
        # A render change must regenerate bar-app/test/fixtures/render.json, so `npm test` keeps the port in step.
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "render_fixtures.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
