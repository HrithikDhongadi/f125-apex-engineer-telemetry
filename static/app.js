const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtTime = (ms) => ms == null ? "—" : `${Math.floor(ms / 60000)}:${String(((ms % 60000) / 1000).toFixed(3)).padStart(6, "0")}`;
const fmtSetup = (s) => s ? `${s.front_wing??"unknown"}/${s.rear_wing??"unknown"} wings · ${s.on_throttle_diff??"unknown"}/${s.off_throttle_diff??"unknown"}% diff · ${setupFields.filter(f=>s[f.key]!==undefined).length}/${setupFields.length} fields` : "unknown";
const colors = {green:"#65e6ad",orange:"#ffad5a",blue:"#65b8ff",red:"#ff7379",yellow:"#ffd166"};
let sessions = [], laps = [], allLaps = [], personalBests = [], setupFields = [], selectedLapIds = new Set(), selectedSessionId = null, activeRecording = null, latestState = {}, comparison = null, editingLap = null;

async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
async function post(url, payload={}) { return request(url, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)}); }

document.querySelectorAll(".tabs button").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(item => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `view-${button.dataset.view}`));
  if (button.dataset.view === "analysis" && comparison) requestAnimationFrame(() => drawCharts(comparison));
}));

function chips(target, values) {
  target.classList.remove("empty");
  target.innerHTML = values.map(([label,value]) => `<span class="chip">${esc(label)}: <b>${esc(value)}</b></span>`).join("");
}
function renderLive(latest) {
  latestState = latest;
  $("connection").textContent = latest.connected ? "UDP connected" : "Waiting for UDP";
  $("connection").classList.toggle("online", Boolean(latest.connected));
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
  if(!recording) $("historical-badge").textContent="Historical/closed session · live telemetry remains current";
  else $("historical-badge").textContent="Historical view · live telemetry remains current";
}
function sessionLabel(session){return `${session.name} · ${session.mode.replace("_"," ")} · ${session.track_name} · ${session.lap_count} laps`;}
function renderSessionLibrary() {
  const previous=$("session-select").value;
  $("session-select").innerHTML=sessions.map(session=>`<option value="${esc(session.id)}">${esc(sessionLabel(session))}</option>`).join("");
  $("session-select").value=sessions.some(s=>s.id===selectedSessionId)?selectedSessionId:previous;
  $("session-cards").innerHTML=sessions.map(session=>{
    const date=session.started_at?new Date(session.started_at).toLocaleString():"Original v0.1/v0.2 files";
    return `<article class="session-card ${session.id===selectedSessionId?"selected":""}" data-session="${esc(session.id)}"><h3>${esc(session.name)}</h3><div class="session-meta">${esc(session.mode.replace("_"," "))} · ${esc(session.track_name)} · ${esc(session.status)}<br>${esc(date)} · ${session.lap_count} laps · best ${fmtTime(session.best_valid_lap_ms)}</div></article>`;
  }).join("");
  document.querySelectorAll(".session-card").forEach(card=>card.addEventListener("click",()=>selectSession(card.dataset.session)));
  const selected=sessions.find(s=>s.id===selectedSessionId);
  $("rename-session").value=selected?.read_only?"":selected?.name||"";
  $("rename-session").disabled=Boolean(!selected||selected.read_only);
  $("rename-button").disabled=Boolean(!selected||selected.read_only);
  $("track-override-row").hidden=Boolean(!selected||!selected.can_override_track_name||selected.read_only);
  $("track-override").value=selected?.track_name_override||"";
  renderRecorder();
}
async function selectSession(id){try{await post("/api/sessions/select",{session_id:id});selectedSessionId=id;selectedLapIds.clear();comparison=null;$("comparison").hidden=true;$("analysis-empty").hidden=false;await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}}

