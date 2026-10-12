"""Incremental, crash-tolerant packet store for full race recordings."""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from time import monotonic
from typing import Any
import zlib


SCHEMA_VERSION = 2
PACKET_JSON_ENCODING = "zlib-json-v1"
PACKET_JSON_COMPRESSION_LEVEL = 1
TIMELINE_RECONSTRUCTION_VERSION = 2
EVENT_ATTRIBUTION_VERSION = 2
CONTINUITY_ANALYSIS_VERSION = 2
DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024
AUTOCOMMIT_WARMUP_PACKETS = 16
BATCH_PACKET_LIMIT = 128
BATCH_MAX_SECONDS = 0.25
LIVE_TIMELINE_LIMIT = 2_000
SIGNED_INT64_MAX = (1 << 63) - 1
SIGNED_INT64_MIN = -(1 << 63)
UINT64_MAX = (1 << 64) - 1
HIGH_RATE_PACKET_NAMES = {
    0: "motion", 2: "lap data", 6: "car telemetry", 7: "car status", 13: "motion extended",
}

EVENT_CODE_NAMES = {
    "SSTA": "session_started", "SEND": "session_ended", "FTLP": "fastest_lap",
    "RTMT": "retirement", "DRSE": "drs_enabled", "DRSD": "drs_disabled",
    "TMPT": "team_mate_in_pits", "CHQF": "chequered_flag", "RCWN": "race_winner",
    "PENA": "penalty", "SPTP": "speed_trap", "STLG": "start_lights",
    "LGOT": "lights_out", "DTSV": "drive_through_served", "SGSV": "stop_go_served",
    "FLBK": "flashback", "RDFL": "red_flag", "OVTK": "overtake",
    "SCAR": "safety_car", "COLL": "collision", "BUTN": "controller_input",
}

PENALTY_TYPE_LABELS = {
    0: "drive through", 1: "stop go", 2: "grid penalty", 3: "penalty reminder",
    4: "time penalty", 5: "warning", 6: "disqualified",
    7: "removed from formation lap", 8: "parked-too-long timer",
    9: "tyre regulations", 10: "this lap invalidated",
    11: "this and next lap invalidated", 12: "this lap invalidated without reason",
    13: "this and next lap invalidated without reason",
    14: "this and previous lap invalidated",
    15: "this and previous lap invalidated without reason",
    16: "retired", 17: "black-flag timer",
}
INFRINGEMENT_TYPE_LABELS = {3: "big collision", 4: "small collision"}
ACTUAL_PENALTY_TYPES = {0, 1, 2, 4, 6, 7, 9}


def _event_scope(event_type: str, details: dict[str, Any], player_index: int | None) -> tuple[str, bool | None]:
    """Classify a timeline event without discarding session-wide UDP evidence."""
    if isinstance(details.get("player_relevant"), bool):
        relevant = bool(details["player_relevant"])
        return str(details.get("scope") or ("player" if relevant else "other_car")), relevant
    if player_index is None:
        return "session", None
    targeted_fields = {
        "overtake": ("overtaking_vehicle_index", "overtaken_vehicle_index"),
        "collision": ("vehicle_1_index", "vehicle_2_index"),
        "penalty": ("vehicle_index",),
        "retirement": ("vehicle_index",),
        "team_mate_in_pits": ("vehicle_index",),
        "drive_through_served": ("vehicle_index",),
        "stop_go_served": ("vehicle_index",),
    }
    fields = targeted_fields.get(event_type)
    if fields:
        relevant = any(details.get(field) == player_index for field in fields)
        return ("player" if relevant else "other_car"), relevant
    if event_type in {
        "pit_status_change", "pit_entry", "pit_exit", "pit_lane_state_change", "pit_stop",
        "position_change", "compound_change", "damage_change",
    }:
        return "player", True
    return "session", None


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


def _sqlite_int64_bits(value: Any) -> int:
    """Store an F1 uint64 losslessly in SQLite's signed INTEGER domain."""
    number = int(value)
    if SIGNED_INT64_MIN <= number <= SIGNED_INT64_MAX:
        return number
    if SIGNED_INT64_MAX < number <= UINT64_MAX:
        return number - (1 << 64)
    raise OverflowError("session UID is outside the 64-bit packet field")


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _packet_json_select(connection: sqlite3.Connection, name: str) -> str:
    columns = _table_columns(connection, "packets")
    compressed = f"{name}_zlib"
    if name not in columns:
        return "NULL,NULL"
    return f"{name},{compressed}" if compressed in columns else f"{name},NULL"


