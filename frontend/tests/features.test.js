import test from "node:test";
import assert from "node:assert/strict";

import {createApiClient} from "../api/client.js";
import {comparisonCompatibility, sourceLaps} from "../features/analysis/model.js";
import {turnBoundaries} from "../features/circuit-profiler/model.js";
import {recorderPresentation, wheelTelemetry} from "../features/live/model.js";
import {filteredSessions} from "../features/sessions/model.js";
import {settingsPayload} from "../features/settings/model.js";

test("API client preserves server errors and JSON POST bodies", async () => {
  let captured;
  const client = createApiClient(async (url, options) => {
    captured = {url, options};
    return {ok: true, json: async () => ({saved: true})};
  });
  assert.deepEqual(await client.post("/api/example", {value: 7}), {saved: true});
  assert.equal(captured.url, "/api/example");
  assert.equal(captured.options.body, '{"value":7}');

  const failing = createApiClient(async () => ({ok: false, status: 409, json: async () => ({error: "conflict"})}));
  await assert.rejects(() => failing.request("/api/example"), /conflict/);
});

test("analysis feature keeps PB traces separate and reports exploratory comparisons", () => {
  const pbLaps = sourceLaps("pb:spa", [], [{
    key: "spa", trace_available: true, comparison_id: "pb:spa",
    comparison_source_lap_number: 4, comparison_lap_time_ms: 105000,
  }]);
  assert.equal(pbLaps[0].number, 4);
  assert.equal(pbLaps[0].time_ms, 105000);

  const result = comparisonCompatibility(
    {track_id: 10, mode: "time_trial", session_id: "a"},
    {track_id: 10, mode: "race", session_id: "b"},
    {invalid: false},
    {invalid: true},
    [],
  );
  assert.equal(result.error, "");
  assert.equal(result.warnings.length, 2);
  assert.match(result.warnings[0], /exploratory/);
  assert.match(result.warnings[1], /game-invalid/);
});

test("feature models preserve live wheel order, recorder wording and profiler margins", () => {
  const wheels = wheelTelemetry({
    tyre_inner_c: [80, 81, 95, 82],
    brake_temps_c: [400, 500, 450, 420],
  });
  assert.deepEqual(wheels.map(wheel => wheel.name), ["Rear left", "Rear right", "Front left", "Front right"]);
  assert.equal(wheels[2].hottestInner, true);
  assert.equal(wheels[1].hottestBrake, true);
  assert.match(recorderPresentation({status: "armed"}, {game_mode: "time_trial"}).statusMessage, /partial approach is excluded/);
  assert.deepEqual(turnBoundaries(500, 60, 40, 80, 60, 1000), {entry_m: 460, apex_m: 500, exit_m: 580});
});

test("session and settings models remain deterministic", () => {
  const sessions = [
    {id: "a", started_at: "2026-10-01T00:00:00Z"},
    {id: "b", started_at: "2026-10-02T00:00:00Z"},
  ];
  assert.deepEqual(filteredSessions(sessions, {year: "2026", month: "10", day: ""}).map(item => item.id), ["b", "a"]);
  const values = new Map([
    ["setting-web-host", "127.0.0.1"], ["setting-web-port", "8025"],
    ["setting-udp-port", "20777"], ["setting-trash-days", "30"],
    ["setting-max-recording", "8"], ["setting-diagnostic-mb", "50"],
    ["setting-refresh-seconds", "9"],
  ]);
  assert.equal(settingsPayload(id => values.get(id)).dashboard_refresh_seconds, 9);
});
