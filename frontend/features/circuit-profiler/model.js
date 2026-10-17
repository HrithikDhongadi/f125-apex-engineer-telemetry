export function turnBoundaries(apex, margin, beforeMargin, afterMargin, originalMargin, length) {
  if (!Number.isFinite(apex) || !Number.isFinite(margin) || margin < 0) return null;
  const changed = !Number.isFinite(originalMargin) || Math.abs(margin - originalMargin) > 0.001;
  const before = changed ? margin : beforeMargin;
  const after = changed ? margin : afterMargin;
  return {
    apex_m: apex,
    entry_m: Math.max(0, apex - (Number.isFinite(before) ? before : margin)),
    exit_m: Math.min(length, apex + (Number.isFinite(after) ? after : margin)),
  };
}

export function traceDirection(entry, apex, exit) {
  const first = Math.atan2(apex.z - entry.z, apex.x - entry.x);
  const second = Math.atan2(exit.z - apex.z, exit.x - apex.x);
  const change = Math.atan2(Math.sin(second - first), Math.cos(second - first));
  return Math.abs(change) < 0.04 ? "straight" : change > 0 ? "right" : "left";
}
