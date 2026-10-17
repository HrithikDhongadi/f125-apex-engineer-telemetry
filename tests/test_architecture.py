import ast
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.f1telemetry.application.comparisons import (
    ComparisonPolicyError,
    resolve_comparison_profile,
)
from src.f1telemetry.bootstrap import ApplicationRuntime
from src.f1telemetry.domain.errors import SessionStateError as DomainSessionStateError
from src.f1telemetry.domain.models import Lap as DomainLap, RecordingSession as DomainRecordingSession
from src.f1telemetry.receiver import Lap, RecordingSession, SessionStateError


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_receiver_preserves_model_and_error_import_compatibility(self):
        self.assertIs(Lap, DomainLap)
        self.assertIs(RecordingSession, DomainRecordingSession)
        self.assertIs(SessionStateError, DomainSessionStateError)

    def test_domain_does_not_import_delivery_or_persistence(self):
        forbidden = {
            "bootstrap", "server", "receiver", "race_store", "personal_bests",
            "circuit_profiles", "settings", "reports",
        }
        domain = Path("src/f1telemetry/domain")
        violations = []
        for path in sorted(domain.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    parts = set((node.module or "").split("."))
                    blocked = sorted(parts & forbidden)
                    if blocked:
                        violations.append(f"{path}:{node.lineno} imports {blocked[0]}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = set(alias.name.split("."))
                        blocked = sorted(parts & forbidden)
                        if blocked:
                            violations.append(f"{path}:{node.lineno} imports {blocked[0]}")
        self.assertEqual(violations, [])

    def test_application_services_do_not_import_live_delivery_or_recorder(self):
        forbidden = {"bootstrap", "server", "receiver"}
        application = Path("src/f1telemetry/application")
        violations = []
        for path in sorted(application.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    parts = set((node.module or "").split("."))
                    blocked = sorted(parts & forbidden)
                    if blocked:
                        violations.append(f"{path}:{node.lineno} imports {blocked[0]}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = set(alias.name.split("."))
                        blocked = sorted(parts & forbidden)
                        if blocked:
                            violations.append(f"{path}:{node.lineno} imports {blocked[0]}")
        self.assertEqual(violations, [])

    def test_comparison_profile_policy_is_shared_and_track_identity_safe(self):
        baseline = DomainLap(
            number=1, time_ms=90_000, invalid=False, saved_at="a",
            recording_session_id="session-a", mode="race", track_id=7,
        )
        candidate = DomainLap(
            number=2, time_ms=91_000, invalid=False, saved_at="b",
            recording_session_id="session-b", mode="race", track_id=7,
        )
        session_a = DomainRecordingSession(
            id="session-a", name="A", mode="race", started_at="2026-10-15T00:00:00Z",
            ended_at="2026-10-15T00:10:00Z", status="completed", game_session_uid=1,
            track_id=7, track_length_m=5890, circuit_profile_id="profile-a",
        )
        session_b = DomainRecordingSession(
            id="session-b", name="B", mode="race", started_at="2026-10-15T01:00:00Z",
            ended_at="2026-10-15T01:10:00Z", status="completed", game_session_uid=2,
            track_id=7, track_length_m=5890, circuit_profile_id="profile-b",
        )
        profiles = SimpleNamespace(get=lambda profile_id: {"id": profile_id})

        with self.assertRaisesRegex(ComparisonPolicyError, "same verified circuit profile"):
            resolve_comparison_profile(
                baseline, candidate, {"session-a": session_a, "session-b": session_b}, profiles,
            )

        session_b.circuit_profile_id = "profile-a"
        self.assertEqual(
            resolve_comparison_profile(
                baseline, candidate, {"session-a": session_a, "session-b": session_b}, profiles,
            ),
            {"id": "profile-a"},
        )

    def test_module_entrypoint_delegates_to_bootstrap(self):
        with patch("src.f1telemetry.bootstrap.serve") as serve:
            runpy.run_module("src.f1telemetry.__main__", run_name="__main__")
        serve.assert_called_once_with()

    def test_server_serve_remains_a_compatibility_entrypoint(self):
        from src.f1telemetry.server import serve

        with patch("src.f1telemetry.bootstrap.serve") as bootstrap_serve:
            serve(port=9000, udp_port=20778, host="127.0.0.1")
        bootstrap_serve.assert_called_once_with(port=9000, udp_port=20778, host="127.0.0.1")

    def test_runtime_owns_start_and_ordered_shutdown(self):
        calls = []

        class Receiver:
            def start(self):
                calls.append("receiver.start")

            def stop(self):
                calls.append("receiver.stop")

        class Server:
            def serve_forever(self):
                calls.append("server.serve_forever")
                raise KeyboardInterrupt

            def server_close(self):
                calls.append("server.server_close")

        class Janitor:
            def start(self):
                calls.append("janitor.start")

            def is_alive(self):
                return True

            def join(self, timeout):
                calls.append(f"janitor.join:{timeout}")

        class StopEvent:
            def set(self):
                calls.append("janitor_stop.set")

        store = SimpleNamespace(interrupt_active=lambda: calls.append("store.interrupt_active"))
        runtime = ApplicationRuntime(
            store=store,
            receiver=Receiver(),
            server=Server(),
            janitor_stop=StopEvent(),
            janitor=Janitor(),
            host="127.0.0.1",
            port=8025,
            udp_port=20777,
            log_path=Path("test.log"),
        )
        with patch("src.f1telemetry.bootstrap.LOGGER.handlers", []):
            runtime.run()

        self.assertEqual(calls, [
            "receiver.start",
            "janitor.start",
            "server.serve_forever",
            "store.interrupt_active",
            "janitor_stop.set",
            "janitor.join:2",
            "receiver.stop",
            "server.server_close",
        ])


if __name__ == "__main__":
    unittest.main()
