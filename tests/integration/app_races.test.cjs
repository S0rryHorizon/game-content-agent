// Run the actual UI with manually resolved fetches to force response ordering.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../../src/game_content_agent/web/static/app.js"), "utf8");
const storageKey = "misttide-planner-session";
const session = (id) => ({ session_id: id, world_version: "world", versions: [], runs: [] });
const run = (id, status = "succeeded") => ({ run_id: id, status, purpose: "prepare_brief" });

function harness() {
  const elements = new Map();
  const storage = new Map();
  const requests = [];
  const timers = [];
  function element() {
    return {
      value: "", textContent: "", children: [], dataset: {},
      get firstChild() { return this.children[0]; },
      appendChild(child) { this.children.push(child); },
      removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
      addEventListener() {},
      setAttribute() {},
    };
  }
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: $, createElement: element, createElementNS: element },
    localStorage: {
      getItem: (key) => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    fetch: (url) => new Promise((resolve, reject) => requests.push({
      url, reject, resolve: (body) => resolve({ ok: true, json: async () => body }),
    })),
    setTimeout: (callback) => timers.push(callback),
  });
  assert.ok(source.includes("  init();"));
  vm.runInContext(source.replace("  init();", `  globalThis.ui = {
    state, refreshSession, restoreSession, newSession, loadVersion, selectRun, pollRun, setBusy,
  };`), context);
  return { ...context.ui, $, storage, requests, timers };
}

// Drain fetch/json continuations without real timers or network scheduling.
async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }

async function createSessionB(h) {
  const pending = h.newSession();
  h.requests.at(-1).resolve(session("B"));
  await flush();
  assert.equal(h.requests.at(-1).url, "/sessions/B");
  h.requests.at(-1).resolve(session("B"));
  await pending;
}

for (const outcome of ["success", "failure"]) {
  for (const stage of ["session", "run", "replay"]) {
    test(`late restore ${stage} ${outcome} cannot replace or clear session B`, async () => {
      const h = harness();
      h.storage.set(storageKey, "A");
      const restoring = h.restoreSession();
      if (stage !== "session") {
        h.requests.at(-1).resolve({ ...session("A"), runs: [run("A-run")] });
        await flush();
      }
      if (stage === "replay") {
        h.requests.at(-1).resolve(run("A-run", "running"));
        await flush();
      }
      const stale = h.requests.at(-1);
      await createSessionB(h);
      const notice = h.$("notice").textContent;
      if (outcome === "failure") stale.reject(new Error("old restore failed"));
      else stale.resolve(stage === "session" ? session("A")
        : stage === "run" ? run("A-run", "running") : { events: [{ kind: "start" }] });
      await restoring;
      assert.equal(h.state.session.session_id, "B");
      assert.equal(h.storage.get(storageKey), "B");
      assert.equal(h.state.activeRunId, null);
      assert.equal(h.state.busy, false);
      assert.equal(h.$("notice").textContent, notice);
      assert.equal(h.$("replay-list").children.length, 0);
    });
  }

  test(`latest same-version request wins over stale ${outcome}`, async () => {
    const h = harness();
    h.state.session = { ...session("A"), candidate_id: "c1", versions: [{ version: 1 }] };
    h.state.selectedVersion = 1;
    const old = h.loadVersion(1);
    const latest = h.loadVersion(1);
    const accepted = { candidate: { candidate_id: "c1", version: 1 }, review: { decision: "accept" } };
    h.requests[1].resolve(accepted);
    await latest;
    h.$("notice").textContent = "current notice";
    if (outcome === "failure") h.requests[0].reject(new Error("old version failed"));
    else h.requests[0].resolve({ ...accepted, review: null });
    await old;
    assert.equal(h.state.detail.review.decision, "accept");
    assert.equal(h.$("export-json").disabled, false);
    assert.equal(h.$("notice").textContent, "current notice");
  });

  test(`latest same-run replay wins over stale ${outcome}`, async () => {
    const h = harness();
    const old = h.selectRun("r1");
    const latest = h.selectRun("r1");
    h.requests[1].resolve({ events: [{ kind: "start" }, { kind: "termination" }] });
    await latest;
    h.$("notice").textContent = "current notice";
    if (outcome === "failure") h.requests[0].reject(new Error("old replay failed"));
    else h.requests[0].resolve({ events: [{ kind: "start" }] });
    assert.equal(await old, undefined);
    assert.equal(h.$("replay-list").children.length, 2);
    assert.equal(h.$("notice").textContent, "current notice");
  });

  test(`poll ignores ${outcome} after the active run changes`, async () => {
    const h = harness();
    h.state.activeRunId = "r1";
    h.state.busy = true;
    const pending = h.pollRun("r1");
    h.state.activeRunId = "r2";
    if (outcome === "failure") h.requests[0].reject(new Error("old poll failed"));
    else h.requests[0].resolve(run("r1"));
    await pending;
    assert.equal(h.state.lastRun, null);
    assert.equal(h.state.activeRunId, "r2");
    assert.equal(h.state.busy, true);
    assert.equal(h.requests.length, 1);
    assert.equal(h.timers.length, 0);
  });
}

