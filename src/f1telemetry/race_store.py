"""Incremental, crash-tolerant packet store for full race recordings."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024


def _utc_now_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class RaceSessionStore:
    """One append-only SQLite/WAL database per modern recording session.

    SQLite commits each packet before returning. A process crash can therefore
    leave an unfinished but readable race rather than losing the session.
    """

    def __init__(self, path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(
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
                game_lap_distance REAL,
                freshness_ms REAL,
                payload_json TEXT NOT NULL
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
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(packets)")}
        if "header_json" not in columns:
            self.connection.execute("ALTER TABLE packets ADD COLUMN header_json TEXT NOT NULL DEFAULT '{}'")
        self.connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        self.last_frame_by_packet: dict[int, int] = {}
        self.last_time_by_packet: dict[int, float] = {}
        self.dropped_packets = int(self._metadata("dropped_packets", "0"))
        self.missing_frames = int(self._metadata("missing_frame_estimate", "0"))
        self.append_count = int(self.connection.execute("SELECT COUNT(*) FROM packets").fetchone()[0])
        self.storage_full = self._metadata("storage_limit_reached", "") != ""

    def _metadata(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_metadata(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, separators=(",", ":")) if not isinstance(value, str) else value),
        )

    def _disk_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += self.path.with_name(self.path.name + suffix).stat().st_size
            except OSError:
                pass
        return total

    def append_packet(self, header: Any, payload: dict[str, Any], game_lap_distance: float | None = None) -> bool:
        if self.storage_full or (self.append_count % 128 == 0 and self._disk_bytes() >= self.max_bytes):
            first_drop = not self.storage_full
            self.storage_full = True
            self.dropped_packets += 1
            if first_drop:
                self.set_metadata("storage_limit_reached", _utc_now_ms())
            if first_drop or self.dropped_packets % 128 == 0:
                self.set_metadata("dropped_packets", str(self.dropped_packets))
            return False
        packet_id = int(header.packet_id)
        frame = int(header.overall_frame_identifier)
        previous = self.last_frame_by_packet.get(packet_id)
        # Only an estimate: slower packet types intentionally skip frames.
        if previous is not None and frame > previous + 1 and packet_id in {0, 2, 6, 7, 10, 13}:
            self.missing_frames += frame - previous - 1
            self.set_metadata("missing_frame_estimate", str(self.missing_frames))
        previous_time = self.last_time_by_packet.get(packet_id)
        freshness_ms = None if previous_time is None else max(0.0, (float(header.session_time) - previous_time) * 1000)
        self.connection.execute(
            """INSERT INTO packets(
                received_at,session_uid,session_time,frame_identifier,overall_frame_identifier,
                packet_id,packet_version,player_car_index,header_json,game_lap_distance,freshness_ms,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _utc_now_ms(), int(header.session_uid), float(header.session_time), int(header.frame_identifier), frame,
                packet_id, int(header.packet_version), int(header.player_car_index),
                json.dumps({
                    "packet_format": int(header.packet_format), "game_year": int(header.game_year),
                    "game_major_version": int(header.game_major_version), "game_minor_version": int(header.game_minor_version),
                    "packet_version": int(header.packet_version), "packet_id": packet_id,
                    "session_uid": int(header.session_uid), "session_time": float(header.session_time),
                    "frame_identifier": int(header.frame_identifier), "overall_frame_identifier": frame,
                    "player_car_index": int(header.player_car_index),
                    "secondary_player_car_index": int(header.secondary_player_car_index),
                }, separators=(",", ":")),
                game_lap_distance, freshness_ms,
                json.dumps(_json_safe(payload), separators=(",", ":"), allow_nan=False),
            ),
        )
        self.last_frame_by_packet[packet_id] = frame
        self.last_time_by_packet[packet_id] = float(header.session_time)
        self.append_count += 1
        return True

    def append_event(
        self, session_time: float, event_type: str, details: dict[str, Any],
        source: str = "inferred", lap_number: int | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO timeline(received_at,session_time,lap_number,event_type,source,details_json) VALUES(?,?,?,?,?,?)",
            (_utc_now_ms(), float(session_time), lap_number, event_type, source, json.dumps(details, separators=(",", ":"))),
        )

    def summary(self) -> dict[str, Any]:
        packets = self.connection.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
        events = self.connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0]
        first_last = self.connection.execute("SELECT MIN(session_time),MAX(session_time) FROM packets").fetchone()
        return {
            "schema_version": SCHEMA_VERSION, "path": self.path.name, "packet_count": packets,
            "event_count": events, "first_session_time": first_last[0], "last_session_time": first_last[1],
            "dropped_packets": self.dropped_packets, "missing_frame_estimate": self.missing_frames,
            "bytes": self._disk_bytes(), "max_bytes": self.max_bytes,
        }

    def timeline(self, lap_from: int | None = None, lap_to: int | None = None) -> list[dict[str, Any]]:
        return _timeline_query(self.connection, lap_from, lap_to)

    def export_packets(self, start_time: float, end_time: float) -> bytes:
        return _packet_export_query(self.connection, start_time, end_time)

    def snapshot_bytes(self) -> bytes:
        return _database_snapshot(self.connection, self.path.parent)

    @staticmethod
    def read_timeline(path: Path, lap_from: int | None = None, lap_to: int | None = None) -> list[dict[str, Any]]:
        """Read a closed session without opening its database for writes."""
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return _timeline_query(connection, lap_from, lap_to)
        finally:
            connection.close()

    @staticmethod
    def read_packet_export(path: Path, start_time: float, end_time: float) -> bytes:
        """Export a time-bounded decoded packet stream without mutating it."""
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return _packet_export_query(connection, start_time, end_time)
        finally:
            connection.close()

    @staticmethod
    def read_snapshot(path: Path) -> bytes:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return _database_snapshot(connection, path.parent)
        finally:
            connection.close()

    def close(self) -> None:
        try:
            self.set_metadata("dropped_packets", str(self.dropped_packets))
            self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            self.connection.close()

    def checkpoint(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")


def _timeline_query(
    connection: sqlite3.Connection, lap_from: int | None = None, lap_to: int | None = None,
) -> list[dict[str, Any]]:
    clauses, values = [], []
    if lap_from is not None:
        clauses.append("(lap_number IS NULL OR lap_number>=?)")
        values.append(lap_from)
    if lap_to is not None:
        clauses.append("(lap_number IS NULL OR lap_number<=?)")
        values.append(lap_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        "SELECT received_at,session_time,lap_number,event_type,source,details_json FROM timeline" + where + " ORDER BY session_time,sequence",
        values,
    )
    return [
        {"received_at": row[0], "session_time": row[1], "lap_number": row[2], "type": row[3], "source": row[4], "details": json.loads(row[5])}
        for row in rows
    ]


def _packet_export_query(connection: sqlite3.Connection, start_time: float, end_time: float) -> bytes:
    rows = connection.execute(
        """SELECT received_at,session_time,frame_identifier,overall_frame_identifier,
                  packet_id,packet_version,player_car_index,header_json,
                  game_lap_distance,freshness_ms,payload_json
           FROM packets WHERE session_time>=? AND session_time<=?
           ORDER BY session_time,sequence""",
        (float(start_time), float(end_time)),
    )
    output = bytearray()
    for row in rows:
        record = {
            "received_at": row[0], "session_time": row[1],
            "frame_identifier": row[2], "overall_frame_identifier": row[3],
            "packet_id": row[4], "packet_version": row[5], "player_car_index": row[6],
            "header": json.loads(row[7]), "game_lap_distance": row[8], "freshness_ms": row[9],
            "payload": json.loads(row[10]),
        }
        output.extend(json.dumps(record, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        output.extend(b"\n")
    return bytes(output)


def _database_snapshot(connection: sqlite3.Connection, directory: Path) -> bytes:
    """Use SQLite's online backup API so exported bytes include committed WAL rows."""
    handle = tempfile.NamedTemporaryFile(prefix=".race-export-", suffix=".sqlite3", dir=directory, delete=False)
    temporary = Path(handle.name)
    handle.close()
    destination = sqlite3.connect(temporary)
    try:
        connection.backup(destination)
        destination.close()
        return temporary.read_bytes()
    finally:
        try:
            destination.close()
        finally:
            temporary.unlink(missing_ok=True)
