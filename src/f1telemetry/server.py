"""Small local web server. Deliberately dependency-free for easy first use."""

from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlparse

from .receiver import SessionStore, UdpReceiver

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
DATA = ROOT / "data"


class DashboardHandler(SimpleHTTPRequestHandler):
    store: SessionStore

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/snapshot":
            return self._json(self.store.snapshot())
        if path.startswith("/api/laps/"):
            lap = self.store.lap(path.rsplit("/", 1)[-1])
            return self._json(lap or {"error": "Lap not found"}, 200 if lap else 404)
        return super().do_GET()

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
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
