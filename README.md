# Apex Engineer v0.4

A local-first F1 25 telemetry and lap-analysis tool for repeatable setup development. The runtime server uses only the Python standard library; the interactive Circuit Profiler is built with Konva and bundled for the browser. Telemetry and notes remain on the receiving computer.

## Run

```bash
cd ~/work/f125-apex-engineer-telemetry
python3 -m src.f1telemetry
```

Open [http://127.0.0.1:8025](http://127.0.0.1:8025). The dashboard binds to `127.0.0.1` by default; select local-network dashboard access in Settings only when another device needs to open the web interface. Independently, the UDP receiver listens on `0.0.0.0:20777` by default so the game can send telemetry from another device.

Each launch creates `logs/server-YYYY-MM-DD_HH-MM-SS.log`. Console and file entries use `YYYY-MM-DD HH:MM:SS` timestamps and include startup, HTTP requests, client disconnects and unexpected request errors. Runtime `.log` files are ignored by Git.

The **Settings** page stores configuration in `data/settings.json`. It controls dashboard binding/port, UDP listening port, deleted-session retention, recording size cap, diagnostic file size and library refresh rate. Host and port changes require a server restart; the page says exactly which saved fields are waiting for restart. Other settings apply immediately or to the next recording.

The generated browser bundle is included, so Node is not required to run the dashboard. After changing `frontend/circuit-profiler.js` or updating Konva, rebuild it with:

```bash
npm install
npm run build:frontend
```

If port `8025` is already in use, an earlier copy is probably running. Open the existing dashboard or find it with `lsof -nP -iTCP:8025 -sTCP:LISTEN`, stop that PID, and restart.

## F1 25 UDP settings

| Setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP IP Address | Receiving computer's LAN IPv4 address (`127.0.0.1` on the same computer) |
| UDP Port | `20777` |
| UDP Send Rate | `60 Hz` recommended |
| UDP Format | `2025` |
| UDP Broadcast Mode | Off |

The decoder follows EA's **F1 25 UDP specification v3**, validates format `2025`, and checks the packet-specific version before applying offsets. Unsupported versions are ignored rather than decoded with guessed layouts. Session type `18` is treated as Time Trial; types `15–17` are races. Track identity comes from the game rather than being assumed. The complete documented track-ID appendix is built in: Silverstone is ID `7`, while its reverse layout is the independent ID `39` (Austria/Zandvoort reverse are `40`/`41`). Unrecognised IDs appear as `Unknown track (ID n)`.

The continuous store decodes Motion (0), Session (1), Lap Data (2), Event (3), Participants (4), Setups (5), Car Telemetry (6), Car Status (7), Final Classification (8), Car Damage (10), Session History (11), Tyre Sets (12), Motion Ex (13), and Lap Positions (15). Every stored row retains the session UID, packet ID/version, both frame identifiers, session/receive time, player index, and available lap distance. Same-frame rows can be joined by overall frame ID; slower packets carry their own timestamp and freshness interval.

Packet ID `5` is decoded using the complete packed 50-byte `CarSetupData` stride. Setup snapshots include aero, both differential settings, signed camber/toe floats, suspension, anti-roll bars, ride heights, brakes, engine braking, the four configured tyre pressures with explicit wheel names, ballast, and fuel load. These pressures are setup values and are never substituted with measured Car Telemetry pressures. Older four-field files remain valid; all absent settings display as `unknown`.

## Recording workflows

### Time Trial

1. Enter Time Trial and wait for **Time trial** and the track name to appear.
2. Enter a short run name such as `18/18 baseline` or `rear wing +1`.
3. Select **Start new run**.
4. If the car is already partway around the lap, the run shows **Armed — waiting for the next start/finish crossing; the partial approach is excluded.** At that crossing, capture begins using the lap number reported by the game. The dashboard then shows **Recording lap n**.
5. Select **Stop recording** when the run is finished. Completed laps remain saved; only the current incomplete lap is discarded.
6. Start another named run under the same in-game Time Trial session whenever required.

**Restart Lap and flashbacks:** a mid-lap flashback no longer deletes the lap. Samples from the superseded future branch are pruned, recording continues from the rewound point, and the saved lap carries a flashback event plus the validity reported by the game. This keeps invalid Time Trial laps and permits race laps when the game allows them without duplicating distance in analysis. Restart Lap discards only the abandoned partial samples and begins a clean capture at the reset/crossing under the lap number reported by the game. F1 25 may relocate the car to an untimed approach where lap distance increases but total distance rewinds; that relocation is treated as Restart Lap rather than a completed lap. A run started before the first Lap Data packet remains armed until a verified start/finish crossing.

**Why the first saved number can look skipped:** the game owns lap numbering; Apex Engineer never renumbers laps. For example, if a run is started just before the line while the HUD says lap 19, that lap-19 approach is only a partial lap and is excluded. When the HUD changes to lap 20 at the line, the recorder begins capturing lap 20 and saves it at the following crossing. Earlier laps may belong to another recording, so the new session correctly begins with a `lap-...-20.json` file and does not create a placeholder lap 19.

F1 25 can also label an untimed approach with the *upcoming* lap number while holding its current-lap timer at zero. In the captured Silverstone reproduction, lap 31 stayed unchanged across the line while lap distance wrapped from 5,888.52 m to 1.32 m and total distance advanced by 3.47 m. Apex Engineer treats that continuous forward wrap as the verified start of lap 31. A distance rewind or flashback does not qualify because total distance must remain continuous across the line.

### Opt-in lap-boundary diagnostics

For an unexplained missing first lap, enable **Capture lap-boundary diagnostics** before selecting **Start new run**. Drive at least two complete laps and stop the recording. Player Lap Data and every recorder decision are written separately under that session's `diagnostics/` directory as bounded JSON Lines files; lap JSON, PBs, and reports are unaffected. Each entry includes reception timestamp, game session UID/time, both frame identifiers, current and previous lap values, track length, recorder status before/after, active/captured lap number, sample count, decision flags, and explicit accept/reject/discard/finish reasons.

The initial segment captures the first few laps; up to seven additional rewind/Restart Lap segments are retained when those events occur. Each segment is capped at 50,000 events and 50 MiB. Diagnostics are deliberately opt-in because they perform a small append write for every player Lap Data packet. Disable the checkbox after the short reproduction run.

### Race

Race types start a recording automatically. The first lap is captured directly from the starting grid when the initial packet identifies lap 1 at the beginning of game session time; grid position may place the car beyond 0 m and does not cause lap 1 to be omitted. A recording closes when the game session ends or its session UID changes. **Stop recording** can close it manually; the same game UID will not automatically reopen afterward. A new game session can start another recording.

At the finish, F1 25 can briefly report another session UID and then send one final packet bundle under the completed race UID. Apex Engineer guards that completed UID for five seconds so the trailing bundle cannot create an empty duplicate race; a genuinely new UID remains eligible for immediate automatic recording.

Race packet data is committed continuously to `race-data.sqlite3` in WAL mode. Grid time, incomplete laps, pit-lane running, safety-car periods and the finish therefore survive without waiting for a completed lap. On restart, an open `session.json` is marked `interrupted`, while the already committed database remains readable. The database is capped at 8 GiB per recording so a 60 Hz full race has room while disk growth remains bounded. Continuity is calculated separately for Motion, Lap Data, Car Telemetry, Car Status and Motion Ex; lower-rate streams are excluded and gaps shared by several streams are never added together. Each stream reports received/unique frames, duplicates, out-of-order frames, gap events, largest gaps and effective receive rate. A game-ID/session-time jump with no matching recorder wall-time gap is labelled `sequence_discontinuity`, not UDP loss; only wall-clock-supported gaps are labelled `capture_loss`.

F1 session UIDs are unsigned 64-bit values. Values above SQLite's signed integer maximum are stored using their equivalent signed 64-bit bit pattern; packet header JSON and exports retain the exact original unsigned value. Unexpected decoding or persistence errors are shown in the dashboard and contained so one packet cannot silently terminate the UDP receiver.

The timeline preserves direct game events (lights, lights out, penalty notifications, overtakes, safety car, red flag, flashback, chequered flag and final classification) and labels derived changes such as pit entry/exit, position, tyre compound, weather and discrete damage as `inferred`. Reconstruction follows packet arrival/overall-frame order rather than sorting rewound session timestamps. F1 25 `FLBK` targets create replacement branches; raw events remain stored, while report/API rows identify `timeline_branch_id`, `event_validity` (`accepted`, `superseded`, or `unknown`), `superseded_by`, `flashback_reference`, and `validation_reason`. `overallFrameIdentifier` is the stable receive chronology because the game specification says it does not rewind after flashbacks.

Vehicle-indexed events are classified against the packet's player-car index. Player `OVTK` notifications are reconciled against surrounding all-car Lap Data and separately reported as raw notifications, confirmed manoeuvres and net race position. Repeated accepted passes remain distinct. F1 25 penalty type 5 is a warning, not an applied penalty; reports separate warning/infringement events from time, drive-through, stop-go and other actual penalty types and cross-check lap warning/penalty counters. Collision messages are retained individually and conservatively grouped into incidents only when participant pair, branch, time and location support it. The dashboard hides superseded flashback-branch events by default; **Show superseded branches** reveals them with their branch and validation reason. Other-car events stay in SQLite and full exports but are hidden from lap attribution and the dashboard unless **Show other cars** is enabled. Continuous tyre wear/damage channels remain engineering data and no longer flood the event timeline. The game's `BUTN` event is shown as **Controller input**; its 32-bit button-status mask is retained, while these noisy events are hidden by default and can be enabled with **Show controller inputs**. Completed lap JSON also snapshots available start/finish race context: fuel, compound/age, ERS, position/gaps, pit state, safety car/weather and damage. Restricted opponent channels remain absent/zero exactly as sent; Apex Engineer does not reconstruct private fuel, setup, damage or tyre data.

Each completed lap retains a frozen setup snapshot. During only the first 1.5 seconds of a captured lap, newer Car Setups packets may replace the provisional snapshot; this prevents an old pre-grid packet becoming Lap 1's setup. Historical reports also audit the stored Packet 5 history: provisional values remain visible, superseded values are labelled, and a later player setup is `confirmed_active` only after repeated identical packets. The original lap JSON is never rewritten during report reprocessing. Setup fuel is the configured starting fuel from Packet 5, not instantaneous fuel remaining; measured fuel comes from Car Status. Zero wing values are retained exactly when the game transmits zero and are not guessed or replaced; persistent 0/0 readings are labelled unverified unless corroborated externally.

## Session library and historical data

The displayed session controls the **Session** table and the default Lap Analysis choices. Selecting an old session is read-only inspection and never resumes it. Live telemetry remains live while historical data is displayed. Use **Jump to active session** to return to the current recording.

Saved sessions are collapsed on initial load so a large archive does not crowd the dashboard. Use the shared **Year**, **Month**, and **Date** query above the pages to reveal them: choosing only a year shows that whole year, adding a month narrows it to that month, and adding a date narrows it to that local calendar day. The displayed session remains available while the archive is collapsed. The same query limits the saved-session choices in Lap Analysis; stored PB choices remain available independently. **Legacy / no date** exposes older captures that do not have a session start timestamp. Historical session cards only check whether continuous race data exists, so opening the library never scans every large packet database. Detailed per-stream continuity is calculated when a report is explicitly exported.

Sessions can be renamed without changing their IDs or lap files. A closed session can also be deleted from the library; this atomically moves its complete directory to `data/trash/sessions/` rather than immediately erasing it. Active recordings and Legacy captures are protected. **Settings → Deleted sessions** shows the deletion and scheduled permanent-purge time and can restore a session without restarting the server. The default retention is 30 days; set it to `0` to keep trash until manually restored. Expired trash is permanently removed on startup and by the server's minute-by-minute expiry check. Independent PB snapshots and entries in `data/notes.json` remain preserved even after session expiry. If an ID is absent from the official appendix, a separate circuit display-name override is available; it does not alter the numeric identity, PB key, or compatibility rules. A recording open during application shutdown or a crash is marked `interrupted` when loaded again.

The Session page shows the circuit profile currently assigned to the selected session and links directly to the Circuit Profiler. Loading a saved profile restores its stored centreline and turn definitions for further editing. Deleting a user-created profile moves it to `data/trash/circuit_profiles/` and clears session references to it; the built-in Silverstone seed cannot be deleted.

Storage layout:

```text
data/
  lap-*.json                         Existing v0.1/v0.2 files, unchanged
  notes.json                         Per-lap notes, when used
  personal_bests/
    track-<id>__<mode>.json          Atomic full-fidelity winning-lap copies
  audit/setup-amendments/<lap>/<id>/ Exact original lap/PB and amendment manifest
  sessions/
    session-<timestamp>-<suffix>/
      session.json                   Name, mode, UID, track, status, lap IDs
      lap-*.json                     Completed laps from this recording
      race-data.sqlite3              Incremental packet stream and event timeline
  trash/sessions/                    Recoverable directories removed from the library
  circuit_profiles/
    f1-2025-track-<id>-<layout>-v1.json  Editable circuit/turn calibration
```

Root-level captures appear as the read-only **Legacy captures** group. They are never moved, renamed, overwritten, or deleted.

## Engineering reports and raw export

In the **Session** view, use the checkbox beside each lap or **Select all/Clear**, choose either **Selected laps** or **Whole session**, then select a report format. **Automatic for session** chooses Race or Time Trial from the recorded game mode.

- **Race engineer report** includes an executive result overview, lap table, inferred stints, final-classification packet when available, key-event summary, and a detailed sheet for every lap. Each sheet covers official sectors, validity/coverage, position and gaps, penalties/warnings, pit state, weather/safety car, compound/age, four-wheel tyre wear and per-lap degradation, measured inner/surface temperatures, measured running pressures, tyre damage/blisters, fuel use, ERS, brake temperatures, car damage/faults, inputs, setup provenance and lap events.
- **Time Trial engineer report** emphasizes best lap, consistency, validity, setup development, lap deltas, circuit-profile turn evidence and the same detailed tyre/temperature/input sheets.
- **Lap Analysis report** can be created for selected session laps or downloaded directly after comparing a baseline and candidate in **Lap Analysis**. The direct comparison report includes official/sector deltas, setup comparison, endpoint/alignment quality, circuit-profile turn-window evidence, and a full engineering sheet for each lap.
- **Download full data ZIP** contains that report, scoped session metadata, the selected circuit profile/provenance, byte-for-byte copies of the original included lap JSON files, and the relevant event timeline. A whole-session ZIP also includes a consistent online-backup snapshot of the continuous SQLite database, including committed WAL rows. A selected-lap ZIP includes only the selected lap JSON plus `selected-packets.jsonl`, bounded to the selected laps' recorded session-time range with a one-second margin for slower state packets.

Configured setup tyre pressures and measured running pressures are deliberately separate. Configured starting fuel and instantaneous measured fuel are also labelled separately. Wheel data always uses the UDP order rear-left, rear-right, front-left, front-right and prints those names explicitly. Invalid laps may be exported for engineering review and are labelled `INVALID (game flag)`. Reports label missing and estimated values and do not claim that a setup change caused a time change. Export generation is read-only and does not rewrite the source session.

Reports explicitly separate recorded official lap/result fields, raw UDP notifications, accepted-timeline reconstruction, and inferred engineering observations. Processing-version lines identify the decoder, timeline reconstruction, event attribution, continuity analysis and report format used. Regenerate a historical report from immutable stored data with:

```bash
python3 scripts/reprocess_session.py session-20261008T090243-5dd3a5b6 data/reports/spa-race-corrected.md --report-type race
```

The regenerated timeline JSON in a full ZIP retains packet/frame provenance and discarded-branch evidence; the readable Markdown does not dump repetitive raw events.

Use **Edit/Add setup** on a saved lap to enter a historical setup from the game or screenshots. Existing values are prepopulated; only changed/new fields are marked `manual`, while captured fields retain UDP provenance. Values are type/range checked. Before an amendment, exact originals of the source lap and any matching PB snapshot are placed in `data/audit/setup-amendments/`; then the lap and PB are updated with atomic replacement. Lap samples, official times, validity, and session identity are not edited. If no PB exists, the lap is corrected immediately and a later PB rebuild will copy it. Missing fields stay unknown—no balanced defaults are generated.

## Personal best registry

Every newly completed lap is evaluated automatically. A PB update requires:

- a valid official lap of at least 30 seconds;
- a known track ID and compatible `race` or `time_trial` mode;
- at least 30 telemetry samples;
- real lap-distance data covering at least 85% of the track, starting and ending near the timing line.

Slower, invalid, partial, legacy-distance, and unknown-track laps cannot replace a PB. Keys include actual track ID and mode, so race and Time Trial records are not mixed.

Each PB file is an independent full copy of the winning lap: all telemetry samples and units, official times and sectors, setup snapshot, source IDs/session name, note/provenance, data-quality result, and the date it became PB. Updates use a temporary file plus atomic replacement and never mutate the source lap. Use **Rebuild from saved sessions** deliberately to consider historical session laps. A stored compatible PB can be selected independently as either baseline or candidate.

Back up `data/personal_bests/` together with `data/sessions/`. PB copies remain usable for comparison even if their original session is later unavailable.

## Lap comparison rules

Delta is always `candidate − baseline`; negative means the candidate is faster.

Baseline and candidate each have their own session/PB selector followed by a lap-number selector. Lap options show official time and validity. Selections survive the periodic dashboard refresh, and incompatibility is explained before the comparison request.

- Race laps compare freely within one race recording. Cross-session race comparison is enabled only when both sessions select the same manually `verified` circuit profile; race-context flags remain visible so fuel, tyres, traffic, weather, damage, pit or safety-car effects are not mislabelled as pure pace.
- Time Trial laps from separate runs may be compared when both have the same known track.
- Race and Time Trial laps on the same numeric track ID may be compared for exploratory analysis. The dashboard and report warn that fuel, tyres, grip, traffic and rules make this non-like-for-like evidence rather than a pure pace claim.
- Game-invalid laps may be compared and remain explicitly labelled invalid. Their traces are diagnostic evidence only and never valid-lap or PB evidence.
- A comparison still rejects different/unknown tracks, mixed distance versus legacy-time alignment, and recorded distance coverage below 80%, preventing a partial capture from being normalized into a misleading full-lap trace.
- Legacy captures may be compared with other legacy captures, but their missing lap distance means the trace is a time-normalized approximation.
- Turn notes appear only when both laps have real distance data and a compatible circuit profile with turn boundaries is selected. A profile-level `verified` label does not override individual turn states: unverified/unnamed turns and overlapping windows are flagged, and their analysis remains exploratory. Overlapping window deltas are never additive.

The **Where the lap time was won** map uses the comparison's selected circuit-profile centreline and profile apex labels. Teal sections mean the candidate is gaining time locally, red sections mean the baseline is gaining, and grey sections are within the small neutral threshold. This is calculated from the local change in cumulative delta, not from which car is ahead at that point. If an older profile has turn data but no stored centreline, the map falls back to the baseline lap's recorded Motion X/Z trace and says so visibly. Game-world X is mirrored for presentation so familiar layouts match conventional broadcast/circuit-map orientation; raw Motion coordinates and saved profiles are not changed. It uses the same Konva interaction model as Circuit Profiler: drag anywhere on the canvas or use arrow keys to pan, wheel or `+`/`-` to zoom, Shift+wheel or `Q`/`E` to rotate, and `0` to reset. Lines and markers retain fixed screen width while transforming.

The final UDP sample usually occurs just before the timing line. To avoid a one-point delta spike, the small difference between sampled progression and each official lap time is distributed linearly across the trace. The API and UI report the applied correction.

The bundled normal-Silverstone profile is only a migration of the earlier normalized windows and is visibly labelled `approximate`; it is not calibrated metre mapping. Every other circuit starts with full-lap/sector comparison and no invented turns. Reverse layouts have distinct profile identities and can never borrow the normal-layout profile silently.

## Circuit profile calibration

Profiles are keyed by UDP format, numeric track ID, layout and schema version. They store the game-measured length, optional X/Z centreline, sector boundaries, editable turns (number/name, entry, estimated apex, exit, direction and linked complex), provenance and verification state.

1. Record a clean, complete lap with Motion packets enabled. Do not use a pit lap, incomplete/invalid lap, flashback or Restart Lap branch.
2. Open **Circuit Profiler**, choose the calibration lap from the displayed session, and select **Load distance map**. The trace uses the game's X/Z coordinates and lap distance. Orange curvature candidates can be added as unverified draft turns; they are suggestions only.
3. To map a turn manually, enter its number, optional name and a margin for each side, select **Point apex on map**, then click only the turn's apex/midpoint. The click snaps to the nearest recorded lap distance; entry and exit are calculated from the chosen margin. Click an existing turn marker or card to select it: its entry-to-exit trace uses that turn's accent color and two small ring handles mark the margin boundaries. Drag the apex marker to refine it while keeping the margin. The **Turn display** menu offers minimal markers, labelled apex dots, or distinct colored ranges for every turn. Drag anywhere on the canvas to pan; the same arrow, zoom, rotate and reset shortcuts listed above work here, including while pointing a turn. Track strokes, labels and handles retain a usable screen size while zooming. The completed turn remains editable and unverified until you review it. Draft edits survive normal dashboard refreshes.
4. Selecting a marker opens only that turn's detail card. Use **Show all turns** when you need the complete card list, then return to **Show selected only** to keep the editor compact. Adjust each turn through its apex distance and margin-per-side controls; the stored entry and exit boundaries are calculated automatically. Edit names, direction and linked complex where needed, then save and select the profile. Use **Create alternate** when identification or calibration is uncertain; it creates a separately keyed profile and never changes track/PB identity.
5. Verify the result in game against several clean laps in both dry and wet conditions. FIA diagrams may guide turn numbers/layout only; they are not treated as F1 25 metre calibration. No third-party map graphic is bundled.

## F1 25 telemetry setup

The Settings page displays the current UDP port and detected receiver-computer addresses. In F1 25, open the main Options menu, then **Settings → UDP Telemetry Settings** and use:

- UDP Telemetry: On
- UDP Send Rate: 60 Hz
- UDP Format: 2025
- UDP Port: the port displayed by Apex Engineer (default `20777`)
- Same computer IP: `127.0.0.1`
- Console/different computer IP: one of the LAN addresses displayed by Apex Engineer
- UDP Broadcast Mode: Off when using the displayed direct IP; broadcast mode may be used when intentionally sending to the whole subnet
- Your Telemetry: Restricted is sufficient for your own car; F1 25 always exposes the driven car to its own UDP receiver

The game device and receiver must share a network, and the receiver computer's firewall must allow inbound UDP on the configured port. These controls and broadcast semantics follow EA's [official F1 25 UDP specification](https://forums.ea.com/t5/s/tghpe58374/attachments/tghpe58374/f1-games-game-info-hub-en/61/4/Data%20Output%20from%20F1%2025%20v3.pdf).

Turn comparison reports braking threshold, estimated apex/minimum speed, throttle pickup, exit speed and time gain/loss when the channels exist. Motion Ex wheel slip, front-wheel angle and chassis yaw are captured for future overlays. Labels avoid diagnosing understeer/oversteer from steering alone.

## API

```text
GET  /api/snapshot
GET  /api/sessions
GET  /api/sessions/<session_id>/laps
GET  /api/sessions/<session_id>/export?scope=session&format=markdown
GET  /api/sessions/<session_id>/export?scope=selected&format=zip&lap=<lap_id>
POST /api/sessions/select               {"session_id":"..."}
POST /api/sessions/<session_id>/rename  {"name":"rear wing +1"}
POST /api/sessions/<session_id>/delete  {"confirm":true}
POST /api/sessions/<session_id>/track-name {"name":"Local circuit"}
POST /api/runs/start                     {"name":"18/18 baseline"}
POST /api/recording/stop
POST /api/diagnostics                    {"enabled":true}
GET  /api/laps[?session=<session_id>]
GET  /api/laps/<lap_id>
POST /api/laps/<lap_id>/note             {"note":"Stowe test"}
GET  /api/setup-schema
GET  /api/settings
POST /api/settings                       {settings fields}
POST /api/trash/sessions/<id>/restore   {"confirm":true}
GET  /api/circuit-profiles[?session=<session_id>]
POST /api/circuit-profiles               {profile JSON}
POST /api/circuit-profiles/<id>/delete   {"confirm":true}
POST /api/sessions/<id>/circuit-profile {"profile_id":"..."}
GET  /api/laps/<lap_id>/trace
GET  /api/sessions/<id>/timeline[?lap_from=n&lap_to=n]
POST /api/laps/<lap_id>/setup            {"setup":{"front_camber":-3.5}}
GET  /api/compare?baseline=<id>&candidate=<id>
GET  /api/personal-bests
GET  /api/personal-bests/<track-mode-key>
POST /api/personal-bests/rebuild
```

Invalid state changes, incompatible comparisons, and unknown IDs return JSON errors. Session deletion requires explicit confirmation and is recoverable local trash, not permanent erasure.

## Checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/f1telemetry/*.py
npm run check:frontend
```

## Current limitations

- Race-strategy prediction is not implemented.
- Only normal Silverstone has a bundled turn seed, and that seed is approximate. All other normal/reverse circuits need a clean Motion X/Z calibration lap and manually verified turn distances/names before turn-level claims appear.
- The next validation captures needed are: one clean 60 Hz race from grid to classification with at least one pit stop; one safety-car or VSC race; one race flashback where the game clears validity; and one clean lap for each layout to calibrate. Keep packet IDs 0–15 enabled via format 2025 and retain the resulting `race-data.sqlite3`.
- UDP is lossy and opponent telemetry may be deliberately restricted by the game. Missing channels are shown as unavailable; frame gaps are classified evidence, not reconstructed samples, and `unknown_gap` remains possible.
- Restart Lap and flashback transitions retain the existing synthetic and live-sequence regressions. Additional real captures on non-Silverstone layouts remain valuable for validating boundary ordering.
