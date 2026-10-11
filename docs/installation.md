# Installation

## Install the package

Choose Homebrew or [uv](https://docs.astral.sh/uv/):

```sh
brew install cmdai-hq/tap/busy-crew
# or:
uv tool install busy-crew
```

If a uv installation succeeds but `busy-crew` is not found, run `uv tool update-shell` and reopen your terminal.

## First connection: USB

1. Connect the bar to your Mac by USB.
2. Open [http://10.0.4.20](http://10.0.4.20) to confirm it is reachable.
3. Install the collector, using your IANA time zone:

```sh
busy-crew install --address http://10.0.4.20 --timezone Europe/Paris
```

Wait a few seconds, then run `busy-crew status`. A connected collector reports no `device_error` and `inputs_connected: true`. Source errors are listed separately.

`health.level` can be `ok`, `paused` (selector on Off/Settings) or `update` (an update is available or a reload is needed). Move the selector to an active position to see the sessions.

Restart already-open Claude sessions if the new hooks do not appear in `/hooks`. To track Claude without hooks, add `--no-claude-hooks`; this mode does not infer a finished `✓` from busy → idle.

## Connect over Wi-Fi

The bar's Wi-Fi API is **disabled by default** and requires a password. Prepare it while connected by USB:

1. Open [http://10.0.4.20](http://10.0.4.20). Under **Network → HTTP API**, enable **HTTP API access** and set a password.
2. Find the Wi-Fi IP under **Settings → Wi-Fi → your network → View IP Address** on the bar.
3. Edit Busy Crew's [`config.json`](#edit-configuration-and-reload), preserving its other settings. Change these two values:

```json
{
  "address": "http://192.168.1.20",
  "token": "your-local-api-password"
}
```

Use your bar's IP. The address must start with `http://` or `https://` and must **not** end in `/api`. `token` is the local Wi-Fi password, not a BUSY cloud token.

4. Reload the collector as described below, then check `busy-crew status`.

For a first install directly over Wi-Fi, run `busy-crew install --address http://YOUR_BAR_IP` to create the configuration, then add the password and reload. Connection errors are expected until the password is configured.

See [BUSY's API setup](https://docs.busy.app/bar/dev/http-api#wi-fi-access) for the device settings. Over plain HTTP, frames and the password are unencrypted; use a trusted network.

## Edit configuration and reload

To open `config.json` for a new installation:

```sh
open -e "$HOME/Library/Application Support/BusyCrew/config.json"
```

Pre-1.0 installations keep `BusybarAgentDeck`; a custom `BUSY_CREW_STATE` overrides the location. See the [example](config.example.json) and [configuration keys](../README.md#configuration).

Changes take effect when the collector restarts. Choose **Reload collector** in the menu bar, or run:

```sh
launchctl kickstart -k gui/$UID/local.busybar.agent-deck
```

`busy-crew refresh` recounts tokens; it does not reload configuration.

## Token counts (optional)

Install a persistent [ccusage](https://ccusage.com/guide/installation) 20+ command. For example, with Node.js and npm installed:

```sh
npm install -g ccusage
ccusage --version
command -v ccusage
```

Set `ccusage` in `config.json` to the absolute path printed by the last command, then reload the collector. Its Node.js or Bun runtime must also be available in the collector's PATH, captured during `install`.

Use an executable path, not `npx ccusage@latest` or `bunx ccusage`. Busy Crew adds the report arguments itself and runs ccusage offline. Run `busy-crew refresh` to start a recount; a report can take up to 45 seconds.

Sessions work without ccusage. Token views show `?` when totals are unavailable.

## Updates and removal

For Homebrew and uv installs, use:

```sh
busy-crew upgrade
```

It preserves your settings and hook choice, restarts the collector, and reinstalls the menu icon if present. Update output is saved in `upgrade.log` in the state folder.

For a manual package update, run `brew update && brew upgrade busy-crew` or `uv tool upgrade busy-crew`, followed by `busy-crew install`. **Repeat `--no-claude-hooks` if you use passive tracking.** Run `busy-crew menubar install` too if you use the icon.

From a checkout:

```sh
git pull --ff-only
uv sync --locked
uv run busy-crew install   # add --no-claude-hooks for passive tracking
```

Reinstall the menu icon with `uv run busy-crew menubar install` if you use it. For developing alongside a released copy, see [CONTRIBUTING](../CONTRIBUTING.md#developing-on-the-mac-that-runs-your-bar).

To remove Busy Crew, run `busy-crew uninstall` before `brew uninstall busy-crew` or `uv tool uninstall busy-crew`. Private state and backups remain for recovery; delete that folder yourself when no longer needed.
