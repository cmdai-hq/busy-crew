# Changelog

## Unreleased

- Add a month-to-date token view: Start / Pause cycles sessions, today and
  this month, and the scroll wheel steps between them. Each token view names
  its period at the bottom.
- Treat a finished or failed Codex conversation silent for two hours as dead:
  it leaves the count with its `✓`, instead of piling up until acknowledged.
- Keep open Claude sessions visible after work completes and after acknowledgment;
  show a sleeping indicator when the Claude family is entirely idle.
- Isolate global hook imports, record owned hook commands, respect custom roots,
  and make installation conflicts/failures recoverable.
- Refuse credential-bearing redirects and scope display cleanup to Agent Deck.
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
- Add an optional macOS menu bar icon (`agent-deck menubar install`) showing the
  collector's health (stopped, degraded, update available, paused), with
  acknowledge, reload and open-log actions. The collector publishes `health`,
  `code_changed`, `pid` and `started_at` in `status.json`.
- Add `agent-deck refresh` and a **Refresh tokens now** menu bar action to recount
  today's tokens immediately, instead of waiting up to two minutes. The menu also
  shows the token totals and when they were counted.

## 0.1.0

Initial local Claude/Codex front-display monitor, controls and optional daily
ccusage token view. No remote-machine tracking.
