# Apex Engineer — architecture review and agent implementation plan

Reviewed 15 October 2026. Repository: [HrithikDhongadi/f125-apex-engineer-telemetry](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry).

**Source snapshot:** `13b1ad572e23d38305fe4d0bca59c9935af2259d`, dated 14 October 2026, “feat: rename race UID guard to session UID guard and update related logic”. Recommendations below apply to this snapshot. Recheck the diff if implementation starts on a newer revision.

## Recommendation

Choose **Option B: an incremental modular monolith**, retaining Python, SQLite, local files, and the current browser UI technology initially. Establish explicit recorder state, inward dependencies, separate persistence and reconstruction, and frontend feature modules. Preserve existing behavior before optimizing it.

The project is about 5,557 production Python lines, plus a 102,434-byte handwritten `static/app.js`, a 556-line Konva source module, and HTML/CSS. It needs clearer ownership and bounded work, not distributed services. “Good DSA” here means appropriate indexes, incremental summaries, bounded caches, streaming queries, explicit ordering, and testable state transitions.

No implementation changes were made during this review. Conclusions come from current source, tests, developer documentation and the historical Spa audit. This is a static architecture review with baseline checks, not a live-game performance or correctness certification.

## 1. What exists and what should be preserved

The application already separates protocol decoding, circuit identities, quality checks, setup validation, PB storage, profile storage, comparisons, and report generation to a useful extent. Existing defensive packet validation, atomic file replacement, WAL storage, compressed decoded packets, conservative evidence labels and bounded live event queues are good foundations.

The current runtime path is:

1. `UdpReceiver` receives a datagram and validates its header.
2. Selected packet types are decoded into player/session values and sent to `SessionStore` methods.
3. `decode_packet()` decodes the packet for continuous storage, so some decoding is repeated.
4. `record_full_packet()` updates packet state, writes decoded packet evidence and creates inferred/timeline events.
5. Lap-boundary decisions construct lap JSON, update the session manifest and PB registry, then flush continuous storage.
6. A threaded HTTP server and janitor access the same `SessionStore` used by the UDP thread.
7. The browser polls live state and synchronizes archive summaries; comparison, reporting and timeline reconstruction run on demand.

**Important:** the “raw packet” evidence is decoded header/payload JSON, compressed in SQLite. The repository does not currently retain every original UDP datagram byte. Do not describe existing data as a byte-exact datagram archive or claim it can recover undecoded fields.

## 2. Specific findings

