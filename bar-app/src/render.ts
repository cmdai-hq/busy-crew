// Port of agentdeck/render.py: hand-drawn sprites at the front panel's real 72 × 16 resolution.
// It must produce the same XPM as the Python renderer for the same input:
// test/render.test.ts checks it against fixtures that Python generates (scripts/render_fixtures.py).
// Pure code only: no device API, so Node can run it in tests.

export type Provider = "claude" | "codex";
export type State = "working" | "waiting" | "ready" | "idle" | "unknown";
export type View = "sessions" | "tokens" | "month";
export type Session = { provider: Provider; state: State };
export type Tokens = Partial<Record<Provider, number | null>> | null;

export const ORDER: Record<State, number> = { waiting: 0, working: 1, ready: 2, unknown: 3, idle: 4 };

const PALETTE: Record<string, string> = {
  ".": "#000000", c: "#dc866b", B: "#7895ff", s: "#4669c9",
  v: "#192b50", C: "#8cf1e9", m: "#8cf1e9", y: "#ffcb64",
  g: "#87dd9b", d: "#65717e", w: "#dfe9ee",
};
const CLAWD = ["0011111111100", "0011111111100", "0011011101100", "1111011101111",
  "1111111111111", "0011111111100", "0011111111100", "0001100011000", "0001000001000"];
const MINI_CLAWD = ["011111110", "011111110", "110111011", "110111011", "111111111", "011111110", "010101010"];
const CODEX = ["000BB0BB00000", "00BBBBBBBBB00", "0BBBBBBBBBBB0", "BBBvvvvvvvBBB",
  "BBvvvvvvvvvBB", "BBvvCvvvCvvBB", "BBvvCvvvCvvBB", "0BBvvvvvvvBB0",
  "00BBBBBBBBB00", "000BBBmBBB000", "000sBBBBBs000", "000BB000BB000", "000ss000ss000"];
const MINI_CODEX = ["00BB0BB00", "0BBBBBBB0", "BBBvvvBBB", "BBvCvCvBB", "BBvvvvvBB",
  "0BBBBBBB0", "00BBmBB00", "00BBBBB00", "00ss0ss00"];
// Dimming paints sprites gray; Codex's visor and mouth become holes.
const DIM: Record<string, string> = { v: ".", m: "." };
const FONT: Record<string, string[]> = {
  "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
  "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
  "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
  "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
  "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
  "+": ["000", "010", "111", "010", "000"], "?": ["111", "001", "010", "000", "010"],
  K: ["101", "101", "110", "101", "101"], M: ["101", "111", "111", "101", "101"],
  G: ["111", "100", "101", "101", "111"], T: ["111", "010", "010", "010", "010"],
  z: ["11", "01", "10", "11"],
  ".": ["0", "0", "0", "0", "1"], "-": ["000", "000", "111", "000", "000"],
  A: ["010", "101", "111", "101", "101"], D: ["110", "101", "101", "101", "110"],
  H: ["101", "101", "111", "101", "101"], N: ["1001", "1101", "1011", "1001", "1001"],
  O: ["010", "101", "101", "101", "010"], Y: ["101", "101", "010", "010", "010"],
};
const PERIODS: Partial<Record<View, string>> = { tokens: "TODAY", month: "MONTH" };
const MARKS: Record<State, string[]> = {
  working: ["110", "110", "001"], waiting: ["010", "010", "000", "010"],
  ready: ["001", "101", "010"], unknown: FONT["?"], idle: ["000", "111", "000"],
};
const STATE_COLOR: Partial<Record<State, string>> = { waiting: "y", ready: "g", idle: "d", unknown: "d" };

const familyColor = (provider: Provider) => (provider === "claude" ? "c" : "B");

class Frame {
  rows: string[][] = Array.from({ length: 16 }, () => new Array<string>(72).fill("."));

  dot(x: number, y: number, color: string): void {
    if (x >= 0 && x < 72 && y >= 0 && y < 16) this.rows[y][x] = color;
  }

