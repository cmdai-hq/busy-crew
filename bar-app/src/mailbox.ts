// The mailbox between the Mac and the bar. Busy Crew's collector on the Mac writes the crew's state into this
// app's storage through the bar's HTTP API; the app reads it locally, so nothing on the Mac ever listens.
//
//   state.json  written by the Mac  {"schema":1,"seq":42,"sessions":[["claude","working"],...],
//                                   "errors":["codex"],"tokens":{"claude":1,"codex":2},"month":{...}}
//   ack.json    written by the app  {"schema":1,"run":123,"ack":3,"seq":42}  OK pressed: acknowledge finished responses
//
// Sessions come most urgent first and carry no IDs, titles or text. Anyone with access to the bar's API can
// write state.json, so every field is checked here and the worst a bad file can do is a wrong picture.
// The protocol is documented in docs/bar-app.md.
import { ORDER, type Provider, type Session, type SourceError, type State, type Tokens } from "./render.ts";

export const SCHEMA = 1;
export const STATE_FILE = "/ext/data/state.json";
export const ACK_FILE = "/ext/data/ack.json";
const MAX_SESSIONS = 200;

export type Snapshot = {
  seq: number;
  sessions: Session[];
  errors: SourceError[];
  tokens: Tokens;
  month: Tokens;
};

export type Parsed =
  | { kind: "ok"; snapshot: Snapshot }
  | { kind: "newer" } // A newer collector: the app needs an update.
  | { kind: "invalid" };

const PROVIDERS: Provider[] = ["claude", "codex"];
const ERRORS: SourceError[] = ["claude", "codex", "state"];

const isCount = (n: unknown): n is number => typeof n === "number" && Number.isInteger(n) && n >= 0;

function tokens(value: unknown): Tokens {
  if (typeof value !== "object" || value === null) return null;
  const result: Partial<Record<Provider, number | null>> = {};
  for (const provider of PROVIDERS) {
    const n = (value as Record<string, unknown>)[provider];
    result[provider] = isCount(n) ? n : null;
  }
  return result;
}

export function parse(text: string): Parsed {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return { kind: "invalid" };
  }
  if (typeof data !== "object" || data === null) return { kind: "invalid" };
  const raw = data as Record<string, unknown>;
  if (isCount(raw.schema) && raw.schema > SCHEMA) return { kind: "newer" };
  if (raw.schema !== SCHEMA || !isCount(raw.seq) || !Array.isArray(raw.sessions) || raw.sessions.length > MAX_SESSIONS) {
    return { kind: "invalid" };
  }
  const sessions: Session[] = [];
  for (const item of raw.sessions) {
    if (!Array.isArray(item) || item.length !== 2) return { kind: "invalid" };
    const [provider, state] = item as unknown[];
    if (!PROVIDERS.includes(provider as Provider) || !(typeof state === "string" && Object.prototype.hasOwnProperty.call(ORDER, state))) {
      return { kind: "invalid" };
    }
    sessions.push({ provider: provider as Provider, state: state as State });
  }
  const errors = Array.isArray(raw.errors) ? ERRORS.filter((e) => (raw.errors as unknown[]).includes(e)) : [];
  return { kind: "ok", snapshot: { seq: raw.seq, sessions, errors, tokens: tokens(raw.tokens), month: tokens(raw.month) } };
}

/** OK was pressed for the `count`-th time in launch `run`, while the state numbered `seq` was on screen. */
export function ack(run: number, count: number, seq: number): string {
  return JSON.stringify({ schema: SCHEMA, run, ack: count, seq });
}
