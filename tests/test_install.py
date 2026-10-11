import contextlib
import importlib
import io
import json
import os
import plistlib
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentdeck.storage import claude_dir, codex_dir, load_json, write_bytes, write_json

installer = importlib.import_module("agentdeck.install")


class FakeLaunchctl:
    def __init__(self):
        self.loaded = False
        self.calls = []
        self.fail_bootstrap = False
        self.fail_bootout = False
        self.fail_print_after_bootout = False
        self.print_error = False

    def run(self, args, **kwargs):
        self.calls.append(args)
        command = args[1]
        if command == "print":
            if self.print_error:
                self.print_error = False
                raise OSError("simulated launchd query failure")
            return subprocess.CompletedProcess(args, 0 if self.loaded else 113, "", "" if self.loaded else "Could not find service")
        if command == "bootout":
            if not self.fail_bootout:
                self.loaded = False
                self.print_error = self.fail_print_after_bootout
                self.fail_print_after_bootout = False
            return subprocess.CompletedProcess(args, 5 if self.fail_bootout else 0, "", "")
        if command == "bootstrap":
            self.loaded = True
            if self.fail_bootstrap:
                self.fail_bootstrap = False
                raise subprocess.CalledProcessError(5, args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fake home"
        self.home.mkdir()
        self.runtime = self.home / "custom runtime"
        self.runtime.mkdir(mode=0o700)
        environment = dict(os.environ)
        environment.pop("CLAUDE_CONFIG_DIR", None)
        environment.pop("CODEX_HOME", None)
        environment.pop("BUSY_CREW_PYTHON", None)  # Tests choose the interpreter themselves.
        environment.update(HOME=str(self.home), AGENT_DECK_STATE=str(self.runtime))
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, environment, clear=True))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.preflight = self.stack.enter_context(patch.object(installer, "_preflight", return_value="launchctl"))
        self.launch = FakeLaunchctl()
        self.stack.enter_context(patch.object(installer.subprocess, "run", side_effect=self.launch.run))
        self.stack.enter_context(patch.object(installer.shutil, "which", return_value=None))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.settings = self.home / ".claude/settings.json"

    def initial_settings(self):
        data = {"permissions": {"allow": ["Read"], "deny": ["Bash(rm *)"]}, "env": {"FICTIONAL": "value"},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "echo python -m agentdeck.hook"},
                    {"type": "prompt", "prompt": "fictional prompt"}]}], "SubagentStop": [{"hooks": []}]}}
        write_json(self.settings, data)
        return data

    def state_bytes(self):
        return {path: path.read_bytes() if path.exists() else None for path in [
            self.settings, self.runtime / "config.json", self.runtime / installer.MANIFEST, installer.launch_agent_path()]}

    def test_normal_roundtrip_preserves_every_third_party_setting(self):
        original = self.initial_settings()
        installer.install("http://fictional-device", "UTC")
        installer.uninstall()
        self.assertEqual(load_json(self.settings), original)
        self.assertFalse(self.launch.loaded)
        self.assertFalse(installer.launch_agent_path().exists())
        self.assertIn("Stopped service", self.output.getvalue())
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (self.runtime / "recovery").iterdir()))

    def test_isolated_hook_command_pins_runtime_with_spaces(self):
        with patch.object(installer.sys, "executable", str(self.home / "venv with spaces/bin/python")):
            self.assertEqual(shlex.split(installer.hook_command()), [
                str(self.home / "venv with spaces/bin/python"), "-I", "-m", "agentdeck.hook", "--runtime", str(self.runtime)])

    def test_idempotent_hooks_do_not_rewrite_or_create_backups(self):
        self.initial_settings()
        installer.set_hooks(True)
        before = self.settings.read_bytes(), self.settings.stat().st_ino
        backups = list((self.runtime / "recovery").iterdir())
        installer.set_hooks(True)
        self.assertEqual((self.settings.read_bytes(), self.settings.stat().st_ino), before)
        self.assertEqual(list((self.runtime / "recovery").iterdir()), backups)

    def test_active_to_passive_removes_only_recorded_hooks_and_preserves_config(self):
        original = self.initial_settings()
        write_json(self.runtime / "config.json", {"address": "http://saved-device", "timezone": "UTC", "ccusage": "/saved/ccusage", "token": "fictional"})
        installer.install()
        installer.install(hooks=False)
        self.assertEqual(load_json(self.settings), original)
        config = load_json(self.runtime / "config.json")
        self.assertEqual((config["address"], config["timezone"], config["ccusage"], config["token"]),
                         ("http://saved-device", "UTC", "/saved/ccusage", "fictional"))

    def test_initial_passive_install_and_uninstall_never_create_claude_settings(self):
        installer.install(hooks=False)
        installer.uninstall()
        self.assertFalse(self.settings.exists())
        self.assertFalse(self.settings.parent.exists())

    def test_initial_passive_install_ignores_unrelated_broken_claude_settings(self):
        self.settings.parent.mkdir()
        self.settings.write_bytes(b"{broken")
        installer.install(hooks=False)
        installer.uninstall()
        self.assertEqual(self.settings.read_bytes(), b"{broken")

    def test_exact_legacy_migration_uses_owned_launchagent_interpreter(self):
        legacy = f"{shlex.quote(installer.sys.executable)} -m agentdeck.hook"
        write_json(self.settings, {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": legacy}, {"type": "command", "command": "echo python -m agentdeck.hook"}]}]}})
        write_bytes(installer.launch_agent_path(), plistlib.dumps({"Label": installer.LABEL,
            "ProgramArguments": [installer.sys.executable, "-m", "agentdeck.cli", "run"]}))
        installer.set_hooks(True)
        handlers = [handler for group in load_json(self.settings)["hooks"]["Stop"] for handler in group["hooks"]]
        self.assertNotIn(legacy, [handler["command"] for handler in handlers])
        self.assertIn("echo python -m agentdeck.hook", [handler["command"] for handler in handlers])
        self.assertIn(installer.hook_command(), [handler["command"] for handler in handlers])

    def homebrew_python(self, version="1.1.0", linked=True):
        prefix = self.home / "homebrew"
        python = prefix / "Cellar/busy-crew" / version / "libexec/bin/python"
        python.parent.mkdir(parents=True)
        python.touch(mode=0o755)
        if linked:
            (prefix / "opt").mkdir()
            (prefix / "opt/busy-crew").symlink_to(python.parents[2])
        return python, prefix / "opt/busy-crew/libexec/bin/python"

    def test_homebrew_cellar_interpreter_becomes_its_opt_link(self):
        cellar, stable = self.homebrew_python()
        with patch.object(installer.sys, "executable", str(cellar)):
            self.assertEqual(installer.interpreter(), str(stable))

    def test_cellar_interpreter_without_opt_link_is_kept(self):
        cellar, _ = self.homebrew_python(linked=False)
        for executable in [str(cellar), str(self.home / "checkout/.venv/bin/python"), "/opt/homebrew/Cellar/python"]:
            with self.subTest(executable=executable), patch.object(installer.sys, "executable", executable):
                self.assertEqual(installer.interpreter(), executable)

    def test_interpreter_option_wins_over_variable_over_default(self):
        cellar, stable = self.homebrew_python()
        option, variable = str(self.home / "option/python"), str(self.home / "variable/python")
        with patch.object(installer.sys, "executable", str(cellar)):
            self.assertEqual(installer.interpreter(), str(stable))
            with patch.dict(os.environ, {"BUSY_CREW_PYTHON": variable}):
                self.assertEqual(installer.interpreter(), variable)
                self.assertEqual(installer.interpreter(option), option)
            # An option or variable is used as given, without the Homebrew rewrite.
            self.assertEqual(installer.interpreter(str(cellar)), str(cellar))

    def test_install_pins_the_stable_interpreter_everywhere(self):
        cellar, stable = self.homebrew_python()
        with patch.object(installer.sys, "executable", str(cellar)):
            installer.install("http://fictional-device", "UTC")
        expected = installer.hook_command(python=str(stable))
        self.assertEqual(shlex.split(expected)[0], str(stable))
        self.assertEqual(plistlib.loads(installer.launch_agent_path().read_bytes())["ProgramArguments"][0], str(stable))
        commands = {handler["command"] for groups in load_json(self.settings)["hooks"].values()
                    for group in groups for handler in group["hooks"]}
        self.assertEqual(commands, {expected})
        self.assertEqual(load_json(self.runtime / installer.MANIFEST)["hook_commands"], [expected])
        self.assertNotIn(str(cellar), self.settings.read_text())

    def test_reinstall_with_the_same_interpreter_rewrites_nothing(self):
        self.initial_settings()
        installer.install("http://fictional-device", "UTC", python="/fictional/python")
        before = self.state_bytes()
        backups = sorted((self.runtime / "recovery").iterdir())
        installer.install(python="/fictional/python")
        self.assertEqual(self.state_bytes(), before)
        self.assertEqual(sorted((self.runtime / "recovery").iterdir()), backups)

    def test_reinstall_with_another_interpreter_replaces_its_hooks(self):
        original = self.initial_settings()
        installer.install("http://fictional-device", "UTC", python="/checkout/.venv/bin/python")
        installer.install(python="/opt/homebrew/opt/busy-crew/libexec/bin/python")
        settings = self.settings.read_text()
        self.assertNotIn("/checkout/", settings)
        self.assertEqual(settings.count("/opt/homebrew/opt/busy-crew/libexec/bin/python"), len(installer.EVENTS))
        installer.uninstall()
        self.assertEqual(load_json(self.settings), original)

    def test_symlink_valid_and_broken_are_refused_before_any_config_mutation(self):
        for broken in [False, True]:
            with self.subTest(broken=broken):
                target = self.home / "dotfiles.json"
                if not broken:
                    target.write_text('{"env":{"FAKE":"value"}}')
                else:
                    target.unlink(missing_ok=True)
                self.settings.parent.mkdir(exist_ok=True)
                self.settings.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    installer.install()
                self.assertTrue(self.settings.is_symlink())
                self.assertFalse((self.runtime / "config.json").exists())
                self.settings.unlink()

    def test_invalid_runtime_and_claude_json_are_not_reset(self):
        for path, raw in [(self.runtime / "config.json", b'{"token":"fictional", broken'),
                          (self.settings, b"[]"), (self.settings, b'{"hooks":[]}')]:
            with self.subTest(path=path, raw=raw):
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    installer.install()
                self.assertEqual(path.read_bytes(), raw)
                self.assertFalse(installer.launch_agent_path().exists())
                path.unlink()

    def test_custom_roots_are_persisted_and_old_roots_hooks_removed_on_switch(self):
        custom = self.home / "different Claude"
        codex = self.home / "different Codex"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom), "CODEX_HOME": str(codex)}):
            installer.install()
        self.assertFalse(self.settings.exists())
        config = load_json(self.runtime / "config.json")
        self.assertEqual((config["claude_dir"], config["codex_dir"]), (str(custom), str(codex)))
        plist = plistlib.loads(installer.launch_agent_path().read_bytes())
        self.assertEqual(plist["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], str(custom))
        self.assertEqual(plist["ProgramArguments"][1], "-I")
        installer.install()  # No inherited variables: retain the saved roots.
        self.assertFalse(self.settings.exists())
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")}):
            installer.install()
        self.assertEqual(load_json(custom / "settings.json")["hooks"], {})
        self.assertTrue(self.settings.exists())

    def test_late_inplace_and_atomic_edits_are_restored_and_recoverable(self):
        for atomic in [False, True]:
            with self.subTest(atomic=atomic):
                original = {"permissions": {"allow": ["Read"]}}
                write_json(self.settings, original)
                before = self.settings.read_bytes()
                late = b'{"permissions":{"allow":["Read"],"deny":["Bash(rm *)"]}}'
                exchange = installer._exchange
                injected = False
                def interleaving(first, second):
                    nonlocal injected
                    if second == self.settings and not injected:
                        injected = True
                        (write_bytes if atomic else Path.write_bytes)(self.settings, late)
                    return exchange(first, second)
                with patch.object(installer, "_exchange", side_effect=interleaving):
                    with self.assertRaisesRegex(RuntimeError, "Concurrent edit"):
                        installer.set_hooks(True)
                self.assertEqual(self.settings.read_bytes(), late)
                retained = [path.read_bytes() for path in (self.runtime / "recovery").iterdir()]
                self.assertIn(before, retained)
                self.assertTrue(any(b"agentdeck.hook" in raw for raw in retained))

    def test_exclusive_creation_preserves_late_new_file(self):
        original_link = os.link
        late = b'{"permissions":{"deny":["Bash(rm *)"]}}'
        def create_before_link(source, destination, **kwargs):
            if destination == self.settings:
                self.settings.write_bytes(late)
            return original_link(source, destination, **kwargs)
        with patch.object(installer.os, "link", side_effect=create_before_link):
            with self.assertRaises(FileExistsError):
                installer.set_hooks(True)
        self.assertEqual(self.settings.read_bytes(), late)

    def test_failed_bootstrap_restores_previous_files_and_running_service(self):
        self.initial_settings()
        installer.install("http://old-device", "UTC")
        before = self.state_bytes()
        self.launch.fail_bootstrap = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install("http://new-device", "Europe/Paris")
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue(self.launch.loaded)
        self.assertEqual([call[1] for call in self.launch.calls].count("bootstrap"), 3)

    def test_failed_new_bootstrap_cleans_partially_loaded_service(self):
        self.launch.fail_bootstrap = True
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install()
        self.assertFalse(self.launch.loaded)
        self.assertTrue(all(raw is None for raw in self.state_bytes().values()))
        self.assertNotIn("Installed", self.output.getvalue())

    def test_post_bootout_query_failure_restores_the_old_service(self):
        installer.install()
        before = self.state_bytes()
        self.launch.fail_print_after_bootout = True
        with self.assertRaises(OSError):
            installer.install("http://new-device")
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue(self.launch.loaded)

    def test_hook_plist_and_manifest_write_failures_roll_back(self):
        for failing in ["settings.json", installer.LABEL + ".plist", installer.MANIFEST]:
            with self.subTest(failing=failing):
                installer.install("http://old-device")
                before = self.state_bytes()
                real_replace = installer._replace_preserving
                raised = False
                def fail_once(path, *args):
                    nonlocal raised
                    if path.name == failing and not raised:
                        raised = True
                        raise OSError("simulated write failure")
                    return real_replace(path, *args)
                new_root = self.home / "new Claude"
                with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(new_root)}), patch.object(installer, "_replace_preserving", side_effect=fail_once):
                    with self.assertRaises(OSError):
                        installer.install("http://new-device")
                self.assertEqual(self.state_bytes(), before)
                self.assertTrue(self.launch.loaded)
                self.assertFalse((new_root / "settings.json").exists())

    def test_bootout_failure_keeps_uninstall_files_and_reports_no_success(self):
        installer.install()
        before = self.state_bytes()
        self.launch.fail_bootout = True
        self.output.seek(0); self.output.truncate()
        with self.assertRaisesRegex(RuntimeError, "did not stop"):
            installer.uninstall()
        self.assertEqual(self.state_bytes(), before)
        self.assertTrue(self.launch.loaded)
        self.assertNotIn("Stopped", self.output.getvalue())

    def test_failed_uninstall_cleanup_restarts_the_stopped_service(self):
        for failure in ["cleanup", "post-bootout query"]:
            with self.subTest(failure=failure):
                installer.install()
                before = self.state_bytes()
                patches = contextlib.ExitStack()
                if failure == "cleanup":
                    patches.enter_context(patch.object(installer, "_remove_preserving", side_effect=RuntimeError("simulated cleanup failure")))
                else:
                    self.launch.fail_print_after_bootout = True
                with patches, self.assertRaises((RuntimeError, OSError)):
                    installer.uninstall()
                self.assertEqual(self.state_bytes(), before)
                self.assertTrue(self.launch.loaded)

    def test_hook_operations_use_the_recorded_custom_claude_root(self):
        custom = self.home / "custom Claude"
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(custom)}):
            installer.install()
        # Without the variable, the unrelated default settings must stay untouched.
        self.settings.parent.mkdir()
        self.settings.write_bytes(b"{broken")
        installer.set_hooks(True)
        self.assertIn(installer.hook_command(), json.dumps(load_json(custom / "settings.json")))
        installer.uninstall()
        self.assertEqual(load_json(custom / "settings.json")["hooks"], {})
        self.assertEqual(self.settings.read_bytes(), b"{broken")

    def test_missing_launchagent_is_recognized_by_exit_code_or_either_stream(self):
        for code, stdout, stderr, expected in [(113, "", "Unfamiliar wording", False),
                                               (1, "Could not find service", "", False),
                                               (0, "", "", True)]:
            with self.subTest(code=code, stdout=stdout, stderr=stderr), \
                 patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, stderr)):
                self.assertEqual(installer._loaded("launchctl", "gui/1/fictional"), expected)
        with patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "Operation not permitted")):
            with self.assertRaisesRegex(RuntimeError, "LaunchAgent state"):
                installer._loaded("launchctl", "gui/1/fictional")

    def test_late_plist_edit_during_uninstall_is_retained_and_cleanup_rolled_back(self):
        installer.install()
        before = self.state_bytes()
        plist = installer.launch_agent_path()
        edited = plistlib.loads(plist.read_bytes())
        edited["EnvironmentVariables"]["FICTIONAL_EDIT"] = "keep"
        late = plistlib.dumps(edited)
        rename = os.rename
        def edit_before_removal(source, destination):
            if source == plist:
                write_bytes(plist, late)
            return rename(source, destination)
        self.output.seek(0); self.output.truncate()
        with patch.object(installer.os, "rename", side_effect=edit_before_removal):
            with self.assertRaisesRegex(RuntimeError, "Concurrent edit during removal"):
                installer.uninstall()
        self.assertEqual(plist.read_bytes(), late)
        for path, raw in before.items():
            if path != plist:
                self.assertEqual(path.read_bytes(), raw)
        self.assertIn(late, [path.read_bytes() for path in (self.runtime / "recovery").iterdir()])
        self.assertNotIn("Stopped", self.output.getvalue())

    def test_installation_operations_do_not_overlap(self):
        with installer._installation_lock(self.runtime):
            with self.assertRaisesRegex(RuntimeError, "Another Busy Crew"):
                installer.install()
        self.assertFalse((self.runtime / "config.json").exists())

    def test_uninstall_does_not_require_service_dependencies(self):
        installer.install(hooks=False)
        installer.uninstall()
        self.assertEqual(self.preflight.call_args.kwargs, {"imports": False})

    def test_uncooperative_edit_during_rollback_is_preserved(self):
        installer.install()
        late = b'{"permissions":{"deny":["Bash(rm *)"]}}'
        def fail_after_edit(*args):
            self.settings.write_bytes(late)
            raise RuntimeError("simulated startup failure")
        # Force a settings update by changing the Claude source root.
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / "new Claude")}), patch.object(installer, "_bootstrap", side_effect=fail_after_edit):
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete"):
                installer.install()
        self.assertEqual(self.settings.read_bytes(), late)


