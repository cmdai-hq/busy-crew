# Publication-readiness review

The read-only review of revision `ba46aff` on 2026-10-04 identified 22 concrete
findings. This change addresses the implementation findings with isolated
regressions. The original review preceded the owner's decision to use PolyForm
Noncommercial; publication is now **source-available**, with the terms in LICENSE.

| Findings | Change | Evidence |
| --- | --- | --- |
| A01 | Isolate Python hook/service imports from the active project | Command regression and installed-wheel shadow-package smoke |
| A02–A03 | Preserve displaced settings on conflicts, preflight and recover failed installation | Late-write and failure-injection fixtures; native atomic exchange in temporary files |
| A04–A05 | Reject REST redirects; identify Agent Deck in the DELETE query | Real urllib redirect handler with fake transport; request regression against the firmware 1.2.4 contract |
| A06–A10 | Verify launchctl state, exact hook ownership, refuse symlinks, passive migration, pinned custom roots | Isolated install/uninstall lifecycle fixtures |
| A11 | Validate bounded JSON and isolate invalid records/commands | Corrupt and unexpected schemas alongside valid provider data |
| A12–A15 | Explicit terminal outcomes, interaction IDs, bounded backward turn scan, durable result identity | Error/abort, concurrent tools, long rollouts, acknowledgment and restart fixtures |
| A16 | Reject incompatible ccusage reports and retry after failure | Schema, subprocess limits and worker recovery fixtures |
| A17–A18 | Acknowledge represented results only; back off on every stream closure | Tokens/overflow/lease fixtures and simulated normal/error WebSocket closure |
| A19–A22 | Idempotent settings, consistent usage snapshot, explicit SQLite closure, width-bounded tokens | No-change, interleaving, resource and pixel-boundary regressions |

The suite never installs real hooks, invokes launchctl against a user account,
modifies agent databases, starts an AI model or contacts hardware. Python 3.11
and 3.14 runs, source/wheel builds and an installed-wheel CLI/demo smoke are
separate checks. Green CI is evidence for those checks, not for native device
and login lifecycle behavior.

Remaining publication decisions and limits:

- The existing sprites are retained. Their precise provenance and redistribution
  rights have not been established; resolve that or replace them before a public
  release. The code license does not grant rights in third-party characters.
- Claude's native registry and Codex's SQLite/rollout formats are internal and
  can change. Codex approvals have partial local-event coverage. Oversized or
  incompatible records produce unknown/error status rather than inferred work.
- A macOS test account with real launchd, device reconnect, sleep/wake and selector
  startup checks is still needed. Current lifecycle tests simulate launchctl;
  the cleanup fix follows official firmware sources rather than a destructive
  test on a user's device.
- Atomic exchange preserves displaced files and supports conflict recovery.
  It cannot serialize arbitrary external writers after a commit or guarantee
  one atomic transaction across launchd and several files. Recovery copies remain
  private and require a deliberate merge after a reported conflict.
- Runtime backups and per-session lock files remain locally for recovery/safe
  synchronization. Remove runtime data only after uninstalling and closing the
  affected sessions. Logs and command processing are bounded independently.

See [COMPATIBILITY](COMPATIBILITY.md), [SECURITY](../SECURITY.md) and the README
for supported contracts, diagnostics and installation recovery.
