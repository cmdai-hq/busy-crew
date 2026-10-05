# Menu bar status for the collector

Date: 2026-10-05. Status: approved in conversation, implementation authorized.

## Intent

The BUSY Bar shows sessions. It cannot show the health of the collector feeding it. When the
service stops, the device lease expires after 15 s and the bar goes dark without saying why.
When the service runs stale code (an editable checkout updated after it started), nothing
says so either. Only `agent-deck status` exposes this today.

Goal: an optional macOS menu bar icon showing the collector's health at a glance, with a menu
that gives the reasons and three actions: acknowledge results, reload the collector, open the
log. It ships with the project, is optional and documented, and is tested in CI.

Constraint carried over from the performance pass: the icon must stay cheap. It reads
`status.json` only. It never re-reads Claude/Codex sources and never contacts the device.

## Architecture

```
service (Python, every 1 s) ──► status.json ──► menu app (Swift, every 5 s and on menu open)
  + health {level, reasons}                       ├─ icon: stopped / degraded / update / paused / ok
  + code_changed, started_at, pid                 └─ menu: reasons, counts, actions
```

- **Health is computed in Python** (`agentdeck/health.py`) and written to `status.json` each
  poll, so the existing unittest suite covers it. The Swift app only derives one thing itself:
  *stopped*, meaning `status.json` is missing, unreadable, invalid, or older than 10 s. A fresh
  `status.json` without `health` comes from a collector that predates this feature. It reads
  as *update*, with the reason `Collector predates health reporting` (final review ruling).
- **The menu app polls; it does not watch.** `status.json` is atomically replaced every second.
  A file watch would therefore fire every second and need re-arming on each new inode. A 5 s
  timer with 1 s tolerance reading 1.5 KB is cheaper, and 5 s is fresh enough for health.
- **Actions reuse existing mechanisms:**
  - acknowledge: `<python> -I -m agentdeck.cli ack`;
  - reload: `launchctl kickstart -k gui/<uid>/local.busybar.agent-deck`;
  - open log: `NSWorkspace.open(service.log)`.

  No new command channel is added.

## Health model (`status.json → health`)

`level` is the first matching row; `reasons` lists every matching row except `ok`.

| level | condition | reason text |
| --- | --- | --- |
| `degraded` | `device_error` set | `Bar unreachable` |
| `degraded` | each provider in `source_errors` | `Claude source error`, `Codex source error`, `State files unreadable` |
| `degraded` | `inputs_connected` is false more than 10 s after start | `Bar buttons disconnected` |
| `update` | `code_changed` | `New version on disk` |
| `paused` | `paused` | `Paused by the bar selector` |
| `ok` | none of the above | — |

A token (`ccusage`) failure does not change the level, because the tool is optional. The menu
shows it as a note.

`code_changed`:
- The service fingerprints `(relative path, mtime_ns, size)` of every `*.py` and `*.swift` file
  under its own package directory at start.
- It re-checks every 30 s. The value latches true once a difference is seen.
- This works without git, and for wheel installs, whose files only change on reinstall.
- A dependency change (`uv.lock`) is out of scope. The README says to run `uv sync` before
  reloading.

## Menu app (`agentdeck/menubar/AgentDeckMenu.swift`)

- A single-file AppKit app: `NSStatusItem`, accessory activation policy, no Dock icon and no
  app bundle. Compiled in Swift 5 language mode for older toolchains. Targets macOS 12+.
- **Icon:** monochrome template SF Symbols.

  | Level | Symbol |
  | --- | --- |
  | `stopped` | `xmark.circle` |
  | `degraded` | `exclamationmark.triangle` |
  | `update` | `arrow.clockwise.circle` |
  | `paused` | `pause.circle` |
  | `ok` | drawn bar outline with two rows of LEDs (22×12 pt template image; `rectangle.split.3x1` read as a generic layout icon) |

