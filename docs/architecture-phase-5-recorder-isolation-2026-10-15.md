# Architecture phase 5 — recorder isolation

Completed 15 October 2026 against the behavioral contracts recorded in
`docs/architecture-baseline-2026-10-15.md`.

This phase implements only **Isolate the recorder** from the approved
architecture plan.

## Problem and resulting ownership

Lap-boundary inference previously lived inside `SessionStore.record_lap_state()`
alongside trace mutation, session/lap persistence, SQLite timeline events and
diagnostic JSONL output. State was distributed across several attributes and
decision reasons were unstructured strings assembled by effect branches.

Ownership after this phase:

- `domain/recording.py` owns `RecorderState`, lifecycle/decision/reason enums,
  normalized `LapDataObservation`, immutable `RecordingContext`, structured
  `RecorderDecision`, and the pure `decide_lap_boundary()` function.
- `application/recorder.py` owns ordered application of a decision to the active
  mutable attempt. Persistence is supplied through narrow callbacks, so the
  service does not import the recorder, delivery, filesystem, SQLite or
  diagnostics.
- `receiver.py` remains the compatibility façade, UDP dispatcher and sole lock
  owner. It constructs observations/context, asks the domain engine to decide,
  invokes the application service, then records timeline and diagnostics from
  the result.
- Existing names such as `active_lap_number`, `active_samples` and
  `capture_current_lap` forward to the single `RecorderState`; callers do not
  need to migrate in this phase.

## Preserved decisions and ordering

The extracted engine retains the characterized thresholds and branch priority:

- opening evidence uses lap 1, zero previous time, at most 1.5 seconds current
  time and game session time from 0–5 seconds;
- near-line evidence uses at most 5 seconds and −200 m through the larger of
  250 m or 5% track length;
- rewind tolerances remain 1 second for time and 50 m for distance;
- same-number wrap still requires the previous 85% and new 5% track windows plus
  continuous total-distance evidence;
- official completion still requires at least 30 seconds, followed by the
  existing minimum-sample and session-UID checks in `_finish_lap()`;
- finish/reject is evaluated before status transition and new-attempt creation;
  diagnostics remain observational and do not influence the decision.

UDP handling order remains specialized packet handling, full decoded-packet
storage/correlation, then `SEND` closure. `UdpReceiver` remains one daemon
thread, and `SessionStore` retains its single existing lock. No queue, new
worker, reordered datagrams or independent engine lock was introduced.

## Regression coverage

New dependency-neutral tests exercise first-full-Time-Trial arming, same-number
Restart Lap, flashback pruning with game-owned validity, standing race start,
One-Shot Qualifying start, finish/status/start effect precedence and specialized
packet handling before full-packet storage.

The existing integration suite remains the authoritative coverage for captured
live sequences. It verifies first complete laps, negative-distance and
same-number crossings, restart relocation, mid-lap flashbacks, race grid starts,
One-Shot Qualifying first laps/UID guards and same-UID qualifying phase changes.

## Compatibility and migration impact

There is no persisted-data, SQLite, API, diagnostics, packet-order, lock or user
migration. Lap JSON, session manifests, PB behavior and decision/reason strings
retain their existing external shapes. Only internal ownership changed.

## Scope boundary

This phase does not split SQLite writing/reconstruction, move exports outside
the live lock, add a datagram queue, change decoding, or optimize archive data
structures. Those remain later approved phases.
