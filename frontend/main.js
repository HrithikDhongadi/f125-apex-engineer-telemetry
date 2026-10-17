import {createApiClient} from "./api/client.js";
import {esc, fmtSetup as formatSetup, fmtTime} from "./components/format.js";
import {pageData} from "./components/pagination.js";
import {createEventRegistry} from "./events/register.js";
import {comparisonCompatibility, setupDifferences as compareSetups, sourceLaps as lapsForSource} from "./features/analysis/model.js";
import {analysisReady, chronology, deviation, meanNumbers, median, sector3, setupState, tyreCompoundLabel} from "./features/circuit-stats/model.js";
import {traceDirection as directionFromTrace, turnBoundaries} from "./features/circuit-profiler/model.js";
import {recorderPresentation, wheelTelemetry} from "./features/live/model.js";
import {personalBestSources} from "./features/personal-bests/model.js";
import {filteredSessions as filterSessions, sampleRate, sessionChoices as chooseSessions, sessionDateParts, sessionLabel, sessionListKey} from "./features/sessions/model.js";
import {deletedSessionDescription, settingsPayload} from "./features/settings/model.js";
import {renderChips, updatePager as renderPager} from "./rendering/dom.js";
import {createDashboardState} from "./state/store.js";
import {mergeProfilesPreservingDraft, resolveComparisonSources, retainSelectedIds} from "./state/selectors.js";
import {createPollingController} from "./sync/polling.js";
import {CircuitProfilerCanvas, PaceMapCanvas} from "./circuit-profiler.js";

const $ = id => document.getElementById(id);
const {request, post} = createApiClient();
const dashboardState = createDashboardState();
const events = createEventRegistry();
const fmtSetup = setup => formatSetup(setup, setupFields);
const colors = {baseline:"#43c6e8",candidate:"#e10600",baselineAlt:"#45a7ff",candidateAlt:"#ff6b55",delta:"#ffb020"};
let sessions = [], laps = [], allLaps = [], personalBests = [], setupFields = [], circuitProfiles = [], circuitTrace = [], suggestedTurns = [], raceTimeline = [], selectedLapIds = new Set(), selectedSessionId = null, activeRecording = null, latestState = {}, diagnosticState = {}, comparison = null, editingLap = null;
const {sessionsById, lapsById, lapsBySession, timelineBySession, timelineRequests, libraryGeneration, comparisonGeneration} = dashboardState;
const sessionById = id => sessionsById.get(id);
const lapById = id => lapsById.get(id);
let circuitEditor = null, paceMapCanvas = null, paceMapSource = null, pointedTurn = null, selectedTurnIndex = null, profileEditorDirty = false, profileEditorSessionId = null;
let showAllTurnCards = false;
let turnDisplayMode = "minimal";
let sessionFilter = {year:"", month:"", day:""};
let circuitStatsPage = 1, raceStatsPage = 1, sessionPage = 1, sessionLapsPage = 1, pbPage = 1, deletedPage = 1, timelinePage = 1;
const CIRCUIT_STATS_PAGE_SIZE = 10;
const DASHBOARD_PAGE_SIZE = 10;
const TIMELINE_PAGE_SIZE = 25;
let dashboardSettings = {}, deletedSessions = [], receiverIps = [];
let libraryInitialized = false, libraryRequestPending = false, libraryReloadRequested = false, liveRequestPending = false;
let syncedSelectedSessionId = null, sessionFormSessionId = null, renameDraftDirty = false, trackDraftDirty = false, settingsDraftDirty = false, recorderFeedbackUntil = 0;
let polling = null;
try { turnDisplayMode = localStorage.getItem("circuit-profiler-turn-display") || "minimal"; } catch (_) {}
if (!["minimal","dots","ranges"].includes(turnDisplayMode)) turnDisplayMode = "minimal";

function updatePager(previousId,labelId,nextId,page,pages,total,noun="items"){renderPager(document,previousId,labelId,nextId,page,pages,total,noun);}

