# Menu Bar Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An optional macOS menu bar icon showing the collector's health, with acknowledge,
reload and open-log actions.

**Architecture:**
- The Python service computes `health` and `code_changed` and adds them to `status.json`.
- A single-file AppKit app polls that file every 5 s and renders an icon and a menu.
- `agent-deck menubar install|uninstall` compiles the app with `xcrun swiftc` and manages its
  own LaunchAgent, reusing the installer's preserving writes and rollback.

**Tech Stack:** Python 3.11 stdlib and unittest; Swift 5 language mode with AppKit; launchd.

**Spec:** `docs/superpowers/specs/2026-10-05-menu-bar-status-design.md`

## Global Constraints

- **The menu app reads only** `$AGENT_DECK_STATE/status.json`, defaulting to
  `~/Library/Application Support/BusybarAgentDeck`. It never reads sources and never contacts
  the device.
- **Timing:**
  - menu poll: 5 s with 1 s tolerance;
  - *stopped* when `status.json` is missing, invalid, or more than 10 s old;
  - startup grace for `inputs_connected`: 10 s;
  - fingerprint re-check: 30 s.
- **Level order:** `degraded` > `update` > `paused` > `ok`. The app adds `stopped`.
- **Reason strings (exact):**
  - `Bar unreachable`
  - `Claude source error`
  - `Codex source error`
  - `State files unreadable`
  - `Bar buttons disconnected`
  - `New version on disk`
  - `Paused by the bar selector`
- **Symbols:**

  | Level | Symbol |
  | --- | --- |
  | `stopped` | `xmark.circle` |
  | `degraded` | `exclamationmark.triangle` |
  | `update` | `arrow.clockwise.circle` |
  | `paused` | `pause.circle` |
  | `ok` | `rectangle.split.3x1` |

- **Agent:** label `local.busybar.agent-deck.menubar`, binary at
  `<runtime>/menubar/AgentDeckMenu` with mode 0700.
- **Plist:**
  - `RunAtLoad`, `KeepAlive={SuccessfulExit: false}`, `LimitLoadToSessionType=Aqua`,
    `ProcessType=Interactive`;
  - environment: `AGENT_DECK_STATE`, `AGENT_DECK_PYTHON`, `AGENT_DECK_LABEL`.
- **Compilation:** `xcrun swiftc -swift-version 5 -O`. Use only APIs available on macOS 12.
- **UI copy is English**, like the rest of the project.

## Review Focus

1. A `status.json` whose `at` lies in the future (clock change) must read as *stopped*, not ok.
   The app treats `abs(now - at) > 10` as stale. This goes in Task 3's fixtures.
2. Reloading when the collector agent is not loaded makes `kickstart` exit non-zero. The menu
   must show `Last action failed: Reload collector` and keep running. Task 3 verifies it
   manually, because no UI test harness exists.
3. A health computed at the very first poll, before the WebSocket connects, must not flash
   *degraded*. This is the startup grace, tested in Task 1.
4. `agent-deck uninstall` without a menu bar plist must not issue extra launchctl calls, which
   existing tests count. Tested in Task 4.
5. A compile failure must leave any previous binary and plist untouched. Tested in Task 4.

---

### Task 1: Health model and code fingerprint

**Files:**
- Create: `agentdeck/health.py`
- Test: `tests/test_menubar.py`

**Interfaces:**
- Produces:
  - `health(status: dict, now: float, started_at: float) -> dict`, returning
    `{"level": str, "reasons": list[str]}`;
  - `code_fingerprint(root: Path) -> tuple`, a sorted tuple of
    `(relative_path, mtime_ns, size)` for `*.py` and `*.swift` under `root`, recursive.

