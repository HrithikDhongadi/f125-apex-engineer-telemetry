# Architecture phase 4 — frontend structure

Completed 15 October 2026 against the contracts recorded in
`docs/architecture-baseline-2026-10-15.md`.

This phase implements only **Structure the frontend** from the approved
architecture plan.

## Source and generated-asset boundary

`frontend/main.js` is now the sole ES-module dashboard entry point. Esbuild
bundles that source graph into the committed `static/app.bundle.js` served by
the standard-library HTTP server. The old handwritten `static/app.js` and the
separate generated `static/circuit-profiler.bundle.js` are retired. End users
still require only Python; Node.js remains a development dependency.

Frontend ownership is split as follows:

- `frontend/api/` owns JSON request and POST behavior.
- `frontend/state/` owns archive/timeline caches, request generations and
  refresh-safe selection helpers.
- `frontend/sync/` owns visible-tab polling and timer replacement.
- `frontend/rendering/` and `frontend/components/` own shared DOM and formatting
  primitives.
- `frontend/events/` owns static event registration and cleanup. Listeners on
  transient rendered rows remain colocated with the renderer that creates them.
- `frontend/features/` contains focused Live, Sessions, Analysis, Circuit Stats,
  Circuit Profiler, Settings and Personal Best behavior.
- `frontend/circuit-profiler.js` exports the two Konva canvases as modules; it no
  longer installs constructor globals on `window`.
- `frontend/main.js` composes these boundaries with the existing page markup and
  HTTP contracts.

## Refresh and interaction guarantees

Archive and comparison requests use independent monotonically increasing
generation gates. A response applies only while its generation remains current;
selecting another session invalidates the previous archive generation. This
prevents a slow response for the old session from repainting the new selection.

Background refresh remains change-aware and pauses in hidden tabs. Dirty circuit
profile drafts are retained, selected export lap IDs are intersected with the
still-available laps, and baseline/candidate source and lap selections are
resolved independently. Settings, rename and track-name draft flags continue to
prevent refreshes from overwriting active edits.

## Tests and generated assets

Browser-independent Node tests cover API errors and POST bodies, stale-response
ordering, polling visibility, event cleanup, draft/export preservation,
independent comparison selectors, live wheel ordering and recorder wording,
analysis compatibility, session filtering, settings payloads and profiler
boundaries.

Python dashboard tests now inspect the authored ES-module source graph rather
than the generated bundle. Assertions tied only to the former monolithic
function declarations or `window` globals were replaced with module-boundary
contracts. Existing HTML, API and user-visible behavior checks remain.

The generated asset is rebuilt with `npm run build:frontend`; the complete
verification command is `npm run check`.

## Compatibility and migration impact

There is no data, API, settings or user migration. Existing recordings, PBs,
exports, circuit profiles and browser workflows keep their current server
contracts. Cached browsers revalidate static assets and load the new single
bundle on the next page refresh.

## Scope boundary

This phase does not alter recorder decisions, persistence schemas, reconstruction
rules or export snapshot behavior. Those remain later tasks in the approved plan.
