"""Small local web server. Deliberately dependency-free for easy first use."""

from __future__ import annotations

from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sys
from threading import Event, Thread
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import ComparisonError, calibration_trace, compare_laps
from .circuit_profiles import CircuitProfileError
from .receiver import SessionStateError, SessionStore, UdpReceiver
from .reports import ExportError
from .setups import SETUP_FIELDS, SetupValidationError
from .settings import SettingsError, SettingsStore, settings_payload

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
LOGGER = logging.getLogger("apex-engineer.server")


def configure_server_logging(directory: Path = LOGS) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"server-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for handler in (logging.StreamHandler(), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    return path


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        LOGGER.error("Unhandled request error from %s", client_address, exc_info=sys.exc_info())


class DashboardHandler(SimpleHTTPRequestHandler):
    store: SessionStore

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, format: str, *args) -> None:
        LOGGER.info("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802
        request = urlparse(self.path)
        path = request.path
        if path == "/api/snapshot":
            return self._json(self.store.snapshot())
        if path == "/api/laps":
            session_id = parse_qs(request.query).get("session", [None])[0]
            try:
                return self._json({"laps": self.store.list_laps(session_id)})
            except SessionStateError as error:
                return self._json({"error": str(error)}, 404)
        if path == "/api/sessions":
            return self._json({"sessions": self.store.list_sessions(), **self.store.snapshot()})
        if path == "/api/personal-bests":
            return self._json({"personal_bests": self.store.list_personal_bests()})
        if path == "/api/setup-schema":
            return self._json({"fields": SETUP_FIELDS})
        if path == "/api/settings":
            return self._json({**settings_payload(self.store.settings), "deleted_sessions": self.store.list_deleted_sessions()})
        if path == "/api/circuit-profiles":
            session_id = parse_qs(request.query).get("session", [None])[0]
            return self._json({"profiles": self.store.list_profiles(session_id)})
        if path.startswith("/api/sessions/") and path.endswith("/timeline"):
            session_id = unquote(path[len("/api/sessions/"):-len("/timeline")].rstrip("/"))
            query = parse_qs(request.query)
            try:
                lap_from = int(query["lap_from"][0]) if query.get("lap_from") else None
                lap_to = int(query["lap_to"][0]) if query.get("lap_to") else None
                return self._json({"events": self.store.race_timeline(session_id, lap_from, lap_to)})
            except (SessionStateError, ValueError) as error:
                return self._json({"error": str(error)}, 404)
        if path.startswith("/api/personal-bests/"):
            key = unquote(path[len("/api/personal-bests/"):].rstrip("/"))
            result = self.store.personal_best(key)
            return self._json(result or {"error": "Personal best not found"}, 200 if result else 404)
        if path.startswith("/api/sessions/") and path.endswith("/export"):
            session_id = unquote(path[len("/api/sessions/"):-len("/export")].rstrip("/"))
            query = parse_qs(request.query)
            try:
                filename, content_type, body = self.store.export_session(
                    session_id, query.get("scope", ["session"])[0], query.get("lap", []),
                    query.get("format", ["markdown"])[0], query.get("report", ["auto"])[0],
                )
                return self._download(body, content_type, filename)
            except ExportError as error:
                return self._json({"error": str(error)}, 400 if "Unknown session" not in str(error) else 404)
        if path.startswith("/api/sessions/") and path.endswith("/laps"):
            session_id = unquote(path[len("/api/sessions/"):-len("/laps")].rstrip("/"))
            try:
                return self._json({"session_id": session_id, "laps": self.store.list_laps(session_id)})
            except SessionStateError as error:
                return self._json({"error": str(error)}, 404)
        if path == "/api/compare/report":
            query = parse_qs(request.query)
            baseline_id, candidate_id = query.get("baseline", [""])[0], query.get("candidate", [""])[0]
            if not baseline_id or not candidate_id:
                return self._json({"error": "baseline and candidate lap IDs are required"}, 400)
            try:
                filename, content_type, body = self.store.export_lap_analysis(baseline_id, candidate_id)
                return self._download(body, content_type, filename)
            except (ExportError, ComparisonError) as error:
                return self._json({"error": str(error)}, 400)
        if path == "/api/compare":
            query = parse_qs(request.query)
            baseline_id = query.get("baseline", [""])[0]
            candidate_id = query.get("candidate", [""])[0]
            if not baseline_id or not candidate_id:
                return self._json({"error": "baseline and candidate lap IDs are required"}, 400)
            baseline = self.store.get_lap_object(baseline_id)
            candidate = self.store.get_lap_object(candidate_id)
            if baseline is None or candidate is None:
                missing = baseline_id if baseline is None else candidate_id
                return self._json({"error": f"Unknown lap ID: {missing}"}, 404)
            try:
                session = self.store.sessions.get(baseline.recording_session_id)
                candidate_session = self.store.sessions.get(candidate.recording_session_id)
                profile = self.store.profile_store.get(session.circuit_profile_id) if session and session.circuit_profile_id else None
                if profile is None and candidate_session and candidate_session.circuit_profile_id:
                    profile = self.store.profile_store.get(candidate_session.circuit_profile_id)
                if (
                    baseline.mode == candidate.mode == "race"
                    and baseline.recording_session_id != candidate.recording_session_id
                    and (not session or not candidate_session or session.circuit_profile_id != candidate_session.circuit_profile_id)
                ):
                    raise ComparisonError("Cross-session race laps must select the same verified circuit profile")
                return self._json(compare_laps(baseline, candidate, profile))
            except ComparisonError as error:
                return self._json({"error": str(error)}, 400)
        if path.startswith("/api/laps/"):
            if path.endswith("/trace"):
                lap_id = unquote(path[len("/api/laps/"):-len("/trace")].rstrip("/"))
                lap_object = self.store.get_lap_object(lap_id)
                return self._json(calibration_trace(lap_object) if lap_object else {"error": "Lap not found"}, 200 if lap_object else 404)
            lap = self.store.lap(unquote(path.rsplit("/", 1)[-1]))
            return self._json(lap or {"error": "Lap not found"}, 200 if lap else 404)
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._request_json()
        except ValueError:
            return self._json({"error": "Request body must be a JSON object"}, 400)
        try:
            if path == "/api/runs/start":
                return self._json(self.store.start_time_trial_run(str(payload.get("name", ""))), 201)
            if path == "/api/recording/stop":
                return self._json(self.store.stop_recording())
            if path == "/api/diagnostics":
                if not isinstance(payload.get("enabled"), bool):
                    return self._json({"error": "enabled must be true or false"}, 400)
                return self._json(self.store.configure_diagnostics(payload["enabled"]))
            if path == "/api/settings":
                return self._json(self.store.update_settings(payload))
            if path.startswith("/api/trash/sessions/") and path.endswith("/restore"):
                trash_id = unquote(path[len("/api/trash/sessions/"):-len("/restore")].rstrip("/"))
                return self._json(self.store.restore_deleted_session(trash_id, payload.get("confirm") is True))
            if path == "/api/personal-bests/rebuild":
                return self._json(self.store.rebuild_personal_bests())
            if path == "/api/circuit-profiles":
                return self._json(self.store.save_profile(payload), 201)
            if path.startswith("/api/circuit-profiles/") and path.endswith("/delete"):
                profile_id = unquote(path[len("/api/circuit-profiles/"):-len("/delete")].rstrip("/"))
                return self._json(self.store.delete_profile(profile_id, payload.get("confirm") is True))
            if path == "/api/sessions/select":
                return self._json(self.store.select_session(str(payload.get("session_id", ""))))
            if path.startswith("/api/sessions/") and path.endswith("/rename"):
                session_id = unquote(path[len("/api/sessions/"):-len("/rename")].rstrip("/"))
                return self._json(self.store.rename_session(session_id, str(payload.get("name", ""))))
            if path.startswith("/api/sessions/") and path.endswith("/delete"):
                session_id = unquote(path[len("/api/sessions/"):-len("/delete")].rstrip("/"))
                return self._json(self.store.delete_session(session_id, payload.get("confirm") is True))
            if path.startswith("/api/sessions/") and path.endswith("/track-name"):
                session_id = unquote(path[len("/api/sessions/"):-len("/track-name")].rstrip("/"))
                return self._json(self.store.override_track_name(session_id, str(payload.get("name", ""))))
            if path.startswith("/api/sessions/") and path.endswith("/circuit-profile"):
                session_id = unquote(path[len("/api/sessions/"):-len("/circuit-profile")].rstrip("/"))
                return self._json(self.store.select_profile(session_id, str(payload.get("profile_id", ""))))
            if path.startswith("/api/laps/") and path.endswith("/setup"):
                lap_id = unquote(path[len("/api/laps/"):-len("/setup")].rstrip("/"))
                return self._json(self.store.amend_lap_setup(lap_id, payload.get("setup")))
        except (SessionStateError, SetupValidationError, CircuitProfileError, SettingsError) as error:
            status = 404 if "Unknown" in str(error) else 409
            return self._json({"error": str(error)}, status)
        if path.startswith("/api/laps/") and path.endswith("/note"):
            lap_id = unquote(path[len("/api/laps/"):-len("/note")].rstrip("/"))
            if not isinstance(payload.get("note", ""), str):
                return self._json({"error": "Request body must contain a text note"}, 400)
            result = self.store.set_note(lap_id, payload.get("note", ""))
            return self._json(result or {"error": "Lap not found"}, 200 if result else 404)
        return self._json({"error": "Endpoint not found"}, 404)

    def _request_json(self) -> dict:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 1024 * 1024))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError from error
        if not isinstance(payload, dict):
            raise ValueError
        return payload

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.warning("Client disconnected while receiving %s", self.path)

    def _download(self, body: bytes, content_type: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.warning("Client disconnected while downloading %s", self.path)


def serve(port: int | None = None, udp_port: int | None = None, host: str | None = None) -> None:
    log_path = configure_server_logging()
    configured = SettingsStore(DATA / "settings.json").load()
    port = int(port if port is not None else configured["web_port"])
    udp_port = int(udp_port if udp_port is not None else configured["udp_port"])
    host = str(host if host is not None else configured["web_host"])
    store = SessionStore(DATA)
    receiver = UdpReceiver(store, udp_port)
    DashboardHandler.store = store
    server = DashboardHTTPServer((host, port), DashboardHandler)
    receiver.start()
    janitor_stop = Event()
    def trash_janitor() -> None:
        while not janitor_stop.wait(60):
            try:
                result = store.purge_expired_trash()
                if result["purged"]:
                    LOGGER.info("Permanently purged expired sessions: %s", ", ".join(result["purged"]))
            except Exception:
                LOGGER.exception("Deleted-session expiry check failed")
    janitor = Thread(target=trash_janitor, name="trash-janitor", daemon=True)
    janitor.start()
    LOGGER.info("Apex Engineer: http://%s:%s", host, port)
    LOGGER.info("Listening for F1 25 UDP telemetry on 0.0.0.0:%s", udp_port)
    LOGGER.info("Server log: %s", log_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping Apex Engineer")
    finally:
        store.interrupt_active()
        janitor_stop.set()
        janitor.join(timeout=2)
        receiver.stop()
        server.server_close()
        for handler in list(LOGGER.handlers):
            handler.flush()
            handler.close()
            LOGGER.removeHandler(handler)
