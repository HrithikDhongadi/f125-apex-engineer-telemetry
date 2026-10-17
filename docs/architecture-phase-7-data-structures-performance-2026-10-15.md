# Architecture Phase 7: Data Structures and Performance

Date: 2026-10-15

This phase completes only **Improve data structures and performance** from the
approved architecture plan. It does not introduce a recorder queue, change the
single receiver-thread model, or begin the optional concurrency/platform work.

## Ownership and data flow

- `SessionArchiveRepository` owns the rebuildable `archive-index.json` lap
  metadata sidecar and full-lap hydration from authoritative JSON files.
- `SessionStore` owns in-process identity maps, summary caches, and the
  byte-bounded historical trace cache.
- `storage.packets` streams continuity rows from SQLite and owns the bounded
  top-gap calculation and ordered event queues.
- `application.exports` captures immutable export inputs in temporary storage;
  `reports.build_zip_file` creates the ZIP on disk; the HTTP server streams and
  removes that artifact.
- The frontend state store owns `Map` indexes for repeated session/lap joins.

## Compatibility and recovery

Raw lap JSON, session manifests, PB snapshots, notes, and SQLite stores retain
their existing formats and authority. `archive-index.json` is derived data: it
can be deleted and is rebuilt, and each entry is invalidated by file size and
nanosecond modification time. Setup amendments explicitly remove the affected
entry. Historical four-field setup files and legacy root-level laps continue
to load.

Unpaginated archive endpoints keep their original response shapes. Pagination
is enabled only when a page or archive filter parameter is supplied. Direct
Python ZIP callers still receive bytes; the server opts into a temporary
`Path` and streams it.

## Implemented changes

1. Lap IDs and per-session lap IDs use dictionaries instead of repeated global
   linear searches. Browser session/lap joins use `Map` indexes.
2. Lap summaries and stable session summaries are cached and invalidated on
   the corresponding mutation. Completed laps update indexes incrementally.
3. Startup creates metadata-only `Lap` objects. Full sample arrays are loaded
   on demand through a 128 MiB LRU cache.
4. Continuity analysis consumes SQLite cursors rather than allocating complete
   packet tables, retaining only required counters, exact frame identities,
   and the ten largest gaps.
5. FIFO event matching uses `deque.popleft()`.
6. ZIP inputs and output are temporary files; HTTP delivery is chunked.
7. Session and lap archive pagination is additive and filterable.

## Benchmark

Command:

```bash
python3 utils/benchmark_archive.py
```

Synthetic fixture: 12 sessions, 20 laps per session, 1,200 samples per lap
(240 laps and 288,000 samples), followed by 2,000 decoded mixed-type packet
operations and a 20-lap ZIP export.

| Measurement | Result |
| --- | ---: |
| Cold sidecar build | 1.5222 s |
| Warm indexed startup | 0.0276 s |
| Cold current/peak Python memory | 918 / 1,712 KiB |
| Warm current/peak Python memory | 547 / 548 KiB |
| Cold/warm peak RSS increase | 2,560 / 0 KiB |
| Packet processing p50 / p95 / p99 | 0.0872 / 0.1666 / 0.2480 ms |
| Packet processing maximum | 3.5508 ms |
| Live snapshot p50 / p95 / p99 | 0.0018 / 0.0019 / 0.0020 ms |
| 20-lap ZIP | 1.2294 s; 269,976 bytes |

Results vary by host and filesystem. This benchmark calls the decoded
application path and does not measure socket receive/decode overhead. It is
repeatable and suitable for detecting architectural regressions. The measured
processing latency does not justify adding a recorder queue in this phase.

## Validation

- `npm run check`
- Python compilation of nested source and utility modules passed.
- Frontend production bundling passed.
- 141 Python tests passed with no skips or failures.
- 2 frontend behavioral test files passed with no skips or failures.
- Added regressions cover lazy trace hydration, additive archive
  pagination/filtering, frontend identity maps, disk-backed ZIP output, and
  recording progress during ZIP rendering.

## Remaining risks

- The exact continuity unique-frame set can still grow with recording length;
  it is intentionally retained for exact duplicate counts while full row
  allocation has been removed.
- Legacy callers requesting ZIP bytes still necessarily allocate the final ZIP
  once. HTTP downloads use the scalable streamed path.
- `archive-index.json` optimizes warm startup. The first startup after deleting
  it must parse each original lap once to reconstruct metadata.
