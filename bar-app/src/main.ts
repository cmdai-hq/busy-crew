// Busy Crew for BUSY Bar: your Claude Code and Codex agents on the front display.
// The Mac's collector writes the crew's state into this app's storage (see mailbox.ts); with no Mac, a demo plays.
import { device } from "@shared/device";
import manifest from "./appmeta/manifest.json";
import { render, type Scene, type View } from "./render.ts";
import { APP_FILE, STATE_FILE, appRecord, parse, previousAcks, type Snapshot } from "./mailbox.ts";
import { DEMO, DEMO_MONTH, DEMO_TOKENS } from "./demo.ts";

const APP = manifest.id;
const STORAGE = "http://localhost/api/storage";
/** Reading the mailbox costs one local request; every 2 s keeps the bar's web server idle most of the time. */
const POLL_SECONDS = 2;
/** The Mac rewrites the state at least once a minute; three minutes of silence means it is asleep or stopped. */
const STALE_SECONDS = 180;
/** app.json is refreshed this often, so the Mac knows the app still runs. */
const BEAT_SECONDS = 60;
/** A read still pending after this long is given up, so a stuck request cannot freeze the picture. */
const READ_TIMEOUT = 10;
const VIEWS: View[] = ["sessions", "tokens", "month"];

type Mode = "demo" | "live" | "offline" | "update";

let mode: Mode = "demo";
let snapshot: Snapshot | null = null;
let seenAt = 0; // `clock` when the state shown was written
let clock = 0; // seconds since start, from the app's own timer: no wall clock to jump
let barTime: number | null = null; // the bar's Unix time at clock 0, to tell how old a state is
let view: View = "sessions";
let demoIndex = 0;
let demoLeft = DEMO[0].seconds;
let polling = true;
let stopped = false;

// ---------- Errors: logged once per kind until it recovers, so a long outage does not flood the bar's log ----------

const failing = new Set<string>();
function fail(kind: string, err: unknown): void {
  if (failing.has(kind)) return;
  failing.add(kind);
  console.error(`${APP}: ${kind}: ${err instanceof Error ? err.message : String(err)}`);
}
const recover = (kind: string) => failing.delete(kind);

// ---------- Screens ----------

function scene(): Scene {
  const phase = clock % 2;
  if (mode === "demo") {
    const demo = DEMO[demoIndex].scene;
    if (view === "sessions") return { ...demo, phase };
    return { sessions: [], view, tokens: view === "month" ? DEMO_MONTH : DEMO_TOKENS, phase };
  }
  if (mode === "live" && snapshot) {
    const { sessions, errors, tokens, month } = snapshot;
    return { sessions, errors, view, tokens: view === "month" ? month : tokens, phase };
  }
  // Offline or outdated: the "?" the collector shows for a source it cannot read.
  return { sessions: [], errors: ["claude", "codex"] };
}

// The back screen is 160 × 80, with the firmware's status icons in its right 16 columns: text stays left of them.
const BACK_CENTER = 72;
const BACK: Record<Mode, [string, string, string]> = {
  live: ["Busy Crew", "Live from your Mac", " "],
  demo: ["Busy Crew demo", "Install it on your Mac:", "busycrew.app"],
  offline: ["Mac offline", "No news from", "Busy Crew on your Mac"],
  update: ["Update this app", "Your Mac runs a newer", "Busy Crew"],
};

let drawing = false;
let dirty = false;
let shownFront = "";
let shownBack: Mode | null = null;

/** Sends what changed, one request at a time; a change made meanwhile is sent once the request ends. */
function draw(): void {
  if (stopped) return;
  if (drawing) {
    dirty = true;
    return;
  }
  const front = render(scene());
  const back = shownBack === mode ? null : mode;
  if (front === shownFront && back === null) return;
  const elements: object[] = [];
  if (front !== shownFront) elements.push({ id: "crew", type: "xpmbitmap", display: "front", x: 0, y: 0, data: front });
  if (back) {
    const [title, line1, line2] = BACK[back];
    const text = (id: string, y: number, font: string, value: string) =>
      ({ id, type: "text", display: "back", x: BACK_CENTER, y, align: "center", font, color: "#FFFFFFFF", text: value });
    elements.push(text("title", 20, "normal", title), text("line1", 44, "small", line1), text("line2", 58, "small", line2));
  }
  drawing = true;
  dirty = false;
  device
    .DisplayDraw({ application_name: APP, priority: 50, elements } as Parameters<typeof device.DisplayDraw>[0])
    .then(
      () => {
        shownFront = front;
        if (back) shownBack = back;
        recover("draw");
      },
      (err: unknown) => fail("draw", err),
    )
    .then(() => {
      drawing = false;
      if (dirty) draw();
    });
}

// ---------- The Mac's state ----------

let reading = 0; // `clock` when the pending read started, 0 when none
let readGeneration = 0;