- **Menu:**
  - a title line `Agent Deck — <level text>`, followed by one disabled line per reason, a
    `Started <relative time>` line, and the token note when relevant;
  - one count line per agent family, for example `Claude: 3 working · 1 ✓`;
  - when `code_changed`, the item `New version on disk — Reload collector`;
  - the actions `Acknowledge results` (enabled only when the collector publishes
    `acknowledgeable > 0`, the number of results OK would acknowledge on the bar right now),
    `Reload collector` and `Open log`;
  - `Quit menu icon` at the end.
- **Actions** run asynchronously. A non-zero exit adds a `Last action failed: <name>` line
  until the next successful action.
- **Testability:** `AgentDeckMenu --state <status.json>` prints the computed level and exits,
  without starting any UI.

## Installation

- `agent-deck menubar install` and `agent-deck menubar uninstall` are separate from
  `agent-deck install`. Compiling (Command Line Tools, up to tens of seconds) must not be able
  to fail or roll back the collector's own transactional installation.
- **`menubar install`:**
  1. Preflight: macOS, `launchctl`, and `xcrun --find swiftc`. If the compiler is missing, it
     fails with `run xcode-select --install`, and nothing is changed.
  2. Compile with `xcrun swiftc -swift-version 5 -O` into a private temporary directory inside
     the runtime folder.
  3. Under the installation lock:
     - stop the menu agent if loaded;
     - move the binary to `<runtime>/menubar/AgentDeckMenu` (mode 0700), keeping the previous
       binary for rollback;
     - write `~/Library/LaunchAgents/local.busybar.agent-deck.menubar.plist` with the existing
       preserving writer;
     - bootstrap and verify that the agent is loaded.

     Any failure restores the previous binary and plist and restarts the previous agent.
- **The plist:**
  - `RunAtLoad`, `KeepAlive = {SuccessfulExit: false}` (restarted after a crash, not after
    Quit), `LimitLoadToSessionType = Aqua`, `ProcessType = Interactive`;
  - environment: `AGENT_DECK_STATE`, `AGENT_DECK_PYTHON` (the installing interpreter, used for
    `ack`), `AGENT_DECK_LABEL`.
- **Uninstall:** `menubar uninstall` stops the agent and removes the plist and the binary.
  `agent-deck uninstall` also removes the menu bar agent when its plist exists, so a full
  uninstall stays complete. Without that plist, it makes no extra launchctl calls.
- **Updates:** the Swift source is part of the package. When it changes, `code_changed`
  flags it, and the user re-runs `agent-deck menubar install`.

## Error handling

- **The menu app never crashes on data.** A missing, unreadable or invalid `status.json` shows
  `stopped` with the reason.
- **Actions are guarded:** `Acknowledge` is disabled when stopped or paused. A failed action is
  shown as text and never as a modal dialog.
- **Install failures:** they leave the previous state, using the same rollback pattern as
  `install()`.

## Testing

- **Python:**
  - health levels, their priority and the startup grace period;
  - fingerprint change detection on temporary files;
  - the new `status.json` fields written by the service;
  - `menubar install`/`uninstall` with simulated `xcrun` and `launchctl`: success, missing
    compiler, compile failure, failed bootstrap rollback;
  - `uninstall` also removing the menu agent.
- **Swift:**
  - a test compiles the app and runs `--state` against fixtures for all five levels;
  - it runs in CI (`CI` set) or with `AGENT_DECK_SWIFT_TESTS=1`, so local runs stay fast.
- **Manual check:**
  - stop the service, then check the icon reads `stopped`;
  - set the selector to Off, then check `paused`;
  - touch a source file, then check `update`;
  - exercise the three actions.

## Out of scope

- Session counts in the menu bar title.
- View switching from the menu.
- Detecting dependency changes.
- Rebuilding the menu app automatically.
- Signing or notarization: the binary is compiled locally, so no quarantine applies.