| Finding | Source evidence | Consequence / priority |
|---|---|---|
| `SessionStore` has too many responsibilities | `receiver.py`: 1,804 lines; class has 65 methods covering loading, recording, diagnostics, trash, settings, profiles, setup amendments, PB rebuilds and exports | Changes in archive features touch recorder code. Highest structural priority. |
| Recorder transitions are implicit and interleaved with I/O | `record_lap_state()`, lines 847–1139, is 293 lines; decisions, sample pruning, manifest writes and diagnostics share one method | Hard to reason about ordering; extract without changing thresholds or precedence. |
| Exports can block packet handling | `export_session()`, lines 1623–1702, holds `self.lock` through reconstruction, exact storage audit, Markdown generation, DB snapshot and ZIP construction | A large export has a direct path to delaying UDP processing. This is a code-path risk; actual loss was not measured. |
| Offline reporting constructs the recorder | `utils/reprocess_session.py` instantiates `SessionStore`; its constructor purges expired trash, loads the archive and marks open sessions interrupted | Offline read/report operations should not require recorder startup side effects. |
| Archive memory grows with all recorded samples | `_load_all()`, lines 258–301, reads every lap JSON into `Lap.samples`; PB registry also loads embedded traces | Startup and RAM scale with total historical telemetry rather than currently viewed data. |
| Lookup and summaries repeatedly scan data | `get_lap_object()` uses a linear search; `_refresh_lap_index()` rebuilds the flat archive on completion; `Lap.summary()` calls `lap_quality()` | Archive lookups and UI polling can perform unnecessary work. |
| Live payload is not fully O(1) as documented | `live_snapshot()` → `_session_summary()` → `RecordingSession.summary(laps)` uses `asdict(self)` including `lap_ids` and scans lap times; snapshot constructs active and selected summaries | DB scans are avoided, but payload work grows with lap count. The active summary is built even when selected/active match. |
| “Compact” lap list responses still scan traces | `list_all_laps()` calls every lap's `summary()`; quality scans distances in every sample | Initial browser hydration is not just metadata retrieval. |
| Storage and reconstruction share a module | `race_store.py`: 1,130 lines; schema/write batching, continuity, attribution, branches, setup audit, snapshotting and migration coexist | Separate write concerns from read algorithms and migration tooling. |
| Reconstruction contains archive-sized allocations | `_timeline_query()` materializes packet collections; `_continuity_query()` materializes each stream; event matching uses `list.pop(0)` | Optimize with profiling after extraction; avoid assuming every algorithm is currently quadratic. |
| Inner-layer models live in the recorder | `analysis.py` imports `Lap` from `receiver.py`; receiver imports reports inside methods; reports imports analysis | There is a runtime dependency cycle, partly deferred with local imports. Move neutral models first. |
| API handlers know internal representation | `/api/compare` reads `store.sessions` and `store.profile_store`; comparison/profile checks also appear in `export_lap_analysis()` | Policy can drift between API and report paths. |
| Frontend has broad shared mutable state | `static/app.js`: API wrappers, many globals, polling, six views, selectors, statistics, profile editing, charting and event registration | Split by feature and give each state field an owner. A framework is optional. |
| Frontend checks are primarily structural | Most `test_dashboard.py` cases assert source strings, HTML IDs or CSS fragments | Moving code can fail tests without changing behavior; actual race/request/render regressions can still pass. |
| Build/check setup assumes the current flat layout | `py_compile src/f1telemetry/*.py`; frontend build/check only bundles `frontend/circuit-profiler.js` | Nested modules and the main dashboard need explicit build/check coverage after extraction. |

The 14,117-line `static/circuit-profiler.bundle.js` is generated output including dependencies. Do not treat it as a handwritten architecture problem or edit it directly. `style.css` has only 47 lines but 30,095 bytes: formatting obscures its true size.

### Scope of validation

`npm ci --ignore-scripts` and `npm run check` succeeded in a clean checkout. The latter ran Python compilation, the Circuit Profiler build check, and **122 Python tests: 121 passed, one skipped**. The skipped recorded-Spa regression expects `data/sessions/session-20261008T090243-5dd3a5b6/race-data.sqlite3`, which is absent here. The historical audit's older “96 tests” result is not the current suite count. No real game, browser interaction, full race data archive or recording/load benchmark was exercised.

## 3. Architecture options

| Option | Changes | Benefits | Costs / limits | When to choose |
|---|---|---|---|---|---|
| A. Conservative extraction | Move models, archive operations, diagnostics and frontend features into modules; retain `SessionStore` as the implementation center | Small reviewable diffs; fastest readability improvement | Shared state, lock contention and archive memory remain unless separately addressed | You want easier maintenance with minimum behavioral change now. |
| **B. Modular monolith — recommended** | Domain models + recorder engine; application services; explicit repositories; reconstruction/read models; feature-based frontend; compatibility facade during migration | Clear ownership, deterministic replay, scalable archive reads and safe future extension | Several staged PRs; requires behavioral characterization and import-boundary enforcement | Best fit for this project and its existing correctness-sensitive features. |
| C. Platform upgrade | Option B boundaries plus, separately, FastAPI/Pydantic and TypeScript/React or another chosen UI framework | Typed API tooling, richer UI composition and a cleaner path toward larger product features | More dependencies, build/runtime changes and much larger regression surface | You have a concrete need for remote clients, complex UI workflows or a broader team. |

Option C is a later platform decision, not a prerequisite for B. Retain the Python-standard-library user runtime and committed browser bundles for the initial refactor. Microservices, Redis, Kafka, a generic plugin bus and a complete event-sourcing rewrite have no demonstrated need here.

## 4. Target boundaries and folder structure

Dependency rule: **delivery → application → domain**. Infrastructure implements application-facing storage/transport contracts. The composition root wires concrete implementations together. Domain code must not import HTTP, SQLite, paths, sockets or application startup.

