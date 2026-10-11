import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdeck.cli import main
from agentdeck.storage import write_json


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch("agentdeck.cli.state_dir", return_value=self.runtime).start()
        patch("agentdeck.service.state_dir", return_value=self.runtime).start()

    def run_cli(self, *args):
        with patch("sys.argv", ["agent-deck", *args]), contextlib.redirect_stdout(io.StringIO()) as output:
            main()
        return output.getvalue()

    def test_invalid_status_does_not_crash_or_enqueue_command(self):
        for data in [[], {"at": []}, {"at": True}, {"at": float("nan")},
                     {"at": time.time(), "sessions": [None]},
                     {"at": time.time(), "sessions": [{"provider": [], "state": "idle"}]}]:
            write_json(self.runtime / "status.json", data)
            with self.subTest(data=data), self.assertRaises(SystemExit):
                self.run_cli("ack")
            self.assertFalse((self.runtime / "inbox").exists())

    def test_install_and_menubar_forward_the_python_option(self):
        with patch("agentdeck.install.install") as install, patch("agentdeck.install.install_menubar") as menubar:
            self.run_cli("install", "--python", "/fictional/python")
            self.run_cli("menubar", "install", "--python", "/fictional/python")
            self.run_cli("install")
        self.assertEqual(install.call_args_list[0].args, (None, None, True, "/fictional/python"))
        self.assertEqual(install.call_args_list[1].args, (None, None, True, None))
        menubar.assert_called_once_with("/fictional/python")

    def test_status_omits_conversation_identity(self):
        write_json(self.runtime / "status.json", {
            "at": time.time(), "sessions": [{"provider": "claude", "state": "idle",
                                              "id": "private-fixture-id", "version": "private-turn"}]})
        output = self.run_cli("status")
        self.assertIn('"idle"', output)
        self.assertNotIn("private-fixture", output)
        self.assertNotIn("private-turn", output)

    def test_corrupt_status_is_reported_separately_from_stopped_service(self):
        path = self.runtime / "status.json"
        path.write_text("{broken")
        with self.assertRaisesRegex(SystemExit, "status is invalid"):
            self.run_cli("status")
        path.unlink()
        with self.assertRaisesRegex(SystemExit, "not running"):
            self.run_cli("status")

    def test_refresh_queues_a_token_refresh(self):
        write_json(self.runtime / "status.json", {"at": time.time(), "sessions": []})
        self.run_cli("refresh")
        queued = [json.loads(path.read_text()) for path in (self.runtime / "inbox").glob("*.json")]
        self.assertEqual(queued, [{"command": "refresh"}])

    def test_command_backlog_is_bounded(self):
        write_json(self.runtime / "status.json", {"at": time.time(), "sessions": []})
        for i in range(128):
            write_json(self.runtime / "inbox" / f"{i}.json", {"command": "sessions"})
        with self.assertRaisesRegex(SystemExit, "queue is full"):
            self.run_cli("view", "tokens")
        self.assertEqual(len(list((self.runtime / "inbox").glob("*.json"))), 128)

    def test_snapshot_refuses_corrupt_config_before_network_access(self):
        (self.runtime / "config.json").write_text("{broken")
        with patch("agentdeck.device.Device") as device, self.assertRaisesRegex(SystemExit, "Configuration is invalid"):
            self.run_cli("snapshot", str(self.runtime / "out.png"))
        device.assert_not_called()

    def test_service_initialization_failure_is_logged_without_error_details(self):
        import logging
        (self.runtime / "config.json").write_text("{broken")
        with self.assertRaisesRegex(SystemExit, "private service.log"):
            self.run_cli("run")
        content = (self.runtime / "service.log").read_text()
        self.assertIn("ValueError", content)
        self.assertNotIn("{broken", content)
        self.assertFalse(any(getattr(handler, "baseFilename", None) == str(self.runtime / "service.log")
                             for handler in logging.getLogger("agentdeck").handlers))

    def test_run_failure_remains_logged_after_service_closes_handler(self):
        import logging
        def fail_after_cleanup():
            logger = logging.getLogger("agentdeck")
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            raise RuntimeError("fixture-sensitive-detail")
        with patch("agentdeck.service.Service") as service:
            service.return_value.run.side_effect = fail_after_cleanup
            with self.assertRaisesRegex(SystemExit, "private service.log"):
                self.run_cli("run")
        content = (self.runtime / "service.log").read_text()
        self.assertIn("RuntimeError", content)
        self.assertNotIn("fixture-sensitive-detail", content)


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.support = self.home / "Library/Application Support"

    def resolve(self, **env):
        from agentdeck.storage import state_dir
        clean = {key: value for key, value in os.environ.items() if key not in {"BUSY_CREW_STATE", "AGENT_DECK_STATE"}}
        with patch.dict(os.environ, {**clean, "HOME": str(self.home), **env}, clear=True):
            return state_dir()

    def test_new_installation_uses_the_busy_crew_folder(self):
        self.assertEqual(self.resolve(), self.support / "BusyCrew")

    def test_existing_installation_keeps_its_folder(self):
        (self.support / "BusybarAgentDeck").mkdir(parents=True)
        self.assertEqual(self.resolve(), self.support / "BusybarAgentDeck")
        self.assertFalse((self.support / "BusyCrew").exists())

    def test_busy_crew_state_wins_and_the_legacy_variable_still_works(self):
        chosen, legacy = self.home / "chosen", self.home / "legacy"
        self.assertEqual(self.resolve(BUSY_CREW_STATE=str(chosen), AGENT_DECK_STATE=str(legacy)), chosen)
        self.assertEqual(self.resolve(AGENT_DECK_STATE=str(legacy)), legacy)

    def test_both_command_names_are_installed(self):
        import tomllib
        scripts = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["scripts"]
        self.assertEqual(scripts, {"busy-crew": "agentdeck.cli:main", "agent-deck": "agentdeck.cli:main"})
