# How Busy Crew works

The collector runs on your Mac. It reads local Claude and Codex data, draws a 72 × 16 pixel frame and sends it to the bar when it changes. It reads files read-only and sends only pixels to the bar.

It reads once a second while a session works or waits for you, and for 10 seconds after anything it shows changes. When everything is at rest it reads every 2.5 seconds. A button press reads at once; a command from `busy-crew` or the menu, or a write from Claude, within a second.

- [Results and acknowledgment](#results-and-acknowledgment)
- [Tokens](#tokens)
- [Tracking sources and limitations](#tracking-sources-and-limitations)
- [Display lease and privacy](#display-lease-and-privacy)
- [Bar controls](#bar-controls)

## Results and acknowledgment

When an agent finishes a response while the monitor is running, its session shows a green `✓`. The check mark is kept across monitor restarts, so you don't miss a response that finished while you were away. It clears when any of these happens:

- you acknowledge it;
- the session starts new work;
- the session goes away: a closed Claude process, or an archived Codex conversation;
- a Codex response finished more than two hours ago: that conversation is considered dead.

A `✓` **counts toward its family's total.** For example, "Codex 5" can mean five finished conversations waiting for acknowledgment, with nothing working.

To acknowledge, use any of these:

- press the scroll wheel (the bar's OK / Skip);
- choose **Acknowledge results** in the [menu bar icon](menu-bar.md);
- run `busy-crew ack`.

Only results represented in Busy Crew's current frame are acknowledged, so nothing is acknowledged in a token view. A session that needs you brings the sessions frame back. Another app can cover that frame; Busy Crew does not detect screen ownership.

| Result | After acknowledgment |
| --- | --- |
| Open Claude session | Turns gray and stays visible while its process is open |
| Finished Codex conversation | Removed from the count |

Acknowledgment never sends anything to an agent: it doesn't answer a question or grant a permission. It also leaves two indicators alone:

- **Yellow `!`:** the agent is waiting for you. Answer it in Claude or Codex.
- **Gray `?`:** an unknown state. A Claude `?`, for example after a failed turn, clears with the session's next activity. Failed, interrupted or cancelled Codex turns age out after two hours; archiving removes them, and new work replaces their state.

A response blocked by Codex's own content filter counts as failed, not finished.

At its first startup, the monitor ignores old completed responses. A transition to idle alone does not establish a successful response.

## Tokens

Two token views show totals **by agent family**: today (labelled `TODAY`), and the calendar month to date, from the 1st through today (labelled `MONTH`). The month count starts again on the 1st.

- Both come from one [ccusage](https://ccusage.com/) `daily --by-agent` report, refreshed every two minutes. `busy-crew refresh` or **Refresh tokens now** starts a background recount at the next collector poll. A report can take up to 45 seconds; requests during a run join that run.
- Totals use ccusage's `totalTokens`, which includes cache tokens. They are shown in K (thousand), M (million) or G (billion).
- There is no instantaneous rate, context size or remaining subscription quota, and no cost is extrapolated for a model without known pricing.
- A family with usage earlier in the month shows 0 for a day without any. An error, stale data, or a change of day produces a `?`.

## Tracking sources and limitations

### Claude Code

`~/.claude/sessions/*.json` provides identity, busy/idle state and process lifetime. The PID and UTC start time exclude closed processes and reused PIDs. Only sessions in this registry are counted; subagents do not increase the main count.

`PermissionRequest`, `PreToolUse` for `AskUserQuestion`, and `Elicitation` identify waits. `PostToolUse` and `UserPromptSubmit` indicate that work has resumed. CLI variants without a native registry entry need another adapter.

Permission requests lack tool IDs in Claude's documented schema. The hook correlates them with a unique preceding tool call using an input hash, storing only that hash and the call ID. Identical concurrent calls and notifications/elicitation events without a usable identity remain unknown instead of guessing which interaction was resolved. The payload contents are never persisted.

Hooks use isolated Python imports, store status metadata without prompts, make no approval decisions and exit with code 0. They run synchronously with a 2-second timeout and no network access.

The collector checks snapshots for cleanup once an hour, starting with its first
poll. It removes snapshots older than 24 hours (already ignored for display),
or older than an hour whose session is absent from the native registry. The
age is measured from the snapshot's last event timestamp. It rechecks each
candidate under the hook writer's lock and skips a busy lock. Empty lock files
are retained: deleting one could let concurrent hooks lock different inodes.
An unavailable or incompatible registry directory remains a source error and
disables absence-based deletion, while cleanup by maximum age still runs.
An initially absent directory is normal, but absence-based deletion still waits
until an existing registry is read successfully.
The adapter collects native identities before updating result ledgers. An
unreadable, malformed or vanished record makes identity completeness uncertain.
Cleanup runs in a `finally` block, so result-ledger write failures cannot bypass
it or change which session identities were observed. Snapshot age uses wall
timestamps; the hourly cadence uses a monotonic clock.
After cleanup, a new prompt or resumed tool activity creates a new random turn
ID, so an earlier acknowledgment cannot hide its next result.

`install --no-claude-hooks` uses the native registry alone. Activity remains visible, but busy → idle does not establish a successful response or produce a `✓`. Repeat the option on manual reinstalls. For automatic upgrades and older passive installs, see [update guidance](installation.md#updates-and-removal).

### Codex app and local sessions

Read-only access to `~/.codex/state_5.sqlite`, `thread_history_1.sqlite`, and rollout events. The latest turn of each user conversation is used; archived conversations and subagents are excluded.

The first read finds the turn's start by scanning backward up to 256 MB. Subsequent polls read only appended records. If the start is outside that initial window, the source shows `?`.

- A finished or failed conversation silent for two hours is dead and leaves the count, along with its `✓`.
- If the rollout already contains a newer root turn than SQLite, the previous completion is cleared and the session remains unknown until the sources agree.
- An `inProgress` turn with no local writes for 15 minutes is discarded. This avoids ghost sessions, but can also hide a very long, silent turn or an extended wait.
- Synchronous question detection uses rollout events when available. **These files do not expose every approval request from the Codex app: yellow indicators for Codex provide only partial coverage.** An asynchronous question does not mark a still-active session as waiting.
- Recognized records that do not need decoding, such as tool outputs, can exceed 2 MB and are skipped. A turn event, question call or unreadable record that needs decoding and exceeds 2 MB makes the family show `?`.
- Codex's internal databases and formats can change: a read incompatibility displays a `?` without modifying the app's data. The service log names the cause, never a path or conversation content.
- Tasks orchestrated in the cloud and sessions on other machines are not tracked.

A source error never reuses the last known count. See [compatibility notes](COMPATIBILITY.md) for the tested versions and contracts.

## Display lease and privacy

Bitmaps have a 15-second lease; the loop targets a renewal every 5 seconds, subject to observation and I/O delays. If the Mac or program becomes unavailable, the firmware lease expires and the bar goes back to its own display. A higher-priority application can take over; the program does not raise its priority to displace it, and cleanup is scoped to Busy Crew. There is no custom display on the rear screen.

The program reads the local files needed for tracking and ccusage, but does not copy them into the repository or send any conversation content to the bar. It sends only pixels. It does not change the bar's brightness, network, or WARP settings, and uses no BUSY cloud access. REST redirects are refused, so the API key is never forwarded to another host.

## Bar controls

BUSY's documentation describes Start / Pause as "also OK", but the bar reports that button as `start`. Only a press on the scroll wheel reaches Busy Crew as OK, so Start / Pause never acknowledges. The scroll wheel steps along sessions → today → month and stops at either end; Start / Pause wraps around. `snapshot` reads the bar's framebuffer; it is not a photograph of the LEDs.
