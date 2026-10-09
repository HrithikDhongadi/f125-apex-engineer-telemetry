const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtTime = (ms) => ms == null ? "—" : `${Math.floor(ms / 60000)}:${String(((ms % 60000) / 1000).toFixed(3)).padStart(6, "0")}`;
const fmtSetup = (s) => s ? `${s.front_wing??"unknown"}/${s.rear_wing??"unknown"} wings · ${s.on_throttle_diff??"unknown"}/${s.off_throttle_diff??"unknown"}% diff · ${setupFields.filter(f=>s[f.key]!==undefined).length}/${setupFields.length} fields` : "unknown";
const colors = {green:"#65e6ad",orange:"#ffad5a",blue:"#65b8ff",red:"#ff7379",yellow:"#ffd166"};
let sessions = [], laps = [], allLaps = [], personalBests = [], setupFields = [], circuitProfiles = [], circuitTrace = [], suggestedTurns = [], raceTimeline = [], selectedLapIds = new Set(), selectedSessionId = null, activeRecording = null, latestState = {}, diagnosticState = {}, comparison = null, editingLap = null;
let circuitEditor = null, paceMapCanvas = null, paceMapSource = null, pointedTurn = null, selectedTurnIndex = null, profileEditorDirty = false, profileEditorSessionId = null;
let showAllTurnCards = false;
let turnDisplayMode = "minimal";
let sessionFilter = {year:"", month:"", day:""};
let dashboardSettings = {}, deletedSessions = [], receiverIps = [], libraryTimer = null, libraryRefreshSeconds = 8;
try { turnDisplayMode = localStorage.getItem("circuit-profiler-turn-display") || "minimal"; } catch (_) {}
if (!["minimal","dots","ranges"].includes(turnDisplayMode)) turnDisplayMode = "minimal";

async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
async function post(url, payload={}) { return request(url, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}); }

