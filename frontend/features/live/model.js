export const WHEEL_NAMES = ["Rear left", "Rear right", "Front left", "Front right"];

export function wheelTelemetry(latest) {
  const inner = latest.tyre_inner_c || [];
  const surface = latest.tyre_surface_c || [];
  const brakes = latest.brake_temps_c || [];
  const pressures = latest.tyre_pressures_psi || [];
  const hottestInner = inner.length ? inner.indexOf(Math.max(...inner)) : -1;
  const hottestBrake = brakes.length ? brakes.indexOf(Math.max(...brakes)) : -1;
  return WHEEL_NAMES.map((name, index) => ({
    name,
    pressure: pressures[index],
    inner: inner[index],
    surface: surface[index],
    brake: brakes[index],
    hottestInner: index === hottestInner,
    hottestBrake: index === hottestBrake,
  }));
}

export function recorderPresentation(recording, latest) {
  const mode = latest.game_mode || "unknown";
  const sessionTypeName = latest.session_type_name || mode.replaceAll("_", " ");
  const badge = recording
    ? recording.status === "armed"
      ? "Armed"
      : recording.capturing_lap_number != null
        ? `Recording lap ${recording.capturing_lap_number}`
        : "Recording"
    : "Not recording";
  const statusMessage = recording?.status === "armed"
    ? "Armed — waiting for the next start/finish crossing; the partial approach is excluded."
    : recording?.capturing_lap_number != null
      ? `Capturing lap ${recording.capturing_lap_number}; it will be saved at the next verified start/finish crossing.`
      : recording
        ? "Recording active — waiting for the next complete-lap start/finish crossing."
        : "";
  return {mode, sessionTypeName, badge, statusMessage};
}