function activateView(button) {
  document.body.dataset.view=button.dataset.view;
  document.querySelectorAll(".tabs button").forEach(item => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${button.dataset.view}`));
  if (button.dataset.view === "analysis" && comparison) requestAnimationFrame(() => drawCharts(comparison));
  if (button.dataset.view === "circuit-stats") renderCircuitStats();
  if (button.dataset.view === "session") void ensureSelectedTimeline();
  if (button.dataset.view === "circuit-profiler") requestAnimationFrame(renderCircuitMap);
}
events.onAll(document.querySelectorAll(".tabs button"), "click", event => activateView(event.currentTarget));
events.on(document, "keydown", event => {
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
  renderChips(target, values, esc);
}
function renderSettings(){
  if(!dashboardSettings.web_port)return;
  if(!settingsDraftDirty){
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
  const deletedPagination=pageData(deletedSessions,deletedPage);deletedPage=deletedPagination.current;
  $("deleted-session-list").innerHTML=deletedSessions.length?deletedPagination.items.map(item=>{const description=deletedSessionDescription(item);return `<article class="deleted-session"><div><strong>${esc(item.name)}</strong><span>${esc(description.identity)}</span><span>${esc(description.retention)}</span></div><button type="button" data-restore="${esc(item.trash_id)}">Restore</button></article>`;}).join(""):'<p class="empty">No deleted sessions.</p>';
  updatePager("deleted-prev","deleted-pagination-label","deleted-next",deletedPage,deletedPagination.pages,deletedSessions.length,"deleted sessions");
  document.querySelectorAll("[data-restore]").forEach(button=>button.addEventListener("click",()=>restoreDeletedSession(button.dataset.restore)));
}
async function restoreDeletedSession(trashId){
  const item=deletedSessions.find(entry=>entry.trash_id===trashId);if(!item)return;
  if(!window.confirm(`Restore “${item.name}” to the session library?`))return;
  try{const result=await post(`/api/trash/sessions/${encodeURIComponent(trashId)}/restore`,{confirm:true});$("settings-message").textContent=`Restored ${result.restored_session_id} with ${result.lap_count} lap(s).`;await loadLibrary();}catch(error){$("settings-message").textContent=error.message;}
}
function scheduleLibraryRefresh(){
  polling?.setLibraryInterval(Number(dashboardSettings.dashboard_refresh_seconds)||8);
}
function setRecorderFeedback(message){recorderFeedbackUntil=Date.now()+8000;$('recording-message').textContent=message;}
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
  const wheels=wheelTelemetry(latest);
  if(!wheels.some(wheel=>wheel.inner!==undefined||wheel.brake!==undefined)){$("wheels").innerHTML='<p class="empty">Waiting for car telemetry packet…</p>';return;}
  $("wheels").innerHTML=wheels.map(wheel=>`<section class="wheel ${wheel.hottestInner?"hot-inner":""} ${wheel.hottestBrake?"hot-brake":""}"><h3>${wheel.name}</h3><dl><dt>Pressure</dt><dd>${wheel.pressure??"—"} psi</dd><dt>Inner</dt><dd class="${wheel.hottestInner?"hot":""}">${wheel.inner??"—"} °C</dd><dt>Surface</dt><dd>${wheel.surface??"—"} °C</dd><dt>Brake</dt><dd class="${wheel.hottestBrake?"hot":""}">${wheel.brake??"—"} °C</dd></dl></section>`).join("");
}

function renderRecorder() {
  const recording=activeRecording, mode=latestState.game_mode||"unknown", track=latestState.track_name||"Unknown track";
  const presentation=recorderPresentation(recording,latestState);
  const badge=$("recording-badge");
  badge.className=`recording-badge ${recording?.status==="armed"?"armed":recording?"active":""}`;
  badge.textContent=presentation.badge;
  const sessionTypeName=presentation.sessionTypeName;
  $("recording-name").textContent=recording?.name||(mode==="time_trial"?"Time Trial · start a run":["practice","qualifying","race"].includes(mode)?`${sessionTypeName} recorder ready`:"Waiting for game session");
  $("game-context").textContent=`${sessionTypeName} · ${track}${latestState.game_session_uid?` · game UID ${latestState.game_session_uid}`:""}`;
  const statusMessage=presentation.statusMessage;
  if(Date.now()>=recorderFeedbackUntil) $("recording-message").textContent=statusMessage;
  $("run-name").hidden=mode!=="time_trial"||Boolean(recording);
  $("start-run").hidden=mode!=="time_trial"||Boolean(recording);
  $("stop-recording").hidden=!recording;
  $("jump-active").hidden=!recording||recording.id===selectedSessionId;
  document.querySelectorAll("[data-historical-badge]").forEach(badge=>{badge.hidden=Boolean(recording&&recording.id===selectedSessionId);});
  $("diagnostic-enabled").checked=Boolean(diagnosticState.enabled);
  $("diagnostic-status").textContent=diagnosticState.enabled
    ? `Diagnostics on${diagnosticState.active_file?` · ${diagnosticState.active_file} · ${diagnosticState.events} events`:" · the next recording will create a bounded trace"}${diagnosticState.truncated?" · size limit reached":""}.`
    : "Diagnostics are off. Enable before starting the short test run.";
  document.querySelectorAll("[data-historical-badge]").forEach(badge=>{badge.textContent=!recording?"Historical/closed session · live telemetry remains current":"Historical view · live telemetry remains current";});
}
const statsSetupState=lap=>setupState(lap,setupFields);
const statsAnalysisReady=analysisReady;
const statsChronology=lap=>chronology(lap,sessions);
const statsSector3=sector3;
const statsMedian=median;
const statsDeviation=deviation;
function statsTrackRows(){const trackId=Number($("stats-track").value);return allLaps.map(lap=>({...lap,_session:sessionById(lap.recording_session_id)})).filter(lap=>Number(lap.track_id)===trackId);}
function circuitStatsRows(){
  const mode=$("stats-mode").value,validity=$("stats-validity").value,sessionId=$("stats-session").value,year=$("stats-year").value,compound=$("stats-compound").value,setup=$("stats-setup").value,quality=$("stats-quality").value,search=$("stats-search").value.trim().toLowerCase();
  const rows=statsTrackRows().filter(lap=>{
    if(mode!=="all"&&lap.mode!==mode)return false;
    if(validity==="valid"&&lap.invalid||validity==="invalid"&&!lap.invalid)return false;
    if(sessionId!=="all"&&lap.recording_session_id!==sessionId)return false;
    if(year!=="all"&&sessionDateParts(lap._session||{}).year!==year)return false;
    if(compound!=="all"&&String(lap.actual_tyre_compound??"unknown")!==compound)return false;
    if(setup!=="all"&&statsSetupState(lap)!==setup)return false;
    if(quality==="ready"&&!statsAnalysisReady(lap)||quality==="limited"&&statsAnalysisReady(lap))return false;
    return !search||`${lap._session?.name||""} ${lap.note||""} ${lap.number}`.toLowerCase().includes(search);
  });
  const order=$("stats-order").value;
  rows.sort(order==="worst"?(a,b)=>b.time_ms-a.time_ms:order==="newest"?(a,b)=>statsChronology(b)-statsChronology(a):order==="oldest"?(a,b)=>statsChronology(a)-statsChronology(b):(a,b)=>a.time_ms-b.time_ms);
  return rows;
}
function populateCircuitStatsFilters(){
  const oldTrack=$("stats-track").value,trackMap=new Map();
  sessions.forEach(session=>{if(session.track_id!=null&&Number(session.track_id)>=0)trackMap.set(Number(session.track_id),session.track_name);});
  const tracks=[...trackMap].sort((a,b)=>a[1].localeCompare(b[1])||a[0]-b[0]);
  $("stats-track").innerHTML=tracks.map(([id,name])=>`<option value="${id}">${esc(name)} · ID ${id}</option>`).join("")||'<option value="">No recorded circuits</option>';
  const selectedTrack=sessionById(selectedSessionId)?.track_id;
  $("stats-track").value=tracks.some(([id])=>String(id)===oldTrack)?oldTrack:tracks.some(([id])=>id===selectedTrack)?String(selectedTrack):String(tracks[0]?.[0]??"");
  const trackId=Number($("stats-track").value),trackSessions=sessions.filter(session=>Number(session.track_id)===trackId),oldSession=$("stats-session").value;
  $("stats-session").innerHTML='<option value="all">All sessions</option>'+trackSessions.map(session=>`<option value="${esc(session.id)}">${esc(session.name)} · ${new Date(session.started_at).toLocaleDateString()}</option>`).join("");
  $("stats-session").value=trackSessions.some(session=>session.id===oldSession)?oldSession:"all";
  const oldYear=$("stats-year").value,years=[...new Set(trackSessions.map(session=>sessionDateParts(session).year))].sort((a,b)=>Number(b)-Number(a));
  $("stats-year").innerHTML='<option value="all">All years</option>'+years.map(year=>`<option value="${esc(year)}">${esc(year)}</option>`).join("");
  $("stats-year").value=years.includes(oldYear)?oldYear:"all";
  const oldCompound=$("stats-compound").value,compounds=[...new Set(statsTrackRows().map(lap=>lap.actual_tyre_compound??"unknown"))].sort((a,b)=>String(a).localeCompare(String(b)));
  $("stats-compound").innerHTML='<option value="all">All compounds</option>'+compounds.map(value=>`<option value="${esc(value)}">${esc(tyreCompoundLabel(value==="unknown"?null:value))}</option>`).join("");
  $("stats-compound").value=compounds.map(String).includes(oldCompound)?oldCompound:"all";
}
function circuitStatsSummary(rows){
  const valid=rows.filter(lap=>!lap.invalid),times=valid.map(lap=>lap.time_ms),best=times.length?Math.min(...times):null,average=times.length?times.reduce((sum,value)=>sum+value,0)/times.length:null,deviation=statsDeviation(times),median=statsMedian(times);
  const sector1=valid.map(lap=>lap.sector1_ms).filter(value=>value>0),sector2=valid.map(lap=>lap.sector2_ms).filter(value=>value>0),sector3=valid.map(statsSector3).filter(value=>value>0),theoretical=sector1.length&&sector2.length&&sector3.length?Math.min(...sector1)+Math.min(...sector2)+Math.min(...sector3):null;
  const chronological=[...valid].sort((a,b)=>statsChronology(a)-statsChronology(b)),trend=chronological.length>1?chronological[0].time_ms-chronological.at(-1).time_ms:null,sessionCount=new Set(rows.map(lap=>lap.recording_session_id)).size,peak=rows.map(lap=>lap.peak_speed_kph).filter(Number.isFinite);
  return [
    ["Best valid lap",fmtTime(best),median?`Median ${fmtTime(median)}`:"No valid timing"],
    ["Theoretical best",fmtTime(theoretical),theoretical&&best?`${((best-theoretical)/1000).toFixed(3)} s below PB from best sectors`:"Needs complete sectors"],
    ["Average pace",fmtTime(average),`${valid.length} valid lap${valid.length===1?"":"s"}`],
    ["Consistency",deviation==null?"—":`±${(deviation/1000).toFixed(3)} s`,"Standard deviation of valid laps"],
    ["Recorded laps",String(rows.length),`${valid.length} valid · ${rows.length-valid.length} invalid`],
    ["Sessions",String(sessionCount),`${new Set(rows.map(lap=>lap.mode)).size} mode${new Set(rows.map(lap=>lap.mode)).size===1?"":"s"}`],
    ["Latest vs first",trend==null?"—":`${trend>=0?"−":"+"}${Math.abs(trend/1000).toFixed(3)} s`,trend>=0?"Latest valid lap is faster":"Latest valid lap is slower"],
    ["Peak speed",peak.length?`${Math.max(...peak)} km/h`:"—","Highest recorded lap peak"],
  ];
}
function raceStats(){
  const year=$("stats-year").value,sessionId=$("stats-session").value,search=$("stats-search").value.trim().toLowerCase(),groups=new Map();
  for(const lap of statsTrackRows()){
    if(lap.mode!=="race")continue;
    if(sessionId!=="all"&&lap.recording_session_id!==sessionId)continue;
    if(year!=="all"&&sessionDateParts(lap._session||{}).year!==year)continue;
    if(search&&!`${lap._session?.name||""} ${lap.note||""} ${lap.number}`.toLowerCase().includes(search))continue;
    if(!groups.has(lap.recording_session_id))groups.set(lap.recording_session_id,[]);
    groups.get(lap.recording_session_id).push(lap);
  }
  return [...groups.entries()].map(([sessionId,laps])=>{
    laps.sort((a,b)=>statsChronology(a)-statsChronology(b));const valid=laps.filter(lap=>!lap.invalid),times=valid.map(lap=>lap.time_ms),first=laps[0],last=laps.at(-1),classification=first._session?.final_classification||null,completedAtLine=first._session?.status==="completed"&&Number.isFinite(last.race_total_laps)&&last.number>=last.race_total_laps&&Number.isFinite(last.finish_position),resultSource=classification?"official":completedAtLine?"finish_line":null,grid=classification?.grid_position??first.grid_position??first.start_position,finishPosition=classification?.position??(completedAtLine?last.finish_position:null);
    const fuelUsed=Number.isFinite(first.start_fuel_kg)&&Number.isFinite(last.finish_fuel_kg)?Math.max(0,first.start_fuel_kg-last.finish_fuel_kg):null,startWear=meanNumbers(first.start_tyre_wear_pct),finishWear=meanNumbers(last.finish_tyre_wear_pct),wearIncrease=startWear!=null&&finishWear!=null?Math.max(0,finishWear-startWear):null;
    return {session_id:sessionId,session:first._session,laps,valid,best:times.length?Math.min(...times):null,average:times.length?times.reduce((sum,value)=>sum+value,0)/times.length:null,deviation:statsDeviation(times),grid,finish_position:finishPosition,result_source:resultSource,positions_gained:Number.isFinite(grid)&&Number.isFinite(finishPosition)?grid-finishPosition:null,pit_stops:Number.isFinite(classification?.pit_stops)?classification.pit_stops:Math.max(...laps.map(lap=>Number(lap.finish_pit_stops??lap.start_pit_stops)).filter(Number.isFinite),0),penalties_s:Number.isFinite(classification?.penalties_s)?classification.penalties_s:Math.max(...laps.map(lap=>Number(lap.finish_penalties_s??lap.start_penalties_s)).filter(Number.isFinite),0),warnings:Math.max(...laps.map(lap=>Number(lap.finish_warnings??lap.start_warnings)).filter(Number.isFinite),0),fuel_used:fuelUsed,wear_increase:wearIncrease,compounds:[...new Set(laps.map(lap=>lap.actual_tyre_compound).filter(value=>value!=null))]};
  }).sort((a,b)=>Date.parse(b.session?.started_at||0)-Date.parse(a.session?.started_at||0));
}
function renderRaceStats(){
  const races=raceStats(),allRaceLaps=races.flatMap(race=>race.laps),valid=allRaceLaps.filter(lap=>!lap.invalid),times=valid.map(lap=>lap.time_ms),classified=races.filter(race=>Number.isFinite(race.finish_position)),finishes=classified.map(race=>race.finish_position),wins=finishes.filter(position=>position===1).length,officialCount=classified.filter(race=>race.result_source==="official").length,fuel=races.map(race=>race.fuel_used).filter(Number.isFinite),pitStops=races.reduce((sum,race)=>sum+race.pit_stops,0),penalties=races.reduce((sum,race)=>sum+race.penalties_s,0),warnings=races.reduce((sum,race)=>sum+race.warnings,0);
  const racePagination=pageData(races,raceStatsPage);raceStatsPage=racePagination.current;
  const cards=[
    ["Races recorded",races.length,`${finishes.length} classified · ${officialCount} official packet${officialCount===1?"":"s"}`],
    ["Race laps",allRaceLaps.length,allRaceLaps.length?`${(valid.length/allRaceLaps.length*100).toFixed(1)}% valid`:"No captured laps"],
    ["Best race lap",fmtTime(times.length?Math.min(...times):null),times.length?`Average ${fmtTime(times.reduce((sum,value)=>sum+value,0)/times.length)}`:"No valid race laps"],
    ["Average finish",finishes.length?`P${(finishes.reduce((sum,value)=>sum+value,0)/finishes.length).toFixed(1)}`:"—","Verified completed results only"],
    ["Wins",wins,finishes.length?`${(wins/finishes.length*100).toFixed(1)}% of classified races`:"No verified completed results"],
    ["Pit stops",pitStops,String(races.length?`${(pitStops/races.length).toFixed(1)} per race`:"No races")],
    ["Recorded penalties",`${penalties} s`,`${warnings} warning${warnings===1?"":"s"}`],
    ["Fuel consumed",fuel.length?`${fuel.reduce((sum,value)=>sum+value,0).toFixed(1)} kg`:"—","Across races with complete fuel context"],
    ["Race consistency",statsDeviation(times)==null?"—":`±${(statsDeviation(times)/1000).toFixed(3)} s`,"All valid race laps"],
  ];
  $("race-stats-count").textContent=`${races.length} race${races.length===1?"":"s"} · ${finishes.length} classified`;
  $("race-stats-summary").innerHTML=cards.map(([label,value,detail])=>`<article><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></article>`).join("");
  $("race-stats-sessions").innerHTML=racePagination.items.map(race=>{const finish=Number.isFinite(race.finish_position)?`P${race.finish_position}${Number.isFinite(race.grid)?` · grid P${race.grid}${Number.isFinite(race.positions_gained)?` · ${race.positions_gained>=0?"+":""}${race.positions_gained}`:""}`:""} · ${race.result_source==="official"?"official":"finish-line verified"}`:"Unknown — incomplete result evidence",date=race.session?.started_at?new Date(race.session.started_at).toLocaleDateString():"Unknown date";return `<tr><td><strong>${esc(race.session?.name||race.session_id)}</strong><br><span class="hint">${esc(date)}</span></td><td>${race.laps.length}<br><span class="hint">${race.valid.length} valid</span></td><td>${fmtTime(race.best)} / ${fmtTime(race.average)}</td><td>${race.deviation==null?"—":`±${(race.deviation/1000).toFixed(3)} s`}</td><td>${esc(finish)}</td><td>${race.pit_stops}</td><td>${race.penalties_s} s / ${race.warnings}</td><td>${race.fuel_used==null?"—":`${race.fuel_used.toFixed(2)} kg`}</td><td>${race.wear_increase==null?"—":`+${race.wear_increase.toFixed(1)} pp avg`}</td><td>${race.compounds.length?race.compounds.map(tyreCompoundLabel).join(", "):"Unknown"}</td><td><button class="race-stats-open" data-session="${esc(race.session_id)}">View session</button></td></tr>`;}).join("")||'<tr><td colspan="11" class="empty-state">No captured race laps match this circuit, year, session and search.</td></tr>';
  updatePager("race-stats-prev","race-stats-pagination-label","race-stats-next",raceStatsPage,racePagination.pages,races.length,"races");
  document.querySelectorAll(".race-stats-open").forEach(button=>button.addEventListener("click",async()=>{await selectSession(button.dataset.session);document.querySelector('[data-view="session"]').click();}));
}
async function openCircuitStatsLap(lapId,action){
  const lap=lapById(lapId);if(!lap)return;
  await selectSession(lap.recording_session_id);
  if(action==="session"){document.querySelector('[data-view="session"]').click();return;}
  document.querySelector('[data-view="analysis"]').click();
  const side=action==="candidate"?"candidate":"baseline",source=`session:${lap.recording_session_id}`;
  $(side+"-session").value=source;populateLapSelector(side,lap.id);$(side).value=lap.id;checkCompatibility();
}
function renderCircuitStats(){
  populateCircuitStatsFilters();const rows=circuitStatsRows(),summary=circuitStatsSummary(rows),pages=Math.max(1,Math.ceil(rows.length/CIRCUIT_STATS_PAGE_SIZE));circuitStatsPage=Math.min(Math.max(1,circuitStatsPage),pages);
  $("circuit-stats-count").textContent=`${rows.length} matching lap${rows.length===1?"":"s"}`;
  $("circuit-stats-summary").innerHTML=summary.map(([label,value,detail])=>`<article><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></article>`).join("");
  const start=(circuitStatsPage-1)*CIRCUIT_STATS_PAGE_SIZE,best=rows.length?Math.min(...rows.map(lap=>lap.time_ms)):null,pageRows=rows.slice(start,start+CIRCUIT_STATS_PAGE_SIZE);
  $("circuit-stats-laps").innerHTML=pageRows.map((lap,index)=>{const s3=statsSector3(lap),date=lap._session?.started_at?new Date(lap._session.started_at).toLocaleDateString():"Unknown date",gap=best==null?"":lap.time_ms===best?"Best":`+${((lap.time_ms-best)/1000).toFixed(3)} s`,coverage=lap.quality?.coverage_pct==null?"legacy":`${lap.quality.coverage_pct}%`,setupState=statsSetupState(lap);return `<tr class="${lap.invalid?"invalid-row":""}"><td><strong>${start+index+1}</strong></td><td class="time">${fmtTime(lap.time_ms)}<br><span class="hint">${gap}</span></td><td>${fmtTime(lap.sector1_ms)} / ${fmtTime(lap.sector2_ms)} / ${fmtTime(s3)}</td><td>${esc(lap._session?.name||lap.recording_session_id)}<br><span class="hint">${esc(date)} · lap ${lap.number}</span></td><td>${esc(lap.mode.replaceAll("_"," "))}<br><span class="${lap.invalid?"stats-invalid":"stats-valid"}">${lap.invalid?"INVALID":"valid"}</span></td><td>${esc(tyreCompoundLabel(lap.actual_tyre_compound))}<br><span class="hint">${lap.tyre_age_laps==null?"age unknown":`age ${lap.tyre_age_laps} lap${lap.tyre_age_laps===1?"":"s"}`}</span></td><td><span class="stats-setup-state">${setupState}</span><br><span class="hint">${esc(fmtSetup(lap.setup))}</span></td><td>${coverage} coverage<br><span class="hint">${lap.sample_count} samples · ${lap.peak_speed_kph??"—"} km/h</span></td><td><div class="stats-row-actions"><button data-stats-open="session" data-id="${esc(lap.id)}">Session</button><button data-stats-open="baseline" data-id="${esc(lap.id)}">Baseline</button><button data-stats-open="candidate" data-id="${esc(lap.id)}">Candidate</button></div></td></tr>`;}).join("")||'<tr><td colspan="9" class="empty-state">No laps match these filters.</td></tr>';
  $("stats-page-status").textContent=rows.length?`Showing ${start+1}–${Math.min(start+CIRCUIT_STATS_PAGE_SIZE,rows.length)} of ${rows.length}`:"No matching laps";$("stats-pagination-label").textContent=`Page ${circuitStatsPage} of ${pages}`;$("stats-prev").disabled=circuitStatsPage<=1;$("stats-next").disabled=circuitStatsPage>=pages;
  document.querySelectorAll("[data-stats-open]").forEach(button=>button.addEventListener("click",()=>openCircuitStatsLap(button.dataset.id,button.dataset.statsOpen)));
  renderRaceStats();
}
const filteredSessions=()=>filterSessions(sessions,sessionFilter);
const sessionChoices=()=>chooseSessions(sessions,selectedSessionId,sessionFilter);
function renderSessionQuery(){
  const years=[...new Set(sessions.map(session=>sessionDateParts(session).year))].sort((a,b)=>a==="legacy"?1:b==="legacy"?-1:Number(b)-Number(a));
  if(sessionFilter.year&&!years.includes(sessionFilter.year))sessionFilter={year:"",month:"",day:""};
  const matchingYear=sessions.filter(session=>sessionDateParts(session).year===sessionFilter.year);
  const months=[...new Set(matchingYear.map(session=>sessionDateParts(session).month).filter(Boolean))].sort();
  if(sessionFilter.month&&!months.includes(sessionFilter.month)){sessionFilter.month="";sessionFilter.day="";}
  const matchingMonth=matchingYear.filter(session=>sessionDateParts(session).month===sessionFilter.month);
  const days=[...new Set(matchingMonth.map(session=>sessionDateParts(session).day).filter(Boolean))].sort((a,b)=>Number(a)-Number(b));
  if(sessionFilter.day&&!days.includes(sessionFilter.day))sessionFilter.day="";
  const count=filteredSessions().length;
  document.querySelectorAll("[data-session-filter]").forEach(query=>{
    const year=query.querySelector('[data-session-filter-field="year"]'),month=query.querySelector('[data-session-filter-field="month"]'),day=query.querySelector('[data-session-filter-field="day"]');
    year.innerHTML='<option value="">Choose year</option>'+years.map(value=>`<option value="${esc(value)}">${value==="legacy"?"Legacy / no date":esc(value)}</option>`).join("");
    year.value=sessionFilter.year;
    month.innerHTML='<option value="">All months</option>'+months.map(value=>`<option value="${value}">${new Intl.DateTimeFormat(undefined,{month:"long"}).format(new Date(2000,Number(value)-1,1))}</option>`).join("");
    month.value=sessionFilter.month;month.disabled=!sessionFilter.year||sessionFilter.year==="legacy";
    day.innerHTML='<option value="">All dates</option>'+days.map(value=>`<option value="${value}">${Number(value)}</option>`).join("");
    day.value=sessionFilter.day;day.disabled=!sessionFilter.month;
    query.querySelector("[data-session-filter-count]").textContent=sessionFilter.year?`${count} session${count===1?"":"s"} found`:"No archive sessions shown";
  });
}
function renderSessionLibrary() {
  const choices=sessionChoices(),matches=filteredSessions();
  const sessionPagination=pageData(matches,sessionPage);sessionPage=sessionPagination.current;
  const choiceHtml=choices.map(session=>`<option value="${esc(session.id)}">${esc(`${session.id===selectedSessionId?"Displayed · ":""}${sessionLabel(session)}`)}</option>`).join("")||'<option value="">Choose a date query</option>';
  document.querySelectorAll("[data-displayed-session-select]").forEach(select=>{select.innerHTML=choiceHtml;select.value=choices.some(session=>session.id===selectedSessionId)?selectedSessionId:"";});
  $("session-cards").innerHTML=sessionPagination.items.map(session=>{
    const date=session.started_at?new Date(session.started_at).toLocaleString():"Original v0.1/v0.2 files";
    const stream=session.continuous_store,streamText=stream?.details_deferred?"<br>Continuous race data available · detailed continuity is calculated for reports":stream?`<br>Continuous: ${stream.packet_count??"?"} packets · ${stream.event_count??"?"} events · ${stream.missing_frame_estimate??"?"} missing in worst 60 Hz stream (${stream.missing_frame_percent??"?"}%) · ${stream.dropped_packets??0} dropped by storage limit`:"";
    return `<article class="session-card ${session.id===selectedSessionId?"selected":""}" data-session="${esc(session.id)}"><h3>${esc(session.name)}</h3><div class="session-meta">${esc(session.mode.replace("_"," "))} · ${esc(session.track_name)} · ${esc(session.status)}<br>${esc(date)} · ${session.lap_count} laps · best ${fmtTime(session.best_valid_lap_ms)}${streamText}</div></article>`;
  }).join("")||(sessionFilter.year?'<p class="empty-state">No sessions match this date query.</p>':'<p class="empty-state">Choose a year above to show saved sessions.</p>');
  updatePager("session-prev","session-pagination-label","session-next",sessionPage,sessionPagination.pages,matches.length,"sessions");
  document.querySelectorAll(".session-card").forEach(card=>card.addEventListener("click",()=>selectSession(card.dataset.session)));
  const selected=sessionById(selectedSessionId);
  if(sessionFormSessionId!==selectedSessionId){sessionFormSessionId=selectedSessionId;renameDraftDirty=false;trackDraftDirty=false;}
  if(!renameDraftDirty) $("rename-session").value=selected?.read_only?"":selected?.name||"";
  $("rename-session").disabled=Boolean(!selected||selected.read_only);
  $("rename-button").disabled=Boolean(!selected||selected.read_only);
  $("delete-session").disabled=Boolean(!selected||!selected.can_delete);
  $("delete-session").title=!selected?"Select a session":selected.read_only?"Legacy captures cannot be deleted":!selected.can_delete?"Stop the active recording before deleting it":"Move this session and its lap files to recoverable local trash";
  $("track-override-row").hidden=Boolean(!selected||!selected.can_override_track_name||selected.read_only);
  if(!trackDraftDirty) $("track-override").value=selected?.track_name_override||"";
  const selectedProfile=circuitProfiles.find(profile=>profile.id===selected?.circuit_profile_id);
  $("session-profile-name").textContent=selectedProfile?.circuit_name||selected?.circuit_profile_id||"None selected";
  $("session-profile-status").textContent=selectedProfile?`${selectedProfile.verification_status} · ${selectedProfile.turns?.length||0} turns · ${selectedProfile.id}`:selected?.circuit_profile_id?"Assigned profile is unavailable; choose another profile.":"Open Circuit Profiler to create or select one.";
  renderRecorder();
}
async function selectSession(id){if(!id||id===selectedSessionId)return;libraryGeneration.invalidate();try{await post("/api/sessions/select",{session_id:id});selectedSessionId=id;selectedLapIds.clear();sessionLapsPage=1;timelinePage=1;profileEditorDirty=false;pointedTurn=null;selectedTurnIndex=null;circuitTrace=[];suggestedTurns=[];circuitEditor?.setTrace([],[]);comparison=null;$("comparison").hidden=true;$("analysis-empty").hidden=false;await loadLibrary({selection:true});}catch(error){setRecorderFeedback(error.message);}}

function selectorSources(){return [...sessionChoices().map(s=>({value:`session:${s.id}`,label:sessionLabel(s)})),...personalBestSources(personalBests,fmtTime)];}
const sourceLaps=value=>lapsForSource(value,allLaps,personalBests);
function lapOption(lap){return `<option value="${esc(lap.id)}">Lap ${lap.number} · ${fmtTime(lap.time_ms)} · ${lap.invalid?"INVALID":"valid"}</option>`;}
function populateLapSelector(side,preferred){const source=$(side+"-session").value,items=sourceLaps(source),select=$(side),old=preferred||select.value;select.innerHTML=items.map(lapOption).join("")||'<option value="">No laps</option>';if(items.some(l=>l.id===old))select.value=old;checkCompatibility();}
function populateSelectors(){
  const sources=selectorSources(),state={bs:$("baseline-session").value,b:$("baseline").value,cs:$("candidate-session").value,c:$("candidate").value};
  const html=sources.map(source=>`<option value="${esc(source.value)}">${esc(source.label)}</option>`).join("");
  $("baseline-session").innerHTML=html;$("candidate-session").innerHTML=html;
  const resolved=resolveComparisonSources(sources,{baselineSource:state.bs,candidateSource:state.cs,baselineLap:state.b,candidateLap:state.c},`session:${selectedSessionId}`);
  $("baseline-session").value=resolved.baselineSource;
  $("candidate-session").value=resolved.candidateSource;
  populateLapSelector("baseline",state.b);populateLapSelector("candidate",state.c);
  $("compare").disabled=!$("baseline").value||!$("candidate").value;
}
function chosenMeta(side){const source=$(side+"-session").value;if(source.startsWith("pb:")){const pb=personalBests.find(item=>`pb:${item.key}`===source);return pb&&{track_id:pb.track_id,mode:pb.mode,session_id:pb.source_session_id,profile_id:null};}const session=sessionById(source.replace(/^session:/,""));return session&&{track_id:session.track_id,mode:session.mode,session_id:session.id,profile_id:session.circuit_profile_id};}
function chosenLapMeta(side){const source=$(side+"-session").value;return sourceLaps(source).find(lap=>lap.id===$(side).value);}
function checkCompatibility(){const result=comparisonCompatibility(chosenMeta("baseline"),chosenMeta("candidate"),chosenLapMeta("baseline"),chosenLapMeta("candidate"),circuitProfiles);$("compare-error").textContent=result.error;$("compare-warning").textContent=result.warnings.join("\n");$("compare").disabled=Boolean(result.error)||!$("baseline").value||!$("candidate").value;}
function isControllerEvent(event){return event.type==="controller_input"||event.type==="butn";}
function renderRaceTimeline(){
  const showController=$("timeline-controller-events").checked;
  const showOther=$("timeline-other-car-events").checked;
  const showSuperseded=$("timeline-superseded-events").checked;
  const filtered=raceTimeline.filter(event=>(showController||!isControllerEvent(event))&&(showOther||event.player_relevant!==false)&&(showSuperseded||event.event_validity!=="superseded"));
  const pages=Math.max(1,Math.ceil(filtered.length/TIMELINE_PAGE_SIZE));timelinePage=Math.min(Math.max(1,timelinePage),pages);
  const end=Math.max(0,filtered.length-(timelinePage-1)*TIMELINE_PAGE_SIZE),start=Math.max(0,end-TIMELINE_PAGE_SIZE),visible=filtered.slice(start,end);
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
  updatePager("timeline-newer","timeline-pagination-label","timeline-older",timelinePage,pages,filtered.length,"events");
  $("timeline-pagination-label").textContent+=timelinePage===1?" · newest":"";
}
function sessionViewIsActive(){return document.querySelector(".view.active")?.id==="view-session";}
async function ensureSelectedTimeline(){
  const session=sessionById(selectedSessionId),sessionId=session?.id;
  if(!sessionId||!session?.continuous_store||session.continuous_store.error){raceTimeline=[];if(sessionViewIsActive())renderRaceTimeline();return;}
  const selectedIsActive=Boolean(activeRecording&&sessionId===activeRecording.id),timelineSafe=!activeRecording||selectedIsActive,cached=timelineBySession.get(sessionId);
  if(!timelineSafe){raceTimeline=cached?.events||[];if(sessionViewIsActive())renderRaceTimeline();return;}
  if(cached&&cached.status===session.status&&!selectedIsActive){raceTimeline=cached.events;if(sessionViewIsActive())renderRaceTimeline();return;}
  if(timelineRequests.has(sessionId))return;
  if(sessionViewIsActive()&&!cached)$("race-timeline").innerHTML='<p class="empty">Loading event timeline…</p>';
  const pending=request(`/api/sessions/${encodeURIComponent(sessionId)}/timeline`);
  timelineRequests.set(sessionId,pending);
  try{
    const events=(await pending).events;
    timelineBySession.set(sessionId,{status:session.status,events});
    if(selectedSessionId===sessionId){raceTimeline=events;if(sessionViewIsActive())renderRaceTimeline();}
  }catch(_){
    if(selectedSessionId===sessionId&&!cached){raceTimeline=[];if(sessionViewIsActive())renderRaceTimeline();}
  }finally{timelineRequests.delete(sessionId);}
}
function renderSession(){
  const sorted=[...laps].sort((a,b)=>a.time_ms-b.time_ms),validTimes=sorted.filter(lap=>!lap.invalid).map(lap=>lap.time_ms),bestValid=validTimes.length?Math.min(...validTimes):null,lapPagination=pageData(sorted,sessionLapsPage),rankStart=lapPagination.start;sessionLapsPage=lapPagination.current;$("lap-count").textContent=`${laps.length} lap${laps.length===1?"":"s"}${bestValid==null?"":` · best ${fmtTime(bestValid)}`}`;
  $("session-laps").innerHTML=lapPagination.items.map((lap,index)=>{const s3=statsSector3(lap),gap=bestValid==null?"No valid reference":!lap.invalid&&lap.time_ms===bestValid?"Best valid":`${lap.time_ms>=bestValid?"+":"−"}${(Math.abs(lap.time_ms-bestValid)/1000).toFixed(3)} s`,setupState=statsSetupState(lap),coverage=lap.quality?.coverage_pct==null?"Legacy coverage":`${lap.quality.coverage_pct}% coverage`,events=lap.event_count?`${lap.event_count} flashback/restart event${lap.event_count===1?"":"s"}`:"No recorded restart events";return `<tr class="${lap.invalid?"invalid-row":""}"><td><label class="lap-rank-select"><input type="checkbox" class="lap-export-check" data-id="${esc(lap.id)}" ${selectedLapIds.has(lap.id)?"checked":""} aria-label="Select lap ${lap.number} for export"><strong>${rankStart+index+1}</strong></label></td><td class="time">${fmtTime(lap.time_ms)}<br><span class="hint">${gap}</span></td><td>${fmtTime(lap.sector1_ms)} / ${fmtTime(lap.sector2_ms)} / ${fmtTime(s3)}</td><td>Lap ${lap.number}<span class="lap-validity ${lap.invalid?"stats-invalid":"stats-valid"}">${lap.invalid?"INVALID":"valid"}</span><span class="hint">${events}</span></td><td>${esc(tyreCompoundLabel(lap.actual_tyre_compound))}<br><span class="hint">${lap.tyre_age_laps==null?"age unknown":`age ${lap.tyre_age_laps} lap${lap.tyre_age_laps===1?"":"s"}`}</span></td><td><span class="stats-setup-state">${setupState}</span><br><span class="hint">${esc(fmtSetup(lap.setup))}</span></td><td>${coverage}<br><span class="hint">${lap.sample_count} samples · ${sampleRate(lap)}<br>${lap.peak_speed_kph??"—"} / ${lap.minimum_speed_kph??"—"} km/h peak / min</span></td><td><input class="tag-input" data-id="${esc(lap.id)}" maxlength="240" value="${esc(lap.note)}" placeholder="e.g. rear wing +1"></td><td><div class="session-lap-actions"><button data-session-compare="baseline" data-id="${esc(lap.id)}">Baseline</button><button data-session-compare="candidate" data-id="${esc(lap.id)}">Candidate</button><button class="edit-setup" data-id="${esc(lap.id)}">Setup</button></div></td></tr>`;}).join("")||'<tr><td colspan="9" class="empty-state">No completed laps in this session.</td></tr>';
  updatePager("session-laps-prev","session-laps-pagination-label","session-laps-next",sessionLapsPage,lapPagination.pages,sorted.length,"laps");
  document.querySelectorAll(".tag-input").forEach(input=>input.addEventListener("change",()=>saveNote(input)));
  document.querySelectorAll(".lap-export-check").forEach(input=>input.addEventListener("change",()=>{if(input.checked)selectedLapIds.add(input.dataset.id);else selectedLapIds.delete(input.dataset.id);updateExportCount();}));
  document.querySelectorAll("[data-session-compare]").forEach(button=>button.addEventListener("click",()=>{const side=button.dataset.sessionCompare,source=`session:${selectedSessionId}`;document.querySelector('[data-view="analysis"]').click();$(side+"-session").value=source;populateLapSelector(side,button.dataset.id);$(side).value=button.dataset.id;checkCompatibility();}));
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
  const session=sessionById(selectedSessionId),old=$("profile-select").value,oldLap=$("profile-lap").value,sessionChanged=profileEditorSessionId!==selectedSessionId;
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
function turnBoundariesFromRow(row){const apexRaw=row.querySelector('[data-key="estimated_apex_m"]')?.value.trim(),marginRaw=row.querySelector('[data-key="margin_m"]')?.value.trim();if(!apexRaw||!marginRaw)return null;const apex=Number(apexRaw),margin=Number(marginRaw),length=Number(sessionById(selectedSessionId)?.track_length_m)||circuitTrace.reduce((maximum,point)=>Math.max(maximum,Number(point.distance_m)||0),apex);return turnBoundaries(apex,margin,Number(row.dataset.beforeMargin),Number(row.dataset.afterMargin),Number(row.dataset.originalMargin),length);}
function turnMarkersFromRows(){return [...document.querySelectorAll(".profile-turn")].flatMap((row,rowIndex)=>{const boundaries=turnBoundariesFromRow(row),number=row.querySelector('[data-key="number"]')?.value||"?";return boundaries?[{rowIndex,number,...boundaries}]:[];});}
function ensureCircuitEditor(){if(circuitEditor)return;circuitEditor=new CircuitProfilerCanvas($("circuit-map"),{onApexSelected:addTurnAtApex,onTurnSelected:rowIndex=>selectTurn(rowIndex,true),onApexMoved:(rowIndex,values)=>{const row=document.querySelectorAll(".profile-turn")[rowIndex];if(!row)return;row.querySelector('[data-key="estimated_apex_m"]').value=Number(values.apex_m).toFixed(1);const boundaries=turnBoundariesFromRow(row),entry=boundaries&&tracePointAtDistance(boundaries.entry_m),apex=boundaries&&tracePointAtDistance(boundaries.apex_m),exit=boundaries&&tracePointAtDistance(boundaries.exit_m);if(entry&&apex&&exit)row.querySelector('[data-key="direction"]').value=traceDirection(entry,apex,exit);profileEditorDirty=true;selectTurn(rowIndex);renderCircuitMap();$("point-turn-status").textContent=`Turn apex moved to ${Number(values.apex_m).toFixed(1)} m.`;}});}
function renderCircuitMap(){ensureCircuitEditor();if(!circuitEditor)return;circuitEditor.setTrace(circuitTrace,suggestedTurns);circuitEditor.setTurns(turnMarkersFromRows());circuitEditor.setSelectedTurn(selectedTurnIndex);circuitEditor.setDisplayMode(turnDisplayMode);circuitEditor.setArmed(pointedTurn);}
function setPointingStatus(){$("profile-point-turn").disabled=!circuitTrace.length||Boolean(pointedTurn);$("profile-cancel-point").disabled=!pointedTurn;$("point-turn-status").textContent=pointedTurn?`Turn ${pointedTurn.number}: click its apex/midpoint on the trace. Margin: ${pointedTurn.margin} m each side.`:circuitTrace.length?"Enter a turn number and margin, then point its apex/midpoint on the map.":"Load a clean distance map first.";renderCircuitMap();}
function startPointingTurn(){const number=$("point-turn-number").value.trim(),margin=Number($("point-turn-margin").value);if(!circuitTrace.length){$("point-turn-status").textContent="Load an eligible clean lap trace first.";return;}if(!number){$("point-turn-status").textContent="Enter the turn number first.";$("point-turn-number").focus();return;}if(!Number.isFinite(margin)||margin<10||margin>1000){$("point-turn-status").textContent="Choose a margin from 10 to 1,000 metres.";$("point-turn-margin").focus();return;}pointedTurn={number,name:$("point-turn-name").value.trim(),margin};setPointingStatus();}
function cancelPointingTurn(){pointedTurn=null;setPointingStatus();}
const traceDirection=directionFromTrace;
function addTurnAtApex(apexDistance){if(!pointedTurn)return;const apex=tracePointAtDistance(apexDistance);if(!apex)return;const length=Number(sessionById(selectedSessionId)?.track_length_m)||circuitTrace.at(-1).distance_m,entryDistance=Math.max(0,apex.distance_m-pointedTurn.margin),exitDistance=Math.min(length,apex.distance_m+pointedTurn.margin),entry=tracePointAtDistance(entryDistance),exit=tracePointAtDistance(exitDistance),margin=pointedTurn.margin;$("profile-turns").insertAdjacentHTML("beforeend",turnRow({number:pointedTurn.number,name:pointedTurn.name,entry_m:entryDistance.toFixed(1),estimated_apex_m:apex.distance_m.toFixed(1),exit_m:exitDistance.toFixed(1),direction:traceDirection(entry,apex,exit),provenance:`manually pointed apex on clean Motion X/Z lap trace; ±${margin} m margin`,verification_status:"unverified"}));selectedTurnIndex=document.querySelectorAll(".profile-turn").length-1;bindTurnRows();profileEditorDirty=true;const numeric=Number(pointedTurn.number);if(Number.isInteger(numeric))$("point-turn-number").value=String(numeric+1);$("point-turn-name").value="";pointedTurn=null;setPointingStatus();$("point-turn-status").textContent=`Turn added at ${apex.distance_m.toFixed(1)} m with a ${margin} m margin each side. Drag its yellow marker to refine it.`;}
async function loadCircuitTrace(){const lapId=$("profile-lap").value;if(!lapId)return;try{const trace=await request(`/api/laps/${encodeURIComponent(lapId)}/trace`),points=trace.points;circuitTrace=trace.eligible_for_profile_calibration?points:[];suggestedTurns=trace.eligible_for_profile_calibration?trace.turn_candidates:[];pointedTurn=null;$("profile-add-suggestions").disabled=!suggestedTurns.length;if(!points.length){$("profile-message").textContent=trace.exclusion_reasons.join("; ")||"No Motion X/Z samples in this lap.";circuitTrace=[];setPointingStatus();return;}if(trace.eligible_for_profile_calibration)profileEditorDirty=true;setPointingStatus();$("profile-message").textContent=`${trace.notice} ${trace.eligible_for_profile_calibration?"Eligible clean trace; saving will attach this centreline.":"Not eligible: "+trace.exclusion_reasons.join("; ")} ${trace.turn_candidates.length} unverified curvature suggestions found.`;}catch(error){circuitTrace=[];suggestedTurns=[];pointedTurn=null;$("profile-add-suggestions").disabled=true;setPointingStatus();$("profile-message").textContent=error.message;}}
function collectTurns(){return [...document.querySelectorAll(".profile-turn")].map(row=>{const value=key=>row.querySelector(`[data-key="${key}"]`).value,boundaries=turnBoundariesFromRow(row)||{};return{number:value("number"),name:value("name"),entry_m:boundaries.entry_m,estimated_apex_m:boundaries.apex_m,exit_m:boundaries.exit_m,direction:value("direction"),linked_group:value("linked_group")||null,provenance:$("profile-source").value||row.dataset.provenance||"manual dashboard edit",verification_status:value("verification_status")};});}
async function saveCircuitProfile(){const session=sessionById(selectedSessionId),base=circuitProfiles.find(p=>p.id===$("profile-select").value)||{};if(!session)return;try{const saved=await post("/api/circuit-profiles",{...base,packet_format:2025,track_id:session.track_id,layout:base.layout||(session.track_id>=39?"reverse":"normal"),circuit_name:base.circuit_name||session.track_name,measured_game_length_m:base.measured_game_length_m||session.track_length_m,centreline:circuitTrace.length?circuitTrace:base.centreline||[],sector_boundaries_m:[session.sector2_start_m,session.sector3_start_m].filter(value=>value!=null),turns:collectTurns(),provenance:{source:$("profile-source").value||"manual dashboard edit",notes:"Turn boundaries edited against a recorded game-coordinate trace."},verification_status:$("profile-verification").value});await post(`/api/sessions/${encodeURIComponent(session.id)}/circuit-profile`,{profile_id:saved.id});profileEditorDirty=false;$("profile-message").textContent="Profile saved atomically and selected for this session.";await loadLibrary();}catch(error){$("profile-message").textContent=error.message;}}
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
function updateExportCount(){const scope=exportScope(),count=scope==="session"?laps.length:selectedLapIds.size,session=sessionById(selectedSessionId),hasContinuous=Boolean(session?.continuous_store?.packet_count);$("export-count").textContent=`${count} completed lap${count===1?"":"s"}${scope==="session"&&hasContinuous?" + continuous session data":""} will be exported`;const unavailable=count===0&&!(scope==="session"&&hasContinuous);$("download-report").disabled=unavailable;$("download-zip").disabled=unavailable;}
function downloadExport(format){const scope=exportScope();if(scope==="selected"&&!selectedLapIds.size){$("export-error").textContent="Select at least one lap.";return;}const session=sessionById(selectedSessionId),report=$("report-type").value;if(report==="race"&&session?.mode!=="race"){$("export-error").textContent="Race reports require a race session.";return;}if(report==="time_trial"&&session?.mode!=="time_trial"){$("export-error").textContent="Time Trial reports require a Time Trial session.";return;}$("export-error").textContent="";const query=new URLSearchParams({scope,format,report});if(scope==="selected")for(const id of selectedLapIds)query.append("lap",id);const link=document.createElement("a");link.href=`/api/sessions/${encodeURIComponent(selectedSessionId)}/export?${query}`;link.click();}

function renderPersonalBests(){
  const pbPagination=pageData(personalBests,pbPage);pbPage=pbPagination.current;
  $("pb-cards").innerHTML=pbPagination.items.map(pb=>`<article class="pb-card"><span class="hint">${esc(pb.track_name)} · ${esc(pb.mode.replace("_"," "))}</span><strong>${fmtTime(pb.time_ms)}</strong><div class="hint">${esc(pb.source_session_name)} · ${new Date(pb.became_pb_at).toLocaleString()}<br>${pb.official_time_only?`Official game timing · telemetry trace incomplete${pb.trace_available?`<br>Comparison trace ${fmtTime(pb.comparison_lap_time_ms)} · Lap ${pb.comparison_source_lap_number}`:""}`:`Setup ${esc(fmtSetup(pb.setup))}`}</div><button class="pb-open" data-key="${esc(pb.key)}">Open details</button>${pb.trace_available!==false?`<button class="pb-use" data-side="baseline" data-key="${esc(pb.key)}">Baseline</button><button class="pb-use" data-side="candidate" data-key="${esc(pb.key)}">Candidate</button>`:""}</article>`).join("")||'<p class="empty">No eligible personal bests stored yet. Complete a valid full lap or rebuild from saved sessions.</p>';
  updatePager("pb-prev","pb-pagination-label","pb-next",pbPage,pbPagination.pages,personalBests.length,"PBs");
  document.querySelectorAll(".pb-open").forEach(button=>button.addEventListener("click",()=>openPersonalBest(button.dataset.key)));
  document.querySelectorAll(".pb-use").forEach(button=>button.addEventListener("click",()=>{const side=button.dataset.side;$(side+"-session").value=`pb:${button.dataset.key}`;populateLapSelector(side);$("pb-message").textContent=`Personal best selected as ${side}.`;}));
}
async function openPersonalBest(key){try{const pb=await request(`/api/personal-bests/${encodeURIComponent(key)}`),details={...pb,lap:pb.lap?{...pb.lap,samples:`${pb.lap.samples.length} telemetry samples (available through this API)`}:null};$("pb-details").textContent=JSON.stringify(details,null,2);$("pb-details").hidden=false;}catch(error){$("pb-message").textContent=error.message;}}

const setupDifferences=(baseline,candidate)=>compareSetups(baseline,candidate,setupFields);
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
  if(!paceMapCanvas){const tooltip=$("tooltip");paceMapCanvas=new PaceMapCanvas(container,{onHover:(item,event)=>{tooltip.hidden=false;tooltip.style.left=`${Math.min(innerWidth-240,event.clientX+12)}px`;tooltip.style.top=`${event.clientY+12}px`;if(item.type==="turn"){const result=Number.isFinite(item.window_delta_s)?`<br>${Math.abs(item.window_delta_s)<.004?"Near even through profile window":`${item.window_delta_s<0?"Candidate":"Baseline"} gains ${Math.abs(item.window_delta_s).toFixed(3)} s through profile window`}`:"";tooltip.innerHTML=`<b>${esc(item.name)}</b><br>Profile apex: ${item.apex_m.toFixed(0)} m${result}`;}else tooltip.innerHTML=`<b>${esc(item.label)}</b><br>${esc(item.distance_label)} · local delta change ${item.change>=0?"+":""}${item.change.toFixed(3)} s`;},onLeave:()=>{tooltip.hidden=true;}});}
  if(paceMapCanvas&&paceMapSource!==data){paceMapCanvas.setData(points,sections,turns);paceMapSource=data;}
}
function drawCharts(data){
  const trace=data.trace,delta=trace.map(row=>row.delta_s),speedsB=trace.map(row=>row.baseline.speed_kph),speedsC=trace.map(row=>row.candidate.speed_kph),deltaRange=Math.max(.1,...delta.map(Math.abs));
  chart(document.querySelector('[data-chart="delta"]'),[{label:"Candidate − baseline",color:colors.delta,values:delta}],-deltaRange,deltaRange," s",2);
  chart(document.querySelector('[data-chart="speed"]'),[{label:"Baseline speed",color:colors.baseline,values:speedsB},{label:"Candidate speed",color:colors.candidate,values:speedsC}],0,Math.ceil(Math.max(...speedsB,...speedsC)/50)*50," km/h");
  drawPaceMap(data);
  chart(document.querySelector('[data-chart="pedals"]'),[{label:"Baseline throttle",color:colors.baseline,values:trace.map(r=>r.baseline.throttle)},{label:"Candidate throttle",color:colors.candidate,values:trace.map(r=>r.candidate.throttle)},{label:"Baseline brake",color:colors.baselineAlt,values:trace.map(r=>r.baseline.brake)},{label:"Candidate brake",color:colors.candidateAlt,values:trace.map(r=>r.candidate.brake)}],0,100,"% ");
  const motionValues=trace.flatMap(r=>[r.baseline.front_wheels_angle,r.candidate.front_wheels_angle,r.baseline.chassis_yaw,r.candidate.chassis_yaw]).filter(Number.isFinite),motionPanel=$("motion-ex-panel");motionPanel.hidden=!motionValues.length;
  if(motionValues.length){const range=Math.max(.05,...motionValues.map(Math.abs));chart(document.querySelector('[data-chart="motion-ex"]'),[{label:"Baseline front-wheel angle",color:colors.baseline,values:trace.map(r=>r.baseline.front_wheels_angle??0)},{label:"Candidate front-wheel angle",color:colors.candidate,values:trace.map(r=>r.candidate.front_wheels_angle??0)},{label:"Baseline chassis yaw",color:colors.baselineAlt,values:trace.map(r=>r.baseline.chassis_yaw??0)},{label:"Candidate chassis yaw",color:colors.candidateAlt,values:trace.map(r=>r.candidate.chassis_yaw??0)}],-range,range," rad",3);}
  const slipValues=trace.flatMap(r=>[r.baseline.front_wheel_slip_ratio,r.candidate.front_wheel_slip_ratio,r.baseline.rear_wheel_slip_ratio,r.candidate.rear_wheel_slip_ratio]).filter(Number.isFinite),slipPanel=$("wheel-slip-panel");slipPanel.hidden=!slipValues.length;
  if(slipValues.length){const maximum=Math.max(.05,...slipValues);chart(document.querySelector('[data-chart="wheel-slip"]'),[{label:"Baseline front",color:colors.baseline,values:trace.map(r=>r.baseline.front_wheel_slip_ratio??0)},{label:"Candidate front",color:colors.candidate,values:trace.map(r=>r.candidate.front_wheel_slip_ratio??0)},{label:"Baseline rear",color:colors.baselineAlt,values:trace.map(r=>r.baseline.rear_wheel_slip_ratio??0)},{label:"Candidate rear",color:colors.candidateAlt,values:trace.map(r=>r.candidate.rear_wheel_slip_ratio??0)}],0,maximum,"",3);}
  const angleValues=trace.flatMap(r=>[r.baseline.front_wheel_slip_angle,r.candidate.front_wheel_slip_angle,r.baseline.rear_wheel_slip_angle,r.candidate.rear_wheel_slip_angle]).filter(Number.isFinite),anglePanel=$("slip-angle-panel");anglePanel.hidden=!angleValues.length;
  if(angleValues.length){const maximum=Math.max(.05,...angleValues);chart(document.querySelector('[data-chart="slip-angle"]'),[{label:"Baseline front",color:colors.baseline,values:trace.map(r=>r.baseline.front_wheel_slip_angle??0)},{label:"Candidate front",color:colors.candidate,values:trace.map(r=>r.candidate.front_wheel_slip_angle??0)},{label:"Baseline rear",color:colors.baselineAlt,values:trace.map(r=>r.baseline.rear_wheel_slip_angle??0)},{label:"Candidate rear",color:colors.candidateAlt,values:trace.map(r=>r.candidate.rear_wheel_slip_angle??0)}],0,maximum," rad",3);}
}

function invalidateComparison(){comparisonGeneration.invalidate();comparison=null;$("download-comparison-report").disabled=true;}
events.onId("compare","click",async()=>{invalidateComparison();const generation=comparisonGeneration.issue();$("compare-error").textContent="";try{const result=await request(`/api/compare?baseline=${encodeURIComponent($("baseline").value)}&candidate=${encodeURIComponent($("candidate").value)}`);if(comparisonGeneration.isCurrent(generation))renderComparison(result);}catch(error){if(comparisonGeneration.isCurrent(generation))$("compare-error").textContent=error.message;}});
events.onId("download-comparison-report","click",()=>{if(!comparison)return;const query=new URLSearchParams({baseline:$("baseline").value,candidate:$("candidate").value});const link=document.createElement("a");link.href=`/api/compare/report?${query}`;link.click();});
events.onId("baseline-session","change",()=>{invalidateComparison();populateLapSelector("baseline");});
events.onId("candidate-session","change",()=>{invalidateComparison();populateLapSelector("candidate");});
events.onId("baseline","change",()=>{invalidateComparison();checkCompatibility();});events.onId("candidate","change",()=>{invalidateComparison();checkCompatibility();});
events.onSelectorAll("[data-displayed-session-select]","change",event=>selectSession(event.target.value));
events.onId("rename-session","input",()=>{renameDraftDirty=true;});
events.onId("track-override","input",()=>{trackDraftDirty=true;});
function refreshSessionQueryViews(){renderSessionQuery();renderSessionLibrary();populateSelectors();}
events.onSelectorAll('[data-session-filter-field="year"]',"change",event=>{sessionFilter={year:event.target.value,month:"",day:""};sessionPage=1;refreshSessionQueryViews();});
events.onSelectorAll('[data-session-filter-field="month"]',"change",event=>{sessionFilter.month=event.target.value;sessionFilter.day="";sessionPage=1;refreshSessionQueryViews();});
events.onSelectorAll('[data-session-filter-field="day"]',"change",event=>{sessionFilter.day=event.target.value;sessionPage=1;refreshSessionQueryViews();});
events.onSelectorAll("[data-session-filter-clear]","click",()=>{sessionFilter={year:"",month:"",day:""};sessionPage=1;refreshSessionQueryViews();});
for(const id of ["stats-track","stats-mode","stats-validity","stats-session","stats-year","stats-compound","stats-setup","stats-quality","stats-order"]){events.onId(id,"change",()=>{circuitStatsPage=1;raceStatsPage=1;renderCircuitStats();});}
events.onId("stats-search","input",()=>{circuitStatsPage=1;raceStatsPage=1;renderCircuitStats();});
events.onId("stats-reset","click",()=>{$("stats-mode").value="all";$("stats-validity").value="valid";$("stats-session").value="all";$("stats-year").value="all";$("stats-compound").value="all";$("stats-setup").value="all";$("stats-quality").value="all";$("stats-order").value="best";$("stats-search").value="";circuitStatsPage=1;raceStatsPage=1;renderCircuitStats();});
events.onId("stats-prev","click",()=>{if(circuitStatsPage>1){circuitStatsPage-=1;renderCircuitStats();}});
events.onId("stats-next","click",()=>{circuitStatsPage+=1;renderCircuitStats();});
events.onId("race-stats-prev","click",()=>{if(raceStatsPage>1){raceStatsPage-=1;renderRaceStats();}});
events.onId("race-stats-next","click",()=>{raceStatsPage+=1;renderRaceStats();});
events.onId("session-prev","click",()=>{if(sessionPage>1){sessionPage-=1;renderSessionLibrary();}});
events.onId("session-next","click",()=>{sessionPage+=1;renderSessionLibrary();});
events.onId("session-laps-prev","click",()=>{if(sessionLapsPage>1){sessionLapsPage-=1;renderSession();}});
events.onId("session-laps-next","click",()=>{sessionLapsPage+=1;renderSession();});
events.onId("pb-prev","click",()=>{if(pbPage>1){pbPage-=1;renderPersonalBests();}});
events.onId("pb-next","click",()=>{pbPage+=1;renderPersonalBests();});
events.onId("deleted-prev","click",()=>{if(deletedPage>1){deletedPage-=1;renderSettings();}});
events.onId("deleted-next","click",()=>{deletedPage+=1;renderSettings();});
events.onId("pace-map-reset","click",()=>paceMapCanvas?.resetView());
events.onId("pace-map-rotate-left","click",()=>paceMapCanvas?.rotateBy(-10));
events.onId("pace-map-rotate-right","click",()=>paceMapCanvas?.rotateBy(10));
events.onId("timeline-controller-events","change",()=>{timelinePage=1;renderRaceTimeline();});
events.onId("timeline-other-car-events","change",()=>{timelinePage=1;renderRaceTimeline();});
events.onId("timeline-superseded-events","change",()=>{timelinePage=1;renderRaceTimeline();});
events.onId("timeline-newer","click",()=>{if(timelinePage>1){timelinePage-=1;renderRaceTimeline();}});
events.onId("timeline-older","click",()=>{timelinePage+=1;renderRaceTimeline();});
events.onId("start-run","click",async()=>{try{await post("/api/runs/start",{name:$("run-name").value});$("run-name").value="";await loadLibrary();}catch(error){setRecorderFeedback(error.message);}});
events.onId("stop-recording","click",async()=>{try{await post("/api/recording/stop");await loadLibrary();}catch(error){setRecorderFeedback(error.message);}});
events.onId("diagnostic-enabled","change",async event=>{event.target.disabled=true;try{diagnosticState=await post("/api/diagnostics",{enabled:event.target.checked});renderRecorder();}catch(error){setRecorderFeedback(error.message);}finally{event.target.disabled=false;}});
events.onId("jump-active","click",()=>activeRecording&&selectSession(activeRecording.id));
events.onId("rename-button","click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/rename`,{name:$("rename-session").value});renameDraftDirty=false;await loadLibrary();}catch(error){setRecorderFeedback(error.message);}});
events.onId("delete-session","click",async()=>{const selected=sessionById(selectedSessionId);if(!selected||!selected.can_delete)return;if(!window.confirm(`Delete session “${selected.name}” and its ${selected.lap_count} lap file(s)?\n\nIt will be moved to local trash. Stored PB snapshots and notes will be preserved.`))return;try{const result=await post(`/api/sessions/${encodeURIComponent(selected.id)}/delete`,{confirm:true});selectedLapIds.clear();comparison=null;editingLap=null;selectedSessionId=result.selected_session_id;await loadLibrary();setRecorderFeedback(`Session moved to ${result.moved_to}. PB snapshots and notes were preserved.`);}catch(error){setRecorderFeedback(error.message);}});
events.onId("settings-form","input",()=>{settingsDraftDirty=true;});
events.onId("settings-form","submit",async event=>{event.preventDefault();const payload=settingsPayload(id=>$(id).value);try{const result=await post("/api/settings",payload);dashboardSettings=result.settings;settingsDraftDirty=false;const restart=result.restart_required||[];$("settings-message").textContent=`Settings saved.${restart.length?` Restart the server to apply: ${restart.join(", ")}.`:" Applied without restart."}${result.purge?.purged?.length?` Permanently removed ${result.purge.purged.length} expired session(s).`:""}`;await loadLibrary();}catch(error){$("settings-message").textContent=error.message;}});
events.onId("track-override-button","click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/track-name`,{name:$("track-override").value});trackDraftDirty=false;await loadLibrary();}catch(error){setRecorderFeedback(error.message);}});
events.onId("edit-session-profile","click",()=>document.querySelector('[data-view="circuit-profiler"]').click());
events.onId("profile-select","change",()=>{profileEditorDirty=false;pointedTurn=null;showProfileFields();setPointingStatus();});
events.onId("profile-load-saved","click",loadSavedProfile);
events.onId("profile-delete","click",deleteCircuitProfile);
events.onId("profile-apply","click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/circuit-profile`,{profile_id:$("profile-select").value});$("profile-message").textContent="Profile selected for this session.";await loadLibrary();}catch(error){$("profile-message").textContent=error.message;}});
events.onId("profile-alternate","click",()=>{const session=sessionById(selectedSessionId),base=circuitProfiles.find(p=>p.id===$("profile-select").value)||{};if(!session)return;const suffix=Date.now().toString(36),alternate={...base,id:`f1-2025-track-${session.track_id}-${base.layout||(session.track_id>=39?"reverse":"normal")}-alternate-${suffix}`,circuit_name:`${base.circuit_name||session.track_name} alternate`,verification_status:"unverified",provenance:{source:"alternate profile created in dashboard",notes:"Independent candidate calibration; not selected until saved."},turns:(base.turns||[]).map(turn=>({...turn,verification_status:"unverified"}))};circuitProfiles.push(alternate);$("profile-select").insertAdjacentHTML("beforeend",`<option value="${esc(alternate.id)}">${esc(alternate.circuit_name)} · ${esc(alternate.layout)} · unverified</option>`);$("profile-select").value=alternate.id;profileEditorDirty=true;showProfileFields();$("profile-message").textContent="Alternate profile draft created. Edit provenance/turns, then Save profile.";});
events.onId("profile-load-trace","click",loadCircuitTrace);
events.onId("profile-point-turn","click",startPointingTurn);
events.onId("profile-cancel-point","click",cancelPointingTurn);
events.onId("profile-reset-view","click",()=>{ensureCircuitEditor();circuitEditor?.resetView();});
events.onId("profile-rotate-left","click",()=>{ensureCircuitEditor();circuitEditor?.rotateBy(-10);});
events.onId("profile-rotate-right","click",()=>{ensureCircuitEditor();circuitEditor?.rotateBy(10);});
events.onId("profile-show-all-turns","click",()=>{showAllTurnCards=!showAllTurnCards;updateTurnCardVisibility();});
$("profile-turn-display").value=turnDisplayMode;
events.onId("profile-turn-display","change",event=>{turnDisplayMode=event.target.value;try{localStorage.setItem("circuit-profiler-turn-display",turnDisplayMode);}catch(_){}circuitEditor?.setDisplayMode(turnDisplayMode);});
events.onId("profile-add-suggestions","click",()=>{const session=sessionById(selectedSessionId),length=session?.track_length_m||Infinity;for(const [index,candidate] of suggestedTurns.entries())$("profile-turns").insertAdjacentHTML("beforeend",turnRow({number:`S${index+1}`,name:"Suggested turn",entry_m:Math.max(0,candidate.distance_m-50),estimated_apex_m:candidate.distance_m,exit_m:Math.min(length,candidate.distance_m+50),direction:candidate.direction,provenance:"curvature suggestion from clean Motion X/Z trace",verification_status:"unverified"}));bindTurnRows();suggestedTurns=[];profileEditorDirty=true;$("profile-add-suggestions").disabled=true;renderCircuitMap();$("profile-message").textContent="Suggestions added as unverified drafts. Check every boundary/name against the trace and game before marking verified.";});
events.onId("profile-add-turn","click",()=>{$("profile-turns").insertAdjacentHTML("beforeend",turnRow());selectedTurnIndex=document.querySelectorAll(".profile-turn").length-1;bindTurnRows();profileEditorDirty=true;renderCircuitMap();});
events.onId("profile-turns","input",event=>{profileEditorDirty=true;const row=event.target.closest(".profile-turn"),key=event.target.dataset.key;if(row&&(key==="estimated_apex_m"||key==="margin_m")){const boundaries=turnBoundariesFromRow(row),entry=boundaries&&tracePointAtDistance(boundaries.entry_m),apex=boundaries&&tracePointAtDistance(boundaries.apex_m),exit=boundaries&&tracePointAtDistance(boundaries.exit_m);if(entry&&apex&&exit)row.querySelector('[data-key="direction"]').value=traceDirection(entry,apex,exit);}renderCircuitMap();});
events.onId("profile-verification","change",()=>{profileEditorDirty=true;});
events.onId("profile-source","input",()=>{profileEditorDirty=true;});
events.onId("profile-save","click",saveCircuitProfile);
events.onId("select-all-laps","click",()=>{selectedLapIds=new Set(laps.map(lap=>lap.id));renderSession();});
events.onId("clear-laps","click",()=>{selectedLapIds.clear();renderSession();});
events.onSelectorAll('input[name="export-scope"]',"change",updateExportCount);
events.onId("download-report","click",()=>downloadExport("markdown"));
events.onId("download-zip","click",()=>downloadExport("zip"));
events.onId("rebuild-pbs","click",async()=>{try{const result=await post("/api/personal-bests/rebuild");$("pb-message").textContent=`Rebuild considered ${result.considered} laps; ${result.personal_best_count} PB records are stored.`;await loadLibrary();}catch(error){$("pb-message").textContent=error.message;}});
events.onId("setup-close","click",()=>$("setup-dialog").close());
events.onId("setup-form","submit",async event=>{event.preventDefault();const changed={};document.querySelectorAll("#setup-fields input").forEach(input=>{if(input.value!==input.dataset.original&&input.value!=="")changed[input.dataset.key]=Number(input.value);});if(!Object.keys(changed).length){$("setup-message").textContent="No values changed.";return;}try{const result=await post(`/api/laps/${encodeURIComponent(editingLap.id)}/setup`,{setup:changed});$("setup-message").textContent=`Saved. ${result.pb_message}. Audit: ${result.audit_record}`;setTimeout(()=>$("setup-dialog").close(),900);await loadLibrary();}catch(error){$("setup-message").textContent=error.message;}});

function cacheAllLaps(items){lapsById.clear();lapsBySession.clear();for(const session of sessions)lapsBySession.set(session.id,[]);for(const lap of items){lapsById.set(lap.id,lap);if(!lapsBySession.has(lap.recording_session_id))lapsBySession.set(lap.recording_session_id,[]);lapsBySession.get(lap.recording_session_id).push(lap);}}

async function loadLibrary(options={}){
  const background=options.background===true;
  const selection=options.selection===true;
  if(background&&document.hidden)return;
  if(libraryRequestPending){if(!background)libraryReloadRequested=true;return;}
  libraryRequestPending=true;
  const generation=libraryGeneration.issue();
  const initial=!libraryInitialized,force=!background&&!selection;
  try{
    const previousSessions=sessions,previousSessionKey=sessionListKey(previousSessions),previousSelected=syncedSelectedSessionId;
    const settingsFallback={settings:{web_host:"127.0.0.1",web_port:8025,udp_port:20777,trash_retention_days:30,max_recording_gb:8,diagnostic_max_mb:50,dashboard_refresh_seconds:8},deleted_sessions:[],receiver_ips:["127.0.0.1"]};
    const data=await request("/api/sessions");
    if(!libraryGeneration.isCurrent(generation))return;
    sessions=data.sessions;sessionsById.clear();for(const session of sessions)sessionsById.set(session.id,session);selectedSessionId=data.selected_session_id;activeRecording=data.recording;diagnosticState=data.diagnostics||{};latestState={...latestState,...data.latest};
    const selectedChanged=previousSelected!==selectedSessionId,metadataChanged=initial||previousSessionKey!==sessionListKey(sessions);
    syncedSelectedSessionId=selectedSessionId;

    let settingsChanged=false,pbChanged=false,schemaChanged=false;
    if(initial||force){
      const [pbData,schema,settingsData]=await Promise.all([request("/api/personal-bests"),request("/api/setup-schema"),request("/api/settings").catch(()=>settingsFallback)]);
      if(!libraryGeneration.isCurrent(generation))return;
      const oldPB=JSON.stringify(personalBests),oldSchema=JSON.stringify(setupFields),oldSettings=JSON.stringify([dashboardSettings,deletedSessions,receiverIps]);
      personalBests=pbData.personal_bests;setupFields=schema.fields;dashboardSettings=settingsData.settings||{};deletedSessions=settingsData.deleted_sessions||[];receiverIps=settingsData.receiver_ips||[];
      pbChanged=initial||oldPB!==JSON.stringify(personalBests);schemaChanged=initial||oldSchema!==JSON.stringify(setupFields);settingsChanged=initial||oldSettings!==JSON.stringify([dashboardSettings,deletedSessions,receiverIps]);
    }

    const previousById=new Map(previousSessions.map(session=>[session.id,session]));
    let changedLapIds=[];
    if(initial){
      const initialLaps=await request("/api/laps?all=1");
      if(!libraryGeneration.isCurrent(generation))return;
      cacheAllLaps(initialLaps.laps);
      changedLapIds=sessions.map(session=>session.id);
    }else{
      changedLapIds=sessions.filter(session=>{
        const previous=previousById.get(session.id),cached=lapsBySession.get(session.id);
        return !previous||!cached||Number(previous.lap_count)!==Number(session.lap_count)||(force&&session.id===selectedSessionId);
      }).map(session=>session.id);
      const groups=await Promise.all(changedLapIds.map(id=>request(`/api/sessions/${encodeURIComponent(id)}/laps`)));
      if(!libraryGeneration.isCurrent(generation))return;
      groups.forEach(group=>{for(const previous of lapsBySession.get(group.session_id)||[])lapsById.delete(previous.id);lapsBySession.set(group.session_id,group.laps);for(const lap of group.laps)lapsById.set(lap.id,lap);});
      if(background&&changedLapIds.length){const pbData=await request("/api/personal-bests");if(!libraryGeneration.isCurrent(generation))return;const oldPB=JSON.stringify(personalBests);personalBests=pbData.personal_bests;pbChanged=oldPB!==JSON.stringify(personalBests);}
    }
    const knownIds=new Set(sessions.map(session=>session.id));
    for(const id of lapsBySession.keys())if(!knownIds.has(id)){for(const lap of lapsBySession.get(id)||[])lapsById.delete(lap.id);lapsBySession.delete(id);}
    for(const id of timelineBySession.keys())if(!knownIds.has(id))timelineBySession.delete(id);
    allLaps=[...lapsBySession.values()].flat();laps=lapsBySession.get(selectedSessionId)||[];
    const selectedLapsChanged=initial||selectedChanged||changedLapIds.includes(selectedSessionId);

    let profilesChanged=false;
    if(initial||force||selectedChanged){
      const draftId=profileEditorDirty?$("profile-select").value:"",loaded=(await request(`/api/circuit-profiles?session=${encodeURIComponent(selectedSessionId||"")}`));
      if(!libraryGeneration.isCurrent(generation))return;
      const loadedProfiles=loaded.profiles,oldProfiles=JSON.stringify(circuitProfiles);
      circuitProfiles=mergeProfilesPreservingDraft(loadedProfiles,circuitProfiles,draftId,profileEditorDirty);
      profilesChanged=initial||oldProfiles!==JSON.stringify(circuitProfiles);
    }

    raceTimeline=timelineBySession.get(selectedSessionId)?.events||[];
    selectedLapIds=retainSelectedIds(selectedLapIds,laps);

    if(initial||metadataChanged||selectedChanged){renderSessionQuery();renderSessionLibrary();}
    if(initial||pbChanged)renderPersonalBests();
    if(initial||metadataChanged||selectedLapsChanged||pbChanged||profilesChanged)populateSelectors();
    if(initial||metadataChanged||selectedLapsChanged||schemaChanged)renderCircuitStats();
    if(initial||settingsChanged)renderSettings();
    if(initial||selectedLapsChanged||profilesChanged||selectedChanged){if(!document.querySelector(".tag-input:focus"))renderSession();}
    if(sessionViewIsActive())void ensureSelectedTimeline();
    scheduleLibraryRefresh();libraryInitialized=true;
  }catch(error){if(!background)setRecorderFeedback(error.message);}
  finally{
    libraryRequestPending=false;
    if(libraryReloadRequested){libraryReloadRequested=false;void loadLibrary();}
  }
}
async function refreshLive(){
  if(document.hidden||liveRequestPending)return;
  liveRequestPending=true;
  try{const data=await request("/api/live");activeRecording=data.recording;diagnosticState=data.diagnostics||{};renderLive(data.latest);renderRecorder();}
  catch(_){$("connection").textContent="Dashboard offline";$("connection").classList.remove("online");}
  finally{liveRequestPending=false;}
}
polling=createPollingController({documentRef:document,onLive:refreshLive,onLibrary:loadLibrary});
events.on(document,"visibilitychange",()=>polling.refreshVisible());
loadLibrary();refreshLive();polling.startLive(1000);scheduleLibraryRefresh();
