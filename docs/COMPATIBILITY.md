# Compatibility and distribution

Busy Crew's installer supports macOS and Python 3.11+. CI covers Python 3.11,
3.14 and 3.15 on macOS and Linux using synthetic data; Linux is a test environment,
not a supported LaunchAgent installation target.

| Source | Contract used | Validation |
| --- | --- | --- |
| Claude Code | Documented status hooks; internal `sessions/*.json` registry with identity, PID, process start and status | Synthetic fixtures and earlier local observation; internal registry is not a versioned public API |
| Codex desktop | Internal `state_5.sqlite`, `thread_history_1.sqlite` and rollout event records | Synthetic latest-turn, failure, wait and lifecycle fixtures; not a stable desktop API |
| BUSY Bar | Front display API, firmware 1.2.4 / OpenAPI 27.5.0 | Earlier hardware XPM write and BGR888 readback; current cleanup regression uses mocked transport |
| busylib | 2.6.0, pinned | Installed SDK inspected; input/reconnect behavior tested with mocks |
| ccusage | 20.0.26 `daily --by-agent --json --offline --no-cost` | Official report shape checked and synthetic parser/process tests; optional local executable |

The documentation audit on 2026-10-11 passed 254 tests on macOS/Python 3.11.13,
including Swift compilation and menu tests. A fresh uv environment and the
installed PyPI 1.1.2 package passed their smoke checks. No hardware was retested.

Earlier observations did not record Claude Code or Codex app version numbers.
No minimum agent version is established; their internal formats can change.

Codex approval indicators are partial because not every approval is written to
local rollouts. The monitor cannot infer missing interaction events. It monitors
this Mac, not remote/cloud agents. A source error is reported explicitly without
inventing a session count. See [how it works](how-it-works.md) for heuristic age limits and startup
behavior.

The project code is licensed under the MIT License. Python wheels include its
LICENSE. Dependencies
are installed separately rather than copied into the wheel. busylib is MIT,
Pillow is MIT-CMU, and zeroconf is LGPL-2.1-or-later; the remaining installed
dependencies have their own notices. Anyone distributing a standalone bundle
must inspect and preserve the licenses of everything in that bundle and satisfy
their applicable obligations. This repository does not distribute that bundle.

Do not interpret the project code license as permission to use another project's
logos or trademarks. This project is independent of Anthropic, OpenAI and BUSY.
