# Menu bar icon

<p align="center">
  <img src="menu-bar.png" width="540" alt="Busy Crew's menu, open from the macOS menu bar">
</p>

The bar shows sessions, not the collector feeding it. When the service stops, the device lease expires after 15 seconds and the bar goes dark without saying why. This optional macOS menu bar icon shows the collector's own health, with a menu of reasons and actions.

```sh
xcode-select --install                 # once, for the Swift compiler
busy-crew menubar install
busy-crew menubar uninstall
```

`menubar install` compiles a small AppKit app (`agentdeck/menubar/BusyCrewMenu.swift`) into the private runtime folder. It then starts the app with its own LaunchAgent (`local.busybar.agent-deck.menubar`) at login. A failed compile or start leaves any previous version in place. `busy-crew uninstall` also removes the icon.

| Icon | Meaning |
| --- | --- |
| `xmark.circle` | Stopped: no fresh `status.json` (missing, invalid, or older than 10 s) |
| `exclamationmark.triangle` | Degraded: the bar is unreachable, a source cannot be read, or the bar's buttons are disconnected |
| `arrow.clockwise.circle` | Update available: the collector's code changed on disk since it started, or the running collector predates health reporting |
| `pause.circle` | Paused by the bar's selector |
| The bar in outline with its LEDs | Running |

The menu shows:
- the reasons and the start time;
- one session count line per agent family, and today's token totals with when they were last counted;
- the actions:
  - **Acknowledge results**: the same as pressing the scroll wheel, enabled when there is something to acknowledge;
  - **Refresh tokens now**: runs ccusage immediately instead of within two minutes;
  - **Reload collector**: `launchctl kickstart -k`;
  - **Open log**.

**Quit menu icon** closes the icon until your next login. A token (`ccusage`) failure appears as a note and does not degrade the icon.

The icon is cheap to run: it reads the 1.5 KB `status.json` every 5 seconds and when you open the menu. It never reads Claude or Codex data and never contacts the bar.

After updating Busy Crew, run `busy-crew install` (it restarts the collector) and `busy-crew menubar install`. In a checkout, **Reload collector** is enough for Python-only changes; run `uv sync --locked` first if dependencies changed.