This is a destination map, not an instruction to create every file immediately. Combine adjacent small modules until a real responsibility justifies a split; avoid empty interfaces and generic `BaseService` classes.

```text
src/f1telemetry/
  __main__.py
  bootstrap.py                  # paths, dependencies, threads and shutdown
  domain/
    models.py                   # Lap, RecordingSession, IDs/value objects
    recording.py                # RecorderState, observations, decisions
    policies.py                 # mode/start/PB/comparison eligibility policies
    setups.py                   # schema and provenance rules
    circuits.py                 # numeric identity and profile rules
    errors.py
  application/
    recorder.py                 # ordered orchestration and durable effects
    library.py                  # rename, notes, trash, restore, archive selection
    comparisons.py              # shared lookup/profile/compatibility orchestration
    exports.py                  # obtain consistent input and render outside live lock
    amendments.py               # setup edit workflow and audit preservation
    ports.py                    # only the few real storage/clock contracts needed
    queries.py                  # live, session and lap DTO/read-model construction
  telemetry/
    protocol.py                 # retain defensive decoder; split packet families later
    dispatch.py                 # normalized observations and ordered processing
    samples.py                  # sample assembly and channel provenance
  infrastructure/
    udp.py
    persistence/
      sessions.py               # manifests and lap files, legacy reader
      packets.py                # SQLite writer/batching/snapshots
      schema.py                 # schema creation and version compatibility
      personal_bests.py
      profiles.py
      settings.py
      atomic_files.py
    diagnostics.py
  reconstruction/
    timeline.py                 # branch acceptance and source correlation
    attribution.py              # overtakes, penalties, collisions
    continuity.py
    setup_audit.py
  analysis/
    alignment.py
    comparison.py
    turns.py
    calibration.py
  reporting/
    models.py                   # report input/results independent of presentation
    markdown.py                 # split race/TT renderers when justified
    archive.py                  # file-based ZIP creation
  web/
    server.py                   # HTTP transport only
    routes.py                   # split by feature if needed
    serializers.py
  cli/
    reprocess.py
    migrate.py
```

Keep `receiver.py`, `race_store.py`, `server.py` and other old entry points as temporary re-export/delegation facades where callers/tests require them. Do not mix this with renaming the Python package. Preserve `python3 -m src.f1telemetry` initially; a `pyproject.toml` and conventional `python -m f1telemetry`/console command can follow as a separate compatibility change. Update recursive compilation and imports when packages become nested.

### Ownership and API contracts

| Component | Owns | Must not own |
|---|---|---|
| Recorder engine | Current game context, active lap attempt, arming/crossing/restart decisions | HTTP, archive traversal, Markdown, SQLite connections |
| Recorder application service | Ordered input processing, persistence effects, flush/stop coordination | Browser selection state, chart calculations |
| Archive repository/query service | Lap/session lookup, metadata indexes, bounded trace loading | Live lap transitions |
| Packet writer | Appending evidence, sequence IDs, transactions and barriers | Race interpretation or report wording |
| Reconstruction | Accepted/superseded/unknown branches and evidence attribution | Mutating captured packet/lap evidence |
| Comparison service | Resolve two lap references and selected profiles once, enforce common policy | Reading global HTTP handler state |
| Reporting | Render a versioned immutable report model and archive manifest | Stopping a recording or instantiating the live recorder |

Use dataclasses and enums for stable internal objects, and `TypedDict` or equally small schemas for wire dictionaries where useful. Avoid converting every channel dictionary in one PR. Keep exact unsigned session UID handling; any future JSON string representation is a versioned API change because JavaScript cannot exactly represent all uint64 values.

## 5. Recorder state, event ordering and threading

Use an explicit `RecorderState` and structured `RecorderDecision` results with reason codes. An idle/armed/capturing state is separate from a closed recording's completed/stopped/interrupted status. Restart and flashback are transitions with evidence, not permanent lifecycle states.