- [ ] **Step 1: Failing tests** in `tests/test_menubar.py`, class `HealthTests`:
  - `test_ok_without_problems`: a healthy status gives `{"level": "ok", "reasons": []}`.
  - `test_degraded_reasons_and_priority`: `device_error`, `source_errors=["claude","codex","state"]`
    and `paused` give level `degraded`. The reasons contain all of `Bar unreachable`,
    `Claude source error`, `Codex source error`, `State files unreadable` and
    `Paused by the bar selector`, in that order.
  - `test_update_beats_paused`: `code_changed=True` with `paused=True` gives level `update`,
    and the reasons are `New version on disk` then `Paused by the bar selector`.
  - `test_buttons_grace_period`: `inputs_connected=False` gives `ok` at `now=started_at+5` and
    `degraded` (`Bar buttons disconnected`) at `now=started_at+11`.
  - `test_token_error_does_not_degrade`: `tokens_error` set gives `ok`.
  - `test_fingerprint_detects_edit_add_and_ignores_other_files`: in a temporary package, the
    fingerprint changes after editing a `.py`, after adding a `.swift`, but not after adding a
    `.png`.
- [ ] **Step 2:** Run `uv run python -m unittest tests.test_menubar -v` and expect an
  ImportError.
- [ ] **Step 3:** Implement both functions. A file that vanishes during the scan is skipped.
- [ ] **Step 4:** Run the same command and expect all tests to pass.
- [ ] **Step 5:** Commit.

### Task 2: Service publishes health

**Files:**
- Modify: `agentdeck/service.py` (`Service.run` loop and the status dict)
- Test: `tests/test_service_reliability.py`

**Interfaces:**
- Consumes: `health()` and `code_fingerprint()` from Task 1.
- Produces: `status.json` keys `pid` (int), `started_at` (float), `code_changed` (bool) and
  `health` (dict). `Service.package_root` defaults to `Path(agentdeck.__file__).parent`.

- [ ] **Step 1: Failing tests:**
  - `test_status_publishes_health_and_identity`: one iteration writes a status with
    `pid == os.getpid()`, a float `started_at`, `code_changed is False` and
    `health["level"] == "ok"`, when the inputs are connected and no errors occur.
  - `test_code_change_is_detected_after_recheck_interval`: `package_root` is a temporary
    directory, and the monotonic clock is patched across the 30 s boundary. After a file is
    touched between iterations, the second status has `code_changed is True` and
    `health["level"] == "update"`.
- [ ] **Step 2:** Run them and expect failures.
- [ ] **Step 3:** Implement. Take the fingerprint once before the loop and re-check when
  `monotonic - last_check >= 30`. `code_changed` latches. Compute health with the same values
  written to status.
- [ ] **Step 4:** Run the full suite and expect it to pass.
- [ ] **Step 5:** Commit.

### Task 3: Swift menu app with a headless `--state` mode

**Files:**
- Create: `agentdeck/menubar/AgentDeckMenu.swift`
- Test: `tests/test_menubar.py`, class `SwiftAppTests`

**Interfaces:**
- Consumes: the `status.json` keys from Task 2.
- Produces:
  - the `AgentDeckMenu` executable;
  - `AgentDeckMenu --state <path>` prints exactly one of `stopped`, `degraded`, `update`,
    `paused` or `ok` and exits 0.

- [ ] **Step 1: Failing test.** `SwiftAppTests` is skipped unless `xcrun` is present *and*
  either `CI` or `AGENT_DECK_SWIFT_TESTS` is set. `setUpClass` compiles into a temporary
  directory. `test_state_mode_levels` writes fixtures and asserts the output:
  - a missing file prints `stopped`;
  - invalid JSON prints `stopped`;
  - `at=now-30` prints `stopped`;
  - `at=now+60` prints `stopped`;
  - health `degraded` prints `degraded`, `update` prints `update`, `paused` prints `paused`
    and `ok` prints `ok`;
  - an unknown level string prints `degraded`.
- [ ] **Step 2:** Run with `AGENT_DECK_SWIFT_TESTS=1` and expect a failure (no source file).
- [ ] **Step 3:** Implement the app:
  - `StatusSnapshot.load(url, now)` handles parsing and staleness;
  - an `AppDelegate` owns the `NSStatusItem`, a 5 s `Timer` with `tolerance = 1`, and an
    `NSMenuDelegate` that rebuilds the menu in `menuNeedsUpdate`;
  - the actions use `Process` with a `terminationHandler` that dispatches to the main queue;
  - `main` handles `--state` before touching `NSApplication`.
