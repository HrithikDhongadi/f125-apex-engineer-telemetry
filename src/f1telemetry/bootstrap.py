"""Application composition, startup, and shutdown for Apex Engineer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread

from .receiver import SessionStore, UdpReceiver
from .server import (
    DATA,
    LOGGER,
    DashboardHandler,
    DashboardHTTPServer,
    configure_server_logging,
)
from .settings import SettingsStore


@dataclass
class ApplicationRuntime:
    """Concrete dependencies and lifecycle owned by the composition root."""

    store: SessionStore
    receiver: UdpReceiver
    server: DashboardHTTPServer
    janitor_stop: Event
    janitor: Thread
    host: str
    port: int
    udp_port: int
    log_path: Path

    def run(self) -> None:
        self.receiver.start()
        self.janitor.start()
        LOGGER.info("Apex Engineer: http://%s:%s", self.host, self.port)
        LOGGER.info("Listening for F1 25 UDP telemetry on 0.0.0.0:%s", self.udp_port)
        LOGGER.info("Server log: %s", self.log_path)
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            LOGGER.info("Stopping Apex Engineer")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.store.interrupt_active()
        self.janitor_stop.set()
        if self.janitor.is_alive():
            self.janitor.join(timeout=2)
        self.receiver.stop()
        self.server.server_close()
        for handler in list(LOGGER.handlers):
            handler.flush()
            handler.close()
            LOGGER.removeHandler(handler)


def create_runtime(
    port: int | None = None,
    udp_port: int | None = None,
    host: str | None = None,
) -> ApplicationRuntime:
    """Create concrete runtime dependencies without starting their threads."""
    log_path = configure_server_logging()
    configured = SettingsStore(DATA / "settings.json").load()
    resolved_port = int(port if port is not None else configured["web_port"])
    resolved_udp_port = int(udp_port if udp_port is not None else configured["udp_port"])
    resolved_host = str(host if host is not None else configured["web_host"])
    store = SessionStore(DATA)
    receiver = UdpReceiver(store, resolved_udp_port)
    DashboardHandler.store = store
    server = DashboardHTTPServer((resolved_host, resolved_port), DashboardHandler)
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
    return ApplicationRuntime(
        store=store,
        receiver=receiver,
        server=server,
        janitor_stop=janitor_stop,
        janitor=janitor,
        host=resolved_host,
        port=resolved_port,
        udp_port=resolved_udp_port,
        log_path=log_path,
    )


def serve(port: int | None = None, udp_port: int | None = None, host: str | None = None) -> None:
    create_runtime(port=port, udp_port=udp_port, host=host).run()
