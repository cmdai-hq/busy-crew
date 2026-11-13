// The TypeScript renderer must draw exactly what agentdeck/render.py draws.
// Fixtures come from `uv run python scripts/render_fixtures.py` at the repository root.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { animated, compact, render, type Scene } from "../src/render.ts";

type Case = { name: string; scene: Scene; xpm: string };
const cases: Case[] = JSON.parse(readFileSync(new URL("./fixtures/render.json", import.meta.url), "utf8"));

test("fixtures cover the renderer", () => {
  assert.ok(cases.length >= 50);
});

for (const { name, scene, xpm } of cases) {
  test(`renders like Python: ${name}`, () => {
    assert.equal(render(scene), xpm);
  });
}

test("compact rejects what Python rejects", () => {
  for (const bad of [null, undefined, -1, 1.5, Number.NaN]) assert.equal(compact(bad as number), "?");
});

test("only a working team tile animates", () => {
  const working = { provider: "claude", state: "working" } as const;
  const ready = { provider: "codex", state: "ready" } as const;
  assert.equal(animated({ sessions: [working] }), true);
  assert.equal(animated({ sessions: [{ ...working, state: "ready" }] }), false);
  assert.equal(animated({ sessions: [working, ready] }), false); // duo: no walking dot
  assert.equal(animated({ sessions: [working], view: "tokens" }), false);
});