test("latest session refresh wins even for the same session", async () => {
  const h = harness();
  h.state.session = session("A");
  const old = h.refreshSession();
  const latest = h.refreshSession();
  h.requests[1].resolve({ ...session("A"), runs: [run("r1")] });
  await latest;
  h.requests[0].resolve(session("A"));
  await old;
  assert.equal(h.state.session.runs.length, 1);
});

test("current failures still report errors and restore failures clear the saved session", async () => {
  const h = harness();
  h.storage.set(storageKey, "A");
  const restoring = h.restoreSession();
  h.requests.at(-1).reject(new Error("missing session"));
  await restoring;
  assert.equal(h.state.session, null);
  assert.equal(h.storage.has(storageKey), false);
  assert.match(h.$("notice").textContent, /上次会话无法恢复/);
  h.state.session = { ...session("A"), candidate_id: "c1" };
  h.state.selectedVersion = 1;
  const version = h.loadVersion(1);
  h.requests.at(-1).reject(new Error("current version failed"));
  await version;
  assert.match(h.$("notice").textContent, /current version failed/);
  const replay = h.selectRun("r1");
  h.requests.at(-1).reject(new Error("current replay failed"));
  await replay;
  assert.match(h.$("notice").textContent, /current replay failed/);
});

test("terminal polling stays busy through session refresh and replay", async () => {
  const h = harness();
  h.state.session = session("A");
  h.state.activeRunId = "r1";
  h.setBusy(true);
  const pending = h.pollRun("r1");
  h.requests[0].resolve(run("r1"));
  await flush();
  assert.equal(h.requests[1].url, "/sessions/A");
  assert.equal(h.state.busy, true);
  assert.equal(h.$("new-session").disabled, true);
  h.requests[1].resolve(session("A"));
  await flush();
  assert.equal(h.requests[2].url, "/runs/r1/replay");
  assert.equal(h.state.busy, true);
  assert.equal(h.$("new-session").disabled, true);
  h.requests[2].resolve({ events: [{ kind: "termination" }] });
  await pending;
  assert.equal(h.state.busy, false);
  assert.equal(h.state.activeRunId, null);
  assert.equal(h.$("new-session").disabled, false);
});

test("overlapping polls for the same run cannot regress status", async () => {
  const h = harness();
  h.state.session = session("A");
  h.state.activeRunId = "r1";
  h.state.busy = true;
  const old = h.pollRun("r1");
  const latest = h.pollRun("r1");
  h.requests[1].resolve(run("r1"));
  await flush();
  h.requests[0].resolve(run("r1", "running"));
  await old;
  assert.equal(h.state.lastRun.status, "succeeded");
  assert.equal(h.requests.length, 3);
  h.requests[2].resolve(session("A"));
  await flush();
  h.requests[3].resolve({ events: [] });
  await latest;
  assert.equal(h.state.busy, false);
});

test("terminal refresh failures stop retrying and release busy after three attempts", async () => {
  const h = harness();
  h.state.session = session("A");
  h.state.activeRunId = "r1";
  h.setBusy(true);
  const pending = h.pollRun("r1");
  for (let i = 0; i < 3; i++) {
    h.requests.at(-1).resolve(run("r1"));
    await flush();
    assert.equal(h.requests.at(-1).url, "/sessions/A");
    h.requests.at(-1).reject(new Error("refresh failed"));
    await flush();
    if (i < 2) {
      assert.equal(h.state.busy, true);
      h.timers.shift()();
      await flush();
    }
  }
  await pending;
  assert.equal(h.state.activeRunId, null);
  assert.equal(h.state.busy, false);
  assert.match(h.$("notice").textContent, /refresh failed/);
});
