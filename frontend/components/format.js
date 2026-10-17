export const esc = value => String(value ?? "").replace(
  /[&<>"']/g,
  character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]),
);

export const fmtTime = milliseconds => milliseconds == null
  ? "—"
  : `${Math.floor(milliseconds / 60000)}:${String(((milliseconds % 60000) / 1000).toFixed(3)).padStart(6, "0")}`;

export function fmtSetup(setup, fields) {
  if (!setup) return "unknown";
  const count = fields.filter(field => setup[field.key] !== undefined).length;
  return `${setup.front_wing ?? "unknown"}/${setup.rear_wing ?? "unknown"} wings · ${setup.on_throttle_diff ?? "unknown"}/${setup.off_throttle_diff ?? "unknown"}% diff · ${count}/${fields.length} fields`;
}
