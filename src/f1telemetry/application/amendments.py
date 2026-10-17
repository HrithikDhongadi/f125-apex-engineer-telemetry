"""Audited historical setup amendments and PB synchronization."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable

from ..domain.errors import SessionStateError
from ..domain.models import Lap
from ..personal_bests import PersonalBestRegistry
from ..setups import SetupValidationError, setup_rows, validate_manual_values, with_provenance
from .archive import SessionArchiveRepository


class SetupAmendmentService:
    def __init__(
        self,
        archive: SessionArchiveRepository,
        personal_bests: PersonalBestRegistry,
        now: Callable[[], str],
    ) -> None:
        self.archive = archive
        self.personal_bests = personal_bests
        self.now = now

    def amend(self, lap: Lap | None, values: Any, note: str = "") -> dict[str, Any]:
        clean = validate_manual_values(values)
        if lap is None:
            raise SessionStateError("Lap not found")
        lap_path = self.archive.lap_path(lap)
        try:
            original_lap = lap_path.read_bytes()
            lap_payload = json.loads(original_lap)
        except (OSError, ValueError, TypeError) as error:
            raise SessionStateError("Original lap JSON is unavailable or invalid") from error

        current = with_provenance(lap.setup, "legacy_decoded_udp") or {"_provenance": {}}
        changed = {key: value for key, value in clean.items() if current.get(key) != value}
        if not changed:
            raise SetupValidationError("No setup values changed")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        audit_dir = self.archive.capture_dir / "audit" / "setup-amendments" / lap.id / stamp
        audit_dir.mkdir(parents=True, exist_ok=False)
        (audit_dir / "original-lap.json").write_bytes(original_lap)

        pb_entry = next(
            (entry for entry in self.personal_bests.entries.values() if entry.get("source_lap_id") == lap.id),
            None,
        )
        if pb_entry is not None:
            pb_path = self.personal_bests.path_for(str(pb_entry["key"]))
            try:
                (audit_dir / "original-personal-best.json").write_bytes(pb_path.read_bytes())
            except OSError as error:
                raise SessionStateError("PB record exists but cannot be backed up; no files were changed") from error

        provenance = dict(current.get("_provenance") or {})
        current.update(changed)
        provenance.update({key: "manual" for key in changed})
        current["_provenance"] = provenance
        current["_amended_at"] = self.now()
        current["_audit_record"] = str(audit_dir.relative_to(self.archive.capture_dir))
        manifest = {
            "lap_id": lap.id,
            "source_session_id": lap.recording_session_id,
            "created_at": current["_amended_at"],
            "changed_fields": changed,
            "preserved_fields": {
                key: value for key, value in (lap.setup or {}).items()
                if not key.startswith("_") and key not in changed
            },
            "pb_key": pb_entry.get("key") if pb_entry else None,
        }
        (audit_dir / "amendment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        lap_payload["setup"] = current
        self.archive.atomic_json(lap_path, lap_payload, compact=True)
        lap.setup = current
        pb_synced = False
        if pb_entry is not None:
            self.personal_bests.update_source_setup(lap.id, current)
            pb_synced = True
        return {
            "lap": lap.summary(note),
            "setup_fields": setup_rows(current),
            "changed_fields": list(changed),
            "pb_synced": pb_synced,
            "pb_message": (
                "Personal best snapshot synchronized"
                if pb_synced
                else "No PB snapshot found; the corrected lap will sync when the PB registry is built"
            ),
            "audit_record": str(audit_dir.relative_to(self.archive.capture_dir)),
        }
