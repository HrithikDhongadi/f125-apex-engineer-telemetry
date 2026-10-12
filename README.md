# Apex Engineer

Apex Engineer is a local F1 25 telemetry recorder and race-engineering dashboard. It records complete laps, compares driving and setups, tracks race context, builds circuit profiles, and exports detailed engineering reports.

Your telemetry stays on your computer. No account or cloud service is required.

## What you can do

- Record Practice, Qualifying, Sprint Shootout, Race and Time Trial sessions from F1 25 UDP telemetry.
- Compare any two compatible laps using speed, inputs, motion and a circuit pace map.
- Review tyre wear, temperatures, fuel, ERS, damage, position and race events.
- Keep separate valid-lap personal bests for Race and Time Trial on every circuit.
- Rank all saved laps for a circuit and review race statistics.
- Create editable circuit profiles by pointing out turn apexes on a recorded lap.
- Download Race, Time Trial and Lap Analysis reports in Markdown or full-data ZIP files.
- Keep invalid and flashback-affected laps for diagnosis without allowing them to replace valid PBs.

## Requirements

- F1 25 with UDP telemetry enabled.
- Python 3.10 or newer.
- A modern browser such as Firefox, Chrome or Edge.
- The game device and this computer on the same network when they are different devices.

Node.js is **not** required to use Apex Engineer. The generated Circuit Profiler bundle is included in the repository.

## Install and start

```bash
git clone https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry.git
cd f125-apex-engineer-telemetry
python3 -m src.f1telemetry
```