The extracted engine should consume a normalized observation and explicit clock inputs, returning state changes plus ordered effects such as start recording, complete lap, discard attempt, prune samples, persist manifest and flush. Preserve mutable active sample buffers where copying the whole lap per packet would be costly; “deterministic” does not require expensive immutable copies.

**Preserve today's processing order first:** specialized session/lap/setup/telemetry handling → full-packet state/storage → SEND closure. Some effects depend on the previous packet state. A switch to decode-once must adapt the same decoded values to that order; it must not accidentally store/update the current packet first. Keep raw datagram replay for decoder/dispatch tests and normalized observation replay for engine tests.

Initial migration: one explicit coordinator retains current locking; remove expensive read/report work from that lock using consistent snapshots. Do not give extracted classes independent locks without a lock/ownership design.

Later, if benchmarks justify it: a single recorder owner processes a bounded FIFO of datagrams and control commands. UDP is inherently lossy; a queue cannot promise lossless capture. Capture receive wall/monotonic timestamps and a local sequence at socket ingress, not after queue delay. Expose overflow counts and policy; do not reorder events by game time/frame, because rewinds, duplicates and out-of-order datagrams are meaningful evidence. Stop/start commands need ordered acknowledgements and flush barriers. Queue size and saturation handling must be benchmarked rather than guessed.

The packet writer should have one logical owner. HTTP readers use separate read-only connections. `check_same_thread=False` is not a replacement for ownership. Shutdown must define: stop accepting new work, stop/join reception, drain accepted work or record a bounded failure, finalize the recording, flush and close storage, then finish shutdown. Current `UdpReceiver.stop()` only sets an event; lifecycle coordination belongs in `bootstrap.py`.

### Snapshot/export consistency

Moving work outside the lock is necessary but insufficient. Capture a mutually consistent export revision: included completed-lap IDs, exact manifest/notes/setup/profile revisions and a committed packet watermark. For active recordings, obtain these at an ordered flush barrier; a separate reader must not silently include later packets while metadata remains older. Use an immutable file snapshot or a read transaction constrained to the captured watermark.

Keep archive files pinned/copied or a session-level export lease so deletion, restore, or setup amendment cannot move/change inputs halfway through export. Define lock ordering and use version checks. Render Markdown and create ZIP files after obtaining those inputs. Use a temporary/spooled file and stream the response; the current `BytesIO` ZIP and database `read_bytes()` can require multiple archive-sized allocations. Preserve SQLite's online backup API rather than copying only the main DB while WAL is active.

## 6. Data structures and algorithms suited to this project

Let L = archived laps, S = samples in a lap, P = packet rows, E = timeline events, F = flashbacks, M = aligned points, and C = channels. Complexity below describes targets or current code paths, not measured speedups.

| Area | Current approach | Proposed structure/algorithm | Expected effect |
|---|---|---|---|
| Lap identity lookup | Linear `next(...)` over all laps | `dict[LapId, LapMetadata]` plus trace repository | Average O(1) metadata lookup instead of O(L). |
| Session membership | Lists of full laps plus rebuilt flat list | Ordered lap-ID list per session and central metadata map | Append without rebuilding every archived lap reference; retain chronology. |
| Polling summaries | Recompute valid minima and quality from laps/samples | Incremental `SessionSummary`, cached `LapSummary` with revision | Live response cost independent of archive/sample size; recompute on relevant amendment. |
| Trace loading | Load every historical sample at startup | Lazy lap loading and LRU cache limited by estimated bytes | RAM follows active/viewed traces. Keep on-disk files authoritative. |
| Frontend joins | Repeated `sessions.find(...)` in lap/stat loops | `Map<SessionId, SessionSummary>`, normalized lap maps and memoized selectors | Avoid repeated O(L × session count) joins. |
| Event matching | Per-key lists with `pop(0)` | `deque` per duplicate-match key | O(1) removal while preserving arrival order. |
| Continuity | Materialize each full stream and all gap details | Stream cursor, maintain running counters and top-10 gap heap | Bound gap-detail memory; exact unique-frame count still needs a set or SQL DISTINCT/external strategy. Do not claim fully O(1) memory while retaining arbitrary exact deduplication. |
| Lap alignment | Sort samples and bisect each channel/point | Keep current implementation first; optionally precompute brackets once or use two-pointer merge after sorting | Current approximate cost O(S log S + M C log S); shared brackets reduce repeated searching. M=501 may already be adequate. |
| Turn/editor distance lookup | Linear nearest-distance reduction | Sorted distance array plus binary search | O(log S) nearest lookup. A spatial tree is unnecessary unless map hit-testing is measured as slow. |
| Flashback reconstruction | Per-event scans over flashbacks and SQL target searches | Extract first; then sequence interval indexes/sweep with binary-search queries | Reduce repeated E×F work; nested/overlapping rewinds must retain current superseded-by and unknown semantics. No blanket O(P) promise. |
| Archive delivery | All summaries hydrated in browser; client pagination | Add indexed server-side filtered/paginated queries after compatibility phase | Browser/server work follows requested page. Existing endpoint responses stay stable until callers migrate. |

