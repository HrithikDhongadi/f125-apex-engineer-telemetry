# Architecture refactor baseline — 15 October 2026

This document completes phase 1, **Establish the baseline**, from
`Apex-Engineer-Architecture-Review-and-Agent-Plan.md`. It records the contracts
that later refactoring phases must preserve. It does not authorize or implement
any later phase.

## Source and checks

- Baseline commit: `ad6d5ef4e40f19eb8ac0928816f6b0e75a1cf289`
  (`feat: add architecture review and agent implementation plan document`).
- Reviewed production snapshot: `13b1ad572e23d38305fe4d0bca59c9935af2259d`.
- Difference from the reviewed snapshot: only the 398-line architecture review
  document; no production or test code changed.
- Initial worktree: clean.
- Dependency baseline: exact `npm ci` completed successfully with zero known
  vulnerabilities.
- Check command: `npm run check`.
- Result: Python compilation passed, the Circuit Profiler bundle check passed,
  and all 122 tests passed with zero failures and zero skips.
- This is an offline source/test baseline. No live F1 25 session or browser
  interaction was performed.

The current Python compilation command only covers the flat module layout:
`python3 -m py_compile src/f1telemetry/*.py utils/*.py`. It must become recursive
when phase 2 introduces nested packages.

## Public entry points and generated assets

| Entry point | Current contract |
|---|---|
| `python3 -m src.f1telemetry` | Loads settings, creates `SessionStore`, starts the UDP receiver, HTTP server and trash janitor. |
| `src.f1telemetry.server.serve(port=None, udp_port=None, host=None)` | Programmatic server entry point; explicit arguments override stored settings. |
| `utils/reprocess_session.py SESSION_ID OUTPUT [--data-dir PATH] [--report-type ...]` | Regenerates one Markdown session report using `SessionStore`. |
| `utils/migrate_race_store.py DATABASE_OR_SESSION_ID [--discard-original]` | Atomically migrates a closed packet database to compressed JSON storage. |
| `static/index.html` | Browser entry document served by the stdlib HTTP server. |
| `static/app.js` | Handwritten dashboard application; it is not generated. |
| `frontend/circuit-profiler.js` | Handwritten Konva source entry point. |
| `static/circuit-profiler.bundle.js` | Generated and committed by `npm run build`; do not edit directly. |
| `static/style.css` | Handwritten stylesheet; it is not currently generated. |

There is no installed package/console-script contract yet. Existing imports from
`src.f1telemetry.receiver`, including `Lap`, `RecordingSession`, `SessionStore`,
`SessionStateError` and `UdpReceiver`, remain compatibility contracts until a
later approved phase provides re-exports.

## HTTP API inventory

All API responses are JSON except download routes. JSON failures use an `error`
string. Existing field names and status behavior must remain stable during
behavior-preserving extraction.

### Read routes

| Route | Query/input | Response source |
|---|---|---|
| `GET /api/live` | none | Compact live state from `live_snapshot()`. |
| `GET /api/snapshot` | none | Full compatibility snapshot from `snapshot()`. |
| `GET /api/sessions` | none | `{sessions, ...live state}`. |
| `GET /api/laps` | optional `session`; `all=1` selects all | `{laps}` summaries. |
| `GET /api/sessions/{id}/laps` | path session ID | `{session_id, laps}`. |
| `GET /api/laps/{id}` | path lap ID | Full saved lap dictionary or 404. |
| `GET /api/laps/{id}/trace` | path lap ID | Calibration trace or 404. |
| `GET /api/sessions/{id}/timeline` | optional integer `lap_from`, `lap_to` | `{events}` reconstructed timeline. |
| `GET /api/personal-bests` | none | `{personal_bests}` summaries. |
| `GET /api/personal-bests/{key}` | PB key | Full PB entry or 404. |
| `GET /api/setup-schema` | none | `{fields}` using `SETUP_FIELDS`. |
| `GET /api/settings` | none | Settings, receiver IPs, restart-required fields and deleted sessions. |
| `GET /api/circuit-profiles` | optional `session` | `{profiles}`. |
| `GET /api/compare` | required `baseline`, `candidate` lap IDs | Comparison JSON or 400/404. |
| `GET /api/compare/report` | required `baseline`, `candidate` | Markdown attachment. |
| `GET /api/sessions/{id}/export` | `scope`, repeated `lap`, `format`, `report` | Markdown or ZIP attachment. |

### Mutation routes

| Route | JSON request | Successful result |
|---|---|---|
| `POST /api/runs/start` | `{name}` | New manual Time Trial recording summary, 201. |
| `POST /api/recording/stop` | `{}` | Closed recording summary. |
| `POST /api/diagnostics` | `{enabled: boolean}` | Diagnostic state. |
| `POST /api/settings` | Partial known settings object | Validated persisted settings. |
| `POST /api/trash/sessions/{id}/restore` | `{confirm: true}` | Restored session summary. |
| `POST /api/personal-bests/rebuild` | `{}` | Rebuild counts. |
| `POST /api/circuit-profiles` | Profile object | Saved profile, 201. |
| `POST /api/circuit-profiles/{id}/delete` | `{confirm: true}` | Recoverable deletion result. |
| `POST /api/sessions/select` | `{session_id}` | Selected session summary. |
| `POST /api/sessions/{id}/rename` | `{name}` | Updated session summary. |
| `POST /api/sessions/{id}/delete` | `{confirm: true}` | Recoverable deletion result. |
| `POST /api/sessions/{id}/track-name` | `{name}` | Updated unknown-track display name. |
| `POST /api/sessions/{id}/circuit-profile` | `{profile_id}` | Updated session summary. |
| `POST /api/laps/{id}/setup` | `{setup: {...}}` | Amendment/audit result. |
| `POST /api/laps/{id}/note` | `{note: string}` | Updated lap summary. |

