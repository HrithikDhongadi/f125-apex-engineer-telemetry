# Apex Engineer

Local-first F1 25 telemetry for finding lap time—not decorating a dashboard.

The first version is designed around the current Silverstone Time Trial work:

- receive F1 25 UDP packets on port `20777`;
- record clean laps automatically;
- retain speed, throttle, brake, steering, gear, tyre pressure and brake temperatures;
- compare any two completed laps over normalized lap distance;
- show the active wing/differential settings reported by the game.

## Run it

Requires Python 3.10+ and no third-party packages.

```bash
cd f1-telemetry-engineer
python3 -m src.f1telemetry
```

Open `http://127.0.0.1:8025` on the receiving computer.

## F1 25 settings

Set **UDP Telemetry** to On, **UDP Port** to `20777`, **UDP Broadcast Mode** to Off and **UDP Format** to `2025`. Enter the receiving computer's LAN IPv4 address as **UDP IP Address**. Use `60 Hz` for detailed traces.

For F1 25: 2026 Season Pack, use format `2026` only after the 2026 packet decoder has been added; the initial implementation deliberately targets the F1 25 (`2025`) protocol.

## Project shape

```text
src/f1telemetry/protocol.py  F1 25 packet decoding
src/f1telemetry/receiver.py  UDP listener and lap capture
src/f1telemetry/server.py    local HTTP API and web application
static/                      dependency-free dashboard
tests/                       packet-decoder tests
```

## Next milestones

1. Establish a repeatable Silverstone baseline (three clean laps per change).
2. Add distance-aligned delta, corner markers, and setup-to-lap annotations.
3. Add an engineer report focused on Stowe, Copse, and Maggotts–Becketts.
4. Support race stints: tyre degradation, fuel, ERS and strategy.

This project stores captured laps locally under `data/`; nothing is uploaded.
