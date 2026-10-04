const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtTime = (ms) => ms == null ? "—" : `${Math.floor(ms / 60000)}:${String(((ms % 60000) / 1000).toFixed(3)).padStart(6, "0")}`;
const fmtSetup = (s) => s ? `${s.front_wing}/${s.rear_wing} · ${s.on_throttle_diff}/${s.off_throttle_diff}%` : "—";
const colors = {green:"#65e6ad", orange:"#ffad5a", blue:"#65b8ff", red:"#ff7379", yellow:"#ffd166"};
let laps = [], comparison = null;

async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

document.querySelectorAll(".tabs button").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(item => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${button.dataset.view}`));
  if (button.dataset.view === "analysis" && comparison) requestAnimationFrame(() => drawCharts(comparison));
}));

function chips(target, values) {
  target.classList.remove("empty");
  target.innerHTML = values.map(([label, value]) => `<span class="chip">${esc(label)}: <b>${esc(value)}</b></span>`).join("");
}

function renderLive(latest) {
  $("connection").textContent = latest.connected ? "UDP connected" : "Waiting for UDP";
  $("connection").classList.toggle("online", Boolean(latest.connected));
  for (const [id, value] of Object.entries({speed:latest.speed_kph, gear:latest.gear, throttle:latest.throttle, brake:latest.brake})) $(id).textContent = value ?? "—";
  const setup = latest.setup || (latest.front_wing !== undefined ? latest : null);
  if (setup) chips($("setup"), [["Front wing", setup.front_wing], ["Rear wing", setup.rear_wing], ["On-throttle diff", `${setup.on_throttle_diff}%`], ["Off-throttle diff", `${setup.off_throttle_diff}%`]]);
  if (latest.lap_number !== undefined) chips($("lap-state"), [["Lap", latest.lap_number], ["Time", fmtTime(latest.current_lap_ms)], ["Distance", `${latest.lap_distance_m ?? "—"} m`], ["Status", latest.invalid ? "Invalid" : "Valid"]]);
  renderWheels(latest);
}

function renderWheels(latest) {
  const names = ["Rear left", "Rear right", "Front left", "Front right"];
  const inner = latest.tyre_inner_c || [], surface = latest.tyre_surface_c || [], brakes = latest.brake_temps_c || [], pressures = latest.tyre_pressures_psi || [];
  if (!inner.length && !brakes.length) { $("wheels").innerHTML = '<p class="empty">Waiting for car telemetry packet…</p>'; return; }
  const hottestInner = inner.indexOf(Math.max(...inner));
  const hottestBrake = brakes.indexOf(Math.max(...brakes));
  $("wheels").innerHTML = names.map((name, i) => `<section class="wheel ${i === hottestInner ? "hot-inner" : ""} ${i === hottestBrake ? "hot-brake" : ""}"><h3>${name}</h3><dl><dt>Pressure</dt><dd>${pressures[i] ?? "—"} psi</dd><dt>Inner</dt><dd class="${i === hottestInner ? "hot" : ""}">${inner[i] ?? "—"} °C</dd><dt>Surface</dt><dd>${surface[i] ?? "—"} °C</dd><dt>Brake</dt><dd class="${i === hottestBrake ? "hot" : ""}">${brakes[i] ?? "—"} °C</dd></dl></section>`).join("");
}

function lapLabel(lap) { return `Lap ${lap.number} · ${fmtTime(lap.time_ms)}${lap.note ? ` · ${lap.note}` : ""}`; }
function populateSelectors() {
  const valid = laps.filter(lap => !lap.invalid).sort((a,b) => a.time_ms - b.time_ms);
  const previous = [$("baseline").value, $("candidate").value];
  const options = valid.map(lap => `<option value="${esc(lap.id)}">${esc(lapLabel(lap))}</option>`).join("");
  $("baseline").innerHTML = options || '<option>No valid laps</option>';
  $("candidate").innerHTML = options || '<option>No valid laps</option>';
  if (valid.length) {
    $("baseline").value = valid.some(l => l.id === previous[0]) ? previous[0] : valid[0].id;
    $("candidate").value = valid.some(l => l.id === previous[1]) ? previous[1] : (valid[1] || valid[0]).id;
  }
  $("compare").disabled = valid.length < 2;
}

function sampleRate(lap) {
  const seconds = (lap.last_session_time ?? 0) - (lap.first_session_time ?? 0);
  return seconds > 0 ? `${(lap.sample_count / seconds).toFixed(1)} Hz` : "—";
}
function renderSession() {
  const sorted = [...laps].sort((a,b) => a.time_ms - b.time_ms);
  $("lap-count").textContent = `${laps.length} lap${laps.length === 1 ? "" : "s"}`;
  $("session-laps").innerHTML = sorted.map(lap => `<tr class="${lap.invalid ? "invalid-row" : ""}"><td>Lap ${lap.number}${lap.invalid ? " · INVALID" : ""}</td><td class="time">${fmtTime(lap.time_ms)}</td><td>${fmtTime(lap.sector1_ms)} / ${fmtTime(lap.sector2_ms)}</td><td>${esc(fmtSetup(lap.setup))}</td><td>${lap.sample_count}<br><span class="hint">${sampleRate(lap)}</span></td><td>${lap.peak_speed_kph ?? "—"} / ${lap.minimum_speed_kph ?? "—"} km/h</td><td><input class="tag-input" data-id="${esc(lap.id)}" maxlength="240" value="${esc(lap.note)}" placeholder="e.g. rear wing +1"></td></tr>`).join("") || '<tr><td colspan="7">No recorded laps yet.</td></tr>';
  document.querySelectorAll(".tag-input").forEach(input => input.addEventListener("change", () => saveNote(input)));
}
async function saveNote(input) {
  input.disabled = true;
  try {
    const saved = await request(`/api/laps/${encodeURIComponent(input.dataset.id)}/note`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({note:input.value})});
    const lap = laps.find(item => item.id === saved.id); if (lap) lap.note = saved.note;
    populateSelectors();
  } catch (error) { alert(error.message); }
  finally { input.disabled = false; }
}

function setupDifference(a, b) {
  if (!a || !b) return "Setup unavailable";
  const labels = {front_wing:"FW", rear_wing:"RW", on_throttle_diff:"On diff", off_throttle_diff:"Off diff"};
  const changes = Object.keys(labels).filter(key => a[key] !== b[key]).map(key => `${labels[key]} ${a[key]}→${b[key]}`);
  return changes.join(" · ") || "No setup change";
}
function renderComparison(data) {
  comparison = data; $("comparison").hidden = false; $("analysis-empty").hidden = true;
  const b = data.baseline, c = data.candidate, delta = data.summary.final_delta_s;
  $("comparison-summary").innerHTML = [
    ["Baseline", `${fmtTime(b.time_ms)} · ${sampleRate(b)}`], ["Candidate", `${fmtTime(c.time_ms)} · ${sampleRate(c)}`],
    ["Final delta", `${delta >= 0 ? "+" : ""}${delta.toFixed(3)} s`], ["Setup change", setupDifference(b.setup, c.setup)],
    ["Baseline sectors", `${fmtTime(b.sector1_ms)} / ${fmtTime(b.sector2_ms)}`], ["Candidate sectors", `${fmtTime(c.sector1_ms)} / ${fmtTime(c.sector2_ms)}`],
    ["Max gain / loss", `${data.summary.maximum_gain_s.toFixed(3)} / +${data.summary.maximum_loss_s.toFixed(3)} s`], ["Validity", "Both valid"],
  ].map(([label,value]) => `<article class="summary-card"><span>${label}</span><strong>${esc(value)}</strong></article>`).join("");
  const legacy = Object.values(data.alignment).includes("legacy-time");
  $("alignment-notice").textContent = legacy ? "Legacy capture: lap distance was not recorded, so this comparison uses time-normalized alignment. Record new laps for true distance alignment." : "Distance-aligned from recorded lap distance (501 shared points).";
  $("calibration").textContent = data.engineer_notes.calibration;
  $("engineer-notes").innerHTML = data.engineer_notes.windows.map(note => `<article class="note"><h3>${esc(note.name)}</h3><div class="window">${note.start_pct.toFixed(0)}–${note.end_pct.toFixed(0)}% lap distance</div><ul>${note.statements.map(text => `<li>${esc(text)}</li>`).join("")}</ul></article>`).join("");
  drawCharts(data);
}

function pathFor(values, x, y) { return values.map((value, i) => `${i ? "L" : "M"}${x(i)} ${y(value)}`).join(" "); }
function chart(target, series, yMin, yMax, unit, decimals=0) {
  const width = 1000, height = 280, left = 58, right = 16, top = 16, bottom = 38;
  const plotW = width-left-right, plotH = height-top-bottom, count = series[0].values.length;
  const x = i => left + i / (count-1) * plotW;
  const y = value => top + (yMax-value) / Math.max(.001,yMax-yMin) * plotH;
  const ticks = [0,.25,.5,.75,1];
  let svg = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"><g>`;
  ticks.forEach(t => { const yy=top+t*plotH, val=yMax-t*(yMax-yMin); svg += `<path class="axis" d="M${left} ${yy}H${width-right}"/><text class="axis-label" x="${left-8}" y="${yy+4}" text-anchor="end">${val.toFixed(decimals)}${unit}</text>`; });
  ticks.forEach(t => { const xx=left+t*plotW; svg += `<path class="axis" d="M${xx} ${top}V${height-bottom}"/><text class="axis-label" x="${xx}" y="${height-12}" text-anchor="middle">${t*100}%</text>`; });
  series.forEach(item => svg += `<path class="trace" stroke="${item.color}" d="${pathFor(item.values, x, y)}"/>`);
  svg += `<line class="cursor" x1="${left}" x2="${left}" y1="${top}" y2="${height-bottom}" visibility="hidden"/><rect class="hit" x="${left}" y="${top}" width="${plotW}" height="${plotH}" fill="transparent"/></g></svg><div class="legend-row">${series.map(item => `<span class="legend-key" style="--key:${item.color}">${esc(item.label)}</span>`).join("")}</div>`;
  target.innerHTML = svg;
  const hit = target.querySelector(".hit"), cursor = target.querySelector(".cursor"), tooltip = $("tooltip");
  hit.addEventListener("mousemove", event => {
    const rect=hit.getBoundingClientRect(), ratio=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)), index=Math.round(ratio*(count-1));
    const cx=x(index); cursor.setAttribute("x1",cx); cursor.setAttribute("x2",cx); cursor.setAttribute("visibility","visible");
    tooltip.hidden=false; tooltip.style.left=`${Math.min(innerWidth-220,event.clientX+12)}px`; tooltip.style.top=`${event.clientY+12}px`;
    tooltip.innerHTML=`<b>${(index/(count-1)*100).toFixed(1)}% distance</b><br>${series.map(item => `${esc(item.label)}: ${Number(item.values[index]).toFixed(decimals)}${unit}`).join("<br>")}`;
  });
  hit.addEventListener("mouseleave",()=>{cursor.setAttribute("visibility","hidden");tooltip.hidden=true;});
}
function drawCharts(data) {
  const trace=data.trace, delta=trace.map(row=>row.delta_s), speedsB=trace.map(row=>row.baseline.speed_kph), speedsC=trace.map(row=>row.candidate.speed_kph);
  const deltaRange=Math.max(.1,...delta.map(Math.abs));
  chart(document.querySelector('[data-chart="delta"]'),[{label:"Candidate − baseline",color:colors.yellow,values:delta}],-deltaRange,deltaRange," s",2);
  chart(document.querySelector('[data-chart="speed"]'),[{label:"Baseline speed",color:colors.green,values:speedsB},{label:"Candidate speed",color:colors.orange,values:speedsC}],0,Math.ceil(Math.max(...speedsB,...speedsC)/50)*50," km/h");
  chart(document.querySelector('[data-chart="pedals"]'),[{label:"Baseline throttle",color:colors.green,values:trace.map(r=>r.baseline.throttle)},{label:"Candidate throttle",color:colors.orange,values:trace.map(r=>r.candidate.throttle)},{label:"Baseline brake",color:colors.blue,values:trace.map(r=>r.baseline.brake)},{label:"Candidate brake",color:colors.red,values:trace.map(r=>r.candidate.brake)}],0,100,"% ");
}

$("compare").addEventListener("click", async () => {
  $("compare-error").textContent="";
  try { renderComparison(await request(`/api/compare?baseline=${encodeURIComponent($("baseline").value)}&candidate=${encodeURIComponent($("candidate").value)}`)); }
  catch(error) { $("compare-error").textContent=error.message; }
});

async function loadLaps() { const data=await request("/api/laps"); laps=data.laps; populateSelectors(); if (!document.querySelector(".tag-input:focus")) renderSession(); }
async function refreshLive() { try { renderLive((await request("/api/snapshot")).latest); } catch (_) { $("connection").textContent="Dashboard offline"; $("connection").classList.remove("online"); } }
loadLaps(); refreshLive(); setInterval(refreshLive,1000); setInterval(loadLaps,10000);
