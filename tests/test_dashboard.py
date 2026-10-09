from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.f1telemetry.circuits import track_name
from src.f1telemetry.server import LOGGER, configure_server_logging


ROOT = Path(__file__).resolve().parents[1]


class DashboardContractTests(unittest.TestCase):
    def test_server_log_uses_dated_filename_and_timestamped_lines(self):
        with TemporaryDirectory() as directory:
            path = configure_server_logging(Path(directory))
            LOGGER.info("test message")
            for handler in LOGGER.handlers:
                handler.flush()
            self.assertRegex(path.name, r"server-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.log")
            text = path.read_text(encoding="utf-8")
            self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO test message")
            for handler in list(LOGGER.handlers):
                handler.close()
                LOGGER.removeHandler(handler)

    def test_independent_session_and_lap_selectors_exist_for_both_sides(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for element_id in ("baseline-session", "baseline", "candidate-session", "candidate"):
            self.assertIn(f'id="{element_id}"', html)

    def test_settings_page_exposes_runtime_and_telemetry_configuration(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-view="settings"', html)
        for element_id in (
            "settings-form", "setting-web-host", "setting-web-port", "setting-udp-port",
            "setting-trash-days", "setting-max-recording", "setting-diagnostic-mb",
            "setting-refresh-seconds", "receiver-ip-list", "deleted-session-list",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('post("/api/settings"', script)
        self.assertIn("/api/trash/sessions/", script)
        self.assertIn("permanent deletion", script)

    def test_official_and_reverse_track_ids_remain_distinct(self):
        self.assertEqual(track_name(7), "Silverstone")
        self.assertEqual(track_name(39), "Silverstone (Reverse)")
        self.assertEqual(track_name(99), "Unknown track (ID 99)")

    def test_circuit_profile_editor_exposes_trace_alternates_and_verification(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="circuit-profiler"', html)
        session_view = html.split('<section id="view-session"', 1)[1].split('<section id="view-circuit-profiler"', 1)[0]
        profiler_view = html.split('<section id="view-circuit-profiler"', 1)[1]
        self.assertNotIn('class="panel profile-editor"', session_view)
        self.assertIn('class="panel profile-editor"', profiler_view)
        for element_id in (
            "profile-select", "profile-alternate", "profile-verification",
            "profile-source", "profile-lap", "profile-load-trace", "circuit-map",
            "profile-reset-view", "profile-load-saved", "profile-delete",
            "profile-turn-display",
            "profile-show-all-turns", "turn-list-status",
            "profile-add-suggestions", "point-turn-number", "point-turn-name",
            "point-turn-margin", "profile-point-turn", "profile-cancel-point",
            "point-turn-status", "race-timeline",
            "session-profile-name", "session-profile-status", "edit-session-profile",
        ):
            self.assertIn(f'id="{element_id}"', html)

        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("click its apex/midpoint on the trace", script)
        self.assertIn("manually pointed apex on clean Motion X/Z lap trace", script)
        self.assertIn("profileEditorDirty", script)
        self.assertIn("turnMarkersFromRows", script)
        self.assertIn("CircuitProfilerCanvas", script)
        self.assertIn("onApexMoved", script)
        self.assertIn("onTurnSelected", script)
        self.assertIn("selectedTurnIndex", script)
        self.assertIn("showAllTurnCards", script)
        self.assertIn("updateTurnCardVisibility", script)
        self.assertIn("deleteCircuitProfile", script)
        self.assertIn("loadSavedProfile", script)
        self.assertIn("/api/circuit-profiles/", script)
        self.assertIn('data-key="margin_m"', script)
        self.assertIn("turnBoundariesFromRow", script)
        self.assertNotIn('data-key="entry_m"', script)
        self.assertNotIn('data-key="exit_m"', script)
        self.assertIn("Margin each side", html)

        editor = (ROOT / "frontend" / "circuit-profiler.js").read_text(encoding="utf-8")
        self.assertIn("new Konva.Stage", editor)
        self.assertIn('this.stage.on("wheel"', editor)
        self.assertIn("draggable: true", editor)
        self.assertIn("onApexSelected", editor)
        self.assertIn("onApexMoved", editor)
        self.assertIn("strokeScaleEnabled: false", editor)
        self.assertIn("TURN_COLORS", editor)
        self.assertIn("setDisplayMode", editor)
        self.assertIn('on("pointerclick"', editor)
        self.assertIn("PAN_SURFACE_SIZE", editor)
        self.assertIn("rotateBy(degrees)", editor)
        self.assertIn("_handleKey(event)", editor)
        self.assertIn("shiftKey", editor)
        self.assertIn("x: offsetX + usedWidth - (Number(point.x) - minX) * scale", editor)
        self.assertIn("x: p.offsetX + p.usedWidth - (Number(point.x) - p.minX) * p.scale", editor)
        self.assertIn('src="circuit-profiler.bundle.js"', html)

    def test_lap_analysis_has_profile_based_local_pace_map(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Where the lap time was won", html)
        self.assertIn('id="pace-map-profile"', html)
        self.assertIn('id="pace-map-canvas"', html)
        self.assertIn('id="pace-map-reset"', html)
        self.assertIn('id="pace-map-rotate-left"', html)
        self.assertIn('id="pace-map-rotate-right"', html)
        self.assertIn("Candidate gains", html)
        self.assertIn("Baseline gains", html)
        self.assertIn("function drawPaceMap", script)
        self.assertIn("new window.PaceMapCanvas", script)
        self.assertIn("circuit_map", script)
        self.assertIn("local change", html)
        editor = (ROOT / "frontend" / "circuit-profiler.js").read_text(encoding="utf-8")
        self.assertIn("class PaceMapCanvas", editor)
        self.assertIn("window.PaceMapCanvas", editor)

    def test_global_and_map_keyboard_shortcuts_are_visible_and_wired(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Keys 1–5: pages", html)
        self.assertIn('aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight + - Q E 0"', html)
        self.assertIn('id="profile-rotate-left"', html)
        self.assertIn('id="profile-rotate-right"', html)
        self.assertIn('document.addEventListener("keydown"', script)
        self.assertIn("activateView(tabs[index])", script)

    def test_session_library_exposes_recoverable_delete_control(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="delete-session"', html)
        self.assertIn('/delete`,{confirm:true}', script)
        self.assertIn("recoverable local trash", html)

    def test_controller_events_have_a_friendly_optional_timeline_filter(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="timeline-controller-events"', html)
        self.assertIn('id="timeline-other-car-events"', html)
        self.assertIn('id="timeline-superseded-events"', html)
        self.assertIn("event.event_validity!==\"superseded\"", script)
        self.assertIn("Show other cars", html)
        self.assertIn("Show controller inputs", html)
        self.assertIn("function isControllerEvent", script)
        self.assertIn('event.type==="butn"', script)
        self.assertIn('label=controller?"Controller input"', script)
        self.assertIn("event.player_relevant!==false", script)

    def test_three_engineering_report_types_are_selectable(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="report-type"', html)
        for value in ("lap_analysis", "race", "time_trial"):
            self.assertIn(f'value="{value}"', html)
        self.assertIn("new URLSearchParams({scope,format,report})", script)
        self.assertIn('id="download-comparison-report"', html)
        self.assertIn("/api/compare/report", script)

    def test_exploratory_invalid_and_cross_mode_comparisons_show_warnings(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="compare-warning"', html)
        self.assertIn('id="comparison-warnings"', html)
        self.assertIn("Race and Time Trial conditions are not like-for-like", script)
        self.assertIn("Baseline is game-invalid", script)
        self.assertIn("data.comparison_warnings", script)

    def test_comparison_summary_uses_compact_expandable_setup_section(self):
        styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("width:min(1680px,100%)", styles)
        self.assertIn(".setup-comparison", styles)
        self.assertIn(".setup-change-grid", styles)
        self.assertIn('<details class="setup-comparison">', script)
        self.assertIn("function setupDifferences", script)

    def test_session_archive_is_hidden_until_filtered_by_date(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        for element_id in (
            "session-filter-year", "session-filter-month", "session-filter-day",
            "session-filter-clear", "session-filter-count",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("No archive sessions shown", html)
        self.assertIn("function filteredSessions()", script)
        self.assertIn("function sessionChoices()", script)
        self.assertIn("function renderSessionQuery()", script)
        self.assertIn("Choose a year above to show saved sessions.", script)
        self.assertIn("sessionChoices().map", script)


if __name__ == "__main__":
    unittest.main()