For persistent metadata, start with derived sidecar summaries or a rebuildable SQLite catalog over the existing lap/session files. A catalog can index track, mode, date, session and lap time without replacing the packet DB or rewriting old JSON. Define the index as a cache: version it, invalidate on mutation, rebuild after restore, and recover cleanly after a crash.

Timeline rows should eventually reference their exact source packet sequence. Make packet append return its sequence and include source references in new event records; preserve old deterministic correlation for historical rows. This needs an explicit additive schema migration, not a silent schema edit inside a file-move PR. Inferred events may require references to both previous and current packets.

Do not deduplicate away captured packet rows. Detect/label duplicates in derived analysis. Do not replace official timing with integrated speed. Keep existing wheel order, units, validity, comparison thresholds and profile uncertainty unchanged during structural work.

Sample assembly currently combines latest available values from several packet types. Later, attach per-channel source time/frame and age so stale pre-rewind or cross-lap values can be identified. Introducing freshness limits changes analysis semantics and needs its own version and tests.

## 7. Frontend organization

Use the existing esbuild toolchain to bundle a normal ES-module entry point into a committed dashboard asset. Node remains a developer dependency. Move behavior before redesigning screens.

```text
frontend/
  main.js
  api/client.js
  state/store.js                # normalized sessions/laps/PBs and revisions
  state/selectors.js            # pure filtering, sorting and summary selectors
  sync/polling.js               # live/archive schedules, visibility and cancellation
  features/
    live/                      # live instruments and recording controls
    sessions/                  # archive selection, lap table, notes and exports
    analysis/                  # independent baseline/candidate and charts
    circuit-stats/             # filters, ranking and aggregate read views
    circuit-profiler/          # draft model, form/editor controller
    settings/                  # settings drafts and trash restore
    personal-bests/            # registry subpanel used by analysis
  components/                  # pager, lap row, setup dialog, shared formatting
  maps/                        # CircuitProfilerCanvas, PaceMapCanvas, viewport helpers
  styles/                      # tokens, layout, shared components, feature CSS
static/
  index.html
  app.bundle.js                # generated; do not edit
  style.css                    # generated if CSS is bundled
```

Distinguish server-owned live state, archive cache, local selection, comparison selection and unsaved form drafts. A selected historical session must never change the live recorder. Existing server-level selection endpoints can remain for compatibility; client-local multi-tab selection is a later API/product change.

Give features explicit initialization/disposal hooks and dependency injection through arguments, not a cross-imported global store singleton everywhere. Preserve non-overlapping polling, hidden-tab pause, lazy historical timelines, export selection across pagination, independent comparison selectors, stale-request protection and dirty forms. Use request generation tokens/AbortController where appropriate so responses for an old selection cannot repaint the current view. Keep the profile draft as data rather than deriving its entire state from DOM fields, but stage that change after extraction.

## 8. Migration sequence and completion gates

