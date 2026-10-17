#!/usr/bin/env python3
"""Repeatable synthetic scale benchmark for the approved archive-performance phase."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import resource
from statistics import median
import sys
import tracemalloc
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.f1telemetry.domain.models import Lap, RecordingSession
from src.f1telemetry.receiver import SessionStore


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def timed_store(root: Path) -> tuple[SessionStore, float, int, int, int]:
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    tracemalloc.start()
    started = perf_counter()
    store = SessionStore(root)
    elapsed = perf_counter() - started
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return store, elapsed, current_bytes // 1024, peak_bytes // 1024, max(0, after - before)


def create_archive(root: Path, sessions: int, laps: int, samples: int) -> None:
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True)
    for session_index in range(sessions):
        session_id = f"session-20261015T{session_index:06d}-bench"
        directory = sessions_dir / session_id
        directory.mkdir()
        lap_ids = []
        for lap_number in range(1, laps + 1):
            saved_at = f"20261015T{session_index:06d}{lap_number:02d}Z"
            rows = [
                {"t": point / 60, "lap_distance_m": point * 5.0, "speed_kph": 200 + point % 90,
                 "throttle": point % 101, "brake": (point * 3) % 101}
                for point in range(samples)
            ]
            lap = Lap(
                lap_number, 90_000 + lap_number, False, rows, saved_at=saved_at,
                recording_session_id=session_id, mode="time_trial", track_id=7, track_length_m=5890,
            )
            lap_ids.append(lap.id)
            payload = asdict(lap)
            payload["sample_count"] = len(rows)
            (directory / f"{lap.id}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        session = RecordingSession(
            session_id, f"Benchmark {session_index}", "time_trial",
            f"2026-10-15T{session_index % 24:02d}:00:00Z", "2026-10-15T23:00:00Z",
            "completed", session_index + 1, 7, 5890, lap_ids,
        )
        (directory / "session.json").write_text(json.dumps(asdict(session)), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=12)
    parser.add_argument("--laps", type=int, default=20)
    parser.add_argument("--samples", type=int, default=1200)
    parser.add_argument("--packets", type=int, default=2000)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="apex-benchmark-") as directory:
        root = Path(directory)
        create_archive(root, args.sessions, args.laps, args.samples)
        cold, cold_seconds, cold_idle_kib, cold_peak_kib, cold_rss_kib = timed_store(root)
        cold_count = len(cold.lap_by_id)
        del cold
        warm, warm_seconds, warm_idle_kib, warm_peak_kib, warm_rss_kib = timed_store(root)

        live_latencies = []
        for _ in range(1000):
            started = perf_counter()
            warm.live_snapshot()
            live_latencies.append((perf_counter() - started) * 1000)

        packet_latencies = []
        packet_ids = (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15)
        race_uid = 999_999
        warm.record_game_session({
            "session_uid": race_uid, "session_type": 15, "session_type_name": "Race",
            "mode": "race", "track_id": 7, "track_length_m": 5890,
        })
        for frame in range(args.packets):
            packet_id = packet_ids[frame % len(packet_ids)]
            header = SimpleNamespace(
                packet_format=2025, game_year=25, game_major_version=1, game_minor_version=0,
                packet_version=1, packet_id=packet_id, session_uid=race_uid,
                session_time=frame / 60, frame_identifier=frame,
                overall_frame_identifier=frame, player_car_index=0,
                secondary_player_car_index=255,
            )
            started = perf_counter()
            warm.record_full_packet(header, {"player": {"lap_number": 1, "lap_distance_m": frame % 5890}})
            packet_latencies.append((perf_counter() - started) * 1000)
        warm.stop_recording()

        target = next(item for item in warm.sessions.values() if item.name == "Benchmark 0")
        started = perf_counter()
        _name, _mime, artifact = warm.export_session(target.id, "session", [], "zip", file_output=True)
        export_seconds = perf_counter() - started
        export_bytes = artifact.stat().st_size
        artifact.unlink(missing_ok=True)

        print(json.dumps({
            "fixture": {"sessions": args.sessions, "laps_per_session": args.laps, "samples_per_lap": args.samples,
                        "lap_count": cold_count, "packet_mix_count": args.packets},
            "startup": {"cold_index_build_s": round(cold_seconds, 4), "warm_indexed_s": round(warm_seconds, 4),
                        "cold_idle_python_kib": cold_idle_kib, "warm_idle_python_kib": warm_idle_kib,
                        "cold_peak_python_kib": cold_peak_kib, "warm_peak_python_kib": warm_peak_kib,
                        "cold_peak_rss_delta_kib": cold_rss_kib, "warm_peak_rss_delta_kib": warm_rss_kib},
            "packet_processing_ms": {"p50": round(median(packet_latencies), 4),
                                     "p95": round(percentile(packet_latencies, 0.95), 4),
                                     "p99": round(percentile(packet_latencies, 0.99), 4),
                                     "max": round(max(packet_latencies), 4)},
            "live_snapshot_ms": {"p50": round(median(live_latencies), 4),
                                 "p95": round(percentile(live_latencies, 0.95), 4),
                                 "p99": round(percentile(live_latencies, 0.99), 4)},
            "session_zip": {"seconds": round(export_seconds, 4), "bytes": export_bytes},
        }, indent=2))


if __name__ == "__main__":
    main()
