# Changelog

## Unreleased

- The collector costs less CPU. It reads Claude's hook snapshots again only
  when one changes: it reread every snapshot left since installation, about 160
  after a week, every second. It also slows to a read every 2.5 seconds when
  no session works or waits and nothing changed for 10 seconds. A button press
  wakes it at once; a `busy-crew` or menu command, or a write from Claude,
  within a second. The bar's lease is still renewed every 5 seconds.
- A Codex turn that writes more than 32 MB to its journal (a long run of tool
  calls) no longer turns the Codex family into `?` with "Selected turn boundary
  unavailable". The collector finds the turn's start once, reading back up to
  256 MB, then reads only what Codex appends: about 1 ms per change instead of
  rereading up to 32 MB every second.
- **Update to X** works on Macs whose Homebrew is managed by Workbrew: it now
  runs `brew` through the wrapper Homebrew requires (`HOMEBREW_FORCE_BREW_WRAPPER`,
  from the environment or `brew.env`), even with launchd's minimal `PATH`.
- **Update to X** runs `brew update` before `brew upgrade busy-crew`, so a
  release published minutes ago is found even when Homebrew has not refreshed
  its taps yet. The README's manual update does the same.

## 1.1.1 — 2026-10-11

- The menu bar icon is now the duo: a rounded square split into Claude's and
  Codex's faces. **Update to 1.1.1** in the menu installs it; after a manual
  `brew upgrade` or `uv tool upgrade`, run `busy-crew menubar install`.
- CONTRIBUTING explains how to develop on the Mac that runs your bar, with the
  everyday copy installed from Homebrew.

## 1.1.0 — 2026-10-11

- Install without a checkout: `brew install cmdai-hq/tap/busy-crew` or
  `uv tool install busy-crew`, then `busy-crew install`. Each GitHub release
  now publishes the package to PyPI and updates the Homebrew formula.
- Offer new releases: once a day the collector asks GitHub for the latest
  release (no credentials, no data about your agents; `"update_check": false`
  in `config.json` turns it off). The menu bar icon then shows "Version X
  available" and **Update to X**, which runs the new `busy-crew upgrade`:
  `brew upgrade` or `uv tool upgrade`, then `install` with the new version and
  a reinstall of the menu bar app. A failed check never degrades the status.
- `install` and `menubar install` write an interpreter path that survives
  package upgrades: inside Homebrew's versioned `Cellar` folder, they use the
  formula's `opt` link instead. `--python` or `BUSY_CREW_PYTHON` choose
  another interpreter, and `install` checks that it can run Busy Crew before
  changing anything.
- The README describes the Homebrew, uv and checkout installs, and its links
  work on the PyPI page.

## 1.0.1 — 2026-10-11

- A Codex conversation with a journal line over 2 MB (a large tool output)
  no longer shows `?` for the whole Codex family for up to a day: the line is
  left out of the turn. Turn events and function calls that large still fail
  the read.
- An unreadable Codex journal is no longer rescanned every second until it
  changes, and the service log names why a source failed.
- Read less: Codex journal records that cannot change a turn (messages,
  reasoning, tool calls other than questions, tool outputs) are recognized from
  their first bytes and never decoded, which also makes reading about four times faster; only the
  needed Codex columns are queried. Claude hook fingerprints are keyed with a
  per-installation secret (fingerprints recorded before the upgrade are migrated), and `front.png` and `service.lock` are private.
- Document what Busy Crew reads, keeps and sends in
  [PRIVACY](docs/privacy.md).

## 1.0.0 — 2026-10-11

First public release, under the MIT License.

- Rename the command to `busy-crew`, the package to `busy-crew` and the menu
  bar app to Busy Crew. `agent-deck` stays as an alias and `AGENT_DECK_STATE`
  is still honored next to `BUSY_CREW_STATE`. New installations use
  `~/Library/Application Support/BusyCrew`; existing ones keep their folder,
  LaunchAgent and hooks. After pulling, run `uv sync --locked` to get the new
  command, and `uv run busy-crew menubar install` to rename the menu bar app.
- License the project under MIT instead of PolyForm Noncommercial.
- Add a month-to-date token view: Start / Pause cycles sessions, today and
  this month, and the scroll wheel steps between them. Each token view names
  its period at the bottom.
- Treat a finished or failed Codex conversation silent for two hours as dead:
  it leaves the count with its `✓`, instead of piling up until acknowledged.
- Keep open Claude sessions visible after work completes and after acknowledgment;
  show a sleeping indicator when the Claude family is entirely idle.
- Isolate global hook imports, record owned hook commands, respect custom roots,
  and make installation conflicts/failures recoverable.
- Refuse credential-bearing redirects and scope display cleanup to Busy Crew.
- Validate local source and command data, isolate malformed records, and preserve
  successful completion identities across monitor restarts.
- Correlate pending interactions, recover long-turn boundaries, and distinguish
  failed/aborted work from successful responses.
- Limit acknowledgment to displayed results, recover from WebSocket closure and
  token-worker errors, bound commands/logs, and close SQLite connections promptly.
- Add macOS/Python CI, package metadata, wheel smoke tests and recovery/security
  documentation. Adapter versions and limitations are listed in
  [COMPATIBILITY](docs/COMPATIBILITY.md).
- Verify Claude process identity through libproc on macOS instead of forking `ps`
  every poll, keeping `ps` as the fallback where libproc is unavailable; skip
  fsync for the per-poll status snapshot and rewrite `front.png` only when the
  frame changes.
- Center the Codex character on Clawd in the duo view and keep its face visible
  when dimmed.
- Add an optional macOS menu bar icon (`uv run busy-crew menubar install`) showing the
  collector's health (stopped, degraded, update available, paused), with
  acknowledge, reload and open-log actions. The collector publishes `health`,
  `code_changed`, `pid` and `started_at` in `status.json`.
- Add `agent-deck refresh` and a **Refresh tokens now** menu bar action to recount
  today's tokens immediately, instead of waiting up to two minutes. The menu also
  shows the token totals and when they were counted.

## 0.1.0

Initial local Claude/Codex front-display monitor, controls and optional daily
ccusage token view. No remote-machine tracking.