  sprite(rows: string[], x: number, y: number, color: string | null = null, dim = false): void {
    rows.forEach((row, dy) => {
      for (let dx = 0; dx < row.length; dx++) {
        const value = row[dx];
        if (value !== "0") this.dot(x + dx, y + dy, dim ? (DIM[value] ?? "d") : (color ?? value));
      }
    });
  }

  text(text: string, x: number, y: number, color = "w", scale = 1): void {
    for (const ch of text) {
      const glyph = FONT[ch] ?? FONT["?"];
      glyph.forEach((row, dy) => {
        for (let dx = 0; dx < row.length; dx++) {
          if (row[dx] !== "1") continue;
          for (let a = 0; a < scale; a++) {
            for (let b = 0; b < scale; b++) this.dot(x + dx * scale + a, y + dy * scale + b, color);
          }
        }
      });
      x += (glyph[0].length + 1) * scale;
    }
  }

  mark(state: State, provider: Provider, x: number, y: number, sleeping = false): void {
    if (sleeping) {
      this.text("zzz", x, y, "d");
      return;
    }
    this.sprite(MARKS[state], x, y, STATE_COLOR[state] ?? familyColor(provider));
  }

  xpm(): string {
    const used = new Set<string>();
    for (const row of this.rows) for (const value of row) used.add(value);
    const colors = Array.from(used).sort();
    return `! XPM2\n72 16 ${colors.length} 1\n${colors.map((v) => `${v} c ${PALETTE[v]}`).join("\n")}\n${this.rows
      .map((row) => row.join(""))
      .join("\n")}\n`;
  }
}

function number(n: number): string {
  return n < 100 ? String(n) : "99+";
}

/** Token totals in K, M, G or T, as the Python renderer writes them. */
export function compact(n: number | null | undefined): string {
  if (typeof n !== "number" || !Number.isInteger(n) || n < 0) return "?";
  const units: [string, number][] = [["T", 1e12], ["G", 1e9], ["M", 1e6]];
  for (const [unit, divisor] of units) {
    if (n < divisor) continue;
    if (n >= 1000 * divisor && unit === "T") return "999T+";
    if (n < 100 * divisor) {
      // Below 100 units, n * 10 stays far under 2^53, so this integer rounding is exact.
      const tenths = Math.floor((n * 10 + Math.floor(divisor / 2)) / divisor);
      return `${Math.floor(tenths / 10)}.${tenths % 10}${unit}`;
    }
    return `${Math.floor(n / divisor)}${unit}`;
  }
  return n >= 1000 ? `${Math.floor(n / 1000)}K` : String(n);
}

/** Most urgent first. The Mac sends sessions already in this order; the index keeps it stable on any engine. */
export function ordered(sessions: Session[]): Session[] {
  return sessions
    .map((session, index) => ({ session, index }))
    .sort((a, b) => ORDER[a.session.state] - ORDER[b.session.state] || a.index - b.index)
    .map((entry) => entry.session);
}

export function aggregate(sessions: Session[]): State {
  let best: State = "idle";
  let found = false;
  for (const s of sessions) {
    if (!found || ORDER[s.state] < ORDER[best]) best = s.state;
    found = true;
  }
  return best;
}

/** Source errors: "claude", "codex", or "state" when Busy Crew's own ledgers could not be read. */
export type SourceError = Provider | "state";

export function layout(sessions: Session[], errors: SourceError[] = []): ["duo" | "team", Provider | null] {
  if (errors.includes("claude") || errors.includes("codex")) return ["duo", null];
  const providers = new Set(sessions.map((s) => s.provider));
  if (providers.size !== 1) return ["duo", null];
  return ["team", sessions[0].provider];
}

export type Scene = {
  sessions: Session[];
  view?: View;
  tokens?: Tokens;
  phase?: number;
  errors?: SourceError[];
};

