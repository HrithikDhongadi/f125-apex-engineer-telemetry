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

    def test_static_dashboard_assets_are_revalidated(self):
        server = (ROOT / "src" / "f1telemetry" / "server.py").read_text(encoding="utf-8")
        self.assertIn("def end_headers(self)", server)
        self.assertIn('self.send_header("Cache-Control", "no-cache")', server)

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
        self.assertIn("Keys 1–6: pages", html)
        self.assertIn('aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight + - Q E 0"', html)
        self.assertIn('id="profile-rotate-left"', html)
        self.assertIn('id="profile-rotate-right"', html)
        self.assertIn('document.addEventListener("keydown"', script)
        self.assertIn("activateView(tabs[index])", script)

    def test_circuit_stats_tab_has_ranked_pagination_and_filters(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('data-view="circuit-stats"', html)
        self.assertIn('id="view-circuit-stats"', html)
        for element_id in (
            "stats-track", "stats-mode", "stats-validity", "stats-session",
            "stats-year", "stats-compound", "stats-setup", "stats-quality",
            "stats-order", "stats-search", "circuit-stats-summary",
            "circuit-stats-laps", "stats-prev", "stats-next",
            "race-stats-summary", "race-stats-sessions", "race-stats-count",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("CIRCUIT_STATS_PAGE_SIZE = 10", script)
        self.assertIn("function renderCircuitStats()", script)
        self.assertIn("function circuitStatsSummary(rows)", script)
        self.assertIn("function renderRaceStats()", script)
        self.assertIn("Verified completed results only", script)
        self.assertIn('["Wins",wins', script)
        self.assertIn("Final Classification packet", html)
        self.assertIn("completedAtLine", script)
        self.assertIn("Best to worst", html)
        self.assertIn("Theoretical best", script)
        self.assertIn(".circuit-summary-grid", styles)
        self.assertIn(".race-summary-grid", styles)

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

    def test_live_and_library_refreshes_are_bounded_and_change_aware(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const lapsBySession = new Map()", script)
        self.assertIn('request("/api/live")', script)
        self.assertIn('request("/api/laps?all=1")', script)
        self.assertIn("Number(previous.lap_count)!==Number(session.lap_count)", script)
        self.assertIn("libraryRequestPending", script)
        self.assertIn("liveRequestPending", script)
        self.assertIn("background&&document.hidden", script)
        self.assertIn("timelineBySession", script)
        self.assertIn("timelineRequests", script)
        self.assertIn("async function ensureSelectedTimeline()", script)
        self.assertIn('if (button.dataset.view === "session") void ensureSelectedTimeline()', script)
        self.assertIn('loadLibrary({selection:true})', script)
        self.assertIn("force=!background&&!selection", script)
        self.assertIn("Loading event timeline", script)
        self.assertIn("renameDraftDirty", script)
        self.assertIn("trackDraftDirty", script)
        self.assertIn("settingsDraftDirty", script)

    def test_page_chrome_hides_irrelevant_recorder_clutter(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('<body data-view="live">', html)
        self.assertIn("document.body.dataset.view=button.dataset.view", script)
        self.assertIn('body[data-view="circuit-stats"] .recorder', styles)
        self.assertIn('body[data-view="settings"] .recorder', styles)
        self.assertIn('body:not([data-view="live"]) .recorder-actions', styles)

    def test_displayed_session_selector_follows_each_date_filter(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        recorder = html.split('<section class="panel recorder"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn("data-displayed-session-select", recorder)
        self.assertEqual(html.count("data-displayed-session-select"), 3)
        self.assertEqual(html.count("data-historical-badge"), 3)
        for view, following in (("analysis", "circuit-stats"), ("session", "circuit-profiler"), ("circuit-profiler", "settings")):
            section = html.split(f'<section id="view-{view}"', 1)[1].split(f'<section id="view-{following}"', 1)[0]
            self.assertLess(section.index("data-session-filter"), section.index("data-displayed-session-select"))
        self.assertIn('document.querySelectorAll("[data-displayed-session-select]")', script)

    def test_personal_best_registry_is_collapsible(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<details class="panel pb-panel">', html)
        self.assertIn("Stored valid PBs and captured traces", html)
        self.assertIn('id="rebuild-pbs"', html)

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
        self.assertIn('conditions are not like-for-like; results will be exploratory', script)
        self.assertIn("Baseline is game-invalid", script)
        self.assertIn("data.comparison_warnings", script)

    def test_practice_qualifying_and_long_lists_are_exposed_cleanly(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('<option value="practice">Practice</option>', html)
        self.assertIn('<option value="qualifying">Qualifying / Shootout</option>', html)
        for element_id in (
            "pb-prev", "pb-next", "race-stats-prev", "race-stats-next",
            "session-prev", "session-next", "session-laps-prev", "session-laps-next",
            "timeline-newer", "timeline-older", "deleted-prev", "deleted-next",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("const DASHBOARD_PAGE_SIZE = 10", script)
        self.assertIn("const TIMELINE_PAGE_SIZE = 25", script)
        self.assertIn("function pageData", script)

    def test_session_lap_ranking_matches_circuit_ranking_evidence(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        session_view = html.split('<section id="view-session"', 1)[1].split('<section id="view-circuit-profiler"', 1)[0]
        for heading in (
            "Rank / export", "Lap time", "Sectors 1 / 2 / 3", "Lap / validity",
            "Tyres", "Setup", "Trace / speed", "Note or tag", "Open",
        ):
            self.assertIn(f"<th>{heading}</th>", session_view)
        for evidence in (
            "bestValid", "statsSector3(lap)", "tyreCompoundLabel(lap.actual_tyre_compound)",
            "statsSetupState(lap)", "lap.quality?.coverage_pct", "sampleRate(lap)",
            'data-session-compare="baseline"', 'data-session-compare="candidate"',
            'class="edit-setup"', 'class="tag-input"', 'class="lap-export-check"',
        ):
            self.assertIn(evidence, script)
        self.assertIn(".session-laps-table th:nth-child(9)", styles)
        self.assertIn(".invalid-row{color:inherit}", styles)
        self.assertNotIn(".invalid-row{color:var(--yellow)}", styles)
        self.assertNotIn(".invalid-row .time{color:var(--yellow)}", styles)

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
        global_shell = html.split('<section id="view-live"', 1)[0]
        analysis_view = html.split('<section id="view-analysis"', 1)[1].split('<section id="view-circuit-stats"', 1)[0]
        session_view = html.split('<section id="view-session"', 1)[1].split('<section id="view-circuit-profiler"', 1)[0]
        profiler_view = html.split('<section id="view-circuit-profiler"', 1)[1].split('<section id="view-settings"', 1)[0]
        for element_id in (
            "session-filter-year", "session-filter-month", "session-filter-day",
            "session-filter-clear", "session-filter-count",
        ):
            self.assertIn(f'id="{element_id}"', session_view)
            self.assertNotIn(f'id="{element_id}"', global_shell)
        self.assertIn('aria-label="Lap Analysis saved session date filter"', analysis_view)
        self.assertIn('aria-label="Circuit Profiler saved session date filter"', profiler_view)
        self.assertEqual(html.count("data-session-filter aria-label="), 3)
        self.assertEqual(html.count('data-session-filter-field="year"'), 3)
        self.assertIn("No archive sessions shown", html)
        self.assertIn("function filteredSessions()", script)
        self.assertIn("function sessionChoices()", script)
        self.assertIn("function renderSessionQuery()", script)
        self.assertIn("Choose a year above to show saved sessions.", script)
        self.assertIn("sessionChoices().map", script)
        self.assertIn('document.querySelectorAll("[data-session-filter]")', script)
        self.assertIn(".session-query select,.session-query button", styles := (ROOT / "static" / "style.css").read_text(encoding="utf-8"))
        self.assertIn("color-scheme:dark", styles)

    def test_f1_theme_keeps_brand_and_telemetry_colours_distinct(self):
        styles = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        profiler = (ROOT / "frontend" / "circuit-profiler.js").read_text(encoding="utf-8")
        self.assertIn("--brand:#e10600", styles)
        self.assertIn("--green:#36d996", styles)
        self.assertIn("--yellow:#ffb020", styles)
        self.assertIn("background:var(--brand)", styles)
        self.assertIn('baseline:"#43c6e8"', script)
        self.assertIn('candidate:"#e10600"', script)
        self.assertIn('candidate: "#e10600"', profiler)
        self.assertIn('baseline: "#43c6e8"', profiler)


if __name__ == "__main__":
    unittest.main()