function selectorSources(){return [...sessions.map(s=>({value:`session:${s.id}`,label:sessionLabel(s)})),...personalBests.map(pb=>({value:`pb:${pb.key}`,label:`Stored PB · ${pb.track_name} · ${pb.mode.replace("_"," ")} · ${fmtTime(pb.time_ms)}`}))];}
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
function chosenMeta(side){const source=$(side+"-session").value;if(source.startsWith("pb:")){const pb=personalBests.find(item=>`pb:${item.key}`===source);return pb&&{track_id:pb.track_id,mode:pb.mode,session_id:pb.source_session_id};}const session=sessions.find(item=>`session:${item.id}`===source);return session&&{track_id:session.track_id,mode:session.mode,session_id:session.id};}
function checkCompatibility(){const b=chosenMeta("baseline"),c=chosenMeta("candidate");let message="";if(b&&c){if(b.track_id==null||c.track_id==null||b.track_id<0||c.track_id<0)message="Unknown track identity cannot be compared.";else if(b.track_id!==c.track_id)message=`Incompatible circuits: track ID ${b.track_id} versus ${c.track_id}.`;else if(b.mode!==c.mode)message=`Incompatible modes: ${b.mode.replace("_"," ")} versus ${c.mode.replace("_"," ")}.`;else if(b.mode==="race"&&b.session_id!==c.session_id)message="Race laps can only be compared within the same recording session.";}$("compare-error").textContent=message;$("compare").disabled=Boolean(message)||!$("baseline").value||!$("candidate").value;}
function sampleRate(lap){const seconds=(lap.last_session_time??0)-(lap.first_session_time??0);return seconds>0?`${(lap.sample_count/seconds).toFixed(1)} Hz`:"—";}
function renderSession(){
  const sorted=[...laps].sort((a,b)=>a.time_ms-b.time_ms);$("lap-count").textContent=`${laps.length} lap${laps.length===1?"":"s"}`;
  $("session-laps").innerHTML=sorted.map(lap=>`<tr class="${lap.invalid?"invalid-row":""}"><td><input type="checkbox" class="lap-export-check" data-id="${esc(lap.id)}" ${selectedLapIds.has(lap.id)?"checked":""} aria-label="Select lap ${lap.number} for export"></td><td>Lap ${lap.number}${lap.invalid?" · INVALID":""}</td><td class="time">${fmtTime(lap.time_ms)}</td><td>${fmtTime(lap.sector1_ms)} / ${fmtTime(lap.sector2_ms)}</td><td>${esc(fmtSetup(lap.setup))}</td><td>${lap.sample_count}<br><span class="hint">${sampleRate(lap)}</span></td><td>${lap.peak_speed_kph??"—"} / ${lap.minimum_speed_kph??"—"} km/h</td><td><input class="tag-input" data-id="${esc(lap.id)}" maxlength="240" value="${esc(lap.note)}" placeholder="e.g. rear wing +1"></td><td><button class="edit-setup" data-id="${esc(lap.id)}">Edit/Add setup</button></td></tr>`).join("")||'<tr><td colspan="9">No completed laps in this session.</td></tr>';
  document.querySelectorAll(".tag-input").forEach(input=>input.addEventListener("change",()=>saveNote(input)));
  document.querySelectorAll(".lap-export-check").forEach(input=>input.addEventListener("change",()=>{if(input.checked)selectedLapIds.add(input.dataset.id);else selectedLapIds.delete(input.dataset.id);updateExportCount();}));
  document.querySelectorAll(".edit-setup").forEach(button=>button.addEventListener("click",()=>openSetupEditor(button.dataset.id)));
  updateExportCount();
}
async function saveNote(input){input.disabled=true;try{const saved=await post(`/api/laps/${encodeURIComponent(input.dataset.id)}/note`,{note:input.value});for(const lap of allLaps)if(lap.id===saved.id)lap.note=saved.note;populateSelectors();}catch(error){alert(error.message);}finally{input.disabled=false;}}

async function openSetupEditor(lapId){
  const detail=await request(`/api/laps/${encodeURIComponent(lapId)}`);editingLap=detail;
  $("setup-title").textContent=`Edit historical setup · Lap ${detail.number} · ${fmtTime(detail.time_ms)}`;
  $("setup-fields").innerHTML=setupFields.map(field=>{const value=detail.setup?.[field.key],source=detail.setup?(detail.setup?._provenance?.[field.key]||(value!==undefined?"legacy_decoded_udp":null)):null;return `<div class="setup-field"><label for="setup-${esc(field.key)}">${esc(field.label)}${field.unit?` (${esc(field.unit)})`:""}</label><input id="setup-${esc(field.key)}" data-key="${esc(field.key)}" data-original="${value??""}" type="number" step="${field.type==="int"?"1":"any"}" min="${field.min}" max="${field.max}" value="${value??""}" placeholder="unknown"><small>${source==="manual"?"manually supplied":source?"decoded from UDP":"unknown"}</small></div>`;}).join("");
  $("setup-message").textContent="Only changed or newly entered fields will be marked manual.";$("setup-dialog").showModal();
}