export function render({ sessions, view = "sessions", tokens = null, phase = 0, errors = [] }: Scene): string {
  const frame = new Frame();
  const [mode, provider] = layout(sessions, errors);
  const claude = sessions.filter((s) => s.provider === "claude");
  const sleeping = claude.length > 0 && !errors.includes("claude") && claude.every((s) => s.state === "idle");
  const waiting = sessions.some((s) => s.state === "waiting");
  const period = PERIODS[view];

  // Required input always takes precedence over the optional token view.
  if (period && !waiting) {
    for (const [name, x] of [["claude", 2], ["codex", 39]] as [Provider, number][]) {
      frame.sprite(name === "claude" ? MINI_CLAWD : MINI_CODEX, x, 1, name === "claude" ? "c" : null);
      frame.text(compact(tokens?.[name]), x + 11, 3);
    }
    frame.dot(35, 3, "d");
    frame.dot(35, 6, "d");
    let width = -1;
    for (const ch of period) width += FONT[ch][0].length + 1;
    frame.text(period, Math.floor((72 - width) / 2), 11, "d");
  } else if (mode === "duo") {
    for (const [name, x] of [["claude", 0], ["codex", 37]] as [Provider, number][]) {
      const group = sessions.filter((s) => s.provider === name);
      frame.sprite(name === "claude" ? CLAWD : CODEX, x + 2, name === "claude" ? 3 : 1, name === "claude" ? "c" : null,
        group.every((s) => s.state === "idle" || s.state === "unknown"));
      const label = errors.includes(name) ? "?" : number(group.length);
      frame.text(label, x + (label.length === 1 ? 19 : 16), 3, "w", label.length === 1 ? 2 : 1);
      const state: State = errors.includes(name) ? "unknown" : aggregate(group);
      const isSleeping = sleeping && name === "claude";
      frame.mark(state, name, x + (isSleeping ? 27 : 28), 3, isSleeping);
      ordered(group).slice(0, 4).forEach((session, i) => {
        const color = STATE_COLOR[session.state] ?? familyColor(name);
        frame.dot(x + 17 + i * 4, 14, color);
        frame.dot(x + 18 + i * 4, 14, color);
      });
    }
    for (const y of [4, 7, 10]) frame.dot(35, y, "d");
  } else if (provider) {
    const group = ordered(sessions.filter((s) => s.provider === provider));
    // Six tiles maximum. The last becomes a numeric overflow tile, never a fake agent.
    const overflow = group.length > 6;
    const visible = group.slice(0, overflow ? 5 : 6);
    const count = visible.length;
    const xs = overflow
      ? [0, 1, 2, 3, 4, 5].map((i) => 2 + i * 12)
      : visible.map((_, i) => Math.trunc(36 - (count * 12) / 2 + i * 12 + 1));
    visible.forEach((session, i) => {
      const x = xs[i];
      frame.sprite(provider === "claude" ? MINI_CLAWD : MINI_CODEX, x, provider === "claude" ? 7 : 5,
        provider === "claude" ? "c" : null, session.state === "idle" || session.state === "unknown");
      frame.mark(session.state, provider, sleeping ? x : x + 3, 0, sleeping);
      if (session.state === "working") frame.dot(x + 3 + phase * 2, 15, familyColor(provider));
    });
    if (overflow) {
      const hidden = group.slice(5);
      frame.text("+", 62, 2, hidden.some((s) => s.state === "waiting") ? "y" : "w");
      frame.text(number(hidden.length), 60, 9);
    }
  }
  if (errors.length > 0) frame.text("?", 69, 11, "d");
  return frame.xpm();
}

/** True when the picture changes from one second to the next: a working tile's dot walks. */
export function animated({ sessions, view = "sessions", errors = [] }: Scene): boolean {
  if (PERIODS[view] && !sessions.some((s) => s.state === "waiting")) return false;
  const [mode, provider] = layout(sessions, errors);
  if (mode !== "team" || !provider) return false;
  const group = ordered(sessions.filter((s) => s.provider === provider));
  return group.slice(0, group.length > 6 ? 5 : 6).some((s) => s.state === "working");
}
