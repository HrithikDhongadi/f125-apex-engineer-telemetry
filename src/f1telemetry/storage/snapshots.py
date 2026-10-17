"""Immutable, WAL-safe SQLite snapshots captured at a committed watermark."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile


@dataclass
class PacketStoreSnapshot:
    """A temporary online-backup snapshot which owns its file lifecycle."""

    path: Path
    packet_watermark: int
    event_watermark: int

    @classmethod
    def capture(cls, connection: sqlite3.Connection) -> "PacketStoreSnapshot":
        packet_watermark = int(connection.execute("SELECT COALESCE(MAX(sequence),0) FROM packets").fetchone()[0])
        event_watermark = int(connection.execute("SELECT COALESCE(MAX(sequence),0) FROM timeline").fetchone()[0])
        handle = tempfile.NamedTemporaryFile(prefix="apex-export-", suffix=".sqlite3", delete=False)
        path = Path(handle.name)
        handle.close()
        destination = sqlite3.connect(path)
        try:
            connection.backup(destination)
        except Exception:
            destination.close()
            path.unlink(missing_ok=True)
            raise
        destination.close()
        return cls(path, packet_watermark, event_watermark)

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def close(self) -> None:
        self.path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

    def __enter__(self) -> "PacketStoreSnapshot":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


def snapshot_path(path: Path) -> PacketStoreSnapshot:
    """Capture a closed or concurrently readable database via SQLite backup."""
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        return PacketStoreSnapshot.capture(source)
    finally:
        source.close()
