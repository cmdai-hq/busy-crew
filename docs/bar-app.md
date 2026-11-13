# The BUSY Bar app

`bar-app/` is a JavaScript app that runs on the BUSY Bar itself and draws the crew there. It is published in the
[BUSY Apps Catalog](https://github.com/busy-app/busy-apps-catalog) as `app.busycrew.crew`. The collector on the Mac
still observes Claude Code and Codex; the app only displays what the collector tells it.

## The mailbox

The collector writes the crew's state into the app's own storage through the bar's HTTP API, with the same address
and password it already uses to draw. The app reads it locally. Nothing listens on the Mac, and nothing leaves
the Mac's link to the bar.

```
Mac collector ──POST /api/storage/write, then rename──▶ /ext/apps_data/js_apps/app.busycrew.crew/state.json
                                                          ▲ GET /api/storage/read?path=/ext/data/state.json (localhost)
                                                     the app draws the crew
Mac collector ◀──GET /api/storage/read── ack.json ◀── the app, when OK is pressed
```

`/ext/data` is the app's sandbox; on firmware 1.4.1 the HTTP API reaches it as
`/ext/apps_data/js_apps/<app id>`. That path was observed on a bar, not documented by BUSY: the collector checks it
before relying on it.

### `state.json`, written by the Mac

```json
{"schema": 1, "seq": 42,
 "sessions": [["claude", "waiting"], ["claude", "working"], ["codex", "ready"]],
 "errors": [],
 "tokens": {"claude": 563744455, "codex": 1447757},
 "month": {"claude": 4545810701, "codex": 3942831}}
```

- `sessions`: most urgent first, as `agentdeck.render.ordered_sessions` orders them. No IDs, titles or text.
- `errors`: sources the collector cannot read: `claude`, `codex`, or `state` for its own ledgers.
- `tokens`, `month`: today's and this month's totals per family, or `null` while unknown.
- `seq` grows with every write. The collector rewrites the file at least once a minute; the app shows the crew as
  unknown (`?`) after three minutes without a new `seq`, so a sleeping Mac never leaves a stale picture.
- A higher `schema` makes the app ask for its own update on the back screen.

### `ack.json`, written by the app

`{"schema": 1, "run": 123456, "ack": 3, "seq": 42}`: OK was pressed for the third time in launch `run`, while state
`seq` was on screen. The collector acknowledges the finished responses it presented, as for OK in direct mode, when
`run` or `ack` differs from what it last saw.

## Rules the bar imposes

These come from tests on a BUSY Bar with firmware 1.4.1 (November 2026):

| Rule | Why |
| --- | --- |
| At most one write every 2 seconds, latest state wins | 150 back-to-back writes while the app read every 100 ms hung the bar's web server until a restart |
| Write `state.tmp`, then rename it over `state.json` | A plain overwrite cut off mid-write leaves an empty file; rename replaces the file whole |
| Write only when the state changes, tokens at most once a minute, plus the one-minute heartbeat | The storage is eMMC; a few thousand small writes a day is decades of wear, one per second is not |
| The app reads every 2 seconds, one request at a time | Every request goes through the same single web server as drawing |
| In app mode the collector neither draws nor reacts to the buttons | An equal-priority draw from another app replaces the screen, and the buttons reach both the app and the Mac |

Measured at that pace: no torn or missing read in 117 reads, writes answered in 56 ms or less.

## Turning it on

Install the app on the bar, then run `busy-crew install --display app` on the Mac (`--display overlay` goes back).
The collector checks once a minute whether the app is installed: until it is, and after it is removed, it keeps
drawing on the bar itself.

## Without a Mac

The app plays a demo when no state arrives: before Busy Crew is installed on the Mac, and on firmware before 1.4,
which gives apps no storage (the storage API for apps arrived in firmware 1.4; BUSY Bar Emulator runs 1.3.1). The
back screen says which.

## One renderer, two languages

`bar-app/src/render.ts` is a port of `agentdeck/render.py` and must draw the same pixels. `scripts/render_fixtures.py`
renders fictional scenes in Python into `bar-app/test/fixtures/render.json`; `npm test` in `bar-app/` renders them in
TypeScript and compares. `tests/test_bar_app.py` fails when the fixtures are older than the Python renderer: after a
render change, run `uv run python scripts/render_fixtures.py` and port the change until `npm test` passes.

## Working on the app

```sh
cd bar-app
npm ci
npm test && npm run typecheck && npm run build
npm run app:install && npm run app:run     # a bar on USB
npx busy-cli app install --addr 127.0.0.1:8640 && npx busy-cli app run --addr 127.0.0.1:8640   # BUSY Bar Emulator
```

While the app runs, the collector in direct mode also draws: stop it for a clean test
(`launchctl bootout gui/$UID/local.busybar.agent-deck`, then `launchctl bootstrap` to restart it).
