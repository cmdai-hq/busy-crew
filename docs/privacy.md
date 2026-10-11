# Privacy

Busy Crew runs on your Mac and talks to your BUSY Bar, at the address in `config.json`. Once a day it also asks GitHub for the number of its latest release. There is no telemetry, no cloud service and no account.

## What is sent

| To | What | How |
| --- | --- | --- |
| Your BUSY Bar | A 72 × 16 pixel frame, and requests to clear Busy Crew's own frame | HTTP on your network; Wi-Fi uses the local API password in a header, USB needs none |
| Your BUSY Bar | A subscription to its button events (OK, Back, Start, the wheel, the selector) | WebSocket through the busylib SDK, which puts the key in the URL query |
| Your BUSY Bar, only when you run `snapshot` | A request for the bar's current screen | HTTP, as above |
| GitHub (`api.github.com`), at most once a day | A request for Busy Crew's latest release, with Busy Crew's version in the `User-Agent` header. No data about your agents or your bar | HTTPS, without credentials. GitHub sees your IP address. `"update_check": false` in `config.json` turns it off |
| Homebrew or PyPI, only when you choose **Update to X** in the menu bar icon or run `busy-crew upgrade` | What `brew upgrade` or `uv tool upgrade` sends to download the new version | Run by those tools |

Nothing else leaves the Mac. Over plain HTTP, someone on your network could see the frames, which show agent counts and token totals, and the API key. Use a trusted network, or HTTPS if your device supports it.

## What is read

| Source | What Busy Crew uses | What it never uses |
| --- | --- | --- |
| Claude Code session registry, `~/.claude/sessions/*.json` | Session ID, process ID and start time, busy/idle status, timestamps | Transcripts: Busy Crew does not open `~/.claude/projects` |
| Claude Code hook events (optional, `--no-claude-hooks` turns them off) | Session ID, event name, tool call IDs, timestamps, and a keyed fingerprint of a tool call's name and input to match a permission request to its call | The prompt text and tool inputs that Claude Code sends to every hook: read from stdin, never stored |
| Codex `state_5.sqlite`, opened read-only | Conversation ID, journal path, last update time | Titles, first messages, previews |
| Codex `thread_history_1.sqlite`, opened read-only | Last turn ID, its status and completion time | Every other column |
| Codex conversation journal | Turn start, end, failure and interruption events; approvals; question calls (`request_user_input`) and the call ID of their answer | Recognized messages, reasoning, other tool calls, tool outputs and token counts are left undecoded |
| ccusage (optional, a third-party tool) | Today's and this month's total tokens per family | ccusage itself reads your Claude Code and Codex usage logs to count them |

A first read scans backward up to 256 MB to find the selected turn's start. Later reads process only appended records. Journal bytes pass through memory; recognized messages, reasoning, other tool calls, tool outputs and token counts are skipped before JSON decoding.

Turn events and question calls are decoded in memory when needed and can include question text or the agent's last message. Those records are cached for the current turn. Conversation text is never written to Busy Crew's state files, logged or sent.

## What is kept

Everything lives in a private folder (directories `0700`, data files and locks `0600`, the menu bar app's executable `0700`): `~/Library/Application Support/BusyCrew`, or `BusybarAgentDeck` for installations made before 1.0, or `BUSY_CREW_STATE`.

| File | Contents |
| --- | --- |
| `config.json` | The bar's address, time zone, ccusage path, and the bar's API key if you set one |
| `status.json` | Session and turn IDs, their states, token totals, collector health |
| `completions.json` | Session and turn IDs of finished responses, completion times and process identity when available |
| `acknowledged.json` | Session and turn IDs of responses you acknowledged |
| `hooks/*.json` | One snapshot per Claude session: IDs, timestamps, keyed tool fingerprints; cleaned up hourly as described below |
| `hooks/*.lock` | Empty per-session synchronization files, retained to keep concurrent hooks on the same lock |
| `fingerprint.key` | The random per-installation key for those fingerprints, so a fingerprint cannot be matched against guessed commands |
| `service.log` | Starts, stops and source or device status, with Busy Crew's own error messages only: never a path or conversation content |
| `front.png` | The last frame sent to the bar |
| `update.json` | When the latest release was last checked, and its version |
| `upgrade.log` | The output of the last `busy-crew upgrade` |
| `claude-settings-*.json`, `recovery/` | Backups of your Claude settings made before installing or removing hooks |

While running, the collector checks hook snapshots once an hour. It deletes
those older than 24 hours, and those older than an hour whose session is
absent from Claude's native registry. Unreadable snapshots are retained and
reported as a source error; an unreadable registry prevents deletion based on
absence. A hook currently writing is skipped until a later check. Empty lock
files remain after their snapshots are deleted.

`uninstall` keeps this folder so you can recover a backup; delete it yourself when you no longer need it.

## What Busy Crew changes

`install` adds hooks to Claude Code's settings after a backup, and LaunchAgents to `~/Library/LaunchAgents`. `uninstall` removes those owned entries. It never writes to Codex data or Claude Code sessions.

If Claude settings are on another volume, private recovery copies may also be kept beside them in `.agent-deck-recovery`; atomic replacement requires the same filesystem.

To report a privacy or security problem, see [SECURITY](../SECURITY.md).
