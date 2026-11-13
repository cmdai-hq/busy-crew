# Busy Crew

**See what your Claude Code and Codex agents are doing at a glance.** Each session is a little character on the
front display: working, waiting for you, or done.

## What you see

- **Terracotta (Claude) or blue (Codex):** the agent is working. With one family, each session gets its own tile.
- **Yellow `!`:** an agent is waiting for you, with a question or a permission request.
- **Green `✓`:** a response is finished. Press **OK** to acknowledge it.
- **Gray, `zzz`:** sessions open but idle.
- **`?`:** the state is unknown, or your Mac has gone quiet.

Turn the **wheel** to see the tokens your agents used today and this month. **Back** returns to the crew, then quits.

## How it works

The app shows what [Busy Crew](https://busycrew.app) on your Mac sees. Busy Crew reads Claude Code and Codex
activity locally and writes a small status file into this app's storage on the BUSY Bar: states and token totals,
never conversation content. Nothing on your Mac listens on the network.

Without Busy Crew on your Mac, or on firmware older than 1.4, the app plays a demo.

## Set it up

1. Install Busy Crew on your Mac: `brew install cmdai-hq/tap/busy-crew`, then follow [the guide](https://busycrew.app).
2. Install this app on your BUSY Bar from the Apps Catalog.
3. Switch Busy Crew to the app: `busy-crew install --display app`.

Privacy: [what Busy Crew reads, keeps and sends](https://github.com/cmdai-hq/busy-crew/blob/main/docs/privacy.md).
Busy Crew is an independent project, not affiliated with BUSY, Anthropic or OpenAI.