function exportScope(){return document.querySelector('input[name="export-scope"]:checked').value;}
function updateExportCount(){const count=exportScope()==="session"?laps.length:selectedLapIds.size;$("export-count").textContent=`${count} lap${count===1?"":"s"} will be exported`;$("download-report").disabled=count===0;$("download-zip").disabled=count===0;}
function downloadExport(format){const scope=exportScope();if(scope==="selected"&&!selectedLapIds.size){$("export-error").textContent="Select at least one lap.";return;}$("export-error").textContent="";const query=new URLSearchParams({scope,format});if(scope==="selected")for(const id of selectedLapIds)query.append("lap",id);const link=document.createElement("a");link.href=`/api/sessions/${encodeURIComponent(selectedSessionId)}/export?${query}`;link.click();}

function renderPersonalBests(){
  $("pb-cards").innerHTML=personalBests.map(pb=>`<article class="pb-card"><span class="hint">${esc(pb.track_name)} · ${esc(pb.mode.replace("_"," "))}</span><strong>${fmtTime(pb.time_ms)}</strong><div class="hint">${esc(pb.source_session_name)} · ${new Date(pb.became_pb_at).toLocaleString()}<br>Setup ${esc(fmtSetup(pb.setup))}</div><button class="pb-open" data-key="${esc(pb.key)}">Open details</button><button class="pb-use" data-side="baseline" data-key="${esc(pb.key)}">Baseline</button><button class="pb-use" data-side="candidate" data-key="${esc(pb.key)}">Candidate</button></article>`).join("")||'<p class="empty">No eligible personal bests stored yet. Complete a valid full lap or rebuild from saved sessions.</p>';
  document.querySelectorAll(".pb-open").forEach(button=>button.addEventListener("click",()=>openPersonalBest(button.dataset.key)));
  document.querySelectorAll(".pb-use").forEach(button=>button.addEventListener("click",()=>{const side=button.dataset.side;$(side+"-session").value=`pb:${button.dataset.key}`;populateLapSelector(side);$("pb-message").textContent=`Personal best selected as ${side}.`;}));
}
async function openPersonalBest(key){try{const pb=await request(`/api/personal-bests/${encodeURIComponent(key)}`),details={...pb,lap:{...pb.lap,samples:`${pb.lap.samples.length} telemetry samples (available through this API)`}};$("pb-details").textContent=JSON.stringify(details,null,2);$("pb-details").hidden=false;}catch(error){$("pb-message").textContent=error.message;}}