The request-body limit is 1 MiB. Static assets are sent with `Cache-Control:
no-cache`; API and download responses use `no-store`.

## Persistence-format inventory

| Path/pattern | Authority and compatibility contract |
|---|---|
| `data/sessions/{session-id}/session.json` | `RecordingSession` manifest. Unknown dataclass fields are ignored on load; open sessions become `interrupted` on restart. |
| `data/sessions/{session-id}/lap-{timestamp}-{number}.json` | Full `Lap` object including samples, events, setup and race context. Historical missing fields use dataclass defaults. |
| `data/lap-*.json` | Legacy lap captures loaded read-only into the synthetic `legacy` session. Original files are not rewritten. |
| `data/sessions/{session-id}/race-data.sqlite3` | Per-session decoded packet evidence and timeline; WAL mode. This is decoded JSON evidence, not original UDP datagram bytes. |
| SQLite `metadata` | Text key/value metadata including schema and encoding details. |
| SQLite `packets` | Ordered header/payload JSON (plain legacy text or zlib BLOB), identifiers, session time, player index, lap distance and freshness. |
| SQLite `timeline` | Ordered raw/inferred/recorder events. `source` is constrained to `udp_event`, `inferred` or `recorder`. |
| `data/personal_bests/track-{track-id}__{mode}.json` | Atomic PB registry entry; may contain a full lap or an official-time-only record with a separate comparison trace. |
| `data/circuit_profiles/{profile-id}.json` | Circuit profile schema version 1; numeric track identity remains authoritative. |
| `data/settings.json` | Settings schema version 1; unknown keys are rejected. |
| `data/notes.json` | Lap-ID to note mapping. |
| `data/sessions/{id}/diagnostics/*.jsonl` | Bounded opt-in recorder decision history, separate from lap files. |
| `data/audit/setup-amendments/{lap-id}/{timestamp}/` | Immutable original lap/PB evidence plus amendment record. |
| `data/trash/sessions/*` and `data/trash/circuit_profiles/*` | Recoverable deletions governed by configured retention. |
| `logs/server-YYYY-MM-DD_HH-MM-SS.log` | Timestamped operational server log. |

Current writes use atomic replacement for individual JSON files. That does not
constitute a transaction across lap, manifest, notes, PB and audit files.

Unsigned 64-bit F1 session UIDs are exact in Python and persisted JSON/SQLite.
Any future browser/API representation change must account for JavaScript's
integer precision rather than silently coercing these values.

## Ordering-sensitive behavior baseline

The UDP dispatch order is part of the current contract: specialized packet
handling occurs before full decoded-packet state/storage, and `SEND` closes the
session afterward. Later extraction must preserve this order until a dedicated
behavior change is separately approved.

The current tests characterize these required behaviors:

- Time Trial started mid-lap stays armed until a verified crossing and never
  creates a placeholder for the partial approach.
- Starting immediately before the line excludes lap 19 and saves full lap 20.
- Negative-distance and same-number start-line transitions are distinguished.
- Restart Lap discards the abandoned attempt and captures the restarted full
  lap using the game's lap number.
- Mid-lap flashback prunes superseded samples while validity continues to come
  only from the game.
- Standing race starts capture lap 1 from a non-zero grid position.
- Race finish and one-shot qualifying UID tails do not create empty duplicates.
- Practice captures complete flying laps but excludes its partial outlap.
- Same-UID qualifying phase changes create separate sessions.
- A new UID closes and resets the old session without mixing samples.
- Manual stop suppresses automatic reopening of the same race UID/type.
- Valid Race and Time Trial laps can update their separate PB registries;
  invalid, partial, unknown-track, practice and qualifying evidence cannot.
- Official Session History timing may become a PB without pretending a full
  telemetry trace exists.
- Raw flashback branches remain available while accepted-timeline statistics
  exclude superseded events.
- Warning-only penalty events remain distinct from applied penalties, and raw
  collision messages remain distinct from grouped incidents.
- Plain/text and compressed SQLite formats, including the earliest schema, stay
  readable and migrate without logical changes.

The test inputs use fixed UIDs, packet values and temporary directories. Current
production timestamps/UUIDs are normalized by asserting semantic results rather
than exact generated IDs. No clock/ID seam has been added in this phase because
that belongs to the separately approved model/composition phase.

## Recorded Spa fixture

The optional recorded-data regression expects:

```text
data/sessions/session-20261008T090243-5dd3a5b6/race-data.sqlite3
```

The local baseline fixture is installed and was exercised successfully:

- Size: `376512512` bytes.
- SHA-256: `0f30cc355fbd50d28c913fbece3a196b69eb8c3cb4998a2e955d44e36fd59023`.
- Test: `test_recorded_spa_flashback_reconciliation`.
- Expected accepted evidence: 9 confirmed player overtakes, 4 confirmed times
  overtaken, 10 player-relevant collision messages, and the player penalty
  event classified as a small-collision warning.

`data/` is intentionally ignored, so this large private recording is not part
of a clean clone or CI. The supported baseline fixture mechanism is to restore
the verified database from private/local artifact storage at the exact path
above and verify its size and SHA-256 before running `npm run check`. If absent,
the test explicitly skips with `recorded Spa regression dataset is not
installed`; that skip must be reported, never treated as recorded-data
validation. Distribution of a sanitized fixture is not introduced in this
phase.

