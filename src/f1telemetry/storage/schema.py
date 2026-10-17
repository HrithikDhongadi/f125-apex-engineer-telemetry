"""SQLite schema and packet JSON codecs shared by storage readers and writers."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any
import zlib


SCHEMA_VERSION = 2
PACKET_JSON_ENCODING = "zlib-json-v1"
PACKET_JSON_COMPRESSION_LEVEL = 1
SIGNED_INT64_MAX = (1 << 63) - 1
SIGNED_INT64_MIN = -(1 << 63)
UINT64_MAX = (1 << 64) - 1


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS packets (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            session_uid INTEGER NOT NULL,
            session_time REAL NOT NULL,
            frame_identifier INTEGER NOT NULL,
            overall_frame_identifier INTEGER NOT NULL,
            packet_id INTEGER NOT NULL,
            packet_version INTEGER NOT NULL,
            player_car_index INTEGER NOT NULL,
            header_json TEXT NOT NULL DEFAULT '{}',
            header_json_zlib BLOB,
            game_lap_distance REAL,
            freshness_ms REAL,
            payload_json TEXT NOT NULL,
            payload_json_zlib BLOB
        );
        CREATE INDEX IF NOT EXISTS packets_frame ON packets(overall_frame_identifier, packet_id);
        CREATE INDEX IF NOT EXISTS packets_time ON packets(session_time, sequence);
        CREATE TABLE IF NOT EXISTS timeline (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            session_time REAL NOT NULL,
            lap_number INTEGER,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('udp_event','inferred','recorder')),
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS timeline_time ON timeline(session_time, sequence);
        """
    )
    columns = table_columns(connection, "packets")
    if "header_json" not in columns:
        connection.execute("ALTER TABLE packets ADD COLUMN header_json TEXT NOT NULL DEFAULT '{}'")
    if "header_json_zlib" not in columns:
        connection.execute("ALTER TABLE packets ADD COLUMN header_json_zlib BLOB")
    if "payload_json_zlib" not in columns:
        connection.execute("ALTER TABLE packets ADD COLUMN payload_json_zlib BLOB")
    connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def sqlite_int64_bits(value: Any) -> int:
    """Store an F1 uint64 losslessly in SQLite's signed INTEGER domain."""
    number = int(value)
    if SIGNED_INT64_MIN <= number <= SIGNED_INT64_MAX:
        return number
    if SIGNED_INT64_MAX < number <= UINT64_MAX:
        return number - (1 << 64)
    raise OverflowError("session UID is outside the 64-bit packet field")


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def packet_json_select(connection: sqlite3.Connection, name: str) -> str:
    columns = table_columns(connection, "packets")
    compressed = f"{name}_zlib"
    if name not in columns:
        return "NULL,NULL"
    return f"{name},{compressed}" if compressed in columns else f"{name},NULL"


def json_storage(value: Any) -> tuple[str, bytes]:
    encoded = json.dumps(json_safe(value), separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "", zlib.compress(encoded, PACKET_JSON_COMPRESSION_LEVEL)


def stored_json_bytes(text: str | bytes | None, compressed: bytes | None) -> bytes:
    if compressed:
        return zlib.decompress(compressed)
    if isinstance(text, bytes):
        return text
    return (text or "{}").encode("utf-8")


def stored_json(text: str | bytes | None, compressed: bytes | None) -> dict[str, Any]:
    value = json.loads(stored_json_bytes(text, compressed))
    return value if isinstance(value, dict) else {}
