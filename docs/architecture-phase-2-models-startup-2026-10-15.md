# Architecture phase 2 — models and startup

Completed 15 October 2026 against baseline commit
`ad6d5ef4e40f19eb8ac0928816f6b0e75a1cf289` and the contracts recorded in
`docs/architecture-baseline-2026-10-15.md`.

This phase implements only **Separate models and startup** from the approved
architecture plan.

## Problem and resulting ownership

Previously, the persisted `Lap` and `RecordingSession` models and
`SessionStateError` were defined in `receiver.py`. Analysis therefore imported
the recorder merely to obtain a model type. Runtime construction, HTTP delivery,
UDP/thread startup, the janitor and shutdown also lived together in `server.py`.

Ownership after this phase:

- `domain/models.py` owns `Lap`, `RecordingSession`, `LEGACY_SESSION_ID` and the
  existing lap enrichment helper.
- `domain/errors.py` owns `SessionStateError`.
- `domain/__init__.py` exposes the neutral domain surface.
- `receiver.py` imports and re-exports the same class objects for compatibility;
  it continues to own `SessionStore`, `UdpReceiver` and all existing recording
  behavior.
- `analysis.py` imports `Lap` from the domain instead of the recorder.
- `bootstrap.py` is the composition root. It creates concrete settings, store,
  receiver and HTTP dependencies and owns thread startup and ordered shutdown.
- `server.py` remains HTTP transport and retains a compatibility `serve()` that
  delegates to the composition root.
- `__main__.py` now enters through `bootstrap.serve()`.

An architecture test parses every domain module and fails if it imports the
delivery, recorder, reporting or persistence modules prohibited by the plan.

## Preserved behavior and compatibility

- `from src.f1telemetry.receiver import Lap`, `RecordingSession` and
  `SessionStateError` remains valid and returns the exact domain class objects.
- `python3 -m src.f1telemetry` remains the user entry point.
- `src.f1telemetry.server.serve(...)` remains a programmatic compatibility entry
  point with the same arguments.
- Runtime startup remains receiver first, then trash janitor, then HTTP serving.
- Shutdown remains: interrupt active recording, stop/join janitor, stop receiver,
  close HTTP server, then flush and close log handlers.
- Dataclass field order, defaults, `Lap.id`, summaries and serialized dictionary
  shapes are unchanged.
- No recorder decision, threshold, packet-processing order, route, file format,
  database schema, generated frontend asset or UI behavior changed.

## Checks

`npm run check` completed successfully:

- Recursive Python compilation passed using
  `python3 -m compileall -q src/f1telemetry utils`.
- Frontend build check passed.
- 127 tests passed with zero failures and zero skips.
- The locally installed recorded Spa regression passed.
- `git diff --check` passed.

New tests verify:

- domain dependency boundaries;
- old import-path compatibility;
- module entry-point delegation;
- `server.serve()` compatibility delegation; and
- runtime startup and shutdown ordering.

## Compatibility and migration impact

There is no data, API or user migration. Existing recordings are read by the
same `SessionStore` code and the moved dataclasses have unchanged persisted
shapes. The old Python imports are compatibility re-exports rather than parallel
model definitions.

The recursive compilation command is the only build/check change. It is needed
because `domain/` is now a nested package.

## Remaining planned boundaries

`SessionStore` still owns archive, settings, trash, diagnostics, amendments and
exports. Models also retain their existing summary methods to avoid a response-
shape change. Those concerns remain in the later, already-approved phases; they
were not altered or expanded in this phase.

