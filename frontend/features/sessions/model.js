export function sessionLabel(session) {
  return `${session.name} · ${session.mode.replace("_", " ")} · ${session.track_name} · ${session.lap_count} laps`;
}

export function sessionDateParts(session) {
  if (!session.started_at) return {year: "legacy", month: "", day: ""};
  const date = new Date(session.started_at);
  if (Number.isNaN(date.getTime())) return {year: "legacy", month: "", day: ""};
  return {
    year: String(date.getFullYear()),
    month: String(date.getMonth() + 1).padStart(2, "0"),
    day: String(date.getDate()).padStart(2, "0"),
  };
}

export function filteredSessions(sessions, filter) {
  if (!filter.year) return [];
  return sessions.filter(session => {
    const date = sessionDateParts(session);
    return date.year === filter.year
      && (!filter.month || date.month === filter.month)
      && (!filter.day || date.day === filter.day);
  }).sort((left, right) => Date.parse(right.started_at || 0) - Date.parse(left.started_at || 0));
}

export function sessionChoices(sessions, selectedSessionId, filter) {
  const selected = sessions.find(session => session.id === selectedSessionId);
  const matches = filteredSessions(sessions, filter);
  return selected ? [selected, ...matches.filter(session => session.id !== selected.id)] : matches;
}

export function sampleRate(lap) {
  const seconds = (lap.last_session_time ?? 0) - (lap.first_session_time ?? 0);
  return seconds > 0 ? `${(lap.sample_count / seconds).toFixed(1)} Hz` : "—";
}

export function sessionSyncKey(session) {
  return JSON.stringify([
    session.id, session.name, session.mode, session.status, session.started_at,
    session.ended_at, session.track_id, session.track_name, session.track_name_override,
    session.track_length_m, session.lap_count, session.best_valid_lap_ms,
    session.circuit_profile_id, session.can_delete, session.game_session_type,
    session.game_session_name, session.final_classification,
  ]);
}

export const sessionListKey = sessions => sessions.map(sessionSyncKey).join("|");
