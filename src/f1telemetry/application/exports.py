"""Consistent export capture and lock-free report rendering."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

from ..circuits import track_name
from ..domain.models import Lap, RecordingSession
from ..race_store import PacketStoreSnapshot, RaceSessionStore
from ..reports import (
    ExportError,
    build_lap_analysis_markdown,
    build_markdown,
    build_zip_file,
    safe_filename,
)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(values: dict[str, Path]) -> str:
    digest = hashlib.sha256()
    for key in sorted(values):
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        with values[key].open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass
class CapturedSessionExport:
    """Pinned inputs from one recorder revision; safe after its lock is released."""

    session: RecordingSession
    laps: list[Lap]
    notes: dict[str, str]
    circuit_profile: dict[str, Any] | None
    raw_laps: dict[str, bytes]
    raw_lap_paths: dict[str, Path]
    raw_lap_directory: Path | None
    database: PacketStoreSnapshot | None
    scope: str
    export_format: str
    report_type: str
    revision: dict[str, Any]

    def close(self) -> None:
        if self.database is not None:
            self.database.close()
        if self.raw_lap_directory is not None:
            shutil.rmtree(self.raw_lap_directory, ignore_errors=True)


@dataclass(frozen=True)
class CapturedComparisonExport:
    baseline: Lap
    candidate: Lap
    baseline_note: str
    candidate_note: str
    profile: dict[str, Any] | None


def capture_session_export(
    *,
    archive: Any,
    sessions_dir: Path,
    session: RecordingSession,
    available_laps: list[Lap],
    notes: dict[str, str],
    circuit_profile: dict[str, Any] | None,
    active_store: RaceSessionStore | None,
    scope: str,
    lap_ids: list[str],
    export_format: str,
    report_type: str,
) -> CapturedSessionExport:
    """Capture an export at the caller's ordered recorder-lock barrier."""
    if scope == "session":
        included = list(available_laps)
    elif scope == "selected":
        requested = list(dict.fromkeys(lap_ids))
        if not requested:
            raise ExportError("Select at least one lap before exporting")
        lookup = {lap.id: lap for lap in available_laps}
        unknown = [lap_id for lap_id in requested if lap_id not in lookup]
        if unknown:
            raise ExportError(f"Lap does not belong to this session: {unknown[0]}")
        included = [lookup[lap_id] for lap_id in requested]
    else:
        raise ExportError("Export scope must be 'session' or 'selected'")
    if export_format not in {"markdown", "zip"}:
        raise ExportError("Export format must be 'markdown' or 'zip'")

    database_path = sessions_dir / session.id / session.race_store_file if session.race_store_file else None
    if not included and not (scope == "session" and database_path and database_path.exists()):
        raise ExportError("This export contains no completed laps")

    session_copy = deepcopy(session)
    lap_copies = deepcopy(included)
    notes_copy = deepcopy(notes)
    profile_copy = deepcopy(circuit_profile)
    raw_laps: dict[str, bytes] = {}
    raw_lap_paths: dict[str, Path] = {}
    raw_lap_directory = None
    if export_format == "zip":
        raw_lap_directory = Path(tempfile.mkdtemp(prefix="apex-export-laps-"))
        try:
            for lap in included:
                source = archive.lap_path(lap)
                target = raw_lap_directory / f"{lap.id}.json"
                shutil.copyfile(source, target)
                raw_lap_paths[lap.id] = target
        except OSError as error:
            shutil.rmtree(raw_lap_directory, ignore_errors=True)
            raise ExportError(f"Original JSON is unavailable for lap {lap.id}") from error

    database = None
    try:
        if active_store is not None:
            database = active_store.capture_snapshot()
        elif database_path and database_path.exists():
            database = RaceSessionStore.capture_read_snapshot(database_path)
        captured_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        manifest_payload = asdict(session_copy)
        lap_payload = [asdict(lap) for lap in lap_copies]
        revision_basis = {
            "format_version": 1,
            "session_id": session_copy.id,
            "completed_lap_ids": [lap.id for lap in lap_copies],
            "manifest_sha256": _digest(manifest_payload),
            "notes_sha256": _digest(notes_copy),
            "laps_sha256": _digest(lap_payload),
            "profile_sha256": _digest(profile_copy) if profile_copy is not None else None,
            "raw_laps_sha256": _file_digest(raw_lap_paths) if raw_lap_paths else None,
            "packet_watermark": database.packet_watermark if database else None,
            "event_watermark": database.event_watermark if database else None,
        }
        revision = {
            **revision_basis,
            "revision_id": _digest(revision_basis),
            "captured_at": captured_at,
        }
        return CapturedSessionExport(
            session_copy, lap_copies, notes_copy, profile_copy, raw_laps,
            raw_lap_paths, raw_lap_directory, database, scope, export_format, report_type, revision,
        )
    except Exception:
        if database is not None:
            database.close()
        if raw_lap_directory is not None:
            shutil.rmtree(raw_lap_directory, ignore_errors=True)
        raise


