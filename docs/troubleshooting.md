# Troubleshooting and recovery

Run `busy-crew --version` and `busy-crew status` first. In a checkout, use `uv run busy-crew`.

Status omits conversation identifiers and separates source, input and device errors. If the service cannot start, check `service.log` in the [state folder](installation.md#edit-configuration-and-reload). Redact paths and secrets before sharing diagnostics.

## Common problems

**The bar went dark.** Check `busy-crew status` first; the [menu bar icon](menu-bar.md) shows the same at a glance. Three causes look alike on the bar:

- the collector stopped, so its 15-second display lease expired and the bar returned to its own display (no fresh `status.json`, menu bar `xmark.circle`);
- the collector is running but the selector is on Off or Settings, which clears the display (`"paused": true`, menu bar `pause.circle`);
- the collector is running but a higher-priority application owns the display; Busy Crew does not raise its priority to take it back.

The icon cannot detect another app's screen ownership. **Running** confirms the collector's health, not that its frame is visible.

**The bar is unreachable, or the log reports HTTPError.** For Wi-Fi, enable the local HTTP API and set its password as `token` in `config.json`. Missing or incorrect credentials produce a 403. Use the bar's IP if its hostname does not resolve, and omit `/api` from `address`. Follow [Wi-Fi setup](installation.md#connect-over-wi-fi), then reload.

**Token views show `?`.** `ccusage` is missing, failed or stale; sessions still work. Check `ccusage --version` and `command -v ccusage`, set the executable's absolute path in `config.json`, then reload. See [token setup](installation.md#token-counts-optional).

**A config change has no effect.** Choose **Reload collector**, or run:

```sh
launchctl kickstart -k gui/$UID/local.busybar.agent-deck
```

`busy-crew refresh` only recounts tokens with the configuration already loaded.

**`busy-crew: command not found`.** A new uv install may need `uv tool update-shell` and a reopened terminal. Check `command -v busy-crew`; in a checkout, use `uv run busy-crew`.

**`invalid choice: 'menubar'` or `'refresh'`.** Check `command -v busy-crew` for an older copy ahead of the intended one in PATH, then follow [update instructions](installation.md#updates-and-removal). For a checkout, pull the intended branch and run `uv sync --locked`. The pre-1.0 `agent-deck` command remains an alias.

**A Claude session's hooks don't seem active.** For an already open session, check that they appear in `/hooks`; restarting that session loads the new configuration.

**Claude never shows a finished `✓`.** With `--no-claude-hooks`, a busy → idle transition stays gray: it does not establish a successful response. Run `install` with hooks enabled if you want completion detection.

## Claude settings conflicts

If Claude settings contain invalid JSON, a symlink, or an unexpected structure, fix that file yourself before installing with hooks; the installer refuses to replace it. A fresh passive installation does not touch unrelated Claude settings.

For a concurrent-edit error:

1. Close other settings writers.
2. Inspect the private copies in the state's `recovery` folder. For settings on another volume, look beside them in `.agent-deck-recovery`.
3. Compare them with current Claude settings and merge only the intended changes.
4. Re-run install, repeating `--no-claude-hooks` for passive tracking.

Do not replace the entire settings file with an old backup while Claude or an editor is changing it. Replacing existing settings requires atomic-exchange support; creating a new settings file does not.

## Install, uninstall and moving the checkout

An installation failure attempts to restore the previous configuration, plist and service. Any rollback failure is reported with recovery files retained.

Read the uninstall error before retrying: it may concern stopping the collector, updating Claude settings, restoring a failed change, or removing only the menu icon. The collector may already be removed when menu cleanup fails. Keep remaining plist/runtime files until the error is resolved.

Runtime backups and acknowledgment data remain after uninstall; delete that private folder yourself once no longer needed.

To move a checkout, move it, run `uv sync --locked`, then `uv run busy-crew install` from the new location. Repeat `--no-claude-hooks` for passive tracking. Re-run `uv run busy-crew menubar install` too if you use the icon.

## Not covered by the test suite

Real macOS login/restart, sleep/wake, device disconnect/reconnect and selector state at startup require testing on a dedicated account/device. Those scenarios are not established by the synthetic suite.