class StorageAndPreflightTests(unittest.TestCase):
    def test_bounded_strict_json_distinguishes_absence_invalid_and_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.json"
            self.assertEqual(load_json(path, {"absent": True}), {"absent": True})
            for raw in [b"[1]", b"null", b"{broken", b"x" * 100]:
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    load_json(path, {}, max_bytes=32)
            path.write_text("[1]")
            self.assertEqual(load_json(path, require_mapping=False), [1])

    def test_replacement_syncs_the_parent_directory_entry(self):
        synced = []
        real_fsync = os.fsync
        def record(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real_fsync(fd)
        with tempfile.TemporaryDirectory() as root, patch("agentdeck.storage.os.fsync", side_effect=record):
            write_bytes(Path(root) / "ledger.json", b"{}")
            self.assertEqual((Path(root) / "ledger.json").read_bytes(), b"{}")
        self.assertEqual(synced, [False, True])

    def test_paths_honor_both_documented_environment_variables(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/tmp/fake Claude", "CODEX_HOME": "/tmp/fake Codex"}):
            self.assertEqual(claude_dir(), Path("/tmp/fake Claude"))
            self.assertEqual(codex_dir(), Path("/tmp/fake Codex"))

    def test_unsupported_os_refused_before_state_or_settings_access(self):
        with patch.object(installer.sys, "platform", "linux"), patch.object(installer, "state_dir") as state:
            with self.assertRaisesRegex(RuntimeError, "only on macOS"):
                installer.install()
            state.assert_not_called()

    def test_preflight_import_failure_is_before_any_configuration_mutation(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        python = Path(temp.name) / "python"
        python.touch(mode=0o755)
        with patch.object(installer.sys, "platform", "darwin"), patch.object(installer.shutil, "which", return_value="launchctl"), \
             patch.object(installer.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["fake-python"])) as run, \
             patch.object(installer, "state_dir") as state:
            with self.assertRaisesRegex(RuntimeError, "not installed for this Python"):
                installer.install(python=str(python))
            state.assert_not_called()
            self.assertEqual(run.call_args.args[0][0], str(python))

    def test_menubar_refuses_an_interpreter_without_busy_crew_before_compiling(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        python = Path(temp.name) / "python"
        python.touch(mode=0o755)
        with patch.object(installer.sys, "platform", "darwin"), patch.object(installer.shutil, "which", return_value="launchctl"), \
             patch.object(installer.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["fake-python"])) as run, \
             patch.object(installer, "state_dir") as state:
            with self.assertRaisesRegex(RuntimeError, "not installed for this Python"):
                installer.install_menubar(str(python))
            state.assert_not_called()
            self.assertEqual(run.call_count, 1)

    def test_preflight_refuses_a_missing_interpreter_before_any_configuration_mutation(self):
        with patch.object(installer.sys, "platform", "darwin"), patch.object(installer.shutil, "which", return_value="launchctl"), \
             patch.object(installer.subprocess, "run") as run, patch.object(installer, "state_dir") as state:
            with self.assertRaisesRegex(RuntimeError, "interpreter is unavailable"):
                installer.install(python="/fictional/missing/python")
            state.assert_not_called()
            run.assert_not_called()

    def test_address_rejects_credentials_redirect_paths_and_bad_ports(self):
        for address in ["file:///tmp/device", "http://user:pass@device", "http://device/api", "http://device:bad", "http://device/?token=x"]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                installer._validate_address(address)
        installer._validate_address("https://device:8080/")


if __name__ == "__main__":
    unittest.main()
