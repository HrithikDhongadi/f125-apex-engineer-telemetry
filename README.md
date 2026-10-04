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
  sessions/
    session-<timestamp>-<suffix>/
      session.json                   Name, mode, UID, track, status, lap IDs
      lap-*.json                     Completed laps from this recording
```

Root-level captures appear as the read-only **Legacy captures** group. They are never moved, renamed, overwritten, or deleted.

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
POST /api/sessions/select               {"session_id":"..."}
POST /api/sessions/<session_id>/rename  {"name":"rear wing +1"}
POST /api/runs/start                     {"name":"18/18 baseline"}
POST /api/recording/stop
GET  /api/laps[?session=<session_id>]
GET  /api/laps/<lap_id>
POST /api/laps/<lap_id>/note             {"note":"Stowe test"}
GET  /api/compare?baseline=<id>&candidate=<id>
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
- Real F1 25 driving is required to confirm session-end event timing and lap-boundary behavior under flashbacks and every game mode.
