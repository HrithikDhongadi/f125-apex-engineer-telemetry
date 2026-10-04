# Apex Engineer v0.4

A local-first F1 25 telemetry and lap-analysis tool for repeatable setup development. It uses only the Python standard library and vanilla HTML/CSS/JavaScript. Telemetry and notes remain on the receiving computer.

## Run

```bash
cd ~/work/f125-apex-engineer-telemetry
python3 -m src.f1telemetry
```

Open [http://127.0.0.1:8025](http://127.0.0.1:8025). The dashboard now binds only to localhost by default; the UDP receiver remains reachable on `0.0.0.0:20777` so the game can send from another machine.

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

Race packet data is committed continuously to `race-data.sqlite3` in WAL mode. Grid time, incomplete laps, pit-lane running, safety-car periods and the finish therefore survive without waiting for a completed lap. On restart, an open `session.json` is marked `interrupted`, while the already committed database remains readable. The database is capped at 8 GiB per recording so a 60 Hz full race has room while disk growth remains bounded; its dashboard summary reports stored rows, estimated missing high-rate frames, dropped rows and bytes. The estimate is explicitly not proof of network loss for slow packet types.

The timeline preserves direct game events (lights, lights out, penalties, overtakes, safety car, red flag, flashback, chequered flag and final classification) and labels derived changes such as pit entry/exit, position, tyre compound, weather and damage as `inferred`. Completed lap JSON also snapshots available start/finish race context: fuel, compound/age, ERS, position/gaps, pit state, safety car/weather and damage. Restricted opponent channels remain absent/zero exactly as sent; Apex Engineer does not reconstruct private fuel, setup, damage or tyre data.

Changing setup during a lap does not relabel that lap: each completed lap retains the setup seen at its start.

## Session library and historical data

The displayed session controls the **Session** table and the default Lap Analysis choices. Selecting an old session is read-only inspection and never resumes it. Live telemetry remains live while historical data is displayed. Use **Jump to active session** to return to the current recording.

Sessions can be renamed without changing their IDs or lap files. If an ID is absent from the official appendix, a separate circuit display-name override is available; it does not alter the numeric identity, PB key, or compatibility rules. Per-lap notes continue to use `data/notes.json`. A recording open during application shutdown or a crash is marked `interrupted` when loaded again.

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
  circuit_profiles/
    f1-2025-track-<id>-<layout>-v1.json  Editable circuit/turn calibration
```

Root-level captures appear as the read-only **Legacy captures** group. They are never moved, renamed, overwritten, or deleted.

## Race engineer reports and raw export

In the **Session** view, use the checkbox beside each lap or **Select all/Clear**, then choose either **Selected laps** or **Whole session**. The dashboard shows the expected lap count before downloading.

- **Download Markdown** creates a compact report containing session identity, lap validity, official lap/sectors, available setup values, notes, sample coverage, trace-derived speed/braking/throttle observations, and compatible official-time deltas.
- **Download full data ZIP** contains that report, scoped session metadata, the selected circuit profile/provenance, byte-for-byte copies of the original included lap JSON files, and the relevant event timeline. A whole-session ZIP also includes a consistent online-backup snapshot of the continuous SQLite database, including committed WAL rows. A selected-lap ZIP includes only the selected lap JSON plus `selected-packets.jsonl`, bounded to the selected laps' recorded session-time range with a one-second margin for slower state packets.

Invalid laps may be exported for setup review and are labelled `INVALID`. Reports label missing and estimated values and do not claim that a setup change caused a time change. Export generation is read-only and does not rewrite the source session.

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

- Valid race laps compare freely within one race recording. Cross-session race comparison is enabled only when both sessions select the same manually `verified` circuit profile; race-context flags remain visible so fuel, tyres, traffic, weather, damage, pit or safety-car effects are not mislabelled as pure pace.
- Time Trial laps from separate runs may be compared when both have the same known track.
- Race and Time Trial laps cannot be compared.
- Legacy captures may be compared with other legacy captures, but their missing lap distance means the trace is a time-normalized approximation.
- Turn notes appear only when both laps have real distance data and a compatible circuit profile with turn boundaries is selected.

The final UDP sample usually occurs just before the timing line. To avoid a one-point delta spike, the small difference between sampled progression and each official lap time is distributed linearly across the trace. The API and UI report the applied correction.

The bundled normal-Silverstone profile is only a migration of the earlier normalized windows and is visibly labelled `approximate`; it is not calibrated metre mapping. Every other circuit starts with full-lap/sector comparison and no invented turns. Reverse layouts have distinct profile identities and can never borrow the normal-layout profile silently.

## Circuit profile calibration

Profiles are keyed by UDP format, numeric track ID, layout and schema version. They store the game-measured length, optional X/Z centreline, sector boundaries, editable turns (number/name, entry, estimated apex, exit, direction and linked complex), provenance and verification state.

1. Record a clean, complete lap with Motion packets enabled. Do not use a pit lap, incomplete/invalid lap, flashback or Restart Lap branch.
2. In **Session → Circuit profile**, choose the calibration lap and select **Load distance map**. The trace uses the game's X/Z coordinates and lap distance. Orange curvature candidates can be added as unverified draft turns; they are suggestions only.
3. Add or adjust entry/apex/exit distances and names. Record whether a linked sequence is one complex, then save and select the profile. Use **Create alternate** when identification or calibration is uncertain; it creates a separately keyed profile and never changes track/PB identity.
4. Verify the result in game against several clean laps in both dry and wet conditions. FIA diagrams may guide turn numbers/layout only; they are not treated as F1 25 metre calibration. No third-party map graphic is bundled.

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
POST /api/sessions/<session_id>/track-name {"name":"Local circuit"}
POST /api/runs/start                     {"name":"18/18 baseline"}
POST /api/recording/stop
POST /api/diagnostics                    {"enabled":true}
GET  /api/laps[?session=<session_id>]
GET  /api/laps/<lap_id>
POST /api/laps/<lap_id>/note             {"note":"Stowe test"}
GET  /api/setup-schema
GET  /api/circuit-profiles[?session=<session_id>]
POST /api/circuit-profiles               {profile JSON}
POST /api/sessions/<id>/circuit-profile {"profile_id":"..."}
GET  /api/laps/<lap_id>/trace
GET  /api/sessions/<id>/timeline[?lap_from=n&lap_to=n]
POST /api/laps/<lap_id>/setup            {"setup":{"front_camber":-3.5}}
GET  /api/compare?baseline=<id>&candidate=<id>
GET  /api/personal-bests
GET  /api/personal-bests/<track-mode-key>
POST /api/personal-bests/rebuild
```

Invalid state changes, incompatible comparisons, and unknown IDs return JSON errors. There are deliberately no deletion endpoints or dashboard deletion controls.

## Checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/f1telemetry/*.py
```

## Current limitations

- Race-strategy prediction is not implemented.
- Only normal Silverstone has a bundled turn seed, and that seed is approximate. All other normal/reverse circuits need a clean Motion X/Z calibration lap and manually verified turn distances/names before turn-level claims appear.
- The next validation captures needed are: one clean 60 Hz race from grid to classification with at least one pit stop; one safety-car or VSC race; one race flashback where the game clears validity; and one clean lap for each layout to calibrate. Keep packet IDs 0–15 enabled via format 2025 and retain the resulting `race-data.sqlite3`.
- UDP is lossy and opponent telemetry may be deliberately restricted by the game. Missing channels are shown as unavailable; frame-gap counts are estimates, not reconstructed samples.
- Restart Lap and flashback transitions retain the existing synthetic and live-sequence regressions. Additional real captures on non-Silverstone layouts remain valuable for validating boundary ordering.
