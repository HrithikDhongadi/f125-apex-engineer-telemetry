# Architecture phase 6 — storage, reconstruction and exports

Completed 15 October 2026 against the behavioral contracts in the approved
architecture plan. This phase implements only **Separate storage,
reconstruction, and exports**.

## Ownership after extraction

- `storage/schema.py` owns schema initialization plus compatible plain-text and
  compressed packet JSON codecs.
- `storage/packets.py` owns the WAL writer, bounded commits and read repository.
- `storage/snapshots.py` owns immutable SQLite online backups and committed
  packet/event sequence watermarks.
- `storage/migration.py` owns the public closed-store migration boundary.
- `reconstruction/timeline.py` owns the read-only branch/player-attribution
  boundary; the characterized v2 algorithm and its accepted, superseded and
  unknown labels are unchanged.
- `reconstruction/continuity.py` owns the read-only continuity/setup-audit
  boundary; per-stream accounting and classifications are unchanged.
- `application/exports.py` owns export capture and rendering. `receiver.py`
  retains the sole recorder lock and compatibility API.
- `race_store.py` is now a compatibility import module for existing scripts,
  tests and external callers.

No SQLite schema version, persisted row, lap/session JSON shape, route or
download response contract changed.

## Consistent export revision

While holding the existing recorder lock, export capture now validates the
selection and detaches the exact session, completed laps, notes, setup-bearing
lap models, profile and original lap bytes. At the same ordered barrier it
flushes the active packet writer and uses SQLite's online backup API. The
snapshot records the maximum committed packet and event sequence.

The resulting revision has a stable SHA-256 identity over component hashes,
lap IDs and database watermarks. ZIP exports expose it as
`export-revision.json`. Reconstruction reads the immutable snapshot, so it
cannot include packets received after the metadata revision was captured.

After capture, the recorder lock is released. Timeline/continuity derivation,
Markdown rendering, database byte loading and ZIP creation occur from detached
inputs. Temporary database snapshots live outside the session directory and
are deleted in a `finally` path, so recoverable session deletion or setup
amendment can proceed without changing the running export.

Direct lap-comparison exports likewise deep-copy both laps, notes and profile
under the lock, then render after releasing it.

## Compatibility and evidence preservation

SQLite v1 plain-text rows, v2 compressed rows, unsigned session UIDs and the
existing migration digest checks remain covered. Online backup remains the only
method used to capture WAL-backed databases; the main database file is never
copied alone. Existing reconstruction tests continue to verify flashback
supersession, unknown correlation, player attribution, warning classification,
collision grouping and the recorded Spa reconciliation fixture when present.

## Validation

The full check completed with:

- 138 Python tests passing;
- 2 frontend Node test suites passing;
- Python compilation passing.

New export tests deliberately pause report/ZIP rendering after capture. They
verify that telemetry recording continues while the large-export phase is
blocked, and that a concurrent setup amendment followed by recoverable session
deletion does not change the already captured original lap bytes. ZIP revision
metadata and packet watermarks are also asserted.

## Scope boundary

This phase does not add the Phase 7 catalog, lazy trace cache, server-side
pagination, streaming ZIP delivery, data-structure optimization or recorder
queue. ZIP assembly remains in memory to preserve the current API until that
separately approved performance work.