Open [http://127.0.0.1:8025](http://127.0.0.1:8025) in your browser.

The terminal can remain in the background while you play. Stop the server with `Ctrl+C` after the recording has been stopped or the race has ended.

Each launch also creates a dated log file in `logs/`. If something goes wrong, this is the first file to check.

## Configure F1 25 telemetry

Open **F1 25 → Settings → UDP Telemetry Settings** and use:

| F1 25 setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP Send Rate | 60 Hz |
| UDP Format | 2025 |
| UDP Port | `20777` |
| UDP Broadcast Mode | Off |
| Your Telemetry | Restricted is sufficient for your own car |

For **UDP IP Address**:

- Game and Apex Engineer on the same computer: use `127.0.0.1`.
- Game on a console or another computer: open Apex Engineer's **Settings** tab and use one of the displayed LAN addresses.

The devices must be on the same network. Your firewall must allow inbound UDP traffic on port `20777`.

Return to the dashboard and confirm that the top-right indicator says **UDP connected**. The Live page should begin showing the current circuit, session and car data.

## Your first Time Trial recording

1. Enter Time Trial in F1 25 and drive onto the circuit.
2. Confirm that Apex Engineer shows **Time Trial** and the correct circuit.
3. Enter a useful run name, such as `Baseline 18/18` or `Rear wing -1`.
4. Select **Start new run**.
5. Drive complete laps.
6. Select **Stop recording** when finished.
7. Open **Session** to inspect the saved laps or **Lap Analysis** to compare them.

If you start recording partway through a lap, the status shows:

> Armed — waiting for the next start/finish crossing; the partial approach is excluded.

Capture begins at the next verified crossing and the dashboard then shows the lap number being recorded. The game owns lap numbering, so the first saved lap does not have to be lap 1 and Apex Engineer does not create placeholder laps.

### Flashback and Restart Lap

- A flashback keeps the lap as engineering evidence. Superseded future samples are removed and capture continues from the rewound point.
- Lap validity comes from the game's validity flag; Apex Engineer does not declare a lap invalid merely because a flashback occurred.
- **Restart Lap** discards the abandoned partial attempt and begins capturing the next complete attempt using the lap number reported by the game.
- Invalid Time Trial laps remain available for comparison and reports, but cannot become a valid PB.

## Recording a race

Race recording starts automatically when F1 25 begins a supported race session. You do not need to select **Start new run**.

- Lap 1 is captured from the grid, even when your grid position is beyond the timing line.
- The recording closes when the race/session ends or the game changes session identity.
- You can use **Stop recording** to close it manually.
- Position, gaps, tyres, fuel, ERS, damage, pit state, weather, safety-car state and relevant events are retained when transmitted by the game.
- Flashback-discarded event branches remain in raw data but are excluded from accepted-timeline race statistics when reconstruction can establish that they were superseded.

Wait a few seconds after the finish so the game can transmit final classification data before closing F1 25.

## Practice and qualifying

Practice, Qualifying and Sprint Shootout sessions record automatically, like races. Apex Engineer preserves the exact phase name transmitted by the game, including Practice 1–3, Short Practice, Q1–Q3, Short/One-Shot Qualifying and the Sprint Shootout variants.

- A normal Practice or Qualifying session begins armed. The pit/outlap is excluded and capture begins at the next verified start/finish crossing.
- One-Shot Qualifying and One-Shot Sprint Shootout can capture lap 1 directly from the verified opening state.
- Q1, Q2 and Q3 are stored as separate recording sessions when the game changes phase, even if it retains the same game-session UID.
- Completed laps appear in Session, Circuit Stats, comparisons and Lap Analysis exports.
- Practice and Qualifying laps do not replace the Race or Time Trial PB registry. Use Circuit Stats to rank them by mode.

## Dashboard guide

### Live

Shows the current connection, car, tyres, setup and recording state. Use this page while driving to confirm telemetry is arriving.

### Lap Analysis

Use the year, month and date query to narrow the archive, then choose a baseline session and lap and independently choose a candidate session and lap. The lap options show time and validity. A stored PB with telemetry can also be selected on either side. The optional PB registry is collapsed until you need it, keeping the comparison controls prominent.

The comparison includes:

- official and sector-time differences;
- cumulative delta and speed traces;
- throttle, brake, steering and slip channels when available;
- setup changes;
- circuit-map sections showing where each lap gained time;
- turn-level evidence when a compatible calibrated profile exists.

Delta is always **candidate minus baseline**. A negative final delta means the candidate was faster. Charts use cyan for the baseline and red for the candidate.

Race versus Time Trial and invalid-lap comparisons are allowed on the same circuit, but are clearly marked as non-like-for-like or diagnostic evidence. Different circuits and incomplete traces are rejected.

### Session

Use the year, month and date filters to reveal saved sessions. Selecting a historical session only changes what you are viewing; it does not affect a live recording.

Long session, lap, event, PB, race-stat and deleted-session lists are paginated. Changing page never changes the active recording or the laps already selected for export.

From this page you can:

- rename a session;
- rank its laps fastest-first with the same timing, sector, tyre, setup and trace evidence shown in Circuit Stats;
- inspect, tag and select laps for export;
- send any displayed lap directly to the Baseline or Candidate comparison slot;
- add or correct a historical setup;
- review the race event timeline;
- download a report or full-data ZIP;
- delete a closed session to recoverable local trash.

### Circuit Stats

Ranks saved laps for one numeric circuit ID, ten per page. Filters include mode, validity, session, date, compound, setup completeness, telemetry quality and notes.

It also summarizes race results using verified completed results where possible, including average finish, wins, clean-lap rate, pace consistency, pit stops, warnings, penalties, fuel, tyre wear and compounds.

### Circuit Profiler

Use a clean recorded lap to create or edit corner definitions:

1. Use the date query to find and select the session, then choose a clean calibration lap.
2. Choose **Load distance map**.
3. Enter a turn number, optional name and margin.
4. Select **Point apex on map**, then click the apex/midpoint on the trace.
5. Drag the apex marker or adjust its distance and margin to refine it.
6. Verify names, direction and boundaries, then save the profile.

Map controls:

| Action | Mouse/keyboard |
|---|---|
| Pan | Drag anywhere or use arrow keys |
| Zoom | Mouse wheel or `+` / `-` |
| Rotate | Shift + wheel or `Q` / `E` |
| Reset view | `0` |

Automatically suggested turns are unverified drafts. A profile being marked verified does not automatically verify every individual corner or apex.

### Settings

Settings are stored in `data/settings.json`. The page controls:

- dashboard address and port;
- UDP receiving port;
- deleted-session retention;
- maximum recording and diagnostic sizes;
- dashboard refresh interval;
- restoration of recently deleted sessions.

The dashboard tells you when a change requires a server restart.

## Reports and exports

In **Session**, select individual laps or the whole session, choose the report type, then download Markdown or a full-data ZIP.

- **Race report:** result, progression, stints, events, race context and a detailed engineering sheet for every lap.
- **Time Trial report:** best lap, consistency, validity, setup development and detailed lap evidence.
- **Lap Analysis report:** baseline/candidate deltas, setup comparison, alignment quality and turn-window evidence.
- **Full-data ZIP:** report, session metadata, original lap JSON, relevant event data and—when exporting a whole session—a consistent SQLite snapshot.

Reports distinguish official game results, raw UDP messages, reconstructed statistics and inferred engineering observations. Missing information is shown as unavailable or unknown rather than guessed.

## Personal bests

Valid complete laps are evaluated automatically. PBs are kept separately by numeric circuit ID and mode, so Race and Time Trial records—and normal and reverse layouts—are never mixed. Practice and Qualifying laps are ranked in Circuit Stats instead of being treated as permanent PB evidence.

Use **Rebuild from saved sessions** when you want to rescan historical recordings. An official Time Trial PB recovered from Session History may be shown as timing-only if the full telemetry trace was not captured; it remains a valid time but cannot be used as a comparison trace.

## Data safety and backups

All user data is stored under `data/`:

```text
data/
  sessions/          Recorded sessions, laps and continuous race data
  personal_bests/    Independent PB snapshots
  circuit_profiles/  Saved circuit and corner definitions
  audit/              Immutable backups made before setup amendments
  trash/              Recoverable deleted sessions and profiles
  settings.json       Dashboard configuration
```

Back up at least `data/sessions/`, `data/personal_bests/` and `data/circuit_profiles/`.

Deleting a session in the dashboard moves it to local trash. Restore it from **Settings → Deleted sessions** before its retention period expires. A retention value of `0` keeps deleted sessions until you restore them manually.

## Troubleshooting

### `OSError: [Errno 98] Address already in use`

Another server is already using port `8025`. Open the existing dashboard or find the process with:

```bash
lsof -nP -iTCP:8025 -sTCP:LISTEN
```

Stop that process, or change the dashboard port in Settings and restart the server.

### Dashboard says UDP disconnected

Check the F1 25 UDP format, IP and port. For a console, do not use `127.0.0.1`; use the receiving computer's LAN address shown in Settings. Also check the firewall and confirm both devices are on the same network.

### Sessions appear empty at first

The archive is intentionally hidden until a year is selected. Choose a year, and optionally a month and date, to reveal matching sessions.

### A first lap number appears to be missing

Check whether recording was started during the preceding partial lap. Apex Engineer waits for a verified start/finish boundary and preserves the game's lap number. Enable **Capture lap-boundary diagnostics** before a short reproduction only when a genuinely complete first lap is missing.

### An invalid lap did not replace the PB

This is expected. Invalid laps remain available for engineering analysis but cannot replace valid-lap PB evidence.

### The browser still shows old styling or controls

Restart the server and refresh the page. Dashboard assets are revalidated automatically. If the tab was already open before upgrading from an older version, use `Ctrl+Shift+R` once.

## Known limitations

- Race-strategy prediction is not implemented.
- Most circuits still require a clean Motion X/Z lap and manually verified turn calibration before corner-level conclusions are trustworthy.
- UDP is lossy, and F1 25 may restrict opponent telemetry. Apex Engineer reports unavailable channels and capture-quality gaps instead of inventing samples.
- Setup, telemetry and event availability depend on what the game transmits for the current mode.

## Developer documentation

Architecture, protocol handling, storage formats, API routes, utilities, build instructions and tests are documented in [docs/dev.md](docs/dev.md).

The Spa race reconstruction investigation is preserved in [docs/spa-race-correctness-audit-2026-10-09.md](docs/spa-race-correctness-audit-2026-10-09.md).