| Stage | Concrete work | Acceptance gate |
|---|---|---|
| 0. Characterize | Record source SHA; inventory routes/JSON keys/files/import users; capture deterministic TT/race/restart/rewind traces; add meaningful HTTP/browser behavior coverage | Current full checks pass; fixtures are portable and reproducible; the unavailable Spa regression is explicitly tracked. |
| 1. Models and composition | Extract `Lap`, `RecordingSession`, errors, serializers and clock/ID seams; introduce `bootstrap.py`; preserve old imports | No domain import of receiver/web/storage; existing CLI and response shapes unchanged. |
| 2. Non-recorder services | Extract archive repositories, trash/settings/profile workflows, diagnostics and setup amendments; create shared comparison service; offline reports stop constructing `SessionStore` | File formats/IDs/provenance unchanged; offline report leaves source data, trash and open manifests untouched. |
| 3. Frontend modules | Add dashboard bundle entry; extract API/state/sync/shared components and each feature; format CSS | New bundle builds; behavior tests cover polling, drafts, selection, map editing and exports; no generated file edited manually. |
| 4. Recorder engine | Extract observations, state/decision logic, sample builder and ordered effects; keep current lock/thread model | Replay outputs and decision reasons match the original for all characterized cases; threshold/order changes excluded. |
| 5. Storage/reconstruction and export | Separate SQLite write/read/migration, branch/attribution/continuity; implement consistent export snapshot and render outside recorder lock | Historical outputs preserved, export concurrency test passes, SQLite v1/v2 read compatibility and migration digest checks pass. |
| 6. DSA and archive scale | Metadata map/catalog, incremental summaries, lazy byte-bounded traces, measured query indexes and streaming ZIPs; add paginated API additively | Startup/idle RAM no longer proportional to all trace samples; live query performs no historical sample traversal; old API still works. |
| 7. Optional concurrency/platform | Evaluate recorder queue, background reconstruction cache, new packaging, framework or TypeScript independently | Benchmarks demonstrate a need and benefit; overflow/drain/failure behavior proven; separate ADR for each change. |

Stages 2 and 3 can be scheduled independently after stage 1, but each PR needs a clear owner and scoped behavior. Do not combine state-machine replacement, database-format changes, API changes and a UI framework migration in one PR. The first implementation PR should finish stages 0/1 only; it should not attempt the entire target tree.

### Invariants that every stage must preserve

- TT manual runs arm on partial approaches, save verified complete laps using actual game lap numbers, and create no placeholder laps.
- Restart discards the abandoned partial attempt; same-number and advanced-number transitions remain distinguished using existing evidence.
- Flashback pruning and game validity remain separate. Invalid laps stay available for diagnostic comparisons and cannot replace valid PB evidence.
- Automatic practice/qualifying/race capture, grid lap 1, one-shot starts, same-UID phase changes, manual-stop suppression and trailing-UID guards remain intact.
- Numeric circuit identity/layout and Race/TT PB separation remain intact; practice/qualifying do not enter those PB registries.
- Official-time-only PBs remain distinct from separately available comparison traces, including traces from another source session.
- New reports do not rewrite captured evidence. Branch validity, player attribution, warnings versus penalties, and uncertain conclusions keep their labels.
- Setup configured pressures/fuel stay distinct from measured pressure/fuel remaining; manual amendments retain audit originals and update relevant PB copies.
- Legacy files, SQLite text/compressed formats, exact uint64 UIDs and online snapshots remain readable/correct.
- Trash/restore preserves notes and independent PB snapshots; active and legacy sessions stay protected.
- Baseline/candidate delta remains candidate minus baseline; profile/turn verification and overlapping windows keep their present meaning.

## 9. Tests and measurements the agent should use

Retain existing session/protocol/PB/storage/export tests. Migrate source-string dashboard checks toward observable behavior; a moved function should not require leaving dummy code in `app.js` merely to satisfy a grep assertion. Useful source-level checks remain suitable for import boundaries and generated-asset references.

Add targeted coverage for: live UDP processing while a large export runs; coherent export inputs during notes/setup/delete operations; partial persistence failures and recovery; stop/drain ordering if a queue is introduced; stale frontend responses; dirty forms during background updates; server errors and missing data; and profile/map behavior. Characterize replay outputs before replacing recorder decisions. Normalize timestamps/IDs for comparisons; compare ZIP member names and decoded contents rather than compressed bytes. Preserve exact original lap bytes when the export contract requires them.

