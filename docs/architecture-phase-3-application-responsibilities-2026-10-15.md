# Architecture phase 3 — application responsibilities

Completed 15 October 2026 against baseline commit
`ad6d5ef4e40f19eb8ac0928816f6b0e75a1cf289` and the contracts recorded in
`docs/architecture-baseline-2026-10-15.md`.

This phase implements only **Separate application responsibilities** from the
approved architecture plan.

## Problem and resulting ownership

`SessionStore` mixed live recorder state with archive hydration, recoverable
deletion, settings updates, diagnostic file management, audited setup changes,
comparison profile selection and offline report startup. That made offline work
capable of triggering live-startup mutations such as interrupting open manifests
and purging expired trash.

Ownership after this phase:

- `application/archive.py` owns compatibility loading of legacy laps, session
  manifests, lap JSON, notes, atomic JSON replacement and original lap bytes.
- `application/administration.py` owns session trash/delete/restore/retention and
  validated settings update workflows.
- `application/diagnostics.py` owns bounded, opt-in recorder decision files and
  their segment state.
- `application/amendments.py` owns historical setup validation, immutable audit
  backups, provenance changes and PB snapshot synchronization.
- `application/comparisons.py` owns profile/session compatibility and the shared
  baseline/candidate profile-selection policy used by both HTTP comparison and
  downloaded analysis reports.
- `application/profiles.py` owns profile save/delete administration and applying
  a compatible profile selection to session metadata.
- `application/offline_reports.py` owns read-only Markdown regeneration from
  stored files and read-only SQLite queries.
- `SessionStore` remains the compatibility façade and live recorder coordinator.
  Its public methods and response shapes delegate to these services.
- `utils/reprocess_session.py` now uses `OfflineReportService`; it does not create
  `SessionStore`, start recorder infrastructure, interrupt an open manifest or
  run trash retention.

## Preserved behavior and compatibility

- Existing API routes, `SessionStore` methods, exceptions and response fields are
  unchanged.
- Legacy lap files and current session/lap JSON shapes remain unchanged.
- Open manifests are still marked interrupted when the live application starts;
  the behavior is now an explicit archive-load option.
- Delete/restore protection, retention, notes and independent PB snapshots are
  preserved.
- Diagnostic filenames, limits, reason records and opt-in behavior are
  preserved, including compatibility with the existing patched event limit in
  tests.
- Setup amendments still retain the exact original lap/PB bytes, mark only user
  values as manual, and synchronize matching PB records.
- Race comparison compatibility still requires matching selected profiles across
  sessions; selection still validates packet format and numeric track identity.
- No recorder decision, packet ordering, threshold, lock model, database schema,
  frontend asset or endpoint contract changed.

## Verification

`npm run check` completed successfully:

- recursive Python compilation passed;
- frontend bundle verification passed;
- 130 tests passed with zero failures and zero skips;
- the locally installed recorded Spa regression passed; and
- `git diff --check` passed.

New tests verify that application services do not import live delivery or the
recorder, and that offline report regeneration leaves an open session manifest
and an expired trash entry byte-for-byte untouched.

## Compatibility and migration impact

There is no data, API, CLI or user migration. The offline reprocessing command
and arguments are unchanged. Existing recordings, diagnostics, audit records,
PB files and trash entries continue to use their current paths and formats.

## Remaining planned boundaries

The recorder decision engine, frontend modules, SQLite reconstruction/export
snapshot work and archive scaling remain in later approved phases. This phase
does not begin or add any of those tasks.