def render_session_export(
    snapshot: CapturedSessionExport, *, file_output: bool = False,
) -> tuple[str, str, bytes | Path]:
    """Perform reconstruction, report rendering and ZIP creation without recorder state."""
    try:
        timeline = None
        storage_summary = None
        continuous_data = None
        scoped_packets = None
        if snapshot.database is not None:
            lap_numbers = [lap.number for lap in snapshot.laps]
            try:
                timeline = RaceSessionStore.read_timeline(
                    snapshot.database.path,
                    min(lap_numbers) if lap_numbers else None,
                    max(lap_numbers) if lap_numbers else None,
                )
                storage_summary = RaceSessionStore.read_summary(snapshot.database.path)
            except sqlite3.Error:
                storage_summary = {"path": snapshot.session.race_store_file, "error": "unreadable"}

        report = build_markdown(
            snapshot.session,
            snapshot.laps,
            snapshot.notes,
            track_name(snapshot.session.track_id, snapshot.session.track_name_override),
            timeline,
            storage_summary,
            snapshot.circuit_profile,
            snapshot.report_type,
        )
        resolved_type = snapshot.report_type if snapshot.report_type != "auto" else (
            "race" if snapshot.session.mode == "race" else
            "time_trial" if snapshot.session.mode == "time_trial" else
            "lap_analysis"
        )
        base = safe_filename(f"{snapshot.session.name}-{resolved_type.replace('_', '-')}")
        if snapshot.export_format == "markdown":
            return f"{base}.md", "text/markdown; charset=utf-8", report.encode("utf-8")

        continuous_path = None
        if snapshot.database is not None and snapshot.scope == "session" and file_output:
            continuous_path = snapshot.database.path
        elif snapshot.database is not None and snapshot.scope == "session":
            continuous_data = snapshot.database.read_bytes()
        elif snapshot.database is not None and snapshot.scope == "selected":
            starts = [lap.first_session_time for lap in snapshot.laps if lap.first_session_time is not None]
            ends = [lap.last_session_time for lap in snapshot.laps if lap.last_session_time is not None]
            if starts and ends:
                scoped_packets = RaceSessionStore.read_packet_export(
                    snapshot.database.path,
                    max(0.0, min(starts) - 1.0),
                    max(ends) + 1.0,
                )
        path = build_zip_file(
            report, snapshot.session, snapshot.laps, snapshot.raw_laps,
            timeline, continuous_data, scoped_packets, snapshot.circuit_profile,
            snapshot.revision, continuous_path=continuous_path,
            raw_lap_paths=snapshot.raw_lap_paths,
        )
        if file_output:
            body = path
        else:
            try:
                body = path.read_bytes()
            finally:
                path.unlink(missing_ok=True)
        return f"{base}.zip", "application/zip", body
    finally:
        snapshot.close()


def render_comparison_export(snapshot: CapturedComparisonExport) -> tuple[str, str, bytes]:
    report = build_lap_analysis_markdown(
        snapshot.baseline,
        snapshot.candidate,
        snapshot.baseline_note,
        snapshot.candidate_note,
        track_name(snapshot.baseline.track_id),
        snapshot.profile,
    )
    filename = safe_filename(
        f"{track_name(snapshot.baseline.track_id)}-lap-{snapshot.baseline.number}-vs-{snapshot.candidate.number}-analysis"
    )
    return f"{filename}.md", "text/markdown; charset=utf-8", report.encode("utf-8")