def _json_storage(value: Any) -> tuple[str, bytes]:
    encoded = json.dumps(_json_safe(value), separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "", zlib.compress(encoded, PACKET_JSON_COMPRESSION_LEVEL)


def _stored_json_bytes(text: str | bytes | None, compressed: bytes | None) -> bytes:
    if compressed:
        return zlib.decompress(compressed)
    if isinstance(text, bytes):
        return text
    return (text or "{}").encode("utf-8")


def _stored_json(text: str | bytes | None, compressed: bytes | None) -> dict[str, Any]:
    value = json.loads(_stored_json_bytes(text, compressed))
    return value if isinstance(value, dict) else {}


class RaceSessionStore:
    """One append-only SQLite/WAL database per modern recording session.

    The first packets commit immediately, then packet bursts commit at bounded
    250 ms / 128-packet intervals. This keeps an unfinished race readable
    without forcing an fsync-style transaction for every UDP datagram.
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
        columns = _table_columns(self.connection, "packets")
        if "header_json" not in columns:
            self.connection.execute("ALTER TABLE packets ADD COLUMN header_json TEXT NOT NULL DEFAULT '{}'")
        if "header_json_zlib" not in columns:
            self.connection.execute("ALTER TABLE packets ADD COLUMN header_json_zlib BLOB")
        if "payload_json_zlib" not in columns:
            self.connection.execute("ALTER TABLE packets ADD COLUMN payload_json_zlib BLOB")
        self.connection.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        self.set_metadata("schema_version", str(SCHEMA_VERSION))
        self.set_metadata("packet_json_encoding", PACKET_JSON_ENCODING)
        self.last_frame_by_packet: dict[int, int] = {}
        self.last_time_by_packet: dict[int, float] = {}
        self.dropped_packets = int(self._metadata("dropped_packets", "0"))
        self.missing_frames = int(self._metadata("missing_frame_estimate", "0"))
        self.append_count = int(self.connection.execute("SELECT COUNT(*) FROM packets").fetchone()[0])
        self.event_count = int(self.connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
        self.storage_full = self._metadata("storage_limit_reached", "") != ""
        first_last = self.connection.execute("SELECT MIN(session_time),MAX(session_time) FROM packets").fetchone()
        self.first_session_time = first_last[0]
        self.last_session_time = first_last[1]
        self.current_player_index: int | None = None
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=LIVE_TIMELINE_LIMIT)
        self._transaction_open = False
        self._packets_since_commit = 0
        self._last_commit_at = monotonic()
        self.high_rate_streams = _continuity_query(self.connection)
        for packet_id in HIGH_RATE_PACKET_NAMES:
            row = self.connection.execute(
                "SELECT overall_frame_identifier,session_time FROM packets WHERE packet_id=? ORDER BY sequence DESC LIMIT 1",
                (packet_id,),
            ).fetchone()
            if row:
                self.last_frame_by_packet[packet_id] = int(row[0])
                self.last_time_by_packet[packet_id] = float(row[1])

    def _begin_batch(self) -> None:
        if self.append_count < AUTOCOMMIT_WARMUP_PACKETS or self._transaction_open:
            return
        self.connection.execute("BEGIN")
        self._transaction_open = True
        self._packets_since_commit = 0
        self._last_commit_at = monotonic()

    def flush(self) -> None:
        if self._transaction_open:
            self.connection.commit()
            self._transaction_open = False
            self._packets_since_commit = 0
            self._last_commit_at = monotonic()

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
        self._begin_batch()
        packet_id = int(header.packet_id)
        frame = int(header.overall_frame_identifier)
        self.current_player_index = int(header.player_car_index)
        previous = self.last_frame_by_packet.get(packet_id)
        # Keep the legacy live counter meaningful: packet 10 is normally 10 Hz,
        # and gaps shared by several 60 Hz streams must not be added together.
        if previous is not None and frame > previous + 1 and packet_id in HIGH_RATE_PACKET_NAMES:
            self.missing_frames = max(self.missing_frames, frame - previous - 1)
            self.set_metadata("missing_frame_estimate", str(self.missing_frames))
        previous_time = self.last_time_by_packet.get(packet_id)
        freshness_ms = None if previous_time is None else max(0.0, (float(header.session_time) - previous_time) * 1000)
        header_text, header_zlib = _json_storage({
            "packet_format": int(header.packet_format), "game_year": int(header.game_year),
            "game_major_version": int(header.game_major_version), "game_minor_version": int(header.game_minor_version),
            "packet_version": int(header.packet_version), "packet_id": packet_id,
            "session_uid": int(header.session_uid), "session_time": float(header.session_time),
            "frame_identifier": int(header.frame_identifier), "overall_frame_identifier": frame,
            "player_car_index": int(header.player_car_index),
            "secondary_player_car_index": int(header.secondary_player_car_index),
        })
        payload_text, payload_zlib = _json_storage(payload)
        self.connection.execute(
            """INSERT INTO packets(
                received_at,session_uid,session_time,frame_identifier,overall_frame_identifier,
                packet_id,packet_version,player_car_index,header_json,header_json_zlib,
                game_lap_distance,freshness_ms,payload_json,payload_json_zlib
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _utc_now_ms(), _sqlite_int64_bits(header.session_uid), float(header.session_time), int(header.frame_identifier), frame,
                packet_id, int(header.packet_version), int(header.player_car_index),
                header_text, sqlite3.Binary(header_zlib), game_lap_distance, freshness_ms,
                payload_text, sqlite3.Binary(payload_zlib),
            ),
        )
        self.last_frame_by_packet[packet_id] = frame
        self.last_time_by_packet[packet_id] = float(header.session_time)
        self.append_count += 1
        if self.first_session_time is None:
            self.first_session_time = float(header.session_time)
        self.last_session_time = float(header.session_time)
        if self._transaction_open:
            self._packets_since_commit += 1
            if self._packets_since_commit >= BATCH_PACKET_LIMIT or monotonic() - self._last_commit_at >= BATCH_MAX_SECONDS:
                self.flush()
        if packet_id in HIGH_RATE_PACKET_NAMES:
            stream = next(item for item in self.high_rate_streams if item["packet_id"] == packet_id)
            stream["packet_count"] += 1
            if previous is not None and frame > previous + 1:
                gap = frame - previous - 1
                stream["missing_frame_estimate"] += gap
                stream["gap_events"] += 1
                stream["max_gap_frames"] = max(stream["max_gap_frames"], gap)
            first_time = stream.pop("_first_time", None)
            if first_time is None:
                first_time = float(header.session_time)
            stream["_first_time"] = first_time
            stream["_last_time"] = float(header.session_time)
            duration = stream["_last_time"] - stream["_first_time"]
            stream["observed_hz"] = round((stream["packet_count"] - 1) / duration, 2) if duration > 0 else None
        return True

    def append_event(
        self, session_time: float, event_type: str, details: dict[str, Any],
        source: str = "inferred", lap_number: int | None = None,
    ) -> None:
        self._begin_batch()
        received_at = _utc_now_ms()
        self.connection.execute(
            "INSERT INTO timeline(received_at,session_time,lap_number,event_type,source,details_json) VALUES(?,?,?,?,?,?)",
            (received_at, float(session_time), lap_number, event_type, source, json.dumps(details, separators=(",", ":"))),
        )
        self.event_count += 1
        packet = details.get("_packet") if isinstance(details, dict) else None
        player_index = packet.get("player_car_index") if isinstance(packet, dict) else self.current_player_index
        scope, player_relevant = _event_scope(event_type, details, player_index)
        self.recent_events.append({
            "received_at": received_at, "session_time": float(session_time), "lap_number": lap_number,
            "type": event_type, "source": source, "details": details, "scope": scope,
            "player_relevant": player_relevant, "player_car_index": player_index,
            "timeline_branch_id": "pending", "event_validity": "pending",
            "validation_reason": "live event; final flashback-branch reconstruction runs after recording closes",
        })

    def live_summary(self) -> dict[str, Any]:
        """Return O(1) recorder counters; exact continuity is deferred."""
        populated = [stream for stream in self.high_rate_streams if stream["packet_count"]]
        worst = max(populated, key=lambda item: item["missing_frame_estimate"], default=None)
        missing = int(worst["missing_frame_estimate"]) if worst else 0
        denominator = int(worst["packet_count"]) + missing if worst else 0
        return {
            "schema_version": SCHEMA_VERSION, "path": self.path.name,
            "packet_count": self.append_count, "event_count": self.event_count,
            "first_session_time": self.first_session_time, "last_session_time": self.last_session_time,
            "missing_frame_estimate": missing,
            "missing_frame_percent": round(100 * missing / denominator, 3) if denominator else 0.0,
            "continuity_pending": True,
            "continuity_basis": "live incremental estimate; exact classification is deferred until report/export",
            "dropped_packets": self.dropped_packets, "bytes": self._disk_bytes(), "max_bytes": self.max_bytes,
            "high_rate_streams": [{key: value for key, value in stream.items() if not key.startswith("_")} for stream in populated],
        }

    def live_timeline(self) -> list[dict[str, Any]]:
        """Return bounded in-memory events without scanning the active packet DB."""
        return list(self.recent_events)

    def summary(self) -> dict[str, Any]:
        # Re-query so duplicate/out-of-order/gap classifications are exact for
        # both live and closed stores; the append counters are only a fast UI aid.
        self.flush()
        result = _summary_query(self.connection)
        result.update({
            "schema_version": SCHEMA_VERSION, "path": self.path.name,
            "dropped_packets": self.dropped_packets, "bytes": self._disk_bytes(), "max_bytes": self.max_bytes,
        })
        return result

    def timeline(self, lap_from: int | None = None, lap_to: int | None = None) -> list[dict[str, Any]]:
        self.flush()
        return _timeline_query(self.connection, lap_from, lap_to)

    def export_packets(self, start_time: float, end_time: float) -> bytes:
        self.flush()
        return _packet_export_query(self.connection, start_time, end_time)

    def snapshot_bytes(self) -> bytes:
        self.flush()
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
    def read_summary(path: Path) -> dict[str, Any]:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = _summary_query(connection)
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            result.update({
                "schema_version": SCHEMA_VERSION, "path": path.name, "bytes": path.stat().st_size,
                "dropped_packets": int(metadata.get("dropped_packets", 0)),
            })
            return result
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
    def read_latest_player_session_history(path: Path) -> dict[str, Any] | None:
        """Return the latest player Packet 11 from either storage encoding."""
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            payload_columns = _packet_json_select(connection, "payload_json")
            rows = connection.execute(
                f"""SELECT player_car_index,{payload_columns} FROM packets
                    WHERE packet_id=11 ORDER BY sequence DESC"""
            )
            for player_index, payload_json, payload_zlib in rows:
                payload = _stored_json(payload_json, payload_zlib)
                if payload.get("car_index") == int(player_index):
                    return payload
            return None
        finally:
            connection.close()

    @staticmethod
    def read_player_final_classification(path: Path) -> dict[str, Any] | None:
        """Return the player's latest official Final Classification row."""
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            payload_columns = _packet_json_select(connection, "payload_json")
            row = connection.execute(
                f"""SELECT player_car_index,{payload_columns} FROM packets
                    WHERE packet_id=8 ORDER BY sequence DESC LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            player_index = int(row[0])
            payload = _stored_json(row[1], row[2])
            return next(
                (dict(car) for car in payload.get("cars", []) if car.get("car_index") == player_index),
                None,
            )
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
            self.flush()
            self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            self.connection.close()

    def checkpoint(self) -> None:
        self.flush()
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")


def _timeline_query(
    connection: sqlite3.Connection, lap_from: int | None = None, lap_to: int | None = None,
) -> list[dict[str, Any]]:
    player_row = connection.execute(
        "SELECT player_car_index FROM packets ORDER BY sequence LIMIT 1"
    ).fetchone()
    player_index = int(player_row[0]) if player_row else None
    payload_columns = _packet_json_select(connection, "payload_json")
    packet_events = list(connection.execute(
        f"""SELECT sequence,received_at,session_time,frame_identifier,
                   overall_frame_identifier,player_car_index,game_lap_distance,{payload_columns}
            FROM packets WHERE packet_id=3 ORDER BY sequence"""
    ))
    # Old timeline rows did not persist their source packet identifier. Match
    # UDP rows losslessly by decoded payload and session time; future and
    # inferred rows fall back to the packet received closest in wall time.
    event_queues: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for packet in packet_events:
        payload = _stored_json(packet[7], packet[8])
        key = (round(float(packet[2]), 5), EVENT_CODE_NAMES.get(str(payload.get("code")), str(payload.get("code", "event")).lower()), json.dumps(payload, sort_keys=True))
        event_queues.setdefault(key, []).append(packet)
    compact_packets = list(connection.execute(
        """SELECT sequence,received_at,session_time,frame_identifier,
                  overall_frame_identifier,packet_id,game_lap_distance,player_car_index
           FROM packets WHERE packet_id IN (2,3) ORDER BY sequence"""
    ))
    compact_received = [_iso_seconds(packet[1]) for packet in compact_packets]
    flashbacks = []
    for packet in packet_events:
        payload = _stored_json(packet[7], packet[8])
        if payload.get("code") != "FLBK":
            continue
        target_frame = payload.get("flashback_frame_identifier")
        target_time = payload.get("flashback_session_time")
        if not isinstance(target_frame, int) or not isinstance(target_time, (int, float)):
            continue
        target = connection.execute(
            """SELECT sequence FROM packets
               WHERE sequence<? AND frame_identifier<=? AND session_time<=?
               ORDER BY sequence DESC LIMIT 1""",
            (packet[0], target_frame, float(target_time) + 0.05),
        ).fetchone()
        flashbacks.append({
            "packet_sequence": int(packet[0]), "overall_frame_identifier": int(packet[4]),
            "target_sequence": int(target[0]) if target else None,
            "target_frame_identifier": target_frame, "target_session_time": float(target_time),
        })

    def nearest_packet(received_at: str) -> tuple[Any, ...] | None:
        # ISO UTC strings are lexically ordered. Nearby timeline rows are
        # normally written within a few milliseconds of their source packet.
        if not compact_packets:
            return None
        target = _iso_seconds(received_at)
        index = bisect_left(compact_received, target)
        candidates = compact_packets[max(0, index - 1):min(len(compact_packets), index + 2)]
        return min(candidates, key=lambda packet: abs(_iso_seconds(packet[1]) - target))

    raw_rows = connection.execute(
        "SELECT sequence,received_at,session_time,lap_number,event_type,source,details_json FROM timeline ORDER BY sequence"
    )
    result: list[dict[str, Any]] = []
    for timeline_sequence, received_at, session_time, lap_number, event_type, source, details_json in raw_rows:
        if lap_from is not None and lap_number is not None and lap_number < lap_from:
            continue
        if lap_to is not None and lap_number is not None and lap_number > lap_to:
            continue
        details = json.loads(details_json)
        packet = None
        if source == "udp_event":
            match_details = {key: value for key, value in details.items() if key != "_packet"}
            key = (round(float(session_time), 5), event_type, json.dumps(match_details, sort_keys=True))
            queue = event_queues.get(key)
            if queue:
                packet = queue.pop(0)
        if packet is None:
            close = nearest_packet(received_at)
            if close is not None:
                packet = (close[0], close[1], close[2], close[3], close[4], close[7], close[6], None, None)
        packet_sequence = int(packet[0]) if packet else None
        frame = int(packet[3]) if packet else None
        overall = int(packet[4]) if packet else None
        event_player_index = int(packet[5]) if packet else player_index
        distance = packet[6] if packet else None
        branch_number = sum(
            packet_sequence is not None and packet_sequence > item["packet_sequence"]
            for item in flashbacks
        )
        event_validity = "accepted"
        superseded_by = None
        validation_reason = "event lies on the final observed timeline"
        if packet_sequence is None:
            event_validity = "unknown"
            validation_reason = "historical timeline row could not be correlated to a source packet"
        else:
            for item in flashbacks:
                target_sequence = item["target_sequence"]
                if target_sequence is not None and target_sequence < packet_sequence < item["packet_sequence"]:
                    event_validity = "superseded"
                    superseded_by = {
                        "overall_frame_identifier": item["overall_frame_identifier"],
                        "flashback_reference": {
                            "frame_identifier": item["target_frame_identifier"],
                            "session_time": item["target_session_time"],
                        },
                    }
                    validation_reason = "event is on a branch rewound by a later F1 25 flashback packet"
                    break
        # The FLBK packet establishes the replacement branch itself.
        if event_type == "flashback" and packet_sequence is not None:
            branch_number += 1
            validation_reason = "game flashback event establishes a replacement timeline branch"
        scope, relevant = _event_scope(event_type, details, event_player_index)
        event = {
            "timeline_sequence": int(timeline_sequence), "received_at": received_at,
            "session_time": float(session_time), "lap_number": lap_number,
            "type": event_type, "source": source, "details": details,
            "scope": scope, "player_relevant": relevant, "player_car_index": event_player_index,
            "packet_sequence": packet_sequence, "frame_identifier": frame,
            "overall_frame_identifier": overall, "track_distance_m": distance,
            "timeline_branch_id": f"branch-{branch_number}",
            "event_validity": event_validity, "superseded_by": superseded_by,
            "flashback_reference": superseded_by.get("flashback_reference") if superseded_by else None,
            "validation_reason": validation_reason,
        }
        if event_type == "penalty":
            penalty_type = details.get("penalty_type")
            infringement_type = details.get("infringement_type")
            event["penalty_classification"] = (
                "warning" if penalty_type == 5 else
                "actual_penalty" if penalty_type in ACTUAL_PENALTY_TYPES else
                "notification_or_lap_state"
            )
            event["penalty_label"] = PENALTY_TYPE_LABELS.get(penalty_type, "unknown")
            event["infringement_label"] = INFRINGEMENT_TYPE_LABELS.get(infringement_type, f"infringement {infringement_type}")
        result.append(event)
    _annotate_overtakes(connection, result, flashbacks)
    _group_collision_incidents(result, player_index)
    return result


def _iso_seconds(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _sequence_superseded(sequence: int, flashbacks: list[dict[str, Any]]) -> bool:
    return any(
        item["target_sequence"] is not None
        and item["target_sequence"] < sequence < item["packet_sequence"]
        for item in flashbacks
    )


def _annotate_overtakes(
    connection: sqlite3.Connection, events: list[dict[str, Any]],
    flashbacks: list[dict[str, Any]],
) -> None:
    for event in events:
        if event["type"] != "overtake" or event["player_relevant"] is not True:
            continue
        player_index = event.get("player_car_index")
        if not isinstance(player_index, int):
            continue
        details = event["details"]
        opponent = (
            details.get("overtaken_vehicle_index")
            if details.get("overtaking_vehicle_index") == player_index
            else details.get("overtaking_vehicle_index")
        )
        sequence = event.get("packet_sequence")
        overall = event.get("overall_frame_identifier")
        if not isinstance(opponent, int) or not isinstance(sequence, int) or not isinstance(overall, int):
            event["verification_status"] = "uncertain"
            event["verification_reason"] = "participant or source-packet evidence is incomplete"
            continue
        payload_columns = _packet_json_select(connection, "payload_json")
        rows = connection.execute(
            f"""SELECT sequence,overall_frame_identifier,{payload_columns} FROM packets
                WHERE packet_id=2 AND overall_frame_identifier BETWEEN ? AND ?
                ORDER BY sequence""", (max(0, overall - 60), overall + 60),
        )
        states = []
        for packet_sequence, packet_overall, payload_json, payload_zlib in rows:
            if _sequence_superseded(int(packet_sequence), flashbacks) != (event["event_validity"] == "superseded"):
                continue
            payload = _stored_json(payload_json, payload_zlib)
            cars = payload.get("cars") or []
            if player_index >= len(cars) or opponent >= len(cars):
                continue
            states.append((int(packet_sequence), int(packet_overall), cars[player_index], cars[opponent]))
        before = next((item for item in reversed(states) if item[0] < sequence), None)
        after = next((item for item in states if item[0] > sequence), None)
        if before and after:
            p_before, o_before, p_after, o_after = (
                before[2].get("position"), before[3].get("position"),
                after[2].get("position"), after[3].get("position"),
            )
            event.update({
                "player_position_before": p_before, "player_position_after": p_after,
                "opponent_position_before": o_before, "opponent_position_after": o_after,
                "track_distance_m": before[2].get("lap_distance_m", event.get("track_distance_m")),
            })
            player_made_pass = details.get("overtaking_vehicle_index") == player_index
            relative_flip = (
                p_before > o_before and p_after < o_after
                if player_made_pass else p_before < o_before and p_after > o_after
            ) if all(isinstance(value, int) for value in (p_before, o_before, p_after, o_after)) else False
            player_move = (
                p_after < p_before if player_made_pass else p_after > p_before
            ) if isinstance(p_before, int) and isinstance(p_after, int) else False
            event["verification_status"] = "confirmed" if relative_flip or player_move else "uncertain"
            event["verification_reason"] = (
                "surrounding all-car Lap Data confirms the expected relative-order or player-position transition"
                if event["verification_status"] == "confirmed"
                else "OVTK notification was not corroborated by surrounding participant positions within one second"
            )
        else:
            event["verification_status"] = "uncertain"
            event["verification_reason"] = "surrounding participant state is unavailable"


def _group_collision_incidents(events: list[dict[str, Any]], player_index: int | None) -> None:
    prior: dict[tuple[int, int, str], dict[str, Any]] = {}
    next_id = 1
    for event in events:
        if event["type"] != "collision" or event["player_relevant"] is not True or player_index is None:
            continue
        details = event["details"]
        pair = tuple(sorted((int(details.get("vehicle_1_index", -1)), int(details.get("vehicle_2_index", -1)))))
        key = (pair[0], pair[1], event["timeline_branch_id"])
        previous = prior.get(key)
        same_incident = bool(
            previous and previous["event_validity"] == event["event_validity"]
            and 0 <= event["session_time"] - previous["session_time"] <= 1.0
            and (
                not isinstance(previous.get("track_distance_m"), (int, float))
                or not isinstance(event.get("track_distance_m"), (int, float))
                or abs(float(event["track_distance_m"]) - float(previous["track_distance_m"])) <= 100
            )
        )
        if same_incident:
            event["collision_incident_id"] = previous["collision_incident_id"]
            event["collision_incident_role"] = "repeated_message"
        else:
            event["collision_incident_id"] = f"collision-{next_id}"
            event["collision_incident_role"] = "incident_start"
            next_id += 1
        prior[key] = event


def _continuity_query(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    streams = []
    for packet_id, name in HIGH_RATE_PACKET_NAMES.items():
        rows = list(connection.execute(
            """SELECT received_at,overall_frame_identifier,frame_identifier,
                      session_time,game_lap_distance
               FROM packets WHERE packet_id=? ORDER BY sequence""",
            (packet_id,),
        ))
        missing = gap_events = maximum_gap = duplicates = out_of_order = 0
        gap_details: list[dict[str, Any]] = []
        classified_frames = {
            "capture_loss": 0, "sequence_discontinuity": 0,
            "game_pause_or_rewind": 0, "unknown_gap": 0,
        }
        for previous, current in zip(rows, rows[1:]):
            frame_delta = (int(current[1]) - int(previous[1])) & 0xFFFFFFFF
            if frame_delta == 0:
                duplicates += 1
                continue
            if frame_delta >= 0x80000000:
                out_of_order += 1
                continue
            if frame_delta <= 1:
                continue
            gap = frame_delta - 1
            missing += gap
            gap_events += 1
            maximum_gap = max(maximum_gap, gap)
            expected_seconds = frame_delta / 60.0
            wall_delta = max(0.0, _iso_seconds(current[0]) - _iso_seconds(previous[0]))
            session_delta = float(current[3]) - float(previous[3])
            frame_identifier_delta = int(current[2]) - int(previous[2])
            if frame_identifier_delta < 0 or session_delta < -0.01:
                classification = "game_pause_or_rewind"
                reason = "game frame/session time moved backwards"
            elif wall_delta <= max(0.01, expected_seconds * 0.25) and session_delta >= expected_seconds * 0.75:
                classification = "sequence_discontinuity"
                reason = "game IDs and session time jumped while recorder wall time did not"
            elif wall_delta >= expected_seconds * 0.5:
                classification = "capture_loss"
                reason = "recorder wall-time gap is consistent with absent UDP frames"
            else:
                classification = "unknown_gap"
                reason = "available clocks do not establish whether packets were transmitted"
            classified_frames[classification] += gap
            gap_details.append({
                "missing_frames": gap, "classification": classification, "reason": reason,
                "from_overall_frame": int(previous[1]), "to_overall_frame": int(current[1]),
                "from_frame": int(previous[2]), "to_frame": int(current[2]),
                "from_session_time": round(float(previous[3]), 3),
                "to_session_time": round(float(current[3]), 3),
                "wall_time_gap_s": round(wall_delta, 3),
                "session_time_gap_s": round(session_delta, 3),
                "from_track_distance_m": previous[4], "to_track_distance_m": current[4],
            })
        session_duration = float(rows[-1][3] - rows[0][3]) if len(rows) > 1 else 0.0
        wall_duration = _iso_seconds(rows[-1][0]) - _iso_seconds(rows[0][0]) if len(rows) > 1 else 0.0
        streams.append({
            "packet_id": packet_id, "name": name, "packet_count": len(rows),
            "unique_frames": len({int(row[1]) for row in rows}),
            "missing_frame_estimate": missing, "gap_events": gap_events,
            "max_gap_frames": maximum_gap,
            "duplicate_frames": duplicates, "out_of_order_frames": out_of_order,
            "capture_loss_frames": classified_frames["capture_loss"],
            "sequence_discontinuity_frames": classified_frames["sequence_discontinuity"],
            "game_pause_or_rewind_frames": classified_frames["game_pause_or_rewind"],
            "unknown_gap_frames": classified_frames["unknown_gap"],
            "observed_hz": round((len(rows) - 1) / session_duration, 2) if session_duration > 0 else None,
            "effective_received_hz": round((len(rows) - 1) / wall_duration, 2) if wall_duration > 0 else None,
            "largest_gaps": sorted(gap_details, key=lambda item: item["missing_frames"], reverse=True)[:10],
            "_first_time": float(rows[0][3]) if rows else None,
            "_last_time": float(rows[-1][3]) if rows else None,
        })
    return streams


def _summary_values(connection: sqlite3.Connection, streams: list[dict[str, Any]]) -> dict[str, Any]:
    packets = int(connection.execute("SELECT COUNT(*) FROM packets").fetchone()[0])
    events = int(connection.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
    first_last = connection.execute("SELECT MIN(session_time),MAX(session_time) FROM packets").fetchone()
    populated = [stream for stream in streams if stream["packet_count"]]
    worst = max(populated, key=lambda item: item["missing_frame_estimate"], default=None)
    missing = int(worst["missing_frame_estimate"]) if worst else 0
    capture_loss = max((int(stream["capture_loss_frames"]) for stream in populated), default=0)
    sequence_discontinuity = max((int(stream["sequence_discontinuity_frames"]) for stream in populated), default=0)
    denominator = (int(worst["packet_count"]) + missing) if worst else 0
    return {
        "packet_count": packets, "event_count": events,
        "first_session_time": first_last[0], "last_session_time": first_last[1],
        "missing_frame_estimate": missing,
        "missing_frame_percent": round(100 * missing / denominator, 3) if denominator else 0.0,
        "capture_loss_frame_estimate": capture_loss,
        "sequence_discontinuity_frame_estimate": sequence_discontinuity,
        "continuity_basis": "each 60 Hz stream is tracked independently; streams are not summed; gaps are classified from game and recorder clocks",
        "continuity_analysis_version": CONTINUITY_ANALYSIS_VERSION,
        "high_rate_streams": [{key: value for key, value in stream.items() if not key.startswith("_")} for stream in streams],
    }


def _summary_query(connection: sqlite3.Connection) -> dict[str, Any]:
    result = _summary_values(connection, _continuity_query(connection))
    result["setup_snapshot_audit"] = _setup_snapshot_audit(connection)
    result["processing_versions"] = {
        "udp_decoder": 1,
        "timeline_reconstruction": TIMELINE_RECONSTRUCTION_VERSION,
        "event_attribution": EVENT_ATTRIBUTION_VERSION,
        "continuity_analysis": CONTINUITY_ANALYSIS_VERSION,
    }
    return result


def _setup_snapshot_audit(connection: sqlite3.Connection) -> dict[str, Any]:
    payload_columns = _packet_json_select(connection, "payload_json")
    rows = connection.execute(
        f"""SELECT sequence,session_time,frame_identifier,overall_frame_identifier,
                   player_car_index,{payload_columns}
            FROM packets WHERE packet_id=5 ORDER BY sequence"""
    )
    changes: list[dict[str, Any]] = []
    last_signature = None
    selected: dict[str, Any] | None = None
    repeated = 0
    for sequence, session_time, frame, overall, player_index, payload_json, payload_zlib in rows:
        payload = _stored_json(payload_json, payload_zlib)
        setup = payload.get("player")
        if not isinstance(setup, dict):
            continue
        clean = {key: value for key, value in setup.items() if not str(key).startswith("_") and key != "car_index"}
        signature = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        if signature != last_signature:
            changes.append({
                "packet_sequence": int(sequence), "session_time": float(session_time),
                "frame_identifier": int(frame), "overall_frame_identifier": int(overall),
                "player_car_index": int(player_index), "setup": clean,
                "status": "provisional" if not changes else "confirmed_active",
                "repeat_count": 1,
            })
            selected = changes[-1]
            last_signature = signature
            repeated = 1
        else:
            repeated += 1
            if selected is not None:
                selected["repeat_count"] = repeated
    if not changes:
        return {"status": "unknown", "reason": "no player Car Setups packets were stored", "history": []}
    for change in changes[:-1]:
        change["status"] = "superseded"
    selected = changes[-1]
    confidence = "high" if selected["repeat_count"] >= 3 else "limited"
    return {
        "status": "confirmed_active" if confidence == "high" else "unknown",
        "confidence": confidence,
        "selected_setup": selected["setup"],
        "selected_packet_sequence": selected["packet_sequence"],
        "selected_frame_identifier": selected["frame_identifier"],
        "selected_repeat_count": selected["repeat_count"],
        "reason": (
            f"latest player setup remained unchanged for {selected['repeat_count']} packets"
            if confidence == "high" else "setup did not repeat enough times to establish active state"
        ),
        "history": changes,
        "wing_verification": (
            "unverified_as_transmitted_zero"
            if selected["setup"].get("front_wing") == selected["setup"].get("rear_wing") == 0
            else "decoded_nonzero"
        ),
    }


def _packet_export_query(connection: sqlite3.Connection, start_time: float, end_time: float) -> bytes:
    header_columns = _packet_json_select(connection, "header_json")
    payload_columns = _packet_json_select(connection, "payload_json")
    rows = connection.execute(
        f"""SELECT received_at,session_time,frame_identifier,overall_frame_identifier,
                   packet_id,packet_version,player_car_index,{header_columns},
                   game_lap_distance,freshness_ms,{payload_columns}
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
            "header": _stored_json(row[7], row[8]), "game_lap_distance": row[9], "freshness_ms": row[10],
            "payload": _stored_json(row[11], row[12]),
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


def _packet_content_digest(connection: sqlite3.Connection) -> str:
    """Hash logical header/payload content independent of storage encoding."""
    header_columns = _packet_json_select(connection, "header_json")
    payload_columns = _packet_json_select(connection, "payload_json")
    digest = hashlib.sha256()
    rows = connection.execute(
        f"SELECT sequence,{header_columns},{payload_columns} FROM packets ORDER BY sequence"
    )
    for sequence, header_json, header_zlib, payload_json, payload_zlib in rows:
        digest.update(int(sequence).to_bytes(8, "little", signed=False))
        digest.update(_stored_json_bytes(header_json, header_zlib))
        digest.update(b"\0")
        digest.update(_stored_json_bytes(payload_json, payload_zlib))
        digest.update(b"\n")
    return digest.hexdigest()


def migrate_packet_storage(path: Path, keep_backup: bool = True) -> dict[str, Any]:
    """Atomically rewrite one closed store using compressed packet JSON.

    Logical packet content, row sequence values, metadata and timeline rows are
    preserved. The replacement occurs only after integrity, count and SHA-256
    checks succeed. Callers should keep the default backup unless they have an
    independently verified copy or explicitly intend to reclaim its space.
    """
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        checkpoint = sqlite3.connect(path, timeout=30)
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            checkpoint.close()
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError("database still has an active WAL; stop recording before migration")

    source_bytes = path.stat().st_size
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    handle = tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".migrating", dir=path.parent, delete=False)
    temporary = Path(handle.name)
    handle.close()
    temporary.unlink(missing_ok=True)
    destination_store: RaceSessionStore | None = None
    destination: sqlite3.Connection | None = None
    try:
        source_packets = int(source.execute("SELECT COUNT(*) FROM packets").fetchone()[0])
        source_events = int(source.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
        source_digest = _packet_content_digest(source)

        destination_store = RaceSessionStore(temporary, max_bytes=max(DEFAULT_MAX_BYTES, source_bytes * 2))
        destination_store.close()
        destination_store = None
        destination = sqlite3.connect(temporary, timeout=30)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.execute("PRAGMA synchronous=OFF")
        destination.execute("DELETE FROM packets")
        destination.execute("DELETE FROM timeline")
        destination.execute("DELETE FROM metadata")
        destination.commit()

        source_columns = _table_columns(source, "packets")
        header_text_sql = "header_json" if "header_json" in source_columns else "NULL"
        header_blob_sql = "header_json_zlib" if "header_json_zlib" in source_columns else "NULL"
        payload_blob_sql = "payload_json_zlib" if "payload_json_zlib" in source_columns else "NULL"
        packet_rows = source.execute(
            f"""SELECT sequence,received_at,session_uid,session_time,frame_identifier,
                       overall_frame_identifier,packet_id,packet_version,player_car_index,
                       {header_text_sql},{header_blob_sql},game_lap_distance,freshness_ms,
                       payload_json,{payload_blob_sql}
                FROM packets ORDER BY sequence"""
        )
        insert_packet = """INSERT INTO packets(
            sequence,received_at,session_uid,session_time,frame_identifier,
            overall_frame_identifier,packet_id,packet_version,player_car_index,
            header_json,header_json_zlib,game_lap_distance,freshness_ms,
            payload_json,payload_json_zlib
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        destination.execute("BEGIN")
        while True:
            batch = packet_rows.fetchmany(1000)
            if not batch:
                break
            encoded = []
            for row in batch:
                header_bytes = _stored_json_bytes(row[9], row[10])
                payload_bytes = _stored_json_bytes(row[13], row[14])
                encoded.append((
                    *tuple(row[:9]), "", sqlite3.Binary(zlib.compress(header_bytes, PACKET_JSON_COMPRESSION_LEVEL)),
                    row[11], row[12], "", sqlite3.Binary(zlib.compress(payload_bytes, PACKET_JSON_COMPRESSION_LEVEL)),
                ))
            destination.executemany(insert_packet, encoded)

        timeline_rows = source.execute(
            "SELECT sequence,received_at,session_time,lap_number,event_type,source,details_json FROM timeline ORDER BY sequence"
        )
        while True:
            batch = timeline_rows.fetchmany(2000)
            if not batch:
                break
            destination.executemany(
                "INSERT INTO timeline(sequence,received_at,session_time,lap_number,event_type,source,details_json) VALUES(?,?,?,?,?,?,?)",
                [tuple(row) for row in batch],
            )
        metadata = list(source.execute("SELECT key,value FROM metadata"))
        destination.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", [tuple(row) for row in metadata])
        destination.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        destination.execute(
            "INSERT INTO metadata(key,value) VALUES('packet_json_encoding',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (PACKET_JSON_ENCODING,),
        )
        destination.commit()
        destination.execute("PRAGMA synchronous=NORMAL")
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        destination_packets = int(destination.execute("SELECT COUNT(*) FROM packets").fetchone()[0])
        destination_events = int(destination.execute("SELECT COUNT(*) FROM timeline").fetchone()[0])
        destination_digest = _packet_content_digest(destination)
        if integrity != "ok":
            raise RuntimeError(f"migrated database integrity check failed: {integrity}")
        if (source_packets, source_events) != (destination_packets, destination_events):
            raise RuntimeError("migrated database row counts do not match the source")
        if source_digest != destination_digest:
            raise RuntimeError("migrated packet-content digest does not match the source")
        destination.execute("VACUUM")
        destination.close()
        destination = None
        source.close()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.uncompressed-{stamp}.bak")
        path.replace(backup)
        try:
            temporary.replace(path)
        except Exception:
            backup.replace(path)
            raise
        for suffix in ("-wal", "-shm"):
            path.with_name(temporary.name + suffix).unlink(missing_ok=True)
            path.with_name(backup.name + suffix).unlink(missing_ok=True)
        compressed_bytes = path.stat().st_size
        if not keep_backup:
            backup.unlink()
        return {
            "path": str(path), "schema_version": SCHEMA_VERSION,
            "packet_json_encoding": PACKET_JSON_ENCODING,
            "packet_count": destination_packets, "event_count": destination_events,
            "logical_sha256": destination_digest, "integrity_check": integrity,
            "before_bytes": source_bytes, "after_bytes": compressed_bytes,
            "saved_bytes": source_bytes - compressed_bytes,
            "backup": str(backup) if keep_backup else None,
        }
    finally:
        try:
            source.close()
        except sqlite3.Error:
            pass
        if destination is not None:
            destination.close()
        if destination_store is not None:
            destination_store.close()
        temporary.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
