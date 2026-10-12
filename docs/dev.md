# Apex Engineer developer guide

This document describes the implementation, data model and maintenance workflow. The user installation and operating guide lives in the project [README](../README.md).

## Runtime and dependencies

Apex Engineer requires Python 3.10 or newer. The runtime HTTP server, UDP receiver, decoder, persistence and analysis layers use only the Python standard library.

The Circuit Profiler and lap pace map use Konva. The generated browser bundle is committed, so users do not need Node.js. After editing `frontend/circuit-profiler.js` or changing Konva, rebuild it with:

```bash
npm install
npm run build:frontend
```

Run the application from the repository root:

```bash
python3 -m src.f1telemetry
```

The dashboard binds to `127.0.0.1:8025` by default. The UDP receiver independently listens on `0.0.0.0:20777`. Configuration is stored in `data/settings.json`; host and port changes take effect after restart.

Each launch writes `logs/server-YYYY-MM-DD_HH-MM-SS.log`. Server and console messages use `YYYY-MM-DD HH:MM:SS` timestamps.

## Architecture

```text
F1 25 UDP packets
        |
        v
receiver.py  ->  protocol.py
        |
        +----> live SessionStore state
        |
        +----> completed lap JSON
        |
        +----> race_store.py / SQLite packet stream
                         |
                         v
          timeline reconstruction and continuity
                         |
          +--------------+--------------+
          v                             v
     analysis.py                    reports.py
          |                             |
          +----------> server.py <------+ 
                           |
                    static dashboard
```

Important modules:

| Module | Responsibility |
|---|---|
| `protocol.py` | F1 25 packet headers and payload decoding |
| `receiver.py` | UDP loop, live state, session/lap boundaries and persistence orchestration |
| `race_store.py` | Continuous SQLite storage, event attribution, timeline branches and continuity |
| `analysis.py` | Distance alignment, comparisons, turn metrics and calibration traces |
| `reports.py` | Race, Time Trial and Lap Analysis Markdown/ZIP exports |
| `personal_bests.py` | PB eligibility, atomic registry updates and rebuilds |
| `circuit_profiles.py` | Circuit centreline/turn profile validation and storage |
| `circuits.py` | Official track-ID names and stable circuit identity |
| `quality.py` | Lap completeness and data-quality classification |
| `setups.py` | Setup field schema, compatibility and manual amendment validation |
| `settings.py` | Persistent application configuration and trash retention |
| `server.py` | Local HTTP server and JSON/download API |

## Protocol handling

