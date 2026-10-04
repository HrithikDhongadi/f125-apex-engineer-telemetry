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

The decoder validates F1 25 format `2025` and Session packet version `1`. Session type `18` is treated as Time Trial; types `15–17` are races. Track identity comes from the game rather than being assumed.

## Recording workflows

### Time Trial

1. Enter Time Trial and wait for **Time trial** and the track name to appear.
2. Enter a short run name such as `18/18 baseline` or `rear wing +1`.
3. Select **Start new run**.
4. If the car is already partway around the lap, the run shows **Armed—waiting for start/finish line** and begins with the next complete lap.
5. Select **Stop recording** when the run is finished. Completed laps remain saved; only the current incomplete lap is discarded.
6. Start another named run under the same in-game Time Trial session whenever required.

**Restart Lap and flashbacks:** an abandoned partial lap is discarded. Crossing the line after Restart Lap starts a clean capture even when the game reports `last_lap_ms = 0`; that zero prevents the abandoned lap from being saved but no longer prevents the new lap from being recorded. A run started before the first Lap Data packet remains armed until a verified start/finish crossing. Midlap time/distance rewinds and implausible midlap lap-number jumps do not create completed laps.

### Race

Race types start a recording automatically. A recording closes when the game session ends or its session UID changes. **Stop recording** can close it manually; the same game UID will not automatically reopen afterward. A new game session can start another recording.

Changing setup during a lap does not relabel that lap: each completed lap retains the setup seen at its start.

## Session library and historical data

The displayed session controls the **Session** table and the default Lap Analysis choices. Selecting an old session is read-only inspection and never resumes it. Live telemetry remains live while historical data is displayed. Use **Jump to active session** to return to the current recording.

Sessions can be renamed without changing their IDs or lap files. Per-lap notes continue to use `data/notes.json`. A recording open during application shutdown or a crash is marked `interrupted` when loaded again.

Storage layout:

```text
data/
  lap-*.json                         Existing v0.1/v0.2 files, unchanged
  notes.json                         Per-lap notes, when used
  personal_bests/
    track-<id>__<mode>.json          Atomic full-fidelity winning-lap copies
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

## Personal best registry

Every newly completed lap is evaluated automatically. A PB update requires:

- a valid official lap of at least 30 seconds;
- a known track ID and compatible `race` or `time_trial` mode;
- at least 30 telemetry samples;
- real lap-distance data covering at least 85% of the track, starting and ending near the timing line.

Slower, invalid, partial, legacy-distance, and unknown-track laps cannot replace a PB. Keys include actual track ID and mode, so race and Time Trial records are not mixed.

Each PB file is an independent full copy of the winning lap: all telemetry samples and units, official times and sectors, setup snapshot, source IDs/session name, note/provenance, data-quality result, and the date it became PB. Updates use a temporary file plus atomic replacement and never mutate the source lap. Use **Rebuild from saved sessions** deliberately to consider historical session laps. A stored Time Trial PB can be selected as the baseline for a compatible future run.

Back up `data/personal_bests/` together with `data/sessions/`. PB copies remain usable for comparison even if their original session is later unavailable.

## Lap comparison rules

Delta is always `candidate − baseline`; negative means the candidate is faster.

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
POST /api/runs/start                     {"name":"18/18 baseline"}
POST /api/recording/stop
GET  /api/laps[?session=<session_id>]
GET  /api/laps/<lap_id>
POST /api/laps/<lap_id>/note             {"note":"Stowe test"}
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
- Fuel, ERS, tyre wear/damage, tyre compound/age, gaps, positions, and pit windows await future packet decoders.
- Track names other than normal Silverstone currently appear by numeric ID, although compatibility rules still use that ID correctly.
- Restart Lap and flashback transitions are covered by synthetic regression tests, but real F1 25 driving is still required to confirm every packet-ordering variation and game mode.