- [ ] **Step 4:** Run the test again and expect it to pass. Also run `swiftc -typecheck`, which
  must produce no warnings that block the build.
- [ ] **Step 5:** Commit.

### Task 4: `agent-deck menubar install|uninstall` and full uninstall

**Files:**
- Modify: `agentdeck/install.py`:
  - add `MENU_LABEL`, `menubar_agent_path()`, `install_menubar()` and `uninstall_menubar()`;
  - add a `label` parameter to `_read_plist` and `_bootstrap`;
  - make `uninstall()` call the menu bar removal when its plist exists.
- Modify: `agentdeck/cli.py` (`menubar` subparser with an `action` choice of
  `install` / `uninstall`).
- Test: `tests/test_menubar.py`, class `MenuBarInstallTests`, reusing `FakeLaunchctl`-style
  per-target state.

**Interfaces:**
- Consumes: the Swift source path `Path(__file__).parent / "menubar" / "AgentDeckMenu.swift"`
  from Task 3.
- Produces: `install_menubar() -> None` and `uninstall_menubar() -> None`. Both raise
  `RuntimeError` with a user-facing message.

- [ ] **Step 1: Failing tests.** `subprocess.run` is patched: `xcrun` writes a fake binary to
  the `-o` path, and `launchctl` keeps a loaded set keyed by target.
  - `test_install_compiles_writes_plist_and_bootstraps`: the binary is at
    `runtime/menubar/AgentDeckMenu` with mode 0o700. The plist label, `KeepAlive`,
    `LimitLoadToSessionType` and environment match the constraints, and the agent is loaded.
  - `test_missing_compiler_changes_nothing`: when `xcrun --find swiftc` fails, a
    `RuntimeError` mentioning `xcode-select --install` is raised, and neither the plist nor the
    binary exists.
  - `test_compile_failure_keeps_previous_install`: install once, then fail the compile. The
    binary and plist bytes are unchanged, and the agent is still loaded.
  - `test_failed_bootstrap_restores_previous_binary_and_plist`.
  - `test_uninstall_menubar_removes_agent_plist_and_binary`.
  - `test_full_uninstall_removes_menubar_only_when_present`: with no menu bar plist,
    `uninstall()` issues no launchctl call for the menu label. With one, it boots it out and
    removes it.
- [ ] **Step 2:** Run them and expect failures.
- [ ] **Step 3:** Implement as the spec describes:
  1. Compile into `tempfile.mkdtemp(dir=runtime)`.
  2. Under the lock: stop the agent if loaded, rename the old binary to `AgentDeckMenu.previous`,
     then `os.replace` the new binary and `chmod 0o700`.
  3. Write the plist with `_replace_preserving`, then bootstrap with `label=MENU_LABEL`.

  On any exception, restore the previous binary and plist and re-bootstrap if the agent was
  loaded.
- [ ] **Step 4:** Run the full suite and expect it to pass.
- [ ] **Step 5:** Commit.

### Task 5: Docs, packaging check, changelog

**Files:**
- Modify: `README.md` (new "Menu bar status (optional)" section), `CHANGELOG.md` and
  `tests/wheel_smoke.py`.

- [ ] **Step 1:** `wheel_smoke.py` asserts that `agentdeck/menubar/AgentDeckMenu.swift` exists in
  the installed wheel.
- [ ] **Step 2:** Run `uv build`, then list the wheel and expect the Swift file in it.
- [ ] **Step 3:** The README covers:
  - install and uninstall commands, and the Command Line Tools requirement;
  - the icon states and the actions;
  - the `uv sync` note for dependency updates;
  - re-running `menubar install` after a Swift change;
  - the cost (one 1.5 KB read every 5 s).
- [ ] **Step 4:** Add the CHANGELOG entry.
- [ ] **Step 5:** Run the full suite with `AGENT_DECK_SWIFT_TESTS=1`, then install for real and
  check manually:
  - the five states;
  - the three actions.
- [ ] **Step 6:** Commit, push and open a PR based on `perf/libproc-pid-check-and-quiet-snapshots`.
