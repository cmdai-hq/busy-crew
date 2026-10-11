"""Release check, installation modes and `busy-crew upgrade`; never contacts GitHub or runs a package manager."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentdeck import cli, updates
from agentdeck.health import health
from agentdeck.storage import write_json
from agentdeck.updates import UpdateCheck, install_mode, parse_version, upgrade_command

from test_service_reliability import service_fixture

DAY = updates.CHECK_EVERY


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name)

    def test_versions_compare_numerically_and_reject_prereleases(self):
        self.assertEqual(parse_version("v1.10.0"), (1, 10, 0))
        self.assertGreater(parse_version("1.10.0"), parse_version("1.9.9"))
        for text in ["1.1", "1.1.0rc1", "v1.1.0-beta", "", None, 110]:
            with self.subTest(text=text):
                self.assertIsNone(parse_version(text))

    def test_fetch_reads_the_latest_release_without_credentials(self):
        body = Response(json.dumps({"tag_name": "v1.1.0", "body": "notes"}).encode())
        with patch.object(updates.urllib.request, "urlopen", return_value=body) as urlopen:
            self.assertEqual(updates.fetch_latest("1.0.1"), "1.1.0")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/cmdai-hq/busy-crew/releases/latest")
        self.assertEqual(request.get_header("User-agent"), "busy-crew/1.0.1")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.data)

    def test_fetch_rejects_a_tag_that_is_not_a_version_and_oversized_responses(self):
        for raw in [json.dumps({"tag_name": "nightly"}).encode(), b"[]", b"null", b"{" + b" " * updates.MAX_RESPONSE + b"}"]:
            with self.subTest(size=len(raw)), patch.object(updates.urllib.request, "urlopen", return_value=Response(raw)):
                with self.assertRaises(ValueError):
                    updates.fetch_latest("1.0.1")

    def test_a_newer_release_is_available_an_older_or_equal_one_is_not(self):
        for latest, expected in [("1.1.0", "1.1.0"), ("1.0.1", None), ("0.9.0", None)]:
            with self.subTest(latest=latest):
                check = UpdateCheck(self.runtime, "1.0.1", fetch=lambda _: latest)
                check.check(1000)
                self.assertEqual(check.available(), expected)

    def test_checks_at_most_once_a_day_across_restarts(self):
        fetch = MagicMock(return_value="1.1.0")
        check = UpdateCheck(self.runtime, "1.0.1", fetch=fetch)
        self.assertTrue(check.due(1000))
        check.check(1000)
        restarted = UpdateCheck(self.runtime, "1.0.1", fetch=fetch)
        self.assertEqual(restarted.available(), "1.1.0")
        self.assertFalse(restarted.due(1000 + DAY - 1))
        self.assertTrue(restarted.due(1000 + DAY))
        self.assertTrue(restarted.due(999))  # Clock set back.
        fetch.assert_called_once_with("1.0.1")

    def test_network_failure_keeps_the_previous_answer_and_waits_a_day(self):
        UpdateCheck(self.runtime, "1.0.1", fetch=lambda _: "1.1.0").check(1000)
        check = UpdateCheck(self.runtime, "1.0.1", fetch=MagicMock(side_effect=urllib.error.URLError("offline")))
        with self.assertLogs("agentdeck", "WARNING") as logs:
            check.check(1000 + DAY)
        self.assertEqual(check.available(), "1.1.0")
        self.assertFalse(check.due(1000 + DAY + 1))
        self.assertEqual(logs.output, ["WARNING:agentdeck:Update check failed: URLError"])

    def test_disabled_or_unversioned_installations_never_ask(self):
        write_json(self.runtime / "update.json", {"checked_at": 1, "latest": "9.0.0"})
        for current, enabled in [("1.0.1", False), (None, True), ("development", True)]:
            with self.subTest(current=current, enabled=enabled):
                fetch = MagicMock()
                check = UpdateCheck(self.runtime, current, enabled, fetch=fetch)
                self.assertFalse(check.due(10 ** 10))
                self.assertIsNone(check.available())

    def test_loop_checks_when_due_then_sleeps_an_hour(self):
        stop = threading.Event()
        check = UpdateCheck(self.runtime, "1.0.1", fetch=MagicMock(return_value="1.1.0"))
        waits = []
        stop.wait = lambda timeout: (waits.append(timeout), stop.set())
        check.loop(stop)
        self.assertEqual((check.available(), waits), ("1.1.0", [3600]))

    def test_corrupt_saved_state_is_ignored(self):
        (self.runtime / "update.json").write_text("{broken")
        check = UpdateCheck(self.runtime, "1.0.1", fetch=MagicMock(return_value="1.1.0"))
        self.assertTrue(check.due(1000))
        self.assertIsNone(check.available())


class HealthAndStatusTests(unittest.TestCase):
    def test_new_release_is_an_update_reason_and_its_absence_is_not_a_problem(self):
        base = {"inputs_connected": True}
        self.assertEqual(health(dict(base, latest_version="1.1.0"), 0, 0),
                         {"level": "update", "reasons": ["Version 1.1.0 available"]})
        self.assertEqual(health(dict(base, latest_version=None), 0, 0), {"level": "ok", "reasons": []})

    def test_status_publishes_versions_and_installation_mode(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = service_fixture(Path(temp.name))
        service.updates = UpdateCheck(Path(temp.name), "1.0.1", fetch=lambda _: "1.1.0")
        service.updates.check(1000)
        service.install_mode = "homebrew"
        service.inputs_connected = True
        service.device.draw.side_effect = lambda frame: service.stop.set()
        writes = []
        with patch("agentdeck.service.threading.Thread"), patch("agentdeck.service.signal.signal"), \
             patch("agentdeck.service.write_json", side_effect=lambda path, data, **_: writes.append(data)):
            service.run()
        self.assertEqual({key: writes[0][key] for key in ("version", "latest_version", "install_mode")},
                         {"version": "1.0.1", "latest_version": "1.1.0", "install_mode": "homebrew"})
        self.assertEqual(writes[0]["health"]["reasons"], ["Version 1.1.0 available"])

    def test_update_check_can_be_turned_off_in_config(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch("agentdeck.service.state_dir", return_value=Path(temp.name)), \
             patch("agentdeck.service.installed_version", return_value="1.0.1"):
            from agentdeck.service import Service
            self.assertFalse(Service({"update_check": False}).updates.enabled)
            self.assertTrue(Service({}).updates.enabled)


class InstallModeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_modes_follow_the_environment_layout(self):
        uv = self.root / "uv/tools/busy-crew"
        uv.mkdir(parents=True)
        (uv / "uv-receipt.toml").touch()
        checkout = self.root / "busy-crew"
        (checkout / "agentdeck").mkdir(parents=True)
        (checkout / ".git").touch()  # A worktree's .git is a file.
        (checkout / "pyproject.toml").touch()
        package = self.root / "site-packages/agentdeck"
        cases = [("/opt/homebrew/Cellar/busy-crew/1.1.0/libexec", package, "homebrew"),
                 ("/usr/local/opt/busy-crew/libexec", package, "homebrew"),
                 (uv, package, "uv"),
                 (checkout / ".venv", checkout / "agentdeck", "checkout"),
                 (self.root / "venv", package, None),
                 ("/opt/homebrew/Cellar/python@3.15/3.15.0", package, None)]
        for prefix, where, expected in cases:
            with self.subTest(prefix=prefix):
                self.assertEqual(install_mode(prefix, where), expected)

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o755)
        return str(path)

    def test_package_managers_are_found_outside_a_minimal_path(self):
        brew = self.executable(self.root / "homebrew/bin/brew")
        uv = self.executable(self.root / "home/.local/bin/uv")
        with patch.object(updates.shutil, "which", return_value=None), \
             patch.object(updates.Path, "home", return_value=self.root / "home"):
            self.assertEqual(upgrade_command("homebrew", self.root / "homebrew/Cellar/busy-crew/1.1.0/libexec"),
                             [brew, "upgrade", "busy-crew"])
            self.assertEqual(upgrade_command("uv"), [uv, "tool", "upgrade", "busy-crew"])
            with self.assertRaisesRegex(RuntimeError, "Homebrew was not found"):
                upgrade_command("homebrew", "/nonexistent/opt/busy-crew/libexec")
            with self.assertRaises(ValueError):
                upgrade_command("checkout")

    def test_brew_on_path_wins_over_the_prefix(self):
        wrapper = self.executable(self.root / "wrapper/brew")
        self.executable(self.root / "homebrew/bin/brew")
        with patch.object(updates.shutil, "which", return_value=wrapper):
            self.assertEqual(upgrade_command("homebrew", self.root / "homebrew/opt/busy-crew/libexec")[0], wrapper)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name) / "runtime"
        self.runtime.mkdir()
        self.home = Path(temp.name) / "home"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.mode = stack.enter_context(patch.object(updates, "install_mode", return_value="homebrew"))
        stack.enter_context(patch.object(updates, "upgrade_command", return_value=["/fake/brew", "upgrade", "busy-crew"]))
        stack.enter_context(patch("agentdeck.install.interpreter", return_value="/fake/opt/busy-crew/libexec/bin/python"))
        self.run = stack.enter_context(patch.object(updates.subprocess, "run",
                                                    return_value=subprocess.CompletedProcess([], 0)))
        self.popen = stack.enter_context(patch.object(updates.subprocess, "Popen"))

    def commands(self):
        return [call.args[0] for call in self.run.call_args_list]

    def test_upgrades_then_reinstalls_with_the_new_version(self):
        self.assertEqual(updates.upgrade(self.runtime), "homebrew")
        self.assertEqual(self.commands(), [["/fake/brew", "upgrade", "busy-crew"],
                                           ["/fake/opt/busy-crew/libexec/bin/python", "-I", "-m", "agentdeck.cli", "install"]])
        self.popen.assert_not_called()  # No menu bar app installed.
        log = self.runtime / "upgrade.log"
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn("$ /fake/brew upgrade busy-crew", log.read_text())

    def test_passive_installation_stays_passive(self):
        write_json(self.runtime / "installation.json",
                   {"version": 1, "hook_settings": str(self.home / ".claude/settings.json"), "hook_commands": []})
        updates.upgrade(self.runtime)
        self.assertEqual(self.commands()[1][-2:], ["install", "--no-claude-hooks"])

    def test_menu_bar_app_is_reinstalled_detached_from_itself(self):
        plist = self.home / "Library/LaunchAgents/local.busybar.agent-deck.menubar.plist"
        plist.parent.mkdir(parents=True)
        plist.touch()
        updates.upgrade(self.runtime)
        self.assertEqual(self.popen.call_args.args[0][-3:], ["-m", "agentdeck.updates", "menubar"])
        self.assertTrue(self.popen.call_args.kwargs["start_new_session"])

    def test_failed_package_upgrade_leaves_the_installation_alone(self):
        self.run.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaisesRegex(RuntimeError, "brew upgrade failed; see .*upgrade.log"):
            updates.upgrade(self.runtime)
        self.assertEqual(len(self.commands()), 1)
        self.popen.assert_not_called()

    def test_checkout_and_unknown_installations_are_told_what_to_run(self):
        for mode, message in [("checkout", "git pull --ff-only && uv sync --locked"), (None, "tool that installed it")]:
            with self.subTest(mode=mode):
                self.mode.return_value = mode
                with self.assertRaisesRegex(RuntimeError, message):
                    updates.upgrade(self.runtime)
        self.run.assert_not_called()

    def test_a_second_update_is_refused_while_one_runs(self):
        import fcntl
        with (self.runtime / "upgrade.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                updates.upgrade(self.runtime)
        self.run.assert_not_called()

    def test_failed_detached_menu_bar_reinstall_is_recorded_for_the_collector(self):
        with patch("agentdeck.install.install_menubar", side_effect=RuntimeError("Swift compiler unavailable")):
            with self.assertRaises(RuntimeError):
                updates.reinstall_menubar(self.runtime)
        self.assertTrue((self.runtime / updates.MENUBAR_FAILED).exists())
        self.assertEqual(health({"inputs_connected": True, "menubar_update_failed": True}, 0, 0),
                         {"level": "update", "reasons": ["Menu bar app update failed: see upgrade.log"]})

    def test_cli_reports_failures_without_a_traceback(self):
        self.mode.return_value = "checkout"
        with patch("agentdeck.cli.state_dir", return_value=self.runtime), patch("sys.argv", ["busy-crew", "upgrade"]):
            with self.assertRaisesRegex(SystemExit, "git checkout"):
                cli.main()


class ModuleEntryTests(unittest.TestCase):
    def test_detached_reinstall_runs_as_a_module(self):
        temp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, temp)
        result = subprocess.run([updates.sys.executable, "-I", "-c",
                                 "import runpy, sys; sys.argv = ['x', 'menubar'];"
                                 "import agentdeck.install as i; i.install_menubar = lambda: print('reinstalled');"
                                 "runpy.run_module('agentdeck.updates', run_name='__main__')"],
                                capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, BUSY_CREW_STATE=temp))
        self.assertEqual((result.returncode, result.stdout), (0, "reinstalled\n"), result.stderr)


if __name__ == "__main__":
    unittest.main()
