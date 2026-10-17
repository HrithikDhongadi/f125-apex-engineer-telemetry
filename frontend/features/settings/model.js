export function settingsPayload(read) {
  return {
    web_host: read("setting-web-host"),
    web_port: Number(read("setting-web-port")),
    udp_port: Number(read("setting-udp-port")),
    trash_retention_days: Number(read("setting-trash-days")),
    max_recording_gb: Number(read("setting-max-recording")),
    diagnostic_max_mb: Number(read("setting-diagnostic-mb")),
    dashboard_refresh_seconds: Number(read("setting-refresh-seconds")),
  };
}

export function deletedSessionDescription(item, formatDate = value => new Date(value).toLocaleString()) {
  return {
    identity: `${item.mode.replaceAll("_", " ")} · track ID ${item.track_id ?? "unknown"} · ${item.lap_count} laps`,
    retention: `Deleted ${formatDate(item.deleted_at)} · ${item.purge_at ? `permanent deletion ${formatDate(item.purge_at)}` : "kept until restored"}`,
  };
}
