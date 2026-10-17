export function mergeProfilesPreservingDraft(loadedProfiles, currentProfiles, draftId, dirty) {
  if (!dirty || !draftId) return loadedProfiles;
  const draft = currentProfiles.find(profile => profile.id === draftId);
  if (!draft || loadedProfiles.some(profile => profile.id === draft.id)) return loadedProfiles;
  return [...loadedProfiles, draft];
}

export function retainSelectedIds(selectedIds, availableItems) {
  const available = new Set(availableItems.map(item => item.id));
  return new Set([...selectedIds].filter(id => available.has(id)));
}

export function resolveComparisonSources(sources, previous, fallback) {
  const values = new Set(sources.map(source => source.value));
  const defaultValue = values.has(fallback) ? fallback : sources[0]?.value || "";
  return {
    baselineSource: values.has(previous.baselineSource) ? previous.baselineSource : defaultValue,
    candidateSource: values.has(previous.candidateSource) ? previous.candidateSource : defaultValue,
    baselineLap: previous.baselineLap,
    candidateLap: previous.candidateLap,
  };
}
