// A fictional crew, shown when no Mac sends a state: in BUSY Bar Emulator, on firmware without app storage,
// or before Busy Crew is installed on the Mac. It walks through what the app shows in real use.
import type { Provider, Scene, State } from "./render.ts";

const crew = (spec: [Provider, State][]) => spec.map(([provider, state]) => ({ provider, state }));

export const DEMO_TOKENS = { claude: 563_744_455, codex: 18_447_757 };
export const DEMO_MONTH = { claude: 4_545_810_701, codex: 213_942_831 };

/** Each scene stays on screen for `seconds`. */
export const DEMO: { seconds: number; scene: Scene }[] = [
  // One agent at work, then another joins: the tiles walk while they work.
  { seconds: 6, scene: { sessions: crew([["claude", "working"], ["claude", "idle"]]) } },
  { seconds: 6, scene: { sessions: crew([["claude", "working"], ["claude", "working"], ["claude", "ready"]]) } },
  // An agent needs you: a yellow "!" comes first.
  { seconds: 6, scene: { sessions: crew([["claude", "waiting"], ["claude", "working"], ["claude", "ready"]]) } },
  // Claude and Codex together: the duo view counts each family.
  { seconds: 6, scene: { sessions: crew([["claude", "working"], ["claude", "ready"], ["claude", "ready"], ["codex", "working"], ["codex", "ready"]]) } },
  { seconds: 6, scene: { sessions: crew([["claude", "ready"], ["claude", "ready"], ["claude", "idle"], ["codex", "ready"]]) } },
  // Codex alone, then the token totals the scroll wheel shows.
  { seconds: 6, scene: { sessions: crew([["codex", "working"], ["codex", "ready"], ["codex", "waiting"]]) } },
  { seconds: 5, scene: { sessions: crew([["claude", "working"]]), view: "tokens", tokens: DEMO_TOKENS } },
  { seconds: 5, scene: { sessions: crew([["claude", "working"]]), view: "month", tokens: DEMO_MONTH } },
  // Everyone idle: Claude sleeps.
  { seconds: 5, scene: { sessions: crew([["claude", "idle"], ["claude", "idle"]]) } },
];
