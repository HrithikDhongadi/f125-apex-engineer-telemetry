"""Car-setup field metadata, compatibility and manual amendment validation."""

from __future__ import annotations

import math
from typing import Any


SETUP_FIELDS = (
    {"key": "front_wing", "label": "Front wing", "type": "int", "min": 0, "max": 50, "unit": ""},
    {"key": "rear_wing", "label": "Rear wing", "type": "int", "min": 0, "max": 50, "unit": ""},
    {"key": "on_throttle_diff", "label": "On-throttle differential", "type": "int", "min": 0, "max": 100, "unit": "%"},
    {"key": "off_throttle_diff", "label": "Off-throttle differential", "type": "int", "min": 0, "max": 100, "unit": "%"},
    {"key": "front_camber", "label": "Front camber", "type": "float", "min": -10, "max": 0, "unit": "°"},
    {"key": "rear_camber", "label": "Rear camber", "type": "float", "min": -10, "max": 0, "unit": "°"},
    {"key": "front_toe", "label": "Front toe", "type": "float", "min": -1, "max": 1, "unit": "°"},
    {"key": "rear_toe", "label": "Rear toe", "type": "float", "min": -1, "max": 1, "unit": "°"},
    {"key": "front_suspension", "label": "Front suspension", "type": "int", "min": 0, "max": 50, "unit": ""},
    {"key": "rear_suspension", "label": "Rear suspension", "type": "int", "min": 0, "max": 50, "unit": ""},
    {"key": "front_anti_roll_bar", "label": "Front anti-roll bar", "type": "int", "min": 0, "max": 50, "unit": ""},
    {"key": "rear_anti_roll_bar", "label": "Rear anti-roll bar", "type": "int", "min": 0, "max": 50, "unit": ""},
    {"key": "front_ride_height", "label": "Front ride height", "type": "int", "min": 0, "max": 100, "unit": ""},
    {"key": "rear_ride_height", "label": "Rear ride height", "type": "int", "min": 0, "max": 100, "unit": ""},
    {"key": "brake_pressure", "label": "Brake pressure", "type": "int", "min": 0, "max": 100, "unit": "%"},
    {"key": "brake_bias", "label": "Front brake bias", "type": "int", "min": 0, "max": 100, "unit": "%"},
    {"key": "engine_braking", "label": "Engine braking", "type": "int", "min": 0, "max": 100, "unit": "%"},
    {"key": "rear_left_tyre_pressure_psi", "label": "Rear-left configured tyre pressure", "type": "float", "min": 10, "max": 40, "unit": "PSI"},
    {"key": "rear_right_tyre_pressure_psi", "label": "Rear-right configured tyre pressure", "type": "float", "min": 10, "max": 40, "unit": "PSI"},
    {"key": "front_left_tyre_pressure_psi", "label": "Front-left configured tyre pressure", "type": "float", "min": 10, "max": 40, "unit": "PSI"},
    {"key": "front_right_tyre_pressure_psi", "label": "Front-right configured tyre pressure", "type": "float", "min": 10, "max": 40, "unit": "PSI"},
    {"key": "ballast", "label": "Ballast", "type": "int", "min": 0, "max": 50, "unit": ""},
    {"key": "fuel_load_kg", "label": "Fuel load", "type": "float", "min": 0, "max": 150, "unit": "kg"},
)
SETUP_FIELD_MAP = {field["key"]: field for field in SETUP_FIELDS}
SETUP_KEYS = tuple(SETUP_FIELD_MAP)
PROVENANCE_KEY = "_provenance"


class SetupValidationError(ValueError):
    pass


def with_provenance(setup: dict[str, Any] | None, source: str) -> dict[str, Any] | None:
    if setup is None:
        return None
    result = dict(setup)
    provenance = dict(result.get(PROVENANCE_KEY) or {})
    for key in SETUP_KEYS:
        if key in result:
            provenance.setdefault(key, source)
    result[PROVENANCE_KEY] = provenance
    return result


def validate_manual_values(values: Any) -> dict[str, int | float]:
    if not isinstance(values, dict) or not values:
        raise SetupValidationError("Supply at least one setup field to change")
    clean: dict[str, int | float] = {}
    for key, raw in values.items():
        field = SETUP_FIELD_MAP.get(key)
        if field is None:
            raise SetupValidationError(f"Unknown setup field: {key}")
        if isinstance(raw, bool):
            raise SetupValidationError(f"{field['label']} must be a number")
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise SetupValidationError(f"{field['label']} must be a number") from None
        if not math.isfinite(number):
            raise SetupValidationError(f"{field['label']} must be finite")
        if number < field["min"] or number > field["max"]:
            raise SetupValidationError(
                f"{field['label']} must be between {field['min']} and {field['max']} {field['unit']}".strip()
            )
        if field["type"] == "int":
            if not number.is_integer():
                raise SetupValidationError(f"{field['label']} must be a whole number")
            clean[key] = int(number)
        else:
            clean[key] = float(number)
    return clean


def setup_rows(setup: dict[str, Any] | None) -> list[dict[str, Any]]:
    values = setup or {}
    provenance = values.get(PROVENANCE_KEY) if isinstance(values.get(PROVENANCE_KEY), dict) else {}
    return [
        field | {
            "value": values.get(field["key"]),
            "source": provenance.get(field["key"], "legacy_decoded_udp" if field["key"] in values else None),
        }
        for field in SETUP_FIELDS
    ]
