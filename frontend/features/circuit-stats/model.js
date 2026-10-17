export const TYRE_COMPOUNDS = {7: "Intermediate", 8: "Wet", 16: "C5", 17: "C4", 18: "C3", 19: "C2", 20: "C1", 21: "C0"};

export const tyreCompoundLabel = value => value == null ? "Unknown" : TYRE_COMPOUNDS[value] || `Compound ${value}`;

export function setupState(lap, fields) {
  const count = fields.filter(field => lap.setup?.[field.key] !== undefined).length;
  if (!count) return "missing";
  return count === fields.length ? "complete" : "partial";
}

export function analysisReady(lap) {
  const quality = lap.quality || {};
  return quality.alignment === "distance"
    && Number(quality.coverage_pct) >= 85
    && quality.starts_near_line === true
    && quality.ends_near_line === true
    && Number(lap.sample_count) >= 30;
}

export function chronology(lap, sessions) {
  const session = sessions.find(item => item.id === lap.recording_session_id);
  const base = Date.parse(session?.started_at || lap.saved_at) || 0;
  return base + (Number(lap.first_session_time) || lap.number || 0) * 1000;
}

export function sector3(lap) {
  const value = Number(lap.time_ms) - Number(lap.sector1_ms) - Number(lap.sector2_ms);
  return lap.sector1_ms > 0 && lap.sector2_ms > 0 && value > 0 ? value : null;
}

export function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function deviation(values) {
  if (values.length < 2) return null;
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  return Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length);
}

export function meanNumbers(values) {
  const numbers = (values || []).map(Number).filter(Number.isFinite);
  return numbers.length ? numbers.reduce((sum, value) => sum + value, 0) / numbers.length : null;
}