function activateView(button) {
  document.querySelectorAll(".tabs button").forEach(item => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${button.dataset.view}`));
  if (button.dataset.view === "analysis" && comparison) requestAnimationFrame(() => drawCharts(comparison));
  if (button.dataset.view === "circuit-profiler") requestAnimationFrame(renderCircuitMap);
}
document.querySelectorAll(".tabs button").forEach(button => button.addEventListener("click", () => activateView(button)));
document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    if ($("setup-dialog").open) $("setup-dialog").close();
    if (pointedTurn) cancelPointingTurn();
    $("tooltip").hidden=true;
    return;
  }
  if (event.ctrlKey||event.metaKey||event.altKey||event.target.matches?.("input, textarea, select, [contenteditable=true]")) return;
  const index=Number(event.key)-1,tabs=[...document.querySelectorAll(".tabs button")];
  if(index>=0&&index<tabs.length){event.preventDefault();activateView(tabs[index]);tabs[index].focus({preventScroll:true});}
});

function chips(target, values) {
  target.classList.remove("empty");
  target.innerHTML = values.map(([label,value]) => `<span class="chip">${esc(label)}: <b>${esc(value)}</b></span>`).join("");
}
function renderSettings(){
  if(!dashboardSettings.web_port)return;
  if(!$("settings-form").contains(document.activeElement)){
    $("setting-web-host").value=dashboardSettings.web_host;
    $("setting-web-port").value=dashboardSettings.web_port;
    $("setting-udp-port").value=dashboardSettings.udp_port;
    $("setting-trash-days").value=dashboardSettings.trash_retention_days;
    $("setting-max-recording").value=dashboardSettings.max_recording_gb;
    $("setting-diagnostic-mb").value=dashboardSettings.diagnostic_max_mb;
    $("setting-refresh-seconds").value=dashboardSettings.dashboard_refresh_seconds;
  }
  $("telemetry-help-port").textContent=dashboardSettings.udp_port;
  $("receiver-ip-list").innerHTML=receiverIps.map(ip=>`<span class="chip"><b>${esc(ip)}</b>${ip.startsWith("127.")?" · same computer":" · LAN"}</span>`).join("");
  $("deleted-session-list").innerHTML=deletedSessions.length?deletedSessions.map(item=>`<article class="deleted-session"><div><strong>${esc(item.name)}</strong><span>${esc(item.mode.replaceAll("_"," "))} · track ID ${item.track_id??"unknown"} · ${item.lap_count} laps</span><span>Deleted ${new Date(item.deleted_at).toLocaleString()} · ${item.purge_at?`permanent deletion ${new Date(item.purge_at).toLocaleString()}`:"kept until restored"}</span></div><button type="button" data-restore="${esc(item.trash_id)}">Restore</button></article>`).join(""):'<p class="empty">No deleted sessions.</p>';
  document.querySelectorAll("[data-restore]").forEach(button=>button.addEventListener("click",()=>restoreDeletedSession(button.dataset.restore)));
}
async function restoreDeletedSession(trashId){
  const item=deletedSessions.find(entry=>entry.trash_id===trashId);if(!item)return;
  if(!window.confirm(`Restore “${item.name}” to the session library?`))return;
  try{const result=await post(`/api/trash/sessions/${encodeURIComponent(trashId)}/restore`,{confirm:true});$("settings-message").textContent=`Restored ${result.restored_session_id} with ${result.lap_count} lap(s).`;await loadLibrary();}catch(error){$("settings-message").textContent=error.message;}
}
function scheduleLibraryRefresh(){
  const seconds=Number(dashboardSettings.dashboard_refresh_seconds)||8;
  if(libraryTimer&&seconds===libraryRefreshSeconds)return;
  if(libraryTimer)clearInterval(libraryTimer);
  libraryRefreshSeconds=seconds;libraryTimer=setInterval(loadLibrary,seconds*1000);
}
function renderLive(latest) {
  latestState = latest;
  $("connection").textContent = latest.receiver_error ? `UDP packet error · ${latest.receiver_error}` : latest.connected ? "UDP connected" : "Waiting for UDP";
  $("connection").classList.toggle("online", Boolean(latest.connected&&!latest.receiver_error));
  for (const [id,value] of Object.entries({speed:latest.speed_kph,gear:latest.gear,throttle:latest.throttle,brake:latest.brake})) $(id).textContent=value??"—";
  const setup=latest.setup||(latest.front_wing!==undefined?latest:null);
  if(setup) chips($("setup"),setupFields.map(field=>[field.label,setup[field.key]===undefined?"unknown":`${setup[field.key]}${field.unit?` ${field.unit}`:""}`]));
  if(latest.lap_number!==undefined) chips($("lap-state"),[["Lap",latest.lap_number],["Time",fmtTime(latest.current_lap_ms)],["Distance",`${latest.lap_distance_m??"—"} m`],["Status",latest.invalid?"Invalid":"Valid"]]);
  renderWheels(latest);
}
function renderWheels(latest) {
  const names=["Rear left","Rear right","Front left","Front right"],inner=latest.tyre_inner_c||[],surface=latest.tyre_surface_c||[],brakes=latest.brake_temps_c||[],pressures=latest.tyre_pressures_psi||[];
  if(!inner.length&&!brakes.length){$("wheels").innerHTML='<p class="empty">Waiting for car telemetry packet…</p>';return;}
  const hottestInner=inner.indexOf(Math.max(...inner)),hottestBrake=brakes.indexOf(Math.max(...brakes));
  $("wheels").innerHTML=names.map((name,i)=>`<section class="wheel ${i===hottestInner?"hot-inner":""} ${i===hottestBrake?"hot-brake":""}"><h3>${name}</h3><dl><dt>Pressure</dt><dd>${pressures[i]??"—"} psi</dd><dt>Inner</dt><dd class="${i===hottestInner?"hot":""}">${inner[i]??"—"} °C</dd><dt>Surface</dt><dd>${surface[i]??"—"} °C</dd><dt>Brake</dt><dd class="${i===hottestBrake?"hot":""}">${brakes[i]??"—"} °C</dd></dl></section>`).join("");
}

function renderRecorder() {
  const recording=activeRecording, mode=latestState.game_mode||"unknown", track=latestState.track_name||"Unknown track";
  const badge=$("recording-badge");
  badge.className=`recording-badge ${recording?.status==="armed"?"armed":recording?"active":""}`;
  badge.textContent=recording?(recording.status==="armed"?"Armed":recording.capturing_lap_number!=null?`Recording lap ${recording.capturing_lap_number}`:"Recording"):"Not recording";
  $("recording-name").textContent=recording?.name||(mode==="race"?"Race recorder ready":mode==="time_trial"?"Time Trial · start a run":"Waiting for game session");
  $("game-context").textContent=`${mode.replace("_"," ")} · ${track}${latestState.game_session_uid?` · game UID ${latestState.game_session_uid}`:""}`;
  $("recording-message").textContent=recording?.status==="armed"
    ? "Armed — waiting for the next start/finish crossing; the partial approach is excluded."
    : recording?.capturing_lap_number!=null
      ? `Capturing lap ${recording.capturing_lap_number}; it will be saved at the next verified start/finish crossing.`
      : recording
        ? "Recording active — waiting for the next complete-lap start/finish crossing."
        : "";
  $("run-name").hidden=mode!=="time_trial"||Boolean(recording);
  $("start-run").hidden=mode!=="time_trial"||Boolean(recording);
  $("stop-recording").hidden=!recording;
  $("jump-active").hidden=!recording||recording.id===selectedSessionId;
  $("historical-badge").hidden=Boolean(recording&&recording.id===selectedSessionId);
  $("diagnostic-enabled").checked=Boolean(diagnosticState.enabled);
  $("diagnostic-status").textContent=diagnosticState.enabled
    ? `Diagnostics on${diagnosticState.active_file?` · ${diagnosticState.active_file} · ${diagnosticState.events} events`:" · the next recording will create a bounded trace"}${diagnosticState.truncated?" · size limit reached":""}.`
    : "Diagnostics are off. Enable before starting the short test run.";
  if(!recording) $("historical-badge").textContent="Historical/closed session · live telemetry remains current";
  else $("historical-badge").textContent="Historical view · live telemetry remains current";
}
function sessionLabel(session){return `${session.name} · ${session.mode.replace("_"," ")} · ${session.track_name} · ${session.lap_count} laps`;}
function sessionDateParts(session){
  if(!session.started_at)return {year:"legacy",month:"",day:""};
  const date=new Date(session.started_at);
  if(Number.isNaN(date.getTime()))return {year:"legacy",month:"",day:""};
  return {year:String(date.getFullYear()),month:String(date.getMonth()+1).padStart(2,"0"),day:String(date.getDate()).padStart(2,"0")};
}
function filteredSessions(){
  if(!sessionFilter.year)return [];
  return sessions.filter(session=>{
    const date=sessionDateParts(session);
    return date.year===sessionFilter.year&&(!sessionFilter.month||date.month===sessionFilter.month)&&(!sessionFilter.day||date.day===sessionFilter.day);
  });
}
function sessionChoices(){
  const selected=sessions.find(session=>session.id===selectedSessionId),matches=filteredSessions();
  return selected?[selected,...matches.filter(session=>session.id!==selected.id)]:matches;
}
function renderSessionQuery(){
  const years=[...new Set(sessions.map(session=>sessionDateParts(session).year))].sort((a,b)=>a==="legacy"?1:b==="legacy"?-1:Number(b)-Number(a));
  if(sessionFilter.year&&!years.includes(sessionFilter.year))sessionFilter={year:"",month:"",day:""};
  $("session-filter-year").innerHTML='<option value="">Choose year</option>'+years.map(year=>`<option value="${esc(year)}">${year==="legacy"?"Legacy / no date":esc(year)}</option>`).join("");
  $("session-filter-year").value=sessionFilter.year;

  const matchingYear=sessions.filter(session=>sessionDateParts(session).year===sessionFilter.year);
  const months=[...new Set(matchingYear.map(session=>sessionDateParts(session).month).filter(Boolean))].sort();
  if(sessionFilter.month&&!months.includes(sessionFilter.month)){sessionFilter.month="";sessionFilter.day="";}
  $("session-filter-month").innerHTML='<option value="">All months</option>'+months.map(month=>`<option value="${month}">${new Intl.DateTimeFormat(undefined,{month:"long"}).format(new Date(2000,Number(month)-1,1))}</option>`).join("");
  $("session-filter-month").value=sessionFilter.month;
  $("session-filter-month").disabled=!sessionFilter.year||sessionFilter.year==="legacy";

  const matchingMonth=matchingYear.filter(session=>sessionDateParts(session).month===sessionFilter.month);
  const days=[...new Set(matchingMonth.map(session=>sessionDateParts(session).day).filter(Boolean))].sort((a,b)=>Number(a)-Number(b));
  if(sessionFilter.day&&!days.includes(sessionFilter.day))sessionFilter.day="";
  $("session-filter-day").innerHTML='<option value="">All dates</option>'+days.map(day=>`<option value="${day}">${Number(day)}</option>`).join("");
  $("session-filter-day").value=sessionFilter.day;
  $("session-filter-day").disabled=!sessionFilter.month;
  const count=filteredSessions().length;
  $("session-filter-count").textContent=sessionFilter.year?`${count} session${count===1?"":"s"} found`:"No archive sessions shown";
}
function renderSessionLibrary() {
  const choices=sessionChoices(),matches=filteredSessions(),previous=$("session-select").value;
  $("session-select").innerHTML=choices.map(session=>`<option value="${esc(session.id)}">${esc(`${session.id===selectedSessionId?"Displayed · ":""}${sessionLabel(session)}`)}</option>`).join("")||'<option value="">Choose a date query</option>';
  $("session-select").value=choices.some(s=>s.id===selectedSessionId)?selectedSessionId:previous;
  $("session-cards").innerHTML=matches.map(session=>{
    const date=session.started_at?new Date(session.started_at).toLocaleString():"Original v0.1/v0.2 files";
    const stream=session.continuous_store,streamText=stream?.details_deferred?"<br>Continuous race data available · detailed continuity is calculated for reports":stream?`<br>Continuous: ${stream.packet_count??"?"} packets · ${stream.event_count??"?"} events · ${stream.missing_frame_estimate??"?"} missing in worst 60 Hz stream (${stream.missing_frame_percent??"?"}%) · ${stream.dropped_packets??0} dropped by storage limit`:"";
    return `<article class="session-card ${session.id===selectedSessionId?"selected":""}" data-session="${esc(session.id)}"><h3>${esc(session.name)}</h3><div class="session-meta">${esc(session.mode.replace("_"," "))} · ${esc(session.track_name)} · ${esc(session.status)}<br>${esc(date)} · ${session.lap_count} laps · best ${fmtTime(session.best_valid_lap_ms)}${streamText}</div></article>`;
  }).join("")||(sessionFilter.year?'<p class="empty-state">No sessions match this date query.</p>':'<p class="empty-state">Choose a year above to show saved sessions.</p>');
  document.querySelectorAll(".session-card").forEach(card=>card.addEventListener("click",()=>selectSession(card.dataset.session)));
  const selected=sessions.find(s=>s.id===selectedSessionId);
  $("rename-session").value=selected?.read_only?"":selected?.name||"";
  $("rename-session").disabled=Boolean(!selected||selected.read_only);
  $("rename-button").disabled=Boolean(!selected||selected.read_only);
  $("delete-session").disabled=Boolean(!selected||!selected.can_delete);
  $("delete-session").title=!selected?"Select a session":selected.read_only?"Legacy captures cannot be deleted":!selected.can_delete?"Stop the active recording before deleting it":"Move this session and its lap files to recoverable local trash";
  $("track-override-row").hidden=Boolean(!selected||!selected.can_override_track_name||selected.read_only);
  $("track-override").value=selected?.track_name_override||"";
  const selectedProfile=circuitProfiles.find(profile=>profile.id===selected?.circuit_profile_id);
  $("session-profile-name").textContent=selectedProfile?.circuit_name||selected?.circuit_profile_id||"None selected";
  $("session-profile-status").textContent=selectedProfile?`${selectedProfile.verification_status} · ${selectedProfile.turns?.length||0} turns · ${selectedProfile.id}`:selected?.circuit_profile_id?"Assigned profile is unavailable; choose another profile.":"Open Circuit Profiler to create or select one.";
  renderRecorder();
}
async function selectSession(id){try{await post("/api/sessions/select",{session_id:id});selectedSessionId=id;selectedLapIds.clear();profileEditorDirty=false;pointedTurn=null;selectedTurnIndex=null;circuitTrace=[];suggestedTurns=[];circuitEditor?.setTrace([],[]);comparison=null;$("comparison").hidden=true;$("analysis-empty").hidden=false;await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}}

function selectorSources(){return [...sessionChoices().map(s=>({value:`session:${s.id}`,label:sessionLabel(s)})),...personalBests.map(pb=>({value:`pb:${pb.key}`,label:`Stored PB · ${pb.track_name} · ${pb.mode.replace("_"," ")} · ${fmtTime(pb.time_ms)}`}))];}
function sourceLaps(value){if(value.startsWith("pb:")){const pb=personalBests.find(item=>`pb:${item.key}`===value);return pb?[{...pb,id:pb.comparison_id,number:pb.source_lap_number,invalid:false}]:[];}return allLaps.filter(lap=>`session:${lap.recording_session_id}`===value).sort((a,b)=>a.number-b.number);}
function lapOption(lap){return `<option value="${esc(lap.id)}">Lap ${lap.number} · ${fmtTime(lap.time_ms)} · ${lap.invalid?"INVALID":"valid"}</option>`;}
function populateLapSelector(side,preferred){const source=$(side+"-session").value,items=sourceLaps(source),select=$(side),old=preferred||select.value;select.innerHTML=items.map(lapOption).join("")||'<option value="">No laps</option>';if(items.some(l=>l.id===old))select.value=old;checkCompatibility();}
function populateSelectors(){
  const sources=selectorSources(),state={bs:$("baseline-session").value,b:$("baseline").value,cs:$("candidate-session").value,c:$("candidate").value};
  const html=sources.map(source=>`<option value="${esc(source.value)}">${esc(source.label)}</option>`).join("");
  $("baseline-session").innerHTML=html;$("candidate-session").innerHTML=html;
  const fallback=`session:${selectedSessionId}`,candidateFallback=sources.find(s=>s.value===fallback)?.value||sources[0]?.value||"";
  $("baseline-session").value=sources.some(s=>s.value===state.bs)?state.bs:candidateFallback;
  $("candidate-session").value=sources.some(s=>s.value===state.cs)?state.cs:candidateFallback;
  populateLapSelector("baseline",state.b);populateLapSelector("candidate",state.c);
  $("compare").disabled=!$("baseline").value||!$("candidate").value;
}
function chosenMeta(side){const source=$(side+"-session").value;if(source.startsWith("pb:")){const pb=personalBests.find(item=>`pb:${item.key}`===source);return pb&&{track_id:pb.track_id,mode:pb.mode,session_id:pb.source_session_id,profile_id:null};}const session=sessions.find(item=>`session:${item.id}`===source);return session&&{track_id:session.track_id,mode:session.mode,session_id:session.id,profile_id:session.circuit_profile_id};}
function chosenLapMeta(side){const source=$(side+"-session").value;return sourceLaps(source).find(lap=>lap.id===$(side).value);}
function checkCompatibility(){const b=chosenMeta("baseline"),c=chosenMeta("candidate"),bl=chosenLapMeta("baseline"),cl=chosenLapMeta("candidate");let message="",warnings=[];if(b&&c){if(b.track_id==null||c.track_id==null||b.track_id<0||c.track_id<0)message="Unknown track identity cannot be compared.";else if(b.track_id!==c.track_id)message=`Incompatible circuits: track ID ${b.track_id} versus ${c.track_id}.`;else if(b.mode===c.mode&&b.mode==="race"&&b.session_id!==c.session_id){const profile=circuitProfiles.find(p=>p.id===b.profile_id);if(!profile||b.profile_id!==c.profile_id||profile.verification_status!=="verified")message="Cross-session race comparison requires both sessions to select the same manually verified circuit profile.";}if(!message&&b.mode!==c.mode)warnings.push("Race and Time Trial conditions are not like-for-like; results will be exploratory.");if(bl?.invalid)warnings.push("Baseline is game-invalid and cannot be treated as valid-lap or PB evidence.");if(cl?.invalid)warnings.push("Candidate is game-invalid and cannot be treated as valid-lap or PB evidence.");}$("compare-error").textContent=message;$("compare-warning").textContent=warnings.join("\n");$("compare").disabled=Boolean(message)||!$("baseline").value||!$("candidate").value;}
function sampleRate(lap){const seconds=(lap.last_session_time??0)-(lap.first_session_time??0);return seconds>0?`${(lap.sample_count/seconds).toFixed(1)} Hz`:"—";}
function isControllerEvent(event){return event.type==="controller_input"||event.type==="butn";}
function renderRaceTimeline(){
  const showController=$("timeline-controller-events").checked;
  const showOther=$("timeline-other-car-events").checked;
  const showSuperseded=$("timeline-superseded-events").checked;
  const visible=raceTimeline.filter(event=>(showController||!isControllerEvent(event))&&(showOther||event.player_relevant!==false)&&(showSuperseded||event.event_validity!=="superseded")).slice(-200);
  $("race-timeline").innerHTML=visible.length?visible.map(event=>{
    const controller=isControllerEvent(event);
    const validity=event.event_validity||"unknown";
    const details=event.details||{};
    const label=controller?"Controller input":details.penalty_classification?.replaceAll("_"," ")||event.type.replaceAll("_"," ");
    const controllerStatus=controller&&Number.isInteger(details.button_status)?` · status 0x${details.button_status.toString(16).padStart(8,"0")}`:"";
    const branch=event.timeline_branch_id?` · ${event.timeline_branch_id}`:"";
    const reason=validity!=="accepted"&&event.validation_reason?` · ${event.validation_reason}`:"";
    return `<div class="timeline-event timeline-${esc(validity)}"><time>${Number(event.session_time).toFixed(3)} s</time><strong>${esc(label)}</strong><span>${esc(event.source)} · lap ${event.lap_number??"—"}${branch} · ${esc(validity)}${controllerStatus}${esc(reason)}</span></div>`;
  }).join(""):raceTimeline.length?'<p class="empty">No events match the current timeline filters.</p>':'<p class="empty">No continuous timeline for this session.</p>';
}
function renderSession(){
  const sorted=[...laps].sort((a,b)=>a.time_ms-b.time_ms);$("lap-count").textContent=`${laps.length} lap${laps.length===1?"":"s"}`;
  $("session-laps").innerHTML=sorted.map(lap=>`<tr class="${lap.invalid?"invalid-row":""}"><td><input type="checkbox" class="lap-export-check" data-id="${esc(lap.id)}" ${selectedLapIds.has(lap.id)?"checked":""} aria-label="Select lap ${lap.number} for export"></td><td>Lap ${lap.number}${lap.invalid?" · INVALID":""}${lap.event_count?`<br><span class="hint">${lap.event_count} flashback/restart event${lap.event_count===1?"":"s"}</span>`:""}</td><td class="time">${fmtTime(lap.time_ms)}</td><td>${fmtTime(lap.sector1_ms)} / ${fmtTime(lap.sector2_ms)}</td><td>${esc(fmtSetup(lap.setup))}</td><td>${lap.sample_count}<br><span class="hint">${sampleRate(lap)}</span></td><td>${lap.peak_speed_kph??"—"} / ${lap.minimum_speed_kph??"—"} km/h</td><td><input class="tag-input" data-id="${esc(lap.id)}" maxlength="240" value="${esc(lap.note)}" placeholder="e.g. rear wing +1"></td><td><button class="edit-setup" data-id="${esc(lap.id)}">Edit/Add setup</button></td></tr>`).join("")||'<tr><td colspan="9">No completed laps in this session.</td></tr>';
  document.querySelectorAll(".tag-input").forEach(input=>input.addEventListener("change",()=>saveNote(input)));
  document.querySelectorAll(".lap-export-check").forEach(input=>input.addEventListener("change",()=>{if(input.checked)selectedLapIds.add(input.dataset.id);else selectedLapIds.delete(input.dataset.id);updateExportCount();}));
  document.querySelectorAll(".edit-setup").forEach(button=>button.addEventListener("click",()=>openSetupEditor(button.dataset.id)));
  updateExportCount();
  renderProfileEditor();
  renderRaceTimeline();
}

function turnRow(turn={}){
  const directions=["unknown","left","right","mixed","straight"].map(value=>`<option ${value===(turn.direction||"unknown")?"selected":""}>${value}</option>`).join("");
  const statuses=["unverified","approximate","verified"].map(value=>`<option ${value===(turn.verification_status||"unverified")?"selected":""}>${value}</option>`).join("");
  const apex=Number(turn.estimated_apex_m),entry=Number(turn.entry_m),exit=Number(turn.exit_m),hasBounds=Number.isFinite(apex)&&Number.isFinite(entry)&&Number.isFinite(exit);
  const before=hasBounds?Math.max(0,apex-entry):60,after=hasBounds?Math.max(0,exit-apex):60,margin=hasBounds?(before+after)/2:60;
  const apexValue=Number.isFinite(apex)?apex.toFixed(1):"";
  return `<article class="profile-turn" data-before-margin="${before}" data-after-margin="${after}" data-original-margin="${margin}" data-provenance="${esc(turn.provenance||"manual")}"><header><div><span class="turn-kicker">Turn</span><strong class="turn-card-title">${esc(turn.number||"New")}</strong></div><button class="profile-remove" type="button">Remove</button></header><div class="turn-fields"><label>Number<input data-key="number" value="${esc(turn.number??"")}" placeholder="e.g. 7"></label><label class="turn-name-field">Name<input data-key="name" value="${esc(turn.name??"")}" placeholder="Optional name"></label><label>Apex distance<span class="input-with-unit"><input data-key="estimated_apex_m" type="number" min="0" step="0.1" value="${apexValue}" placeholder="0.0"><span>m</span></span></label><label>Margin each side<span class="input-with-unit"><input data-key="margin_m" type="number" min="5" max="1000" step="1" value="${margin.toFixed(1)}"><span>m</span></span></label><label>Direction<select data-key="direction">${directions}</select></label><label class="turn-linked-field">Linked complex<input data-key="linked_group" value="${esc(turn.linked_group??"")}" placeholder="Optional group"></label><label>Status<select data-key="verification_status">${statuses}</select></label></div><small>${esc(turn.provenance||"manual")}</small></article>`;
}
function updateTurnCardVisibility(){const rows=[...document.querySelectorAll(".profile-turn")];rows.forEach((row,rowIndex)=>row.classList.toggle("turn-card-hidden",!showAllTurnCards&&rowIndex!==selectedTurnIndex));$("profile-turns").classList.toggle("showing-selected",!showAllTurnCards);$("profile-show-all-turns").textContent=showAllTurnCards?"Show selected only":"Show all turns";$("turn-list-status").textContent=showAllTurnCards?`${rows.length} turn${rows.length===1?"":"s"} shown.`:selectedTurnIndex===null?`${rows.length} turn${rows.length===1?"":"s"} mapped · select a marker to edit it.`:`Editing turn ${rows[selectedTurnIndex]?.querySelector('[data-key="number"]')?.value||selectedTurnIndex+1}.`;}
function selectTurn(index,scroll=false){const rows=[...document.querySelectorAll(".profile-turn")];selectedTurnIndex=Number.isInteger(index)&&rows[index]?index:null;rows.forEach((row,rowIndex)=>row.classList.toggle("selected",rowIndex===selectedTurnIndex));updateTurnCardVisibility();circuitEditor?.setSelectedTurn(selectedTurnIndex);if(scroll&&selectedTurnIndex!==null)rows[selectedTurnIndex].scrollIntoView({behavior:"smooth",block:"nearest"});}
function bindTurnRows(){document.querySelectorAll(".profile-turn").forEach(row=>{row.onpointerdown=event=>{if(!event.target.closest(".profile-remove"))selectTurn([...document.querySelectorAll(".profile-turn")].indexOf(row));};row.querySelector(".profile-remove").onclick=()=>{const removedIndex=[...document.querySelectorAll(".profile-turn")].indexOf(row);row.remove();if(selectedTurnIndex===removedIndex)selectedTurnIndex=null;else if(selectedTurnIndex>removedIndex)selectedTurnIndex-=1;bindTurnRows();selectTurn(selectedTurnIndex);profileEditorDirty=true;renderCircuitMap();};const number=row.querySelector('[data-key="number"]');number.oninput=()=>{row.querySelector(".turn-card-title").textContent=number.value.trim()||"New";};});selectTurn(selectedTurnIndex);}
function showProfileFields(){const profile=circuitProfiles.find(p=>p.id===$("profile-select").value);selectedTurnIndex=null;circuitTrace=Array.isArray(profile?.centreline)?profile.centreline:[];suggestedTurns=[];$("profile-verification").value=profile?.verification_status||"unverified";$("profile-source").value=profile?.provenance?.source||"";$("profile-turns").innerHTML=(profile?.turns||[]).map(turnRow).join("");bindTurnRows();renderCircuitMap();$("profile-delete").disabled=!profile;$("profile-load-saved").disabled=!profile;$("profile-apply").disabled=!profile;$("profile-message").textContent=profile?`${profile.verification_status}: ${profile.provenance?.notes||profile.provenance?.source||"source not recorded"} · ${circuitTrace.length} saved trace points loaded.`:"This circuit has no turn profile. Full-lap and sector analysis still works; add turns after recording Motion data.";}
function renderProfileEditor(){
  const session=sessions.find(s=>s.id===selectedSessionId),old=$("profile-select").value,oldLap=$("profile-lap").value,sessionChanged=profileEditorSessionId!==selectedSessionId;
  if(sessionChanged){profileEditorDirty=false;pointedTurn=null;selectedTurnIndex=null;circuitTrace=[];suggestedTurns=[];circuitEditor?.setTrace([],[]);}
  $("profile-select").innerHTML=circuitProfiles.map(profile=>`<option value="${esc(profile.id)}">${esc(profile.circuit_name)} · ${esc(profile.layout)} · ${esc(profile.verification_status)}</option>`).join("")||'<option value="">No profile yet</option>';
  const wantedProfile=sessionChanged?(session?.circuit_profile_id||old):(old||session?.circuit_profile_id);
  $("profile-select").value=circuitProfiles.some(p=>p.id===wantedProfile)?wantedProfile:(circuitProfiles[0]?.id||"");
  $("profile-lap").innerHTML=laps.map(lap=>`<option value="${esc(lap.id)}">Lap ${lap.number} · ${fmtTime(lap.time_ms)}${lap.invalid?" · INVALID":""}</option>`).join("")||'<option value="">No laps</option>';
  if(laps.some(lap=>lap.id===oldLap))$("profile-lap").value=oldLap;
  if(sessionChanged||(!profileEditorDirty&&selectedTurnIndex===null))showProfileFields();
  profileEditorSessionId=selectedSessionId;
  setPointingStatus();
}
function tracePointAtDistance(distance){return circuitTrace.length?circuitTrace.reduce((best,point)=>Math.abs(point.distance_m-distance)<Math.abs(best.distance_m-distance)?point:best,circuitTrace[0]):null;}
function turnBoundariesFromRow(row){const apexRaw=row.querySelector('[data-key="estimated_apex_m"]')?.value.trim(),marginRaw=row.querySelector('[data-key="margin_m"]')?.value.trim();if(!apexRaw||!marginRaw)return null;const apex=Number(apexRaw),margin=Number(marginRaw),original=Number(row.dataset.originalMargin);if(!Number.isFinite(apex)||!Number.isFinite(margin)||margin<0)return null;const marginChanged=!Number.isFinite(original)||Math.abs(margin-original)>.001,before=marginChanged?margin:Number(row.dataset.beforeMargin),after=marginChanged?margin:Number(row.dataset.afterMargin),length=Number(sessions.find(session=>session.id===selectedSessionId)?.track_length_m)||circuitTrace.reduce((maximum,point)=>Math.max(maximum,Number(point.distance_m)||0),apex);return{apex_m:apex,entry_m:Math.max(0,apex-(Number.isFinite(before)?before:margin)),exit_m:Math.min(length,apex+(Number.isFinite(after)?after:margin))};}
function turnMarkersFromRows(){return [...document.querySelectorAll(".profile-turn")].flatMap((row,rowIndex)=>{const boundaries=turnBoundariesFromRow(row),number=row.querySelector('[data-key="number"]')?.value||"?";return boundaries?[{rowIndex,number,...boundaries}]:[];});}
function ensureCircuitEditor(){if(circuitEditor||!window.CircuitProfilerCanvas)return;circuitEditor=new window.CircuitProfilerCanvas($("circuit-map"),{onApexSelected:addTurnAtApex,onTurnSelected:rowIndex=>selectTurn(rowIndex,true),onApexMoved:(rowIndex,values)=>{const row=document.querySelectorAll(".profile-turn")[rowIndex];if(!row)return;row.querySelector('[data-key="estimated_apex_m"]').value=Number(values.apex_m).toFixed(1);const boundaries=turnBoundariesFromRow(row),entry=boundaries&&tracePointAtDistance(boundaries.entry_m),apex=boundaries&&tracePointAtDistance(boundaries.apex_m),exit=boundaries&&tracePointAtDistance(boundaries.exit_m);if(entry&&apex&&exit)row.querySelector('[data-key="direction"]').value=traceDirection(entry,apex,exit);profileEditorDirty=true;selectTurn(rowIndex);renderCircuitMap();$("point-turn-status").textContent=`Turn apex moved to ${Number(values.apex_m).toFixed(1)} m.`;}});}
function renderCircuitMap(){ensureCircuitEditor();if(!circuitEditor)return;circuitEditor.setTrace(circuitTrace,suggestedTurns);circuitEditor.setTurns(turnMarkersFromRows());circuitEditor.setSelectedTurn(selectedTurnIndex);circuitEditor.setDisplayMode(turnDisplayMode);circuitEditor.setArmed(pointedTurn);}
function setPointingStatus(){$("profile-point-turn").disabled=!circuitTrace.length||Boolean(pointedTurn);$("profile-cancel-point").disabled=!pointedTurn;$("point-turn-status").textContent=pointedTurn?`Turn ${pointedTurn.number}: click its apex/midpoint on the trace. Margin: ${pointedTurn.margin} m each side.`:circuitTrace.length?"Enter a turn number and margin, then point its apex/midpoint on the map.":"Load a clean distance map first.";renderCircuitMap();}
function startPointingTurn(){const number=$("point-turn-number").value.trim(),margin=Number($("point-turn-margin").value);if(!circuitTrace.length){$("point-turn-status").textContent="Load an eligible clean lap trace first.";return;}if(!number){$("point-turn-status").textContent="Enter the turn number first.";$("point-turn-number").focus();return;}if(!Number.isFinite(margin)||margin<10||margin>1000){$("point-turn-status").textContent="Choose a margin from 10 to 1,000 metres.";$("point-turn-margin").focus();return;}pointedTurn={number,name:$("point-turn-name").value.trim(),margin};setPointingStatus();}
function cancelPointingTurn(){pointedTurn=null;setPointingStatus();}
function traceDirection(entry,apex,exit){const first=Math.atan2(apex.z-entry.z,apex.x-entry.x),second=Math.atan2(exit.z-apex.z,exit.x-apex.x),change=Math.atan2(Math.sin(second-first),Math.cos(second-first));return Math.abs(change)<0.04?"straight":change>0?"right":"left";}
function addTurnAtApex(apexDistance){if(!pointedTurn)return;const apex=tracePointAtDistance(apexDistance);if(!apex)return;const length=Number(sessions.find(session=>session.id===selectedSessionId)?.track_length_m)||circuitTrace.at(-1).distance_m,entryDistance=Math.max(0,apex.distance_m-pointedTurn.margin),exitDistance=Math.min(length,apex.distance_m+pointedTurn.margin),entry=tracePointAtDistance(entryDistance),exit=tracePointAtDistance(exitDistance),margin=pointedTurn.margin;$("profile-turns").insertAdjacentHTML("beforeend",turnRow({number:pointedTurn.number,name:pointedTurn.name,entry_m:entryDistance.toFixed(1),estimated_apex_m:apex.distance_m.toFixed(1),exit_m:exitDistance.toFixed(1),direction:traceDirection(entry,apex,exit),provenance:`manually pointed apex on clean Motion X/Z lap trace; ±${margin} m margin`,verification_status:"unverified"}));selectedTurnIndex=document.querySelectorAll(".profile-turn").length-1;bindTurnRows();profileEditorDirty=true;const numeric=Number(pointedTurn.number);if(Number.isInteger(numeric))$("point-turn-number").value=String(numeric+1);$("point-turn-name").value="";pointedTurn=null;setPointingStatus();$("point-turn-status").textContent=`Turn added at ${apex.distance_m.toFixed(1)} m with a ${margin} m margin each side. Drag its yellow marker to refine it.`;}
async function loadCircuitTrace(){const lapId=$("profile-lap").value;if(!lapId)return;try{const trace=await request(`/api/laps/${encodeURIComponent(lapId)}/trace`),points=trace.points;circuitTrace=trace.eligible_for_profile_calibration?points:[];suggestedTurns=trace.eligible_for_profile_calibration?trace.turn_candidates:[];pointedTurn=null;$("profile-add-suggestions").disabled=!suggestedTurns.length;if(!points.length){$("profile-message").textContent=trace.exclusion_reasons.join("; ")||"No Motion X/Z samples in this lap.";circuitTrace=[];setPointingStatus();return;}if(trace.eligible_for_profile_calibration)profileEditorDirty=true;setPointingStatus();$("profile-message").textContent=`${trace.notice} ${trace.eligible_for_profile_calibration?"Eligible clean trace; saving will attach this centreline.":"Not eligible: "+trace.exclusion_reasons.join("; ")} ${trace.turn_candidates.length} unverified curvature suggestions found.`;}catch(error){circuitTrace=[];suggestedTurns=[];pointedTurn=null;$("profile-add-suggestions").disabled=true;setPointingStatus();$("profile-message").textContent=error.message;}}
function collectTurns(){return [...document.querySelectorAll(".profile-turn")].map(row=>{const value=key=>row.querySelector(`[data-key="${key}"]`).value,boundaries=turnBoundariesFromRow(row)||{};return{number:value("number"),name:value("name"),entry_m:boundaries.entry_m,estimated_apex_m:boundaries.apex_m,exit_m:boundaries.exit_m,direction:value("direction"),linked_group:value("linked_group")||null,provenance:$("profile-source").value||row.dataset.provenance||"manual dashboard edit",verification_status:value("verification_status")};});}
async function saveCircuitProfile(){const session=sessions.find(s=>s.id===selectedSessionId),base=circuitProfiles.find(p=>p.id===$("profile-select").value)||{};if(!session)return;try{const saved=await post("/api/circuit-profiles",{...base,packet_format:2025,track_id:session.track_id,layout:base.layout||(session.track_id>=39?"reverse":"normal"),circuit_name:base.circuit_name||session.track_name,measured_game_length_m:base.measured_game_length_m||session.track_length_m,centreline:circuitTrace.length?circuitTrace:base.centreline||[],sector_boundaries_m:[session.sector2_start_m,session.sector3_start_m].filter(value=>value!=null),turns:collectTurns(),provenance:{source:$("profile-source").value||"manual dashboard edit",notes:"Turn boundaries edited against a recorded game-coordinate trace."},verification_status:$("profile-verification").value});await post(`/api/sessions/${encodeURIComponent(session.id)}/circuit-profile`,{profile_id:saved.id});profileEditorDirty=false;$("profile-message").textContent="Profile saved atomically and selected for this session.";await loadLibrary();}catch(error){$("profile-message").textContent=error.message;}}
function loadSavedProfile(){if(profileEditorDirty&&!window.confirm("Discard the unsaved circuit profile edits and reload the saved profile?"))return;profileEditorDirty=false;showProfileFields();$("profile-message").textContent="Saved centreline and turn data loaded. You can edit and save this profile again.";}
async function deleteCircuitProfile(){const profile=circuitProfiles.find(item=>item.id===$("profile-select").value);if(!profile)return;if(!window.confirm(`Delete circuit profile “${profile.circuit_name}” (${profile.id})?\n\nIt will be moved to recoverable local trash and removed from sessions that currently use it.`))return;try{const result=await post(`/api/circuit-profiles/${encodeURIComponent(profile.id)}/delete`,{confirm:true});profileEditorDirty=false;selectedTurnIndex=null;circuitTrace=[];suggestedTurns=[];await loadLibrary();$("profile-message").textContent=`Profile moved to ${result.moved_to}. Cleared from ${result.cleared_session_ids.length} session(s).`;}catch(error){$("profile-message").textContent=error.message;}}
async function saveNote(input){input.disabled=true;try{const saved=await post(`/api/laps/${encodeURIComponent(input.dataset.id)}/note`,{note:input.value});for(const lap of allLaps)if(lap.id===saved.id)lap.note=saved.note;populateSelectors();}catch(error){alert(error.message);}finally{input.disabled=false;}}

async function openSetupEditor(lapId){
  const detail=await request(`/api/laps/${encodeURIComponent(lapId)}`);editingLap=detail;
  $("setup-title").textContent=`Edit historical setup · Lap ${detail.number} · ${fmtTime(detail.time_ms)}`;
  $("setup-fields").innerHTML=setupFields.map(field=>{const value=detail.setup?.[field.key],source=detail.setup?(detail.setup?._provenance?.[field.key]||(value!==undefined?"legacy_decoded_udp":null)):null;return `<div class="setup-field"><label for="setup-${esc(field.key)}">${esc(field.label)}${field.unit?` (${esc(field.unit)})`:""}</label><input id="setup-${esc(field.key)}" data-key="${esc(field.key)}" data-original="${value??""}" type="number" step="${field.type==="int"?"1":"any"}" min="${field.min}" max="${field.max}" value="${value??""}" placeholder="unknown"><small>${source==="manual"?"manually supplied":source?"decoded from UDP":"unknown"}</small></div>`;}).join("");
  $("setup-message").textContent="Only changed or newly entered fields will be marked manual.";$("setup-dialog").showModal();
}

function exportScope(){return document.querySelector('input[name="export-scope"]:checked').value;}
function updateExportCount(){const scope=exportScope(),count=scope==="session"?laps.length:selectedLapIds.size,session=sessions.find(s=>s.id===selectedSessionId),hasContinuous=Boolean(session?.continuous_store?.packet_count);$("export-count").textContent=`${count} completed lap${count===1?"":"s"}${scope==="session"&&hasContinuous?" + continuous session data":""} will be exported`;const unavailable=count===0&&!(scope==="session"&&hasContinuous);$("download-report").disabled=unavailable;$("download-zip").disabled=unavailable;}
function downloadExport(format){const scope=exportScope();if(scope==="selected"&&!selectedLapIds.size){$("export-error").textContent="Select at least one lap.";return;}const session=sessions.find(item=>item.id===selectedSessionId),report=$("report-type").value;if(report==="race"&&session?.mode!=="race"){$("export-error").textContent="Race reports require a race session.";return;}if(report==="time_trial"&&session?.mode!=="time_trial"){$("export-error").textContent="Time Trial reports require a Time Trial session.";return;}$("export-error").textContent="";const query=new URLSearchParams({scope,format,report});if(scope==="selected")for(const id of selectedLapIds)query.append("lap",id);const link=document.createElement("a");link.href=`/api/sessions/${encodeURIComponent(selectedSessionId)}/export?${query}`;link.click();}

function renderPersonalBests(){
  $("pb-cards").innerHTML=personalBests.map(pb=>`<article class="pb-card"><span class="hint">${esc(pb.track_name)} · ${esc(pb.mode.replace("_"," "))}</span><strong>${fmtTime(pb.time_ms)}</strong><div class="hint">${esc(pb.source_session_name)} · ${new Date(pb.became_pb_at).toLocaleString()}<br>Setup ${esc(fmtSetup(pb.setup))}</div><button class="pb-open" data-key="${esc(pb.key)}">Open details</button><button class="pb-use" data-side="baseline" data-key="${esc(pb.key)}">Baseline</button><button class="pb-use" data-side="candidate" data-key="${esc(pb.key)}">Candidate</button></article>`).join("")||'<p class="empty">No eligible personal bests stored yet. Complete a valid full lap or rebuild from saved sessions.</p>';
  document.querySelectorAll(".pb-open").forEach(button=>button.addEventListener("click",()=>openPersonalBest(button.dataset.key)));
  document.querySelectorAll(".pb-use").forEach(button=>button.addEventListener("click",()=>{const side=button.dataset.side;$(side+"-session").value=`pb:${button.dataset.key}`;populateLapSelector(side);$("pb-message").textContent=`Personal best selected as ${side}.`;}));
}
async function openPersonalBest(key){try{const pb=await request(`/api/personal-bests/${encodeURIComponent(key)}`),details={...pb,lap:{...pb.lap,samples:`${pb.lap.samples.length} telemetry samples (available through this API)`}};$("pb-details").textContent=JSON.stringify(details,null,2);$("pb-details").hidden=false;}catch(error){$("pb-message").textContent=error.message;}}

function setupDifferences(a,b){if(!a&&!b)return[];return setupFields.filter(f=>(a||{})[f.key]!==undefined||(b||{})[f.key]!==undefined).filter(f=>(a||{})[f.key]!== (b||{})[f.key]).map(f=>({label:f.label,from:(a||{})[f.key]??"unknown",to:(b||{})[f.key]??"unknown",unit:f.unit||""}));}
function renderComparison(data){
  comparison=data;$("comparison").hidden=false;$("analysis-empty").hidden=true;const b=data.baseline,c=data.candidate,delta=data.summary.final_delta_s;
  $("download-comparison-report").disabled=false;
  const summary=[["Baseline",`${fmtTime(b.time_ms)} · ${sampleRate(b)}`],["Candidate",`${fmtTime(c.time_ms)} · ${sampleRate(c)}`],["Final delta",`${delta>=0?"+":""}${delta.toFixed(3)} s`],["Max gain / loss",`${data.summary.maximum_gain_s.toFixed(3)} / ${data.summary.maximum_loss_s.toFixed(3)} s`],["Baseline sectors",`${fmtTime(b.sector1_ms)} / ${fmtTime(b.sector2_ms)}`],["Candidate sectors",`${fmtTime(c.sector1_ms)} / ${fmtTime(c.sector2_ms)}`],["Modes",`${b.mode.replaceAll("_"," ")} → ${c.mode.replaceAll("_"," ")}`],["Validity",`${b.invalid?"Baseline INVALID":"Baseline valid"} · ${c.invalid?"Candidate INVALID":"Candidate valid"}`]],setupChanges=setupDifferences(b.setup,c.setup),setupTitle=!b.setup&&!c.setup?"Setup unavailable":setupChanges.length?`${setupChanges.length} changed setting${setupChanges.length===1?"":"s"}`:"No known setup changes";
  $("comparison-summary").innerHTML=summary.map(([label,value])=>`<article class="summary-card"><span>${label}</span><strong>${esc(value)}</strong></article>`).join("")+`<details class="setup-comparison"><summary><span><small>Setup comparison</small><strong>${esc(setupTitle)}</strong></span>${setupChanges.length?'<em>Show changes</em>':""}</summary>${setupChanges.length?`<div class="setup-change-grid">${setupChanges.map(change=>`<div><span>${esc(change.label)}</span><strong>${esc(change.from)} <b>→</b> ${esc(change.to)}${change.unit?` <small>${esc(change.unit)}</small>`:""}</strong></div>`).join("")}</div>`:""}</details>`;
  const warnings=data.comparison_warnings||[];$("comparison-warnings").hidden=!warnings.length;$("comparison-warnings").textContent=warnings.map(warning=>`Warning: ${warning}`).join("\n");
  $("compare-warning").textContent="";
  const legacy=data.alignment.baseline==="legacy-time"||data.alignment.candidate==="legacy-time",endpoint=data.endpoint_estimation;
  const contextFlags=[...(data.race_context?.baseline_flags||[]),...(data.race_context?.candidate_flags||[])];
  $("alignment-notice").textContent=(legacy?"Legacy approximation: time-normalized traces only; corner and metre analysis is disabled. ":"Distance-aligned from recorded lap distance. ")+`Finish-line estimate: ${endpoint.method} Corrections ${endpoint.baseline_adjustment_ms} / ${endpoint.candidate_adjustment_ms} ms. ${data.race_context?.notice||""}${contextFlags.length?` Flags: ${[...new Set(contextFlags)].join(", ")}.`:""}`;
  $("calibration").textContent=data.engineer_notes.calibration;
  $("engineer-notes").innerHTML=data.engineer_notes.windows.length?data.engineer_notes.windows.map(note=>`<article class="note"><h3>${esc(note.name)}</h3><div class="window">${note.start_pct.toFixed(0)}–${note.end_pct.toFixed(0)}% lap distance · ${esc(note.analysis_confidence||"exploratory")}</div><ul>${note.statements.map(text=>`<li>${esc(text)}</li>`).join("")}</ul></article>`).join(""):'<p class="empty">No track-specific engineer notes for this comparison.</p>';
  drawCharts(data);
}
function pathFor(values,x,y){return values.map((value,i)=>`${i?"L":"M"}${x(i)} ${y(value)}`).join(" ");}
function chart(target,series,yMin,yMax,unit,decimals=0){
  const width=1000,height=280,left=58,right=16,top=16,bottom=38,plotW=width-left-right,plotH=height-top-bottom,count=series[0].values.length,x=i=>left+i/(count-1)*plotW,y=value=>top+(yMax-value)/Math.max(.001,yMax-yMin)*plotH,ticks=[0,.25,.5,.75,1];let svg=`<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"><g>`;
  ticks.forEach(t=>{const yy=top+t*plotH,val=yMax-t*(yMax-yMin);svg+=`<path class="axis" d="M${left} ${yy}H${width-right}"/><text class="axis-label" x="${left-8}" y="${yy+4}" text-anchor="end">${val.toFixed(decimals)}${unit}</text>`;});ticks.forEach(t=>{const xx=left+t*plotW;svg+=`<path class="axis" d="M${xx} ${top}V${height-bottom}"/><text class="axis-label" x="${xx}" y="${height-12}" text-anchor="middle">${t*100}%</text>`;});series.forEach(item=>svg+=`<path class="trace" stroke="${item.color}" d="${pathFor(item.values,x,y)}"/>`);svg+=`<line class="cursor" x1="${left}" x2="${left}" y1="${top}" y2="${height-bottom}" visibility="hidden"/><rect class="hit" x="${left}" y="${top}" width="${plotW}" height="${plotH}" fill="transparent"/></g></svg><div class="legend-row">${series.map(item=>`<span class="legend-key" style="--key:${item.color}">${esc(item.label)}</span>`).join("")}</div>`;target.innerHTML=svg;
  const hit=target.querySelector(".hit"),cursor=target.querySelector(".cursor"),tooltip=$("tooltip");hit.addEventListener("mousemove",event=>{const rect=hit.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),index=Math.round(ratio*(count-1)),cx=x(index);cursor.setAttribute("x1",cx);cursor.setAttribute("x2",cx);cursor.setAttribute("visibility","visible");tooltip.hidden=false;tooltip.style.left=`${Math.min(innerWidth-220,event.clientX+12)}px`;tooltip.style.top=`${event.clientY+12}px`;tooltip.innerHTML=`<b>${(index/(count-1)*100).toFixed(1)}% normalized lap</b><br>${series.map(item=>`${esc(item.label)}: ${Number(item.values[index]).toFixed(decimals)}${unit}`).join("<br>")}`;});hit.addEventListener("mouseleave",()=>{cursor.setAttribute("visibility","hidden");tooltip.hidden=true;});
}
function drawPaceMap(data){
  const trace=data.trace||[],map=data.circuit_map||{},trackLength=Number(map.track_length_m)||0;
  const profilePoints=(map.centreline||[]).filter(point=>Number.isFinite(point.x)&&Number.isFinite(point.z)&&Number.isFinite(point.distance_m));
  const recordedPoints=trace.filter(row=>Number.isFinite(row.baseline.world_x)&&Number.isFinite(row.baseline.world_z)).map(row=>({x:row.baseline.world_x,z:row.baseline.world_z,distance_pct:row.distance_pct,distance_m:trackLength?row.distance_pct/100*trackLength:null}));
  const points=(profilePoints.length>=2?profilePoints.map(point=>({...point,distance_pct:trackLength?point.distance_m/trackLength*100:0})):recordedPoints);
  const panel=$("line-overlay-panel"),container=$("pace-map-canvas");
  const distanceAligned=data.alignment?.baseline==="distance"&&data.alignment?.candidate==="distance";
  panel.hidden=!distanceAligned||points.length<2||trace.length<2;
  if(panel.hidden){if(paceMapSource){paceMapCanvas?.setData([],[],[]);paceMapSource=null;}return;}

  const localChange=distancePct=>{const centre=Math.round(Math.max(0,Math.min(100,distancePct))/100*(trace.length-1)),radius=Math.max(4,Math.round(trace.length*.012)),left=Math.max(0,centre-radius),right=Math.min(trace.length-1,centre+radius);return Number(trace[right].delta_s)-Number(trace[left].delta_s);};
  const paceClass=change=>change<-.004?"candidate":change>.004?"baseline":"neutral";
  const runs=[];
  for(let index=1;index<points.length;index++){
    const before=points[index-1],after=points[index],distancePct=(Number(before.distance_pct)+Number(after.distance_pct))/2,change=localChange(distancePct),kind=paceClass(change),previous=runs.at(-1);
    if(previous&&previous.kind===kind)previous.points.push(after);
    else runs.push({kind,points:[before,after]});
  }
  const sections=runs.map(run=>{const middle=run.points[Math.floor(run.points.length/2)],distancePct=Number(middle.distance_pct)||0,change=localChange(distancePct),distance=trackLength?distancePct/100*trackLength:null,label=run.kind==="candidate"?"Candidate gains":run.kind==="baseline"?"Baseline gains":"Near even";return {...run,label,change,distance_label:distance==null?`${distancePct.toFixed(1)}%`:`${distance.toFixed(0)} m`};});
  const turns=(map.turns||[]).flatMap((turn,index)=>{const apex=Number(turn.estimated_apex_m);if(!Number.isFinite(apex)||!trackLength)return[];const note=(data.engineer_notes?.windows||[]).find(item=>String(item.number)===String(turn.number)),rawDelta=note?.evidence?.window_delta_s,windowDelta=Number(rawDelta),hasDelta=rawDelta!==undefined&&rawDelta!==null&&Number.isFinite(windowDelta);return [{number:turn.number??"?",name:turn.name||`Turn ${turn.number??"?"}`,apex_m:apex,window_delta_s:hasDelta?windowDelta:null,kind:hasDelta?paceClass(windowDelta):"neutral",labelAbove:index%2===1}];});
  const profile=map.profile,source=profilePoints.length>=2?"saved profile centreline":"recorded baseline Motion X/Z fallback";
  $("pace-map-description").textContent=`${source}; colours show local delta gain or loss.`;
  $("pace-map-profile").textContent=profile?`${profile.circuit_name||profile.id} · ${profile.verification_status||"unverified"}`:"No circuit profile";
  if(!paceMapCanvas&&window.PaceMapCanvas){const tooltip=$("tooltip");paceMapCanvas=new window.PaceMapCanvas(container,{onHover:(item,event)=>{tooltip.hidden=false;tooltip.style.left=`${Math.min(innerWidth-240,event.clientX+12)}px`;tooltip.style.top=`${event.clientY+12}px`;if(item.type==="turn"){const result=Number.isFinite(item.window_delta_s)?`<br>${Math.abs(item.window_delta_s)<.004?"Near even through profile window":`${item.window_delta_s<0?"Candidate":"Baseline"} gains ${Math.abs(item.window_delta_s).toFixed(3)} s through profile window`}`:"";tooltip.innerHTML=`<b>${esc(item.name)}</b><br>Profile apex: ${item.apex_m.toFixed(0)} m${result}`;}else tooltip.innerHTML=`<b>${esc(item.label)}</b><br>${esc(item.distance_label)} · local delta change ${item.change>=0?"+":""}${item.change.toFixed(3)} s`;},onLeave:()=>{tooltip.hidden=true;}});}
  if(paceMapCanvas&&paceMapSource!==data){paceMapCanvas.setData(points,sections,turns);paceMapSource=data;}
}
function drawCharts(data){
  const trace=data.trace,delta=trace.map(row=>row.delta_s),speedsB=trace.map(row=>row.baseline.speed_kph),speedsC=trace.map(row=>row.candidate.speed_kph),deltaRange=Math.max(.1,...delta.map(Math.abs));
  chart(document.querySelector('[data-chart="delta"]'),[{label:"Candidate − baseline",color:colors.yellow,values:delta}],-deltaRange,deltaRange," s",2);
  chart(document.querySelector('[data-chart="speed"]'),[{label:"Baseline speed",color:colors.green,values:speedsB},{label:"Candidate speed",color:colors.orange,values:speedsC}],0,Math.ceil(Math.max(...speedsB,...speedsC)/50)*50," km/h");
  drawPaceMap(data);
  chart(document.querySelector('[data-chart="pedals"]'),[{label:"Baseline throttle",color:colors.green,values:trace.map(r=>r.baseline.throttle)},{label:"Candidate throttle",color:colors.orange,values:trace.map(r=>r.candidate.throttle)},{label:"Baseline brake",color:colors.blue,values:trace.map(r=>r.baseline.brake)},{label:"Candidate brake",color:colors.red,values:trace.map(r=>r.candidate.brake)}],0,100,"% ");
  const motionValues=trace.flatMap(r=>[r.baseline.front_wheels_angle,r.candidate.front_wheels_angle,r.baseline.chassis_yaw,r.candidate.chassis_yaw]).filter(Number.isFinite),motionPanel=$("motion-ex-panel");motionPanel.hidden=!motionValues.length;
  if(motionValues.length){const range=Math.max(.05,...motionValues.map(Math.abs));chart(document.querySelector('[data-chart="motion-ex"]'),[{label:"Baseline front-wheel angle",color:colors.green,values:trace.map(r=>r.baseline.front_wheels_angle??0)},{label:"Candidate front-wheel angle",color:colors.orange,values:trace.map(r=>r.candidate.front_wheels_angle??0)},{label:"Baseline chassis yaw",color:colors.blue,values:trace.map(r=>r.baseline.chassis_yaw??0)},{label:"Candidate chassis yaw",color:colors.red,values:trace.map(r=>r.candidate.chassis_yaw??0)}],-range,range," rad",3);}
  const slipValues=trace.flatMap(r=>[r.baseline.front_wheel_slip_ratio,r.candidate.front_wheel_slip_ratio,r.baseline.rear_wheel_slip_ratio,r.candidate.rear_wheel_slip_ratio]).filter(Number.isFinite),slipPanel=$("wheel-slip-panel");slipPanel.hidden=!slipValues.length;
  if(slipValues.length){const maximum=Math.max(.05,...slipValues);chart(document.querySelector('[data-chart="wheel-slip"]'),[{label:"Baseline front",color:colors.green,values:trace.map(r=>r.baseline.front_wheel_slip_ratio??0)},{label:"Candidate front",color:colors.orange,values:trace.map(r=>r.candidate.front_wheel_slip_ratio??0)},{label:"Baseline rear",color:colors.blue,values:trace.map(r=>r.baseline.rear_wheel_slip_ratio??0)},{label:"Candidate rear",color:colors.red,values:trace.map(r=>r.candidate.rear_wheel_slip_ratio??0)}],0,maximum,"",3);}
  const angleValues=trace.flatMap(r=>[r.baseline.front_wheel_slip_angle,r.candidate.front_wheel_slip_angle,r.baseline.rear_wheel_slip_angle,r.candidate.rear_wheel_slip_angle]).filter(Number.isFinite),anglePanel=$("slip-angle-panel");anglePanel.hidden=!angleValues.length;
  if(angleValues.length){const maximum=Math.max(.05,...angleValues);chart(document.querySelector('[data-chart="slip-angle"]'),[{label:"Baseline front",color:colors.green,values:trace.map(r=>r.baseline.front_wheel_slip_angle??0)},{label:"Candidate front",color:colors.orange,values:trace.map(r=>r.candidate.front_wheel_slip_angle??0)},{label:"Baseline rear",color:colors.blue,values:trace.map(r=>r.baseline.rear_wheel_slip_angle??0)},{label:"Candidate rear",color:colors.red,values:trace.map(r=>r.candidate.rear_wheel_slip_angle??0)}],0,maximum," rad",3);}
}

