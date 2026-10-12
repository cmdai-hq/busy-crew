"""Menu bar status: health model, published status and the optional menu app."""
import contextlib
import importlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdeck import cli
from agentdeck.health import code_fingerprint, health, menu_fingerprint

installer = importlib.import_module("agentdeck.install")

SWIFT_TESTS = bool(shutil.which("xcrun") and (os.environ.get("CI") or os.environ.get("AGENT_DECK_SWIFT_TESTS")))

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
            (root / "menubar" / "BusyCrewMenu.swift").write_text("// app\n")
            second = code_fingerprint(root)
            self.assertEqual(second, first)
            menu_before = menu_fingerprint(root)
            (root / "menubar" / "BusyCrewMenu.swift").write_text("// new app\n")
            self.assertNotEqual(menu_fingerprint(root), menu_before)
            self.assertEqual(code_fingerprint(root), first)
            stat = source.stat()
            source.write_text("x = 22\n")
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertNotEqual(code_fingerprint(root), second)


class FakeTools:
    """xcrun and launchctl, with launchd state kept per target."""

    def __init__(self):
        self.loaded, self.calls, self.lingering, self.linger = set(), [], {}, 0
        self.compiler_missing = self.compile_fails = self.fail_bootstrap = self.stuck_bootout = False

    def run(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        if args[0].endswith("xcrun"):
            if args[1] == "--find":
                return subprocess.CompletedProcess(args, 1 if self.compiler_missing else 0, "", "")
            if self.compile_fails:
                return subprocess.CompletedProcess(args, 1, "", "error: fixture")
            Path(args[args.index("-o") + 1]).write_bytes(b"binary %d" % len(self.calls))
            return subprocess.CompletedProcess(args, 0, "", "")
        command = args[1]
        if command == "print":
            if args[2] in self.lingering:  # launchd lists a job briefly after bootout returns.
                self.lingering[args[2]] -= 1
                if self.lingering[args[2]] < 0:
                    del self.lingering[args[2]]
                    self.loaded.discard(args[2])
            loaded = args[2] in self.loaded
            return subprocess.CompletedProcess(args, 0 if loaded else 113, "", "" if loaded else "Could not find service")
        if command == "bootout" and not self.stuck_bootout:
            if self.linger:
                self.lingering[args[2]] = self.linger
            else:
                self.loaded.discard(args[2])
        if command == "bootstrap":
            self.loaded.add(args[2] + "/" + plistlib.loads(Path(args[3]).read_bytes())["Label"])
            if self.fail_bootstrap:
                self.fail_bootstrap = False
                raise subprocess.CalledProcessError(5, args)
        return subprocess.CompletedProcess(args, 0, "", "")


class MenuBarInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "fake home"
        self.home.mkdir()
        self.runtime = self.home / "runtime"
        self.runtime.mkdir(mode=0o700)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home), "AGENT_DECK_STATE": str(self.runtime)}))
        os.environ.pop("BUSY_CREW_PYTHON", None)  # Restored by patch.dict; tests set it themselves.
        stack.enter_context(patch.object(Path, "home", return_value=self.home))
        stack.enter_context(patch.object(installer, "_preflight", return_value="launchctl"))
        stack.enter_context(patch.object(installer.shutil, "which", return_value="/usr/bin/xcrun"))
        self.tools = FakeTools()
        stack.enter_context(patch.object(installer.subprocess, "run", side_effect=self.tools.run))
        stack.enter_context(patch.object(installer, "STOP_TIMEOUT", 0.5))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.binary = self.runtime / "menubar" / "BusyCrewMenu"
        self.plist = installer.menubar_agent_path()
        self.target = "gui/%d/%s" % (os.getuid(), installer.MENU_LABEL)

    def installed(self):
        return self.binary.read_bytes(), self.plist.read_bytes()

    def test_install_compiles_writes_plist_and_bootstraps(self):
        installer.install_menubar()
        self.assertEqual(stat.S_IMODE(self.binary.stat().st_mode), 0o700)
        agent = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(agent["Label"], "local.busybar.agent-deck.menubar")
        self.assertEqual(agent["ProgramArguments"], [str(self.binary)])
        self.assertEqual(agent["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(agent["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(agent["EnvironmentVariables"], {"AGENT_DECK_STATE": str(self.runtime),
                         "AGENT_DECK_PYTHON": sys.executable, "AGENT_DECK_LABEL": installer.LABEL,
                         "AGENT_DECK_MENU_FINGERPRINT": menu_fingerprint(installer.MENU_SOURCE.parent.parent)})
        self.assertIn(self.target, self.tools.loaded)
        compile_call = next(call for call in self.tools.calls if call[1] == "swiftc")
        self.assertEqual(compile_call[2:5], ["-swift-version", "5", "-O"])
        self.assertEqual(list(self.runtime.glob(".menubar-build-*")), [])

    def test_install_passes_the_chosen_interpreter_to_the_app(self):
        with patch.dict(os.environ, {"BUSY_CREW_PYTHON": "/fictional/variable/python"}):
            installer.install_menubar()
            self.assertEqual(plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["AGENT_DECK_PYTHON"],
                             "/fictional/variable/python")
            installer.install_menubar("/fictional/option/python")
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["AGENT_DECK_PYTHON"],
                         "/fictional/option/python")

    def test_successful_install_clears_a_failed_update_marker(self):
        marker = self.runtime / "menubar-update-failed"
        marker.touch()
        self.tools.compile_fails = True
        with self.assertRaises(RuntimeError):
            installer.install_menubar()
        self.assertTrue(marker.exists())
        self.tools.compile_fails = False
        installer.install_menubar()
        self.assertFalse(marker.exists())

    def test_missing_compiler_changes_nothing(self):
        self.tools.compiler_missing = True
        with self.assertRaisesRegex(RuntimeError, "xcode-select --install"):
            installer.install_menubar()
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.binary.exists())

    def test_compile_failure_keeps_previous_install(self):
        installer.install_menubar()
        before = self.installed()
        self.tools.compile_fails = True
        with self.assertRaisesRegex(RuntimeError, "failed to compile"):
            installer.install_menubar()
        self.assertEqual(self.installed(), before)
        self.assertIn(self.target, self.tools.loaded)

    def test_source_change_during_compile_keeps_previous_install(self):
        installer.install_menubar()
        before = self.installed()
        with patch.object(installer, "menu_fingerprint", side_effect=["before", "after"]):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                installer.install_menubar()
        self.assertEqual(self.installed(), before)
        self.assertIn(self.target, self.tools.loaded)

    def test_failed_bootstrap_restores_previous_binary_and_plist(self):
        installer.install_menubar()
        before = self.installed()
        self.tools.fail_bootstrap = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_menubar(python="/different/python")
        self.assertEqual(self.installed(), before)
        self.assertIn(self.target, self.tools.loaded)
        self.assertFalse(self.binary.with_name("BusyCrewMenu.previous").exists())

    def test_stop_waits_while_launchd_finishes_unloading(self):
        installer.install_menubar()
        self.tools.linger = 2  # Measured on macOS 27: still listed ~70 ms after bootout returns.
        installer.install_menubar()
        self.assertIn(self.target, self.tools.loaded)

    def test_failed_stop_keeps_the_installed_binary(self):
        installer.install_menubar()
        before = self.installed()
        self.tools.stuck_bootout = True
        with self.assertRaisesRegex(RuntimeError, "did not stop"):
            installer.install_menubar()
        self.assertEqual(self.installed(), before)

    def test_failed_first_install_does_not_restart_an_agent_without_plist(self):
        self.tools.loaded.add(self.target)  # Loaded, but its plist was deleted.
        self.tools.fail_bootstrap = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_menubar()
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.binary.exists())

    def legacy_install(self):
        """A pre-1.0 install: same label, binary under its former name."""
        installer.install_menubar()
        legacy = self.binary.with_name(installer.LEGACY_MENU_BINARY)
        os.replace(self.binary, legacy)
        agent = plistlib.loads(self.plist.read_bytes())
        agent["ProgramArguments"] = [str(legacy)]
        self.plist.write_bytes(plistlib.dumps(agent))
        return legacy

    def test_upgrade_replaces_the_legacy_binary(self):
        legacy = self.legacy_install()
        legacy.with_name(legacy.name + ".previous").write_bytes(b"old")
        installer.install_menubar()
        self.assertTrue(self.binary.exists())
        self.assertEqual(sorted(p.name for p in self.binary.parent.iterdir()), ["BusyCrewMenu"])
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["ProgramArguments"], [str(self.binary)])

    def test_failed_upgrade_keeps_the_legacy_binary_its_plist_runs(self):
        legacy = self.legacy_install()
        before = self.plist.read_bytes()
        self.tools.fail_bootstrap = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_menubar()
        self.assertTrue(legacy.exists())
        self.assertEqual(self.plist.read_bytes(), before)

    def test_uninstall_removes_the_legacy_binary(self):
        legacy = self.legacy_install()
        installer.uninstall_menubar()
        self.assertFalse(legacy.exists())

    def test_uninstall_menubar_removes_agent_plist_and_binary(self):
        installer.install_menubar()
        for name in ("BusyCrewMenu.previous", "AgentDeckMenu.previous"):
            (self.binary.parent / name).write_bytes(b"interrupted install")
        installer.uninstall_menubar()
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.binary.exists())
        self.assertFalse(self.binary.parent.exists())
        self.assertNotIn(self.target, self.tools.loaded)

    def test_uninstall_retains_unrelated_menu_files(self):
        installer.install_menubar()
        other = self.binary.parent / "unrelated.txt"
        other.write_text("keep")
        installer.uninstall_menubar()
        self.assertEqual(other.read_text(), "keep")

    def test_foreign_menu_plist_aborts_before_removing_collector(self):
        installer.install(hooks=False)
        collector = installer.launch_agent_path()
        before = collector.read_bytes()
        self.plist.write_bytes(plistlib.dumps({"Label": "foreign.agent"}))
        calls = len(self.tools.calls)
        with self.assertRaisesRegex(ValueError, "not owned"):
            installer.uninstall()
        self.assertEqual(collector.read_bytes(), before)
        self.assertIn("gui/%d/%s" % (os.getuid(), installer.LABEL), self.tools.loaded)
        self.assertFalse(any(call[1] == "bootout" for call in self.tools.calls[calls:]))

    def test_compiler_timeouts_leave_previous_install_running(self):
        installer.install_menubar()
        before = self.installed()
        original = self.tools.run
        for command in ("--find", "swiftc"):
            def timeout(args, **kwargs):
                if args[0].endswith("xcrun") and args[1] == command:
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
                return original(args, **kwargs)
            with self.subTest(command=command), patch.object(installer.subprocess, "run", side_effect=timeout):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    installer.install_menubar()
            self.assertEqual(self.installed(), before)
            self.assertIn(self.target, self.tools.loaded)
            self.assertFalse(list(self.runtime.glob(".menubar-build-*")))

    def test_failed_recovery_chmod_undoes_plist_exchange(self):
        installer.install_menubar()
        before = self.installed()
        original = Path.chmod
        def fail_recovery(path, mode, **kwargs):
            if path.parent.name == "recovery" and path.name.startswith(self.plist.name):
                raise OSError("fixture chmod failure")
            return original(path, mode, **kwargs)
        with patch.object(Path, "chmod", fail_recovery):
            with self.assertRaisesRegex(OSError, "fixture chmod failure"):
                installer.install_menubar(python="/different/python")
        self.assertEqual(self.installed(), before)
        self.assertIn(self.target, self.tools.loaded)

    def test_full_uninstall_removes_menubar_only_when_present(self):
        installer.uninstall()
        self.assertFalse(any(installer.MENU_LABEL in " ".join(call) for call in self.tools.calls))
        installer.install_menubar()
        installer.uninstall()
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.binary.exists())
        self.assertNotIn(self.target, self.tools.loaded)

    def test_cli_dispatches_menubar_actions(self):
        for action, function, args in [("install", "install_menubar", (None,)), ("uninstall", "uninstall_menubar", ())]:
            with patch.object(installer, function) as called, patch.object(sys, "argv", ["agent-deck", "menubar", action]):
                cli.main()
            called.assert_called_once_with(*args)


