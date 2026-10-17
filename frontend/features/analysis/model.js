export function sourceLaps(source, allLaps, personalBests) {
  if (source.startsWith("pb:")) {
    const pb = personalBests.find(item => `pb:${item.key}` === source);
    return pb && pb.trace_available !== false ? [{
      ...pb,
      id: pb.comparison_id,
      number: pb.comparison_source_lap_number ?? pb.source_lap_number,
      time_ms: pb.comparison_lap_time_ms ?? pb.time_ms,
      invalid: false,
    }] : [];
  }
  return allLaps
    .filter(lap => `session:${lap.recording_session_id}` === source)
    .sort((left, right) => left.number - right.number);
}

export function comparisonCompatibility(baseline, candidate, baselineLap, candidateLap, profiles) {
  let error = "";
  const warnings = [];
  if (baseline && candidate) {
    if (baseline.track_id == null || candidate.track_id == null || baseline.track_id < 0 || candidate.track_id < 0) {
      error = "Unknown track identity cannot be compared.";
    } else if (baseline.track_id !== candidate.track_id) {
      error = `Incompatible circuits: track ID ${baseline.track_id} versus ${candidate.track_id}.`;
    } else if (
      baseline.mode === "race"
      && candidate.mode === "race"
      && baseline.session_id !== candidate.session_id
    ) {
      const profile = profiles.find(item => item.id === baseline.profile_id);
      if (!profile || baseline.profile_id !== candidate.profile_id || profile.verification_status !== "verified") {
        error = "Cross-session race comparison requires both sessions to select the same manually verified circuit profile.";
      }
    }
    if (!error && baseline.mode !== candidate.mode) {
      warnings.push(`${baseline.mode.replaceAll("_", " ")} and ${candidate.mode.replaceAll("_", " ")} conditions are not like-for-like; results will be exploratory.`);
    }
    if (baselineLap?.invalid) warnings.push("Baseline is game-invalid and cannot be treated as valid-lap or PB evidence.");
    if (candidateLap?.invalid) warnings.push("Candidate is game-invalid and cannot be treated as valid-lap or PB evidence.");
  }
  return {error, warnings};
}

export function setupDifferences(baseline, candidate, fields) {
  if (!baseline && !candidate) return [];
  return fields
    .filter(field => (baseline || {})[field.key] !== undefined || (candidate || {})[field.key] !== undefined)
    .filter(field => (baseline || {})[field.key] !== (candidate || {})[field.key])
    .map(field => ({
      label: field.label,
      from: (baseline || {})[field.key] ?? "unknown",
      to: (candidate || {})[field.key] ?? "unknown",
      unit: field.unit || "",
    }));
}
