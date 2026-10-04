"""Small local web server. Deliberately dependency-free for easy first use."""

from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import ComparisonError, compare_laps
from .receiver import SessionStore, UdpReceiver

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
            return self._json({"laps": self.store.list_laps()})
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
        if path.startswith("/api/laps/") and path.endswith("/note"):
            lap_id = unquote(path[len("/api/laps/"):-len("/note")].rstrip("/"))
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload.get("note", ""), str):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                return self._json({"error": "Request body must contain a text note"}, 400)
            result = self.store.set_note(lap_id, payload.get("note", ""))
            return self._json(result or {"error": "Lap not found"}, 200 if result else 404)
        return self._json({"error": "Endpoint not found"}, 404)

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8025, udp_port: int = 20777) -> None:
    store = SessionStore(DATA)
    receiver = UdpReceiver(store, udp_port)
    receiver.start()
    DashboardHandler.store = store
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Apex Engineer: http://127.0.0.1:{port}")
    print(f"Listening for F1 25 UDP telemetry on 0.0.0.0:{udp_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Apex Engineer.")
    finally:
        receiver.stop()
        server.server_close()