async function readState(generation: number): Promise<void> {
  const res = await fetch(`${STORAGE}/read?path=${STATE_FILE}`);
  if (generation !== readGeneration) return; // Given up meanwhile.
  if (res.status === 403) {
    // Firmware before 1.4 gives apps no storage: nothing will ever arrive, so stop asking and keep the demo.
    polling = false;
    console.log(`${APP}: no app storage on this firmware, showing the demo`);
    return;
  }
  if (!res.ok) return; // No state yet: the Mac has not written one. A live crew goes stale on its own.
  const parsed = parse(await res.text());
  if (generation !== readGeneration) return;
  if (parsed.kind === "newer") {
    mode = "update";
    return;
  }
  if (parsed.kind === "invalid") {
    fail("state", "unreadable state.json");
    return;
  }
  recover("state");
  const next = parsed.snapshot;
  if (!snapshot || next.seq !== snapshot.seq) {
    // How old the state already is, when both clocks are known: a Mac that slept for hours is offline at once.
    const age = barTime !== null && next.at !== null ? Math.max(0, barTime + clock - next.at) : 0;
    seenAt = clock - age;
  }
  snapshot = next;
  mode = clock - seenAt < STALE_SECONDS ? "live" : "offline";
}

function poll(): void {
  if (!polling) return;
  if (reading) {
    if (clock - reading < READ_TIMEOUT) return;
    readGeneration += 1; // The stuck read's answer, if it ever comes, is ignored.
    fail("read", "no answer");
  }
  reading = clock || 1;
  const generation = readGeneration;
  readState(generation)
    .then(() => recover("read"), (err: unknown) => fail("read", err))
    .then(() => {
      if (generation !== readGeneration) return;
      reading = 0;
      draw();
    });
}

// ---------- What the app tells the Mac: app.json ----------

const run = Math.floor(Math.random() * 1e9) ^ Date.now() % 1e9;
let beat = 0;
let running = true;
let acks = 0;
let ackSeq = 0;
let writing = false;
let rewrite = false;

/** One write at a time; a change made meanwhile is written once it ends, so OK presses cannot pile up. */
function writeApp(): void {
  if (writing) {
    rewrite = true;
    return;
  }
  writing = true;
  rewrite = false;
  fetch(`${STORAGE}/write?path=${APP_FILE}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: appRecord({ run, beat, running, ack: acks, seq: ackSeq }),
  })
    .then(
      (res) => (res.ok ? recover("app") : fail("app", `HTTP ${res.status}`)),
      (err: unknown) => fail("app", err),
    )
    .then(() => {
      writing = false;
      if (rewrite) writeApp();
    });
}

/** OK acknowledges what is on screen, as on the Mac: only the live crew, and only finished responses. */
function acknowledge(): void {
  if (mode !== "live" || !snapshot) return;
  const sessions = snapshot.sessions;
  if (view !== "sessions" && !sessions.some((s) => s.state === "waiting")) return; // Tokens on screen
  if (!sessions.some((s) => s.state === "ready")) return;
  acks += 1;
  ackSeq = snapshot.seq;
  writeApp();
}

/** Picks up the OK count where the previous launch left it, then announces this launch. */
function announce(): void {
  fetch(`${STORAGE}/read?path=${APP_FILE}`)
    .then((res) => (res.ok ? res.text().then((text) => (acks = previousAcks(text))) : undefined))
    .catch((err: unknown) => fail("app", err))
    .then(writeApp);
}

function readBarTime(): void {
  fetch("http://localhost/api/time")
    .then((res) => res.json() as Promise<{ timestamp?: unknown }>)
    .then((data) => {
      const ms = typeof data.timestamp === "string" ? Date.parse(data.timestamp) : NaN;
      if (!isNaN(ms)) barTime = Math.floor(ms / 1000) - clock;
    })
    .catch((err: unknown) => fail("time", err));
}

// ---------- Life cycle ----------

function tick(): void {
  clock += 1;
  if (mode === "live" && clock - seenAt >= STALE_SECONDS) mode = "offline";
  if (mode === "demo" && view === "sessions" && --demoLeft <= 0) {
    demoIndex = (demoIndex + 1) % DEMO.length;
    demoLeft = DEMO[demoIndex].seconds;
  }
  if (clock % BEAT_SECONDS === 0 && polling) {
    beat += 1;
    writeApp();
  }
  // A poll draws when it ends; otherwise draw, which sends nothing unless the picture changed.
  if (clock % POLL_SECONDS === 0 && polling && !reading) poll();
  else {
    if (reading) poll(); // Gives up a stuck read.
    draw();
  }
}

function main(): void {
  readBarTime();
  draw();
  poll();
  announce();
  const timer = setInterval(tick, 1000);

  const quit = () => {
    stopped = true;
    clearInterval(timer);
    if (polling) {
      // Tell the Mac at once, so it draws again; this write keeps the app alive until it is sent.
      running = false;
      writeApp();
    }
    // Removing the last listener closes the app; do it after this handler returns.
    setTimeout(unbind, 0);
  };

  const unbind = listen("input", (event) => {
    if (stopped) return;
    if (event.key === "encoder") {
      // Like the collector: the wheel steps through crew → today's tokens → this month's, without wrapping.
      const i = VIEWS.indexOf(view) + event.delta;
      view = VIEWS[Math.min(Math.max(i, 0), VIEWS.length - 1)];
      draw();
    } else if (event.action !== "short") {
      return;
    } else if (event.key === "ok") {
      if (mode === "demo") {
        demoIndex = (demoIndex + 1) % DEMO.length;
        demoLeft = DEMO[demoIndex].seconds;
        draw();
      } else {
        acknowledge();
      }
    } else if (event.key === "back") {
      if (view !== "sessions") {
        view = "sessions";
        draw();
      } else {
        quit();
      }
    }
  });
}

main();