@unittest.skipUnless(SWIFT_TESTS, "compiles the Swift app; runs in CI or with AGENT_DECK_SWIFT_TESTS=1")
class SwiftAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temp.name) / "BusyCrewMenu"
        subprocess.run(["xcrun", "swiftc", *installer.SWIFT_FLAGS, "-o", str(cls.binary), str(installer.MENU_SOURCE)],
                       check=True, timeout=600)
        # Exercise the real delegate with a synthetic state folder and a fake
        # interpreter. The lazy status item keeps this harness off the menu bar.
        harness = Path(cls.temp.name) / "main.swift"
        source = installer.MENU_SOURCE.read_text().split("let arguments = CommandLine.arguments")[0]
        harness.write_text(source + '''
let delegate = AppDelegate()
delegate.menu.autoenablesItems = false
let item = NSMenuItem(title: "Acknowledge results", action: #selector(AppDelegate.acknowledge), keyEquivalent: "")
item.isEnabled = true
delegate.menu.addItem(item)
delegate.loadSnapshot()
if CommandLine.arguments[1] == "tracking" {
    print("fresh: \\(item.isEnabled)")
    delegate.menuWillOpen(delegate.menu)
    let url = delegate.stateDir.appendingPathComponent("status.json")
    var status = try! JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
    status["at"] = Date().timeIntervalSince1970 - 6
    try! JSONSerialization.data(withJSONObject: status).write(to: url)
    _ = NSApplication.shared
    let deadline = Date().addingTimeInterval(1.2)
    while item.isEnabled && Date() < deadline {
        RunLoop.main.run(mode: .eventTracking, before: deadline)
    }
    print("expired: \\(item.isEnabled)")
    delegate.menuDidClose(delegate.menu)
    print("timer cleared: \\(delegate.menuTimer == nil)")
} else {
    delegate.acknowledge()
    RunLoop.main.run(until: Date().addingTimeInterval(0.3))
    let marker = delegate.stateDir.appendingPathComponent("ack-dispatched")
    print("dispatched: \\(FileManager.default.fileExists(atPath: marker.path))")
}
''')
        cls.control_binary = Path(cls.temp.name) / "MenuControlTests"
        subprocess.run(["xcrun", "swiftc", *installer.SWIFT_FLAGS, "-o", str(cls.control_binary), str(harness)],
                       check=True, timeout=600)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def menu(self, content, fingerprint=None):
        path = Path(self.temp.name) / "menu-status.json"
        path.unlink(missing_ok=True)
        if content is not None:
            path.write_text(json.dumps(content))
        env = dict(os.environ)
        if fingerprint is not None:
            env["AGENT_DECK_MENU_FINGERPRINT"] = fingerprint
        result = subprocess.run([str(self.binary), "--menu", str(path)], capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_menu_offers_token_refresh_and_shows_totals(self):
        status = {"at": time.time(), "sessions": [], "health": {"level": "ok", "reasons": []},
                  "tokens": {"claude": 96136546, "codex": 9872119}, "tokens_at": time.time() - 90}
        lines = [line.strip() for line in self.menu(status)]
        self.assertIn("[x] Refresh tokens now", lines)
        self.assertTrue(any(line.startswith("Tokens today: Claude 96.1M · Codex 9.9M") for line in lines), lines)
        self.assertIn("[ ] Refresh tokens now", self.menu(None))
        just_counted = [line.strip() for line in self.menu(dict(status, tokens_at=time.time()))]
        self.assertTrue(any(line.endswith("(updated now)") for line in just_counted), just_counted)
        no_totals = [line.strip() for line in self.menu(dict(status, tokens={}))]
        self.assertFalse(any(line.startswith("Tokens today") for line in no_totals))

    def test_acknowledge_follows_what_the_collector_can_acknowledge(self):
        status = {"at": time.time(), "sessions": [{"provider": "claude", "state": "ready"}],
                  "health": {"level": "ok", "reasons": []}, "code_changed": False}
        self.assertIn("[ ] Acknowledge results", self.menu(dict(status, acknowledgeable=0)))
        self.assertIn("[x] Acknowledge results", self.menu(dict(status, acknowledgeable=1)))
        self.assertIn("[ ] Acknowledge results", self.menu(None))
        self.assertIn("[x] Reload collector", self.menu(None))

    def test_acknowledge_rejects_stale_and_future_status(self):
        status = {"sessions": [], "health": {"level": "ok", "reasons": []}, "acknowledgeable": 1}
        for age in (-2, 6, 11):
            with self.subTest(age=age):
                self.assertIn("[ ] Acknowledge results", self.menu(dict(status, at=time.time() - age)))
        self.assertIn("[x] Acknowledge results", self.menu(dict(status, at=time.time() - 2)))

    def control(self, mode, age):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "status.json").write_text(json.dumps({"at": time.time() - age, "sessions": [],
                "health": {"level": "ok", "reasons": []}, "acknowledgeable": 1}))
            interpreter = root / "fake-python"
            interpreter.write_text('#!/bin/sh\ntouch "$AGENT_DECK_STATE/ack-dispatched"\n')
            interpreter.chmod(0o700)
            env = dict(os.environ, AGENT_DECK_STATE=str(root), AGENT_DECK_PYTHON=str(interpreter))
            result = subprocess.run([str(self.control_binary), mode], capture_output=True, text=True, timeout=10, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.splitlines()

    def test_open_menu_refreshes_existing_acknowledge_item(self):
        self.assertEqual(self.control("tracking", age=0),
                         ["fresh: true", "expired: false", "timer cleared: true"])

    def test_acknowledge_action_rechecks_status_before_launching(self):
        for age in (6, -2, 20):
            with self.subTest(age=age):
                self.assertEqual(self.control("action", age), ["dispatched: false"])
        self.assertEqual(self.control("action", age=0), ["dispatched: true"])

    def test_swift_change_survives_collector_reload(self):
        status = {"at": time.time(), "sessions": [], "health": {"level": "ok", "reasons": []},
                  "menu_fingerprint": "new", "code_changed": False}
        lines = [line.strip() for line in self.menu(status, fingerprint="old")]
        self.assertIn("Menu app changed — run `busy-crew menubar install`", lines)
        self.assertFalse(any("New version on disk" in line for line in lines))
        self.assertNotIn("Menu app changed — run `busy-crew menubar install`",
                         [line.strip() for line in self.menu(status, fingerprint="new")])

    def test_update_entry_follows_the_installation_mode(self):
        status = {"at": time.time(), "sessions": [], "latest_version": "1.1.0",
                  "health": {"level": "update", "reasons": ["Version 1.1.0 available"]}}
        for mode in ["homebrew", "uv"]:
            with self.subTest(mode=mode):
                self.assertIn("[x] Update to 1.1.0", [line.strip() for line in self.menu(dict(status, install_mode=mode))])
        checkout = [line.strip() for line in self.menu(dict(status, install_mode="checkout"))]
        self.assertIn("To update: git pull --ff-only && uv sync --locked, then uv run busy-crew install", checkout)
        self.assertFalse(any("Update to" in line for line in checkout))
        for content in [dict(status, install_mode=None), dict(status, latest_version=None, install_mode="homebrew")]:
            self.assertFalse(any("Update to" in line or "To update" in line for line in self.menu(content)))

    def level(self, content):
        path = Path(self.temp.name) / "status.json"
        path.unlink(missing_ok=True)
        if content is not None:
            path.write_text(content if isinstance(content, str) else json.dumps(content))
        result = subprocess.run([str(self.binary), "--state", str(path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_state_mode_levels(self):
        now = time.time()
        fresh = lambda level: {"at": now, "sessions": [], "health": {"level": level, "reasons": []}}
        cases = [
            (None, "stopped"), ("{not json", "stopped"),
            ({"at": now - 30, "health": {"level": "ok", "reasons": []}}, "stopped"),
            ({"at": now + 60, "health": {"level": "ok", "reasons": []}}, "stopped"),
            (fresh("degraded"), "degraded"), (fresh("update"), "update"), (fresh("paused"), "paused"),
            (fresh("ok"), "ok"), (fresh("from-a-newer-collector"), "degraded"),
            ({"at": now, "sessions": []}, "update"),  # A collector predating health reporting.
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                self.assertEqual(self.level(content), expected)

    def test_running_icon_is_the_drawn_duo_not_a_generic_symbol(self):
        from PIL import Image
        icons = {}
        for level in ["ok", "stopped"]:
            path = Path(self.temp.name) / (level + ".png")
            result = subprocess.run([str(self.binary), "--icon", level, str(path)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            icons[level] = Image.open(path).convert("RGBA")
        # 16 x 16 pt at 2x, unlike the SF Symbols of problem states.
        self.assertEqual(icons["ok"].size, (32, 32))
        self.assertNotEqual(icons["ok"].tobytes(), icons["stopped"].tobytes())
        alpha = icons["ok"].getchannel("A")
        self.assertGreater(alpha.getpixel((16, 2)), 0)    # the divider between the two faces
        self.assertGreater(alpha.getpixel((7, 16)), 0)    # Claude's left eye
        self.assertEqual(alpha.getpixel((10, 22)), 0)     # empty face below the eyes


if __name__ == "__main__":
    unittest.main()
