"""Persistent, validated runtime settings exposed by the local dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from typing import Any
from uuid import uuid4


DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": 1,
    "web_host": "127.0.0.1",
    "web_port": 8025,
    "udp_port": 20777,
    "trash_retention_days": 30,
    "max_recording_gb": 8.0,
    "diagnostic_max_mb": 50,
    "dashboard_refresh_seconds": 8,
}


class SettingsError(ValueError):
    pass


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return self.validate({**DEFAULT_SETTINGS, **payload})
        except FileNotFoundError:
            return dict(DEFAULT_SETTINGS)
        except (OSError, ValueError, TypeError):
            return dict(DEFAULT_SETTINGS)

    def save(self, values: Any) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise SettingsError("Settings must be a JSON object")
        current = self.load()
        unknown = set(values) - set(DEFAULT_SETTINGS)
        if unknown:
            raise SettingsError(f"Unknown setting: {sorted(unknown)[0]}")
        result = self.validate({**current, **values, "schema_version": 1})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return result

    @staticmethod
    def validate(values: dict[str, Any]) -> dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        host = str(values.get("web_host", result["web_host"]))
        if host not in {"127.0.0.1", "0.0.0.0"}:
            raise SettingsError("Web host must be 127.0.0.1 or 0.0.0.0")
        result["web_host"] = host
        for key, minimum, maximum in (
            ("web_port", 1024, 65535), ("udp_port", 1024, 65535),
            ("trash_retention_days", 0, 3650), ("diagnostic_max_mb", 1, 1024),
            ("dashboard_refresh_seconds", 2, 60),
        ):
            try:
                value = int(values.get(key, result[key]))
            except (TypeError, ValueError):
                raise SettingsError(f"{key} must be a whole number") from None
            if not minimum <= value <= maximum:
                raise SettingsError(f"{key} must be between {minimum} and {maximum}")
            result[key] = value
        try:
            maximum_gb = float(values.get("max_recording_gb", result["max_recording_gb"]))
        except (TypeError, ValueError):
            raise SettingsError("max_recording_gb must be a number") from None
        if not 0.25 <= maximum_gb <= 64:
            raise SettingsError("max_recording_gb must be between 0.25 and 64")
        result["max_recording_gb"] = maximum_gb
        result["schema_version"] = 1
        return result


def detected_receiver_ips() -> list[str]:
    addresses = {"127.0.0.1"}
    try:
        addresses.update(
            item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            if item[4][0]
        )
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        addresses.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(addresses, key=lambda value: (value.startswith("127."), value))


def settings_payload(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "settings": settings,
        "receiver_ips": detected_receiver_ips(),
        "restart_required_fields": ["web_host", "web_port", "udp_port"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
