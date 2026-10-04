# Apex Engineer v0.3

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

The decoder validates F1 25 format `2025` and Session packet version `1`. Session type `18` is treated as Time Trial; types `15–17` are races. Track identity comes from the game rather than being assumed. The official F1 25 track appendix is built in: Silverstone is ID `7`, while its reverse layout is the independent ID `39` (Austria/Zandvoort reverse are `40`/`41`). Unrecognised IDs appear as `Unknown track (ID n)`.

Packet ID `5` is decoded using the complete packed 50-byte `CarSetupData` stride. Setup snapshots include aero, both differential settings, signed camber/toe floats, suspension, anti-roll bars, ride heights, brakes, engine braking, the four configured tyre pressures with explicit wheel names, ballast, and fuel load. These pressures are setup values and are never substituted with measured Car Telemetry pressures. Older four-field files remain valid; all absent settings display as `unknown`.

## Recording workflows

### Time Trial

1. Enter Time Trial and wait for **Time trial** and the track name to appear.
2. Enter a short run name such as `18/18 baseline` or `rear wing +1`.
3. Select **Start new run**.
4. If the car is already partway around the lap, the run shows **Armed — waiting for the next start/finish crossing; the partial approach is excluded.** At that crossing, capture begins using the lap number reported by the game. The dashboard then shows **Recording lap n**.
5. Select **Stop recording** when the run is finished. Completed laps remain saved; only the current incomplete lap is discarded.
6. Start another named run under the same in-game Time Trial session whenever required.

**Restart Lap and flashbacks:** an abandoned partial lap is discarded. Crossing the line after Restart Lap starts a clean capture even when the game reports `last_lap_ms = 0`; that zero prevents the abandoned lap from being saved but no longer prevents the new lap from being recorded. A run started before the first Lap Data packet remains armed until a verified start/finish crossing. Midlap time/distance rewinds and implausible midlap lap-number jumps do not create completed laps.

**Why the first saved number can look skipped:** the game owns lap numbering; Apex Engineer never renumbers laps. For example, if a run is started just before the line while the HUD says lap 19, that lap-19 approach is only a partial lap and is excluded. When the HUD changes to lap 20 at the line, the recorder begins capturing lap 20 and saves it at the following crossing. Earlier laps may belong to another recording, so the new session correctly begins with a `lap-...-20.json` file and does not create a placeholder lap 19.

### Race

Race types start a recording automatically. A recording closes when the game session ends or its session UID changes. **Stop recording** can close it manually; the same game UID will not automatically reopen afterward. A new game session can start another recording.

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
```

Root-level captures appear as the read-only **Legacy captures** group. They are never moved, renamed, overwritten, or deleted.

## Race engineer reports and raw export

In the **Session** view, use the checkbox beside each lap or **Select all/Clear**, then choose either **Selected laps** or **Whole session**. The dashboard shows the expected lap count before downloading.

- **Download Markdown** creates a compact report containing session identity, lap validity, official lap/sectors, available setup values, notes, sample coverage, trace-derived speed/braking/throttle observations, and compatible official-time deltas.
- **Download full data ZIP** contains that report, scoped session metadata, and byte-for-byte copies of the original included lap JSON files. Selected-lap exports contain no other raw laps.

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

- Valid race laps may be compared only within the same race recording.
- Time Trial laps from separate runs may be compared when both have the same known track.
- Race and Time Trial laps cannot be compared.
- Legacy captures may be compared with other legacy captures, but their missing lap distance means the trace is a time-normalized approximation.
- Silverstone corner notes appear only when both laps have real distance data and the game positively identifies normal Silverstone as track ID `7`.

The final UDP sample usually occurs just before the timing line. To avoid a one-point delta spike, the small difference between sampled progression and each official lap time is distributed linearly across the trace. The API and UI report the applied correction.

Silverstone windows remain approximate until precise track-distance calibration is implemented. Legacy comparisons and non-Silverstone tracks receive basic trace comparison without corner windows, braking distances, or Silverstone-specific notes.

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
GET  /api/laps[?session=<session_id>]
GET  /api/laps/<lap_id>
POST /api/laps/<lap_id>/note             {"note":"Stowe test"}
GET  /api/setup-schema
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
- Live fuel burn, ERS, tyre wear/damage, tyre compound/age, gaps, positions, and pit windows await future packet decoders. Configured setup fuel load is decoded.
- Restart Lap and flashback transitions are covered by synthetic regression tests, but real F1 25 driving is still required to confirm every packet-ordering variation and game mode.
