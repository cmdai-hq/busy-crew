import { test } from "node:test";
import assert from "node:assert/strict";
import { appRecord, parse, previousAcks } from "../src/mailbox.ts";

const good = {
  schema: 1, seq: 7, at: 1_800_000_000,
  sessions: [["claude", "waiting"], ["codex", "working"]],
  errors: ["codex", "bogus"],
  tokens: { claude: 12, codex: -1 },
  month: null,
};

test("reads a valid state", () => {
  const parsed = parse(JSON.stringify(good));
  assert.equal(parsed.kind, "ok");
  if (parsed.kind !== "ok") return;
  assert.equal(parsed.snapshot.seq, 7);
  assert.equal(parsed.snapshot.at, 1_800_000_000);
  assert.deepEqual(parsed.snapshot.sessions, [
    { provider: "claude", state: "waiting" },
    { provider: "codex", state: "working" },
  ]);
  assert.deepEqual(parsed.snapshot.errors, ["codex"]); // unknown error names are dropped
  assert.deepEqual(parsed.snapshot.tokens, { claude: 12, codex: null }); // bad counts become unknown
  assert.equal(parsed.snapshot.month, null);
});

test("an empty, cut or foreign file is invalid, never a crash", () => {
  for (const text of ["", "{", "null", "[]", '"x"', JSON.stringify(good).slice(0, 40)]) {
    assert.equal(parse(text).kind, "invalid", text);
  }
});

test("rejects bad sessions", () => {
  const bad = [
    { ...good, sessions: [["claude", "sleeping"]] },
    { ...good, sessions: [["claude", "constructor"]] },
    { ...good, sessions: [["gemini", "working"]] },
    { ...good, sessions: [["claude"]] },
    { ...good, sessions: "claude" },
    { ...good, sessions: Array(201).fill(["claude", "idle"]) },
    { ...good, seq: -1 },
    { ...good, schema: 0 },
  ];
  for (const value of bad) assert.equal(parse(JSON.stringify(value)).kind, "invalid", JSON.stringify(value).slice(0, 60));
});

test("a newer schema asks for an app update", () => {
  assert.equal(parse(JSON.stringify({ ...good, schema: 2 })).kind, "newer");
});

test("a state without a valid time is still read, its age unknown", () => {
  for (const at of [undefined, "soon", -5, 1.5]) {
    const parsed = parse(JSON.stringify({ ...good, at }));
    assert.equal(parsed.kind === "ok" && parsed.snapshot.at, null);
  }
});

test("the OK count carries over from the previous launch", () => {
  const record = appRecord({ run: 1, beat: 3, running: false, ack: 9, seq: 7 });
  assert.deepEqual(JSON.parse(record), { schema: 1, run: 1, beat: 3, running: false, ack: 9, seq: 7 });
  assert.equal(previousAcks(record), 9);
  for (const text of ["", "{", "null", '{"schema":2,"ack":4}', '{"schema":1,"ack":-1}']) assert.equal(previousAcks(text), 0, text);
});