The skipped Spa test is a real validation gap: make a small sanitized fixture available through a documented fixture mechanism, or run the full original-data regression where the user keeps it. Do not fabricate Spa evidence or change the expected 9/4 accepted overtakes solely to make a new implementation pass.

Measure a repeatable replay workload with all relevant packet types (not just one 60 Hz stream). Compare: ingress-to-processing latency p50/p95/p99, maximum stall, queue depth/overflow if applicable, packet counts by stream, live API latency, export time, startup time, and peak RSS. Include a long synthetic archive and a representative real race. Record hardware and fixture size; choose thresholds after establishing baseline. No arbitrary millisecond guarantee is implied by this review.

### Failure and recovery boundaries

Atomic replacement of one JSON file is not a transaction across lap JSON, manifest and PB files. Define which captured evidence is authoritative and how startup reconciles orphan lap files, stale manifests, lagging PB/index updates and interrupted amendments. Existing setup audits must survive extraction; do not claim multi-file atomicity merely because each file uses `replace()`. Add revision/generation guards and a small recovery journal only where a demonstrated multi-file workflow needs it.

## 10. Copy/paste instruction for the coding agent

```text
Refactor HrithikDhongadi/f125-apex-engineer-telemetry incrementally using
Option B in the attached architecture review. The review inspected commit
13b1ad572e23d38305fe4d0bca59c9935af2259d. First inspect the current branch and
explain any changes since that snapshot that affect the plan.

For the FIRST PR ONLY:
1. Run npm ci and npm run check. Report the exact pass/skip results and whether
   the recorded Spa fixture is available. Do not claim live validation.
2. Inventory public entry points, API request/response shapes, persistence
   formats, ordering-sensitive recorder behavior and test dependencies.
3. Add/retain behavioral characterization for the highest-risk paths; use
   deterministic clock/ID inputs and portable replay fixtures where available.
4. Move neutral Lap/RecordingSession models and related exceptions into a
   domain package without changing JSON shapes, IDs, summaries or thresholds.
5. Add a small composition root and preserve existing CLI/import compatibility
   through re-exports or delegation. Update compilation to cover nested modules.
6. Enforce that domain code imports neither receiver/web nor persistence.
7. Leave packet ordering, lap-boundary algorithm, lock ownership, data schema,
   endpoint behavior, generated bundles and UI behavior unchanged in this PR.

Then present a revised per-PR checklist for stages 2–7. Keep the stdlib Python
runtime, SQLite, local files and current UI technology initially. Do not add
microservices, a generic event bus, an ORM or a frontend framework without a
separate concrete requirement. Move code by responsibility, not fixed line size.

Subsequent priorities: remove offline-report startup side effects; extract
archive/services and frontend modules; isolate a deterministic recorder engine;
separate packet writing from reconstruction; obtain consistent export snapshots
and render outside the recorder lock; introduce metadata indexes, cached
summaries, lazy trace loading and streaming archives with measured tests.

Preserve all invariants in the attached review, especially TT first-full-lap,
Restart Lap, flashback pruning/game validity, automatic race/practice/qualifying,
same-UID phases and guards, official-time-only PBs, provenance, archive/trash
recovery, legacy/SQLite v1-v2 compatibility and unsigned session identity.

For every PR report the problem, resulting ownership/dependency changes,
behavior preserved or deliberately changed, checks and skipped coverage,
compatibility/migration impact, and remaining risks. Do not weaken tests or
invent fixtures to hide regressions. Keep a working application after each PR.
```

## 11. Implementation checklist

Complete each phase in a separate, reviewable PR where practical. Every phase must leave the application working. Check items only when implemented and verified; record unavailable fixtures or deferred work explicitly.

### 1. Establish the baseline

- [ ] Record the current commit and check differences from reviewed commit `13b1ad5`.
- [ ] Run `npm ci` and `npm run check`; report failures and skipped tests.
- [ ] Inventory CLI commands, API contracts, file formats, and generated assets.
- [ ] Capture expected behavior for first laps, restarts, flashbacks, session changes, and PB updates.
- [ ] Identify how to supply the missing recorded-Spa regression fixture.

### 2. Separate models and startup

