import test from "node:test";
import assert from "node:assert/strict";

import {RequestGeneration, createDashboardState} from "../state/store.js";
import {createEventRegistry} from "../events/register.js";
import {
  mergeProfilesPreservingDraft,
  resolveComparisonSources,
  retainSelectedIds,
} from "../state/selectors.js";
import {createPollingController} from "../sync/polling.js";

test("a stale archive response cannot apply after session selection changes", () => {
  const gate = new RequestGeneration();
  const oldRequest = gate.issue();
  gate.invalidate();
  const selectedSessionRequest = gate.issue();

  assert.equal(gate.isCurrent(oldRequest), false);
  assert.equal(gate.isCurrent(selectedSessionRequest), true);
});

test("dashboard archive state provides constant-time identity maps", () => {
  const state = createDashboardState();
  state.sessionsById.set("session-a", {id: "session-a"});
  state.lapsById.set("lap-a", {id: "lap-a"});
  assert.equal(state.sessionsById.get("session-a").id, "session-a");
  assert.equal(state.lapsById.get("lap-a").id, "lap-a");
});

test("a slower stale response cannot repaint a newer selection", async () => {
  const gate = new RequestGeneration();
  const resolvers = {};
  const displayed = [];
  const load = async name => {
    const generation = gate.issue();
    const value = await new Promise(resolve => { resolvers[name] = resolve; });
    if (gate.isCurrent(generation)) displayed.push(value);
  };

  const oldLoad = load("old");
  const newLoad = load("new");
  resolvers.new("new session");
  await newLoad;
  resolvers.old("old session");
  await oldLoad;
  assert.deepEqual(displayed, ["new session"]);
});

test("dirty profile draft and export selections survive background refresh", () => {
  const draft = {id: "draft", turns: [{number: 1}]};
  assert.deepEqual(
    mergeProfilesPreservingDraft([{id: "saved"}], [draft], "draft", true),
    [{id: "saved"}, draft],
  );
  assert.deepEqual([...retainSelectedIds(new Set(["lap-a", "lap-b"]), [{id: "lap-b"}, {id: "lap-c"}])], ["lap-b"]);
});

test("baseline and candidate sources are preserved independently", () => {
  const sources = [{value: "session:a"}, {value: "session:b"}];
  assert.deepEqual(resolveComparisonSources(sources, {
    baselineSource: "session:a",
    candidateSource: "session:b",
    baselineLap: "lap-1",
    candidateLap: "lap-7",
  }, "session:a"), {
    baselineSource: "session:a",
    candidateSource: "session:b",
    baselineLap: "lap-1",
    candidateLap: "lap-7",
  });
});

test("polling pauses callbacks while the document is hidden", () => {
  const callbacks = [];
  const intervals = [];
  const documentRef = {hidden: true};
  const polling = createPollingController({
    documentRef,
    onLive: () => callbacks.push("live"),
    onLibrary: () => callbacks.push("library"),
    setIntervalImpl: callback => { intervals.push(callback); return intervals.length; },
    clearIntervalImpl: () => {},
  });
  polling.startLive();
  polling.setLibraryInterval(8);
  intervals.forEach(callback => callback());
  assert.deepEqual(callbacks, []);
  documentRef.hidden = false;
  intervals.forEach(callback => callback());
  assert.deepEqual(callbacks, ["live", "library"]);
});

test("event registry owns static listener registration and cleanup", () => {
  const calls = [];
  const target = {
    addEventListener: (name, listener) => calls.push(["add", name, listener]),
    removeEventListener: (name, listener) => calls.push(["remove", name, listener]),
  };
  const documentRef = {
    getElementById: id => id === "save" ? target : null,
    querySelectorAll: () => [target],
  };
  const registry = createEventRegistry(documentRef);
  const listener = () => {};
  registry.onId("save", "click", listener);
  registry.onSelectorAll("[data-control]", "change", listener);
  registry.dispose();
  assert.deepEqual(calls.map(([operation, name]) => [operation, name]), [
    ["add", "click"], ["add", "change"], ["remove", "change"], ["remove", "click"],
  ]);
});