function setupDifference(a,b){if(!a&&!b)return"Setup unknown";const changes=setupFields.filter(f=>(a||{})[f.key]!==undefined||(b||{})[f.key]!==undefined).filter(f=>(a||{})[f.key]!== (b||{})[f.key]).map(f=>`${f.label} ${(a||{})[f.key]??"unknown"}→${(b||{})[f.key]??"unknown"}${f.unit?` ${f.unit}`:""}`);return changes.join(" · ")||"No known setup change";}
function renderComparison(data){
  comparison=data;$("comparison").hidden=false;$("analysis-empty").hidden=true;const b=data.baseline,c=data.candidate,delta=data.summary.final_delta_s;
  $("comparison-summary").innerHTML=[["Baseline",`${fmtTime(b.time_ms)} · ${sampleRate(b)}`],["Candidate",`${fmtTime(c.time_ms)} · ${sampleRate(c)}`],["Final delta",`${delta>=0?"+":""}${delta.toFixed(3)} s`],["Setup change",setupDifference(b.setup,c.setup)],["Baseline sectors",`${fmtTime(b.sector1_ms)} / ${fmtTime(b.sector2_ms)}`],["Candidate sectors",`${fmtTime(c.sector1_ms)} / ${fmtTime(c.sector2_ms)}`],["Max gain / loss",`${data.summary.maximum_gain_s.toFixed(3)} / ${data.summary.maximum_loss_s.toFixed(3)} s`],["Validity","Both valid"]].map(([label,value])=>`<article class="summary-card"><span>${label}</span><strong>${esc(value)}</strong></article>`).join("");
  const legacy=data.alignment.baseline==="legacy-time"||data.alignment.candidate==="legacy-time",endpoint=data.endpoint_estimation;
  $("alignment-notice").textContent=(legacy?"Legacy approximation: time-normalized traces only; corner and metre analysis is disabled. ":"Distance-aligned from recorded lap distance. ")+`Finish-line estimate: ${endpoint.method} Corrections ${endpoint.baseline_adjustment_ms} / ${endpoint.candidate_adjustment_ms} ms.`;
  $("calibration").textContent=data.engineer_notes.calibration;
  $("engineer-notes").innerHTML=data.engineer_notes.windows.length?data.engineer_notes.windows.map(note=>`<article class="note"><h3>${esc(note.name)}</h3><div class="window">${note.start_pct.toFixed(0)}–${note.end_pct.toFixed(0)}% lap distance</div><ul>${note.statements.map(text=>`<li>${esc(text)}</li>`).join("")}</ul></article>`).join(""):'<p class="empty">No track-specific engineer notes for this comparison.</p>';
  drawCharts(data);
}
function pathFor(values,x,y){return values.map((value,i)=>`${i?"L":"M"}${x(i)} ${y(value)}`).join(" ");}
function chart(target,series,yMin,yMax,unit,decimals=0){
  const width=1000,height=280,left=58,right=16,top=16,bottom=38,plotW=width-left-right,plotH=height-top-bottom,count=series[0].values.length,x=i=>left+i/(count-1)*plotW,y=value=>top+(yMax-value)/Math.max(.001,yMax-yMin)*plotH,ticks=[0,.25,.5,.75,1];let svg=`<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"><g>`;
  ticks.forEach(t=>{const yy=top+t*plotH,val=yMax-t*(yMax-yMin);svg+=`<path class="axis" d="M${left} ${yy}H${width-right}"/><text class="axis-label" x="${left-8}" y="${yy+4}" text-anchor="end">${val.toFixed(decimals)}${unit}</text>`;});ticks.forEach(t=>{const xx=left+t*plotW;svg+=`<path class="axis" d="M${xx} ${top}V${height-bottom}"/><text class="axis-label" x="${xx}" y="${height-12}" text-anchor="middle">${t*100}%</text>`;});series.forEach(item=>svg+=`<path class="trace" stroke="${item.color}" d="${pathFor(item.values,x,y)}"/>`);svg+=`<line class="cursor" x1="${left}" x2="${left}" y1="${top}" y2="${height-bottom}" visibility="hidden"/><rect class="hit" x="${left}" y="${top}" width="${plotW}" height="${plotH}" fill="transparent"/></g></svg><div class="legend-row">${series.map(item=>`<span class="legend-key" style="--key:${item.color}">${esc(item.label)}</span>`).join("")}</div>`;target.innerHTML=svg;
  const hit=target.querySelector(".hit"),cursor=target.querySelector(".cursor"),tooltip=$("tooltip");hit.addEventListener("mousemove",event=>{const rect=hit.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),index=Math.round(ratio*(count-1)),cx=x(index);cursor.setAttribute("x1",cx);cursor.setAttribute("x2",cx);cursor.setAttribute("visibility","visible");tooltip.hidden=false;tooltip.style.left=`${Math.min(innerWidth-220,event.clientX+12)}px`;tooltip.style.top=`${event.clientY+12}px`;tooltip.innerHTML=`<b>${(index/(count-1)*100).toFixed(1)}% normalized lap</b><br>${series.map(item=>`${esc(item.label)}: ${Number(item.values[index]).toFixed(decimals)}${unit}`).join("<br>")}`;});hit.addEventListener("mouseleave",()=>{cursor.setAttribute("visibility","hidden");tooltip.hidden=true;});
}
function drawCharts(data){const trace=data.trace,delta=trace.map(row=>row.delta_s),speedsB=trace.map(row=>row.baseline.speed_kph),speedsC=trace.map(row=>row.candidate.speed_kph),deltaRange=Math.max(.1,...delta.map(Math.abs));chart(document.querySelector('[data-chart="delta"]'),[{label:"Candidate − baseline",color:colors.yellow,values:delta}],-deltaRange,deltaRange," s",2);chart(document.querySelector('[data-chart="speed"]'),[{label:"Baseline speed",color:colors.green,values:speedsB},{label:"Candidate speed",color:colors.orange,values:speedsC}],0,Math.ceil(Math.max(...speedsB,...speedsC)/50)*50," km/h");chart(document.querySelector('[data-chart="pedals"]'),[{label:"Baseline throttle",color:colors.green,values:trace.map(r=>r.baseline.throttle)},{label:"Candidate throttle",color:colors.orange,values:trace.map(r=>r.candidate.throttle)},{label:"Baseline brake",color:colors.blue,values:trace.map(r=>r.baseline.brake)},{label:"Candidate brake",color:colors.red,values:trace.map(r=>r.candidate.brake)}],0,100,"% ");}

