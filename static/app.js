const byId = (id) => document.getElementById(id);
const fmtTime = (ms) => `${Math.floor(ms / 60000)}:${String(((ms % 60000) / 1000).toFixed(3)).padStart(6, "0")}`;

async function get(url) { const res = await fetch(url); return res.json(); }

function chips(target, values) {
  target.innerHTML = values.map(([label, value]) => `<span class="chip">${label}: <b>${value}</b></span>`).join("");
}

function renderLive(latest) {
  const online = latest.connected;
  byId("connection").textContent = online ? "UDP connected" : "Waiting for UDP";
  byId("connection").classList.toggle("online", online);
  const values = { speed: latest.speed_kph, gear: latest.gear, throttle: latest.throttle, brake: latest.brake, distance: latest.lap_distance_m };
  for (const [name, value] of Object.entries(values)) byId(name).textContent = value ?? "—";
  if (latest.front_wing !== undefined) chips(byId("setup"), [["Front wing", latest.front_wing], ["Rear wing", latest.rear_wing], ["On throttle diff", `${latest.on_throttle_diff}%`], ["Off throttle diff", `${latest.off_throttle_diff}%`]]);
  if (latest.brake_temps_c) chips(byId("temps"), [["Brake RL/RR/FL/FR", latest.brake_temps_c.join(" / ") + " °C"], ["Pressure RL/RR/FL/FR", latest.tyre_pressures_psi.join(" / ") + " psi"]]);
}

let selected = [];
async function renderLaps(laps) {
  byId("lap-list").innerHTML = laps.slice().reverse().map(lap => `<div class="lap ${lap.invalid ? "invalid" : ""}"><label><input type="checkbox" value="${lap.id}" ${selected.includes(lap.id) ? "checked" : ""} ${lap.invalid ? "disabled" : ""}> Lap ${lap.number} ${lap.invalid ? "(invalid)" : ""}</label><span class="time">${fmtTime(lap.time_ms)}</span><span>${lap.sample_count} samples</span></div>`).join("") || "No completed laps yet. Finish a timed lap to record it.";
  document.querySelectorAll("#lap-list input").forEach(input => input.addEventListener("change", async () => { selected = [...document.querySelectorAll("#lap-list input:checked")].map(x => x.value).slice(-2); await drawSelected(); }));
}

function polyline(samples, color) {
  if (!samples?.length) return "";
  const speeds = samples.map(x => x.speed_kph || 0); const max = Math.max(360, ...speeds);
  const points = speeds.map((speed, i) => `${(i / Math.max(1, speeds.length - 1) * 980 + 10).toFixed(1)},${(280 - speed / max * 260).toFixed(1)}`).join(" ");
  return `<polyline fill="none" stroke="${color}" stroke-width="4" points="${points}"/>`;
}

async function drawSelected() {
  const laps = await Promise.all(selected.map(id => get(`/api/laps/${id}`)));
  byId("chart").innerHTML = `<path d="M10 280H990 M10 150H990 M10 20H990" stroke="#293142" stroke-width="1"/>${polyline(laps[0]?.samples, "#81f7bc")}${polyline(laps[1]?.samples, "#ffad5a")}`;
  byId("chart-title").textContent = laps.length ? `Speed trace — ${laps.map(l => fmtTime(l.time_ms)).join(" vs ")}` : "Lap comparison";
}

async function refresh() { const data = await get("/api/snapshot"); renderLive(data.latest); await renderLaps(data.laps); }
refresh(); setInterval(refresh, 1000);
