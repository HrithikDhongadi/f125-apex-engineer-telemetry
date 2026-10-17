export function personalBestSources(personalBests, formatTime) {
  return personalBests.filter(pb => pb.trace_available !== false).map(pb => ({
    value: `pb:${pb.key}`,
    label: `Stored PB · ${pb.track_name} · ${pb.mode.replace("_", " ")} · ${formatTime(pb.time_ms)}${pb.official_time_only ? ` · captured trace ${formatTime(pb.comparison_lap_time_ms)}` : ""}`,
  }));
}