function invalidateComparison(){comparison=null;$("download-comparison-report").disabled=true;}
$("compare").addEventListener("click",async()=>{invalidateComparison();$("compare-error").textContent="";try{renderComparison(await request(`/api/compare?baseline=${encodeURIComponent($("baseline").value)}&candidate=${encodeURIComponent($("candidate").value)}`));}catch(error){$("compare-error").textContent=error.message;}});
$("download-comparison-report").addEventListener("click",()=>{if(!comparison)return;const query=new URLSearchParams({baseline:$("baseline").value,candidate:$("candidate").value});const link=document.createElement("a");link.href=`/api/compare/report?${query}`;link.click();});
$("baseline-session").addEventListener("change",()=>{invalidateComparison();populateLapSelector("baseline");});
$("candidate-session").addEventListener("change",()=>{invalidateComparison();populateLapSelector("candidate");});
$("baseline").addEventListener("change",()=>{invalidateComparison();checkCompatibility();});$("candidate").addEventListener("change",()=>{invalidateComparison();checkCompatibility();});
$("session-select").addEventListener("change",event=>selectSession(event.target.value));
function refreshSessionQueryViews(){renderSessionQuery();renderSessionLibrary();populateSelectors();}
$("session-filter-year").addEventListener("change",event=>{sessionFilter={year:event.target.value,month:"",day:""};refreshSessionQueryViews();});
$("session-filter-month").addEventListener("change",event=>{sessionFilter.month=event.target.value;sessionFilter.day="";refreshSessionQueryViews();});
$("session-filter-day").addEventListener("change",event=>{sessionFilter.day=event.target.value;refreshSessionQueryViews();});
$("session-filter-clear").addEventListener("click",()=>{sessionFilter={year:"",month:"",day:""};refreshSessionQueryViews();});
$("pace-map-reset").addEventListener("click",()=>paceMapCanvas?.resetView());
$("pace-map-rotate-left").addEventListener("click",()=>paceMapCanvas?.rotateBy(-10));
$("pace-map-rotate-right").addEventListener("click",()=>paceMapCanvas?.rotateBy(10));
$("timeline-controller-events").addEventListener("change",renderRaceTimeline);
$("timeline-other-car-events").addEventListener("change",renderRaceTimeline);
$("timeline-superseded-events").addEventListener("change",renderRaceTimeline);
$("start-run").addEventListener("click",async()=>{try{await post("/api/runs/start",{name:$("run-name").value});$("run-name").value="";await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("stop-recording").addEventListener("click",async()=>{try{await post("/api/recording/stop");await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("diagnostic-enabled").addEventListener("change",async event=>{event.target.disabled=true;try{diagnosticState=await post("/api/diagnostics",{enabled:event.target.checked});renderRecorder();}catch(error){$("recording-message").textContent=error.message;}finally{event.target.disabled=false;}});
$("jump-active").addEventListener("click",()=>activeRecording&&selectSession(activeRecording.id));
$("rename-button").addEventListener("click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/rename`,{name:$("rename-session").value});await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("delete-session").addEventListener("click",async()=>{const selected=sessions.find(session=>session.id===selectedSessionId);if(!selected||!selected.can_delete)return;if(!window.confirm(`Delete session “${selected.name}” and its ${selected.lap_count} lap file(s)?\n\nIt will be moved to local trash. Stored PB snapshots and notes will be preserved.`))return;try{const result=await post(`/api/sessions/${encodeURIComponent(selected.id)}/delete`,{confirm:true});selectedLapIds.clear();comparison=null;editingLap=null;selectedSessionId=result.selected_session_id;await loadLibrary();$("recording-message").textContent=`Session moved to ${result.moved_to}. PB snapshots and notes were preserved.`;}catch(error){$("recording-message").textContent=error.message;}});
$("settings-form").addEventListener("submit",async event=>{event.preventDefault();const payload={web_host:$("setting-web-host").value,web_port:Number($("setting-web-port").value),udp_port:Number($("setting-udp-port").value),trash_retention_days:Number($("setting-trash-days").value),max_recording_gb:Number($("setting-max-recording").value),diagnostic_max_mb:Number($("setting-diagnostic-mb").value),dashboard_refresh_seconds:Number($("setting-refresh-seconds").value)};try{const result=await post("/api/settings",payload);dashboardSettings=result.settings;const restart=result.restart_required||[];$("settings-message").textContent=`Settings saved.${restart.length?` Restart the server to apply: ${restart.join(", ")}.`:" Applied without restart."}${result.purge?.purged?.length?` Permanently removed ${result.purge.purged.length} expired session(s).`:""}`;await loadLibrary();}catch(error){$("settings-message").textContent=error.message;}});
$("track-override-button").addEventListener("click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/track-name`,{name:$("track-override").value});await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("edit-session-profile").addEventListener("click",()=>document.querySelector('[data-view="circuit-profiler"]').click());
$("profile-select").addEventListener("change",()=>{profileEditorDirty=false;pointedTurn=null;showProfileFields();setPointingStatus();});
$("profile-load-saved").addEventListener("click",loadSavedProfile);
$("profile-delete").addEventListener("click",deleteCircuitProfile);
$("profile-apply").addEventListener("click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/circuit-profile`,{profile_id:$("profile-select").value});$("profile-message").textContent="Profile selected for this session.";await loadLibrary();}catch(error){$("profile-message").textContent=error.message;}});
$("profile-alternate").addEventListener("click",()=>{const session=sessions.find(s=>s.id===selectedSessionId),base=circuitProfiles.find(p=>p.id===$("profile-select").value)||{};if(!session)return;const suffix=Date.now().toString(36),alternate={...base,id:`f1-2025-track-${session.track_id}-${base.layout||(session.track_id>=39?"reverse":"normal")}-alternate-${suffix}`,circuit_name:`${base.circuit_name||session.track_name} alternate`,verification_status:"unverified",provenance:{source:"alternate profile created in dashboard",notes:"Independent candidate calibration; not selected until saved."},turns:(base.turns||[]).map(turn=>({...turn,verification_status:"unverified"}))};circuitProfiles.push(alternate);$("profile-select").insertAdjacentHTML("beforeend",`<option value="${esc(alternate.id)}">${esc(alternate.circuit_name)} · ${esc(alternate.layout)} · unverified</option>`);$("profile-select").value=alternate.id;profileEditorDirty=true;showProfileFields();$("profile-message").textContent="Alternate profile draft created. Edit provenance/turns, then Save profile.";});
$("profile-load-trace").addEventListener("click",loadCircuitTrace);
$("profile-point-turn").addEventListener("click",startPointingTurn);
$("profile-cancel-point").addEventListener("click",cancelPointingTurn);
$("profile-reset-view").addEventListener("click",()=>{ensureCircuitEditor();circuitEditor?.resetView();});
$("profile-rotate-left").addEventListener("click",()=>{ensureCircuitEditor();circuitEditor?.rotateBy(-10);});
$("profile-rotate-right").addEventListener("click",()=>{ensureCircuitEditor();circuitEditor?.rotateBy(10);});
$("profile-show-all-turns").addEventListener("click",()=>{showAllTurnCards=!showAllTurnCards;updateTurnCardVisibility();});
$("profile-turn-display").value=turnDisplayMode;
$("profile-turn-display").addEventListener("change",event=>{turnDisplayMode=event.target.value;try{localStorage.setItem("circuit-profiler-turn-display",turnDisplayMode);}catch(_){}circuitEditor?.setDisplayMode(turnDisplayMode);});
$("profile-add-suggestions").addEventListener("click",()=>{const session=sessions.find(s=>s.id===selectedSessionId),length=session?.track_length_m||Infinity;for(const [index,candidate] of suggestedTurns.entries())$("profile-turns").insertAdjacentHTML("beforeend",turnRow({number:`S${index+1}`,name:"Suggested turn",entry_m:Math.max(0,candidate.distance_m-50),estimated_apex_m:candidate.distance_m,exit_m:Math.min(length,candidate.distance_m+50),direction:candidate.direction,provenance:"curvature suggestion from clean Motion X/Z trace",verification_status:"unverified"}));bindTurnRows();suggestedTurns=[];profileEditorDirty=true;$("profile-add-suggestions").disabled=true;renderCircuitMap();$("profile-message").textContent="Suggestions added as unverified drafts. Check every boundary/name against the trace and game before marking verified.";});
$("profile-add-turn").addEventListener("click",()=>{$("profile-turns").insertAdjacentHTML("beforeend",turnRow());selectedTurnIndex=document.querySelectorAll(".profile-turn").length-1;bindTurnRows();profileEditorDirty=true;renderCircuitMap();});
$("profile-turns").addEventListener("input",event=>{profileEditorDirty=true;const row=event.target.closest(".profile-turn"),key=event.target.dataset.key;if(row&&(key==="estimated_apex_m"||key==="margin_m")){const boundaries=turnBoundariesFromRow(row),entry=boundaries&&tracePointAtDistance(boundaries.entry_m),apex=boundaries&&tracePointAtDistance(boundaries.apex_m),exit=boundaries&&tracePointAtDistance(boundaries.exit_m);if(entry&&apex&&exit)row.querySelector('[data-key="direction"]').value=traceDirection(entry,apex,exit);}renderCircuitMap();});
$("profile-verification").addEventListener("change",()=>{profileEditorDirty=true;});
$("profile-source").addEventListener("input",()=>{profileEditorDirty=true;});
$("profile-save").addEventListener("click",saveCircuitProfile);
$("select-all-laps").addEventListener("click",()=>{selectedLapIds=new Set(laps.map(lap=>lap.id));renderSession();});
$("clear-laps").addEventListener("click",()=>{selectedLapIds.clear();renderSession();});
document.querySelectorAll('input[name="export-scope"]').forEach(input=>input.addEventListener("change",updateExportCount));
$("download-report").addEventListener("click",()=>downloadExport("markdown"));
$("download-zip").addEventListener("click",()=>downloadExport("zip"));
$("rebuild-pbs").addEventListener("click",async()=>{try{const result=await post("/api/personal-bests/rebuild");$("pb-message").textContent=`Rebuild considered ${result.considered} laps; ${result.personal_best_count} PB records are stored.`;await loadLibrary();}catch(error){$("pb-message").textContent=error.message;}});
$("setup-close").addEventListener("click",()=>$("setup-dialog").close());
$("setup-form").addEventListener("submit",async event=>{event.preventDefault();const changed={};document.querySelectorAll("#setup-fields input").forEach(input=>{if(input.value!==input.dataset.original&&input.value!=="")changed[input.dataset.key]=Number(input.value);});if(!Object.keys(changed).length){$("setup-message").textContent="No values changed.";return;}try{const result=await post(`/api/laps/${encodeURIComponent(editingLap.id)}/setup`,{setup:changed});$("setup-message").textContent=`Saved. ${result.pb_message}. Audit: ${result.audit_record}`;setTimeout(()=>$("setup-dialog").close(),900);await loadLibrary();}catch(error){$("setup-message").textContent=error.message;}});

async function loadLibrary(){
  const settingsFallback={settings:{web_host:"127.0.0.1",web_port:8025,udp_port:20777,trash_retention_days:30,max_recording_gb:8,diagnostic_max_mb:50,dashboard_refresh_seconds:8},deleted_sessions:[],receiver_ips:["127.0.0.1"]},[data,pbData,schema,settingsData]=await Promise.all([request("/api/sessions"),request("/api/personal-bests"),request("/api/setup-schema"),request("/api/settings").catch(()=>settingsFallback)]);sessions=data.sessions;personalBests=pbData.personal_bests;setupFields=schema.fields;selectedSessionId=data.selected_session_id;activeRecording=data.recording;diagnosticState=data.diagnostics||{};latestState={...latestState,...data.latest};dashboardSettings=settingsData.settings||{};deletedSessions=settingsData.deleted_sessions||[];receiverIps=settingsData.receiver_ips||[];
  const draftId=profileEditorDirty?$("profile-select").value:"",draft=circuitProfiles.find(profile=>profile.id===draftId),loadedProfiles=(await request(`/api/circuit-profiles?session=${encodeURIComponent(selectedSessionId||"")}`)).profiles;circuitProfiles=draft&&!loadedProfiles.some(profile=>profile.id===draft.id)?[...loadedProfiles,draft]:loadedProfiles;
  const groups=await Promise.all(sessions.map(session=>request(`/api/sessions/${encodeURIComponent(session.id)}/laps`)));allLaps=groups.flatMap(group=>group.laps);laps=groups.find(group=>group.session_id===selectedSessionId)?.laps||[];
  const selected=sessions.find(session=>session.id===selectedSessionId);raceTimeline=[];if(selected?.continuous_store&&!selected.continuous_store.error){try{raceTimeline=(await request(`/api/sessions/${encodeURIComponent(selectedSessionId)}/timeline`)).events;}catch(_){raceTimeline=[];}}
  selectedLapIds=new Set([...selectedLapIds].filter(id=>laps.some(lap=>lap.id===id)));renderSessionQuery();renderSessionLibrary();renderPersonalBests();populateSelectors();renderSettings();scheduleLibraryRefresh();if(!document.querySelector(".tag-input:focus"))renderSession();
}
async function refreshLive(){try{const data=await request("/api/snapshot");activeRecording=data.recording;selectedSessionId=data.selected_session_id;diagnosticState=data.diagnostics||{};renderLive(data.latest);renderRecorder();}catch(_){$("connection").textContent="Dashboard offline";$("connection").classList.remove("online");}}
loadLibrary();refreshLive();setInterval(refreshLive,1000);scheduleLibraryRefresh();
