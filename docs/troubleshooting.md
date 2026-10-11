# Troubleshooting and recovery

Run `uv run busy-crew --version` and `uv run busy-crew status` first. Status omits conversation identifiers and reports source, input and device errors separately. Check the private runtime log (`service.log` in the state folder) when the service cannot start; redact local paths and secrets before sharing diagnostics.

## Common problems

**The bar went dark.** Check `uv run busy-crew status` first; the [menu bar icon](menu-bar.md) shows the same at a glance. Three causes look alike on the bar:
- the collector stopped, so its 15-second display lease expired (no fresh `status.json`, menu bar `xmark.circle`);
- the collector is running but the selector is on Off or Settings, which clears the display (`"paused": true`, menu bar `pause.circle`);
- the collector is running but a higher-priority application owns the display; Busy Crew does not raise its priority to take it back.

**Token views show `?`.** `ccusage` is missing, failed, or its data is stale. It is optional: its failure affects the token views, not session monitoring. Set its absolute path as `ccusage` in `config.json` if the LaunchAgent cannot find it.

**`busy-crew: command not found`, or `invalid choice: 'menubar'` (or `'refresh'`).** The command comes from an older checkout or from another installed copy. Check that `git remote -v` points to `cmdai-hq/busy-crew`. Then run `git switch main && git pull --ff-only` and `uv sync --locked`, and always launch it with `uv run busy-crew …` from the checkout. The pre-1.0 `agent-deck` command remains an alias.

**A Claude session's hooks don't seem active.** For an already open session, check that they appear in `/hooks`; restarting that session loads the new configuration.

## Claude settings conflicts

If Claude settings contain invalid JSON, a symlink, or an unexpected structure, fix that file yourself before installing with hooks; the installer refuses to replace it. A fresh passive installation does not touch unrelated Claude settings.

For a reported concurrent edit, close writers and inspect the private copies in the runtime's `recovery` folder. If settings are on another volume, recovery copies are kept beside them in a private `.agent-deck-recovery` folder because atomic exchange requires the same filesystem. Compare copies with the current Claude settings, then merge only the intended changes. Do not overwrite the entire file with an old backup while Claude or an editor is changing it. Re-run install after resolving the conflict. A filesystem without atomic-exchange support is refused without falling back to an unsafe replacement.

## Install, uninstall and moving the checkout

An installation failure attempts to restore the previous configuration, plist and service. Any rollback failure is reported with recovery files retained.

An uninstall error means the stop was not confirmed: retain the plist/runtime files and resolve the launchctl error before retrying. Runtime backups and acknowledgment data remain after uninstall; delete that private folder yourself once you no longer need it.

To move the checkout or environment, uninstall using the old environment, move it, sync dependencies, then reinstall.

## Not covered by the test suite

Real macOS login/restart, sleep/wake, device disconnect/reconnect and selector state at startup require testing on a dedicated account/device. Those scenarios are not established by the synthetic suite.
