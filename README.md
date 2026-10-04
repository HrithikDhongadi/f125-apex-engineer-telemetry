# Apex Engineer v0.2

A local-first F1 25 race-engineer dashboard for repeatable Silverstone setup development. It receives the game's UDP telemetry, saves completed laps as readable JSON, and compares two valid laps without uploading data or requiring third-party packages.

## What's in v0.2

- **Live:** connection state, speed, gear, pedals, active wings/differential, and four-corner pressure, tyre temperature, and brake temperature readings.
- **Lap Analysis:** distance-aligned delta, speed, and pedal charts with labelled axes, hover inspection, setup/sector/sample-rate summaries, and an unambiguous candidate-minus-baseline delta.
- **Engineer Notes:** numerical trace observations for approximate Silverstone windows covering Abbey–Farm, Village–Loop, Luffield–Woodcote, Copse, Maggotts–Becketts–Chapel, Hangar Straight, and Stowe–Vale–Club.
- **Session:** fastest-first lap table with invalid-lap marking, setup, sectors, sample metadata, and locally persisted notes/tags.
- **Capture metadata:** distance and current-lap time on every new sample, sectors, session-time range, speed extrema, and per-wheel maximum inner-tyre/brake temperatures.

The known `1:28.825` Time Trial personal best is a reference, not a direct benchmark for race laps. The proven setup baseline is `18/18` wings with `25/50` differential. Change one setup variable at a time and compare repeatable clean laps.

## Run it

Requires Python 3.10+ and only the Python standard library.

```bash
cd ~/work/f125-apex-engineer-telemetry
python3 -m src.f1telemetry
```

Open [http://127.0.0.1:8025](http://127.0.0.1:8025). The receiver listens on UDP port `20777` by default. Captures remain under `data/`; notes are written to `data/notes.json` when used.

## Required F1 25 UDP settings

In **Settings → Telemetry Settings**, use:

| Setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP IP Address | Receiving computer's LAN IPv4 address (`127.0.0.1` if the game is on the same computer) |
| UDP Port | `20777` |
| UDP Send Rate | `60 Hz` recommended for detailed analysis |
| UDP Format | `2025` |
| UDP Broadcast Mode | Off |

The decoder deliberately targets the F1 25 `2025` packet format. Do not select a later format until its packet decoder is implemented.

## Compare laps

1. Complete at least two valid timed laps. Invalid laps are recorded and shown but excluded from comparison.
2. Open **Lap Analysis** and choose a baseline and candidate.
3. Select **Compare valid laps**.
4. Read delta as `candidate − baseline`: a negative value means the candidate is faster.
5. Use chart hover to inspect both traces at the same normalized lap distance.

Captures made by v0.1 remain readable. Because those files did not contain lap distance, they use a clearly labelled time-normalized fallback; record new laps for true distance alignment. Silverstone corner windows are approximate percentages until track-distance calibration is added.

## Local API

```text
GET  /api/snapshot
GET  /api/laps
GET  /api/laps/<lap_id>
GET  /api/compare?baseline=<lap_id>&candidate=<lap_id>
POST /api/laps/<lap_id>/note     {"note":"rear wing +1"}
```

Comparison responses contain a 501-point shared axis, baseline/candidate speed, throttle, brake, steering, gear and lap-time progression, delta, extrema, control markers, and evidence used in Engineer Notes. Unknown IDs, invalid laps, and incomplete traces return JSON errors.

## Project shape

```text
src/f1telemetry/protocol.py  F1 25 packet decoding
src/f1telemetry/receiver.py  UDP listener, session state, capture loading/storage
src/f1telemetry/analysis.py  alignment, deltas, and Silverstone trace rules
src/f1telemetry/server.py    local HTTP API and static web application
static/                      dependency-free dashboard
tests/                       decoder, storage, and comparison tests
```

Run the checks with:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/f1telemetry/*.py
```

Future packet decoders can extend the capture model with fuel, ERS, tyre wear/damage, compound/age, gaps/position, and pit/strategy data. Race-strategy prediction is intentionally outside v0.2.