The decoder follows the [EA F1 25 UDP specification v3](https://forums.ea.com/t5/s/tghpe58374/attachments/tghpe58374/f1-games-game-info-hub-en/61/4/Data%20Output%20from%20F1%2025%20v3.pdf). It requires packet format `2025` and validates the packet-specific version and minimum length before decoding. Unsupported layouts are ignored instead of being interpreted using guessed offsets.

Official session types are grouped without losing the exact phase name: IDs 1–4 are `practice`, IDs 5–14 (including Sprint Shootout) are `qualifying`, IDs 15–17 are `race`, and ID 18 is `time_trial`.

The continuous store handles:

- Motion (packet 0)
- Session (1)
- Lap Data (2)
- Event (3)
- Participants (4)
- Car Setups (5)
- Car Telemetry (6)
- Car Status (7)
- Final Classification (8)
- Car Damage (10)
- Session History (11)
- Tyre Sets (12)
- Motion Ex (13)
- Lap Positions (15)

Every stored row retains packet/version identity, exact unsigned session UID in JSON, both frame identifiers, game session time, receive time, player index and available lap distance.

F1 session UIDs are unsigned 64-bit values. SQLite stores values above its signed maximum using the equivalent signed 64-bit bit pattern; decoded headers and exports keep the exact original unsigned number.

### Car Setup packet

Packet 5 uses the documented packed 50-byte `CarSetupData` stride. The decoder exposes aero, both differential settings, signed camber/toe floats, suspension, anti-roll bars, ride heights, brakes, engine braking, four configured tyre pressures with explicit wheel names, ballast and configured fuel load.

Configured tyre pressure is never replaced by measured Car Telemetry pressure. Configured starting fuel is distinct from instantaneous fuel remaining. Legacy four-field setup files remain readable and missing fields remain unknown.

## Recording state model

### Time Trial

A manually started run is armed until a verified start/finish boundary when capture begins mid-lap or before the first usable Lap Data packet. The recorder uses the game-provided lap number and never fills numeric gaps.

A valid forward boundary uses lap-number/timer and distance evidence while requiring continuous total distance. This distinguishes a real line crossing from packet gaps, flashbacks and Restart Lap relocation.

A mid-lap flashback prunes samples belonging to the superseded future branch and continues the current lap. Saved validity comes only from the game. Restart Lap discards the abandoned partial attempt and begins a clean attempt when the reset/crossing evidence is established.

Optional lap-boundary diagnostics write bounded JSON Lines segments under the session's `diagnostics/` directory. They are capped by both event count and the configured size (50 MiB by default), kept separate from lap JSON, and record packet fields plus explicit recorder decisions.

### Race

Supported race session types auto-start. Standing-start Lap 1 capture is initialized from early session time and does not require lap distance near zero, because grid position can be beyond the timing line.

Manual stop prevents the same game session UID from reopening. At race completion a short completed-UID guard prevents the game's trailing final packet bundle from creating an empty duplicate session.

Completed laps form explicit SQLite durability boundaries. An interrupted server marks an open manifest as interrupted at the next startup without discarding committed data.

### Practice and qualifying

Practice and qualifying/shootout modes auto-start continuous recordings. Normal sessions remain armed through the initial pit/outlap and begin at a verified line crossing. One-Shot Qualifying types 9 and 14 may start lap-1 capture from the same early-session evidence used to prove a race grid start.

The recording manifest stores both the grouped `mode` and exact `game_session_type`/`game_session_name`. A Practice or Qualifying phase change under the same UID closes the old recording and creates a new session, preventing Q1/Q2/Q3 data from being merged accidentally.

These laps participate in circuit statistics, distance-aligned comparisons and Lap Analysis reports. They are intentionally excluded from the Race/Time Trial PB registry.

## Continuous storage

Race and high-rate session packets are committed to `race-data.sqlite3` in WAL mode. Initial packets commit immediately; sustained traffic is batched to at most 128 packets or 250 ms. The per-recording size cap defaults to 8 GiB.

Schema v2 stores decoded header and payload JSON as level-1 zlib BLOBs. Readers support both schema-v1 text and schema-v2 compressed rows.

### Dashboard synchronization

The one-second browser heartbeat uses `/api/live`, an O(1) payload containing only current telemetry, recorder state and diagnostics. Successful heartbeat requests are logged at debug level so normal operation does not flood the server log.

The slower library synchronization requests `/api/sessions` and compares stable session metadata with its browser cache. Initial startup hydrates every compact lap summary through one `/api/laps?all=1` request; subsequent synchronization fetches only new sessions or sessions whose lap count changed. Personal bests are refreshed when lap data changes. Historical event timelines are loaded lazily only when the Session page is visible, never block session selection, and are cached after their first reconstruction. Live timelines refresh only for the displayed active recording. Overlapping live, library and timeline requests are suppressed and background polling pauses while the browser tab is hidden.

Rendering is change-aware. Unchanged archive, PB, statistics, profile and session sections are not rebuilt on every timer tick. Rename and track-name drafts are explicitly retained until saved or the selected session changes.

Closed schema-v1 stores can be migrated atomically:

```bash
python3 utils/migrate_race_store.py <session-id>
```

The migrator builds a separate database, preserves packet/timeline sequence and metadata, verifies SQLite integrity, row counts and logical decoded-content SHA-256, then replaces the source. It retains an `.uncompressed-<timestamp>.bak` by default. Use `--discard-original` only after verification and never migrate an active recording.

### Continuity

Continuity is calculated independently for Motion, Lap Data, Car Telemetry, Car Status and Motion Ex. Low-rate streams are excluded and gaps across packet types are not summed. Results include received and unique frames, duplicate/out-of-order counts, gap events, largest gap and effective frequency.

Game frame/session-time rewinds without corresponding recorder wall-time gaps are sequence discontinuities rather than asserted UDP loss. Wall-clock-supported gaps are classified as capture loss; unresolved cases remain unknown.

Live polling uses counters and a bounded in-memory timeline and does not scan the growing SQLite store. Exact continuity and branch reconstruction are deferred until a closed-session report/export requests them.

## Flashback-aware event reconstruction

Raw packets and raw events are immutable evidence. Reconstruction follows arrival/overall-frame order because `overallFrameIdentifier` does not rewind during an F1 25 flashback.

`FLBK` targets create replacement branches. Reconstructed rows expose:

- `timeline_branch_id`
- `event_validity` (`accepted`, `superseded`, or `unknown`)
- `superseded_by`
- `flashback_reference`
- `validation_reason`

Vehicle-indexed events use the player-car index carried by the source packet. Other-car events remain stored but are excluded from player attribution by default.

Overtake notifications are separated from confirmed manoeuvres and net position movement. Penalty-related events are classified by protocol meaning: type 5 is a warning rather than an applied penalty. Lap warning/penalty counters are used as corroboration. Collision messages are retained raw and conservatively grouped by participants, branch, time and location.

## Setup snapshot provenance

The captured setup may refresh only during the opening 1.5 seconds of a lap, preventing an old pre-grid packet from being frozen as Lap 1's active setup. Historical report processing audits Packet 5 history and labels snapshots provisional, confirmed active, superseded or unknown. It does not rewrite source lap files.

Manual historical amendments validate field types/ranges, preserve UDP versus manual provenance, make immutable lap/PB backups under `data/audit/setup-amendments/`, and atomically replace the effective lap and matching PB. Telemetry, official timing, validity and session identity are not changed.

## Personal best registry

A full-trace PB requires a valid official lap of at least 30 seconds, a known track ID and mode, at least 30 samples and at least 85% real-distance coverage beginning and ending near the line.

PB keys contain the numeric track ID and mode. Reverse layouts and display-name overrides cannot merge identities. PB snapshots are independent full copies, updated with atomic replacement.

Player Session History can preserve a valid official Time Trial PB when its finish-line trace was missed. Such a record is `official_time_only`; timing and sectors are retained, but unavailable telemetry/setup are not invented. A separate best captured trace can remain available without being presented as the official PB time.

## Comparison and circuit analysis

Comparison aligns laps on recorded distance. Final trace progression is corrected smoothly to official lap time to avoid a one-sample timing-line spike. Delta is candidate minus baseline.

The comparison layer rejects different or unknown track IDs, mixed legacy-time/distance alignment and insufficient distance coverage. Cross-mode and game-invalid comparisons are permitted with explicit evidence-quality warnings.

Circuit profiles are keyed by packet format, numeric track ID, layout and schema version. They contain game-measured length, optional Motion X/Z centreline, sector boundaries, turn windows, provenance and independent verification state.

Profile-level verification does not imply that every turn is verified. Unnamed/unverified turns and overlapping windows remain visibly exploratory; overlapping turn deltas are never summed as though they partitioned the lap.

The Konva maps mirror game-world X only for familiar presentation. Stored coordinates are unchanged. Track strokes and markers disable scale-dependent width so they remain editable when zoomed.

## Reports and historical reprocessing

Reports separate:

1. official game timing/results;
2. raw UDP notifications;
3. accepted-timeline reconstructed statistics;
4. inferred engineering observations;
5. unverified interpretations.

Exports are read-only and do not modify source sessions. Historical sessions can be reprocessed with current logic:

```bash
python3 utils/reprocess_session.py \
  session-20261008T090243-5dd3a5b6 \
  data/reports/spa-race-corrected.md \
  --report-type race
```

The detailed Spa validation and known evidence limitations are documented in [spa-race-correctness-audit-2026-10-09.md](spa-race-correctness-audit-2026-10-09.md).

## Storage layout

```text
data/
  lap-*.json                           Legacy captures (read-only in the UI)
  notes.json                           Per-lap notes
  settings.json                        Runtime configuration
  personal_bests/
    track-<id>__<mode>.json            Atomic PB snapshot
  audit/setup-amendments/<lap>/<id>/   Exact originals and amendment manifest
  sessions/session-<timestamp>-<id>/
    session.json                       Session manifest
    lap-*.json                         Completed laps
    race-data.sqlite3                  Continuous packets and event timeline
    diagnostics/                       Optional bounded boundary traces
  circuit_profiles/
    f1-2025-track-<id>-<layout>-v1.json
  trash/sessions/                      Recoverable deleted sessions
  trash/circuit_profiles/              Recoverable deleted profiles
```

Session deletion is a move to local trash. Active and legacy sessions are protected. Expiry is enforced at startup and by the running server according to `trash_retention_days`; zero disables automatic purge. PB snapshots and notes are independent and survive session deletion.

## HTTP API

```text
GET  /api/live
GET  /api/snapshot
GET  /api/sessions
GET  /api/sessions/<session_id>/laps
GET  /api/sessions/<session_id>/timeline[?lap_from=n&lap_to=n]
GET  /api/sessions/<session_id>/export?scope=session&format=markdown&report=auto
POST /api/sessions/select                         {"session_id":"..."}
POST /api/sessions/<session_id>/rename            {"name":"rear wing +1"}
POST /api/sessions/<session_id>/delete            {"confirm":true}
POST /api/sessions/<session_id>/track-name        {"name":"Local circuit"}
POST /api/sessions/<session_id>/circuit-profile   {"profile_id":"..."}
POST /api/runs/start                               {"name":"18/18 baseline"}
POST /api/recording/stop
POST /api/diagnostics                              {"enabled":true}
GET  /api/laps[?session=<session_id>|?all=1]
GET  /api/laps/<lap_id>
GET  /api/laps/<lap_id>/trace
POST /api/laps/<lap_id>/note                       {"note":"Stowe test"}
POST /api/laps/<lap_id>/setup                      {"setup":{"front_camber":-3.5}}
GET  /api/setup-schema
GET  /api/settings
POST /api/settings                                 {settings fields}
POST /api/trash/sessions/<id>/restore              {"confirm":true}
GET  /api/circuit-profiles[?session=<session_id>]
POST /api/circuit-profiles                         {profile JSON}
POST /api/circuit-profiles/<id>/delete             {"confirm":true}
GET  /api/compare?baseline=<id>&candidate=<id>
GET  /api/compare/report?baseline=<id>&candidate=<id>
GET  /api/personal-bests
GET  /api/personal-bests/<track-mode-key>
POST /api/personal-bests/rebuild
```

Invalid transitions, incompatible comparisons and unknown IDs return JSON error objects. Destructive/recoverable operations require explicit confirmation.

## Tests and validation

Run every Python and frontend check from the repository root:

```bash
npm run check
```

The individual commands remain available as `npm test`, `npm run check:python` and `npm run check:frontend`. Use `npm run build` after changing the Konva-based map source.

GitHub Actions runs the same `npm run check` command on pushes and pull requests using Python 3.10 and Node.js 20.

The suite covers decoder offsets/lengths, setup wheel ordering, historical compatibility, Time Trial arming and Restart Lap sequences, standing race starts, duplicate-finish suppression, flashback pruning/reconstruction, event attribution, penalty and collision classification, continuity accounting, PB eligibility/rebuilds, exports, trash restoration/expiry, dashboard contracts and circuit profiles.

When changing recorder boundary logic, retain both synthetic edge cases and captured live packet-sequence regressions. A synthetic test alone is not proof of live game ordering.

## Current engineering limitations

- Race-strategy prediction is not implemented.
- Only normal Silverstone ships with an approximate migration seed; it is not authoritative metre calibration.
- Other layouts need clean Motion X/Z calibration laps and manual verification before turn-level coaching is trustworthy.
- UDP loss and game-restricted opponent channels cannot be reconstructed without evidence.
- More live validation remains useful for pit-stop races, safety-car/VSC sessions, race flashbacks and non-Silverstone Restart Lap ordering.

## Maintenance priorities

- `static/app.js` currently owns dashboard state, rendering and API orchestration. New page-sized features should be extracted into focused browser modules rather than extending the single file indefinitely.
- `receiver.py` deliberately centralizes recorder state, but storage/library operations can be separated from packet-driven lap state in a future refactor backed by the existing session regressions.
- The committed `static/circuit-profiler.bundle.js` is generated. Edit `frontend/circuit-profiler.js`, then run `npm run build`; do not hand-edit the bundle.