$("compare").addEventListener("click",async()=>{$("compare-error").textContent="";try{renderComparison(await request(`/api/compare?baseline=${encodeURIComponent($("baseline").value)}&candidate=${encodeURIComponent($("candidate").value)}`));}catch(error){$("compare-error").textContent=error.message;}});
$("baseline-session").addEventListener("change",()=>populateLapSelector("baseline"));
$("candidate-session").addEventListener("change",()=>populateLapSelector("candidate"));
$("baseline").addEventListener("change",checkCompatibility);$("candidate").addEventListener("change",checkCompatibility);
$("session-select").addEventListener("change",event=>selectSession(event.target.value));
$("start-run").addEventListener("click",async()=>{try{await post("/api/runs/start",{name:$("run-name").value});$("run-name").value="";await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("stop-recording").addEventListener("click",async()=>{try{await post("/api/recording/stop");await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("jump-active").addEventListener("click",()=>activeRecording&&selectSession(activeRecording.id));
$("rename-button").addEventListener("click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/rename`,{name:$("rename-session").value});await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("track-override-button").addEventListener("click",async()=>{try{await post(`/api/sessions/${encodeURIComponent(selectedSessionId)}/track-name`,{name:$("track-override").value});await loadLibrary();}catch(error){$("recording-message").textContent=error.message;}});
$("select-all-laps").addEventListener("click",()=>{selectedLapIds=new Set(laps.map(lap=>lap.id));renderSession();});
$("clear-laps").addEventListener("click",()=>{selectedLapIds.clear();renderSession();});
document.querySelectorAll('input[name="export-scope"]').forEach(input=>input.addEventListener("change",updateExportCount));
$("download-report").addEventListener("click",()=>downloadExport("markdown"));
$("download-zip").addEventListener("click",()=>downloadExport("zip"));
$("rebuild-pbs").addEventListener("click",async()=>{try{const result=await post("/api/personal-bests/rebuild");$("pb-message").textContent=`Rebuild considered ${result.considered} laps; ${result.personal_best_count} PB records are stored.`;await loadLibrary();}catch(error){$("pb-message").textContent=error.message;}});
$("setup-close").addEventListener("click",()=>$("setup-dialog").close());
$("setup-form").addEventListener("submit",async event=>{event.preventDefault();const changed={};document.querySelectorAll("#setup-fields input").forEach(input=>{if(input.value!==input.dataset.original&&input.value!=="")changed[input.dataset.key]=Number(input.value);});if(!Object.keys(changed).length){$("setup-message").textContent="No values changed.";return;}try{const result=await post(`/api/laps/${encodeURIComponent(editingLap.id)}/setup`,{setup:changed});$("setup-message").textContent=`Saved. ${result.pb_message}. Audit: ${result.audit_record}`;setTimeout(()=>$("setup-dialog").close(),900);await loadLibrary();}catch(error){$("setup-message").textContent=error.message;}});

async function loadLibrary(){
  const [data,pbData,schema]=await Promise.all([request("/api/sessions"),request("/api/personal-bests"),request("/api/setup-schema")]);sessions=data.sessions;personalBests=pbData.personal_bests;setupFields=schema.fields;selectedSessionId=data.selected_session_id;activeRecording=data.recording;latestState={...latestState,...data.latest};
  const groups=await Promise.all(sessions.map(session=>request(`/api/sessions/${encodeURIComponent(session.id)}/laps`)));allLaps=groups.flatMap(group=>group.laps);laps=groups.find(group=>group.session_id===selectedSessionId)?.laps||[];
  selectedLapIds=new Set([...selectedLapIds].filter(id=>laps.some(lap=>lap.id===id)));renderSessionLibrary();renderPersonalBests();populateSelectors();if(!document.querySelector(".tag-input:focus"))renderSession();
}
async function refreshLive(){try{const data=await request("/api/snapshot");activeRecording=data.recording;selectedSessionId=data.selected_session_id;renderLive(data.latest);renderRecorder();}catch(_){$("connection").textContent="Dashboard offline";$("connection").classList.remove("online");}}
loadLibrary();refreshLive();setInterval(refreshLive,1000);setInterval(loadLibrary,8000);
