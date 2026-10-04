"""Small local web server. Deliberately dependency-free for easy first use."""

from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import ComparisonError, compare_laps
from .receiver import SessionStateError, SessionStore, UdpReceiver
from .reports import ExportError
from .setups import SETUP_FIELDS, SetupValidationError

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
DATA = ROOT / "data"


class DashboardHandler(SimpleHTTPRequestHandler):
    store: SessionStore

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

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
                    query.get("format", ["markdown"])[0],
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
                return self._json(compare_laps(baseline, candidate))
            except ComparisonError as error:
                return self._json({"error": str(error)}, 400)
        if path.startswith("/api/laps/"):
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
            if path == "/api/personal-bests/rebuild":
                return self._json(self.store.rebuild_personal_bests())
            if path == "/api/sessions/select":
                return self._json(self.store.select_session(str(payload.get("session_id", ""))))
            if path.startswith("/api/sessions/") and path.endswith("/rename"):
                session_id = unquote(path[len("/api/sessions/"):-len("/rename")].rstrip("/"))
                return self._json(self.store.rename_session(session_id, str(payload.get("name", ""))))
            if path.startswith("/api/sessions/") and path.endswith("/track-name"):
                session_id = unquote(path[len("/api/sessions/"):-len("/track-name")].rstrip("/"))
                return self._json(self.store.override_track_name(session_id, str(payload.get("name", ""))))
            if path.startswith("/api/laps/") and path.endswith("/setup"):
                lap_id = unquote(path[len("/api/laps/"):-len("/setup")].rstrip("/"))
                return self._json(self.store.amend_lap_setup(lap_id, payload.get("setup")))
        except (SessionStateError, SetupValidationError) as error:
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
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 4096))
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
        self.wfile.write(body)

    def _download(self, body: bytes, content_type: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8025, udp_port: int = 20777, host: str = "127.0.0.1") -> None:
    store = SessionStore(DATA)
    receiver = UdpReceiver(store, udp_port)
    DashboardHandler.store = store
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    receiver.start()
    print(f"Apex Engineer: http://{host}:{port}")
    print(f"Listening for F1 25 UDP telemetry on 0.0.0.0:{udp_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Apex Engineer.")
    finally:
        store.interrupt_active()
        receiver.stop()
        server.server_close()