- [ ] Move `Lap`, `RecordingSession`, and shared exceptions into neutral domain modules.
- [ ] Remove analysis dependencies on `receiver.py`.
- [ ] Extract dependency creation, thread startup, and shutdown into `bootstrap.py`.
- [ ] Preserve existing imports and `python3 -m src.f1telemetry`.
- [ ] Update checks to include nested Python modules.

### 3. Separate application responsibilities

- [ ] Extract session/lap file access and archive loading.
- [ ] Extract trash, restoration, settings, and diagnostics workflows.
- [ ] Extract setup amendments while preserving audit backups and PB updates.
- [ ] Centralize comparison compatibility and profile-selection policies.
- [ ] Make offline reporting independent of recorder startup and its side effects.

### 4. Structure the frontend

- [ ] Create an ES-module dashboard entry point using the existing build tool.
- [ ] Separate API access, state, polling, rendering, and event registration.
- [ ] Extract Live, Sessions, Analysis, Circuit Stats, Circuit Profiler, Settings, and PB features.
- [ ] Preserve unsaved drafts, export selections, and independent comparison selectors.
- [ ] Prevent stale responses from repainting a newly selected session.
- [ ] Add behavioral tests for critical interactions; replace fragile source-string assertions where appropriate.
- [ ] Rebuild generated assets from source.

### 5. Isolate the recorder

- [ ] Define explicit recorder state, observations, decisions, and reason codes.
- [ ] Extract lap-boundary decisions from persistence and diagnostics.
- [ ] Preserve current packet-processing order, thresholds, and decision precedence.
- [ ] Verify first-full-lap, same-number restart, flashback pruning, grid-start, and qualifying-phase regressions.
- [ ] Keep the existing threading model during extraction.

### 6. Separate storage, reconstruction, and exports

- [ ] Separate SQLite writing, schema handling, reconstruction, continuity, and migration.
- [ ] Preserve accepted/superseded/unknown branches and player attribution.
- [ ] Capture consistent export inputs at a defined recording revision.
- [ ] Move report rendering and ZIP creation outside the recorder lock.
- [ ] Protect exports against concurrent deletion or setup amendments.
- [ ] Preserve SQLite WAL-safe snapshots and legacy/text/compressed readers.
- [ ] Verify recording continues correctly during a large export.

### 7. Improve data structures and performance

- [ ] Introduce dictionaries for lap/session identity lookup.
- [ ] Cache lap summaries and maintain session summaries incrementally.
- [ ] Load historical telemetry lazily through a byte-bounded cache.
- [ ] Replace unnecessary full-table allocations with streaming queries.
- [ ] Use `deque` for ordered event matching.
- [ ] Generate large ZIPs through temporary files instead of archive-sized memory buffers.
- [ ] Add server-side archive pagination without breaking existing callers.
- [ ] Benchmark startup time, RAM, processing latency, live API latency, and exports before considering a recorder queue.

### Acceptance for every PR

- [ ] All applicable checks pass; skipped coverage is explained.
- [ ] Existing recordings, IDs, official timing, validity, provenance, and PB distinctions remain intact.
- [ ] Any intentional API or storage change has a documented migration and recovery path.
- [ ] PR description explains ownership changes, validation, compatibility, and remaining risks.
- [ ] No unrelated framework migration, algorithm rewrite, or generated-file hand editing is included.

## Source pointers

Links are pinned to the inspected revision so later file changes do not invalidate the review's evidence:

- [Recorder, models and archive facade](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/src/f1telemetry/receiver.py)
- [Continuous storage and reconstruction](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/src/f1telemetry/race_store.py)
- [HTTP routes and composition](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/src/f1telemetry/server.py)
- [Analysis and alignment](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/src/f1telemetry/analysis.py)
- [Dashboard state and rendering](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/static/app.js)
- [Dashboard structural tests](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/tests/test_dashboard.py)
- [Developer guide](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/docs/dev.md)
- [Historical Spa evidence audit](https://github.com/HrithikDhongadi/f125-apex-engineer-telemetry/blob/13b1ad572e23d38305fe4d0bca59c9935af2259d/docs/spa-race-correctness-audit-2026-10-09.md)
