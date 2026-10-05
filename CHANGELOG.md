# Changelog

## Unreleased

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

## 0.1.0

Initial local Claude/Codex front-display monitor, controls and optional daily
ccusage token view. No remote-machine tracking.
