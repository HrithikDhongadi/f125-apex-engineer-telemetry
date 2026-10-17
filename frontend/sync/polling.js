export function createPollingController({documentRef, onLive, onLibrary, setIntervalImpl = setInterval, clearIntervalImpl = clearInterval}) {
  let liveTimer = null;
  let libraryTimer = null;
  let librarySeconds = null;

  const visible = callback => () => {
    if (!documentRef.hidden) void callback();
  };

  function startLive(milliseconds = 1000) {
    if (liveTimer) clearIntervalImpl(liveTimer);
    liveTimer = setIntervalImpl(visible(onLive), milliseconds);
  }

  function setLibraryInterval(seconds) {
    const normalized = Number(seconds) || 8;
    if (libraryTimer && normalized === librarySeconds) return;
    if (libraryTimer) clearIntervalImpl(libraryTimer);
    librarySeconds = normalized;
    libraryTimer = setIntervalImpl(visible(() => onLibrary({background: true})), normalized * 1000);
  }

  function refreshVisible() {
    if (documentRef.hidden) return;
    void onLive();
    void onLibrary({background: true});
  }

  function stop() {
    if (liveTimer) clearIntervalImpl(liveTimer);
    if (libraryTimer) clearIntervalImpl(libraryTimer);
    liveTimer = null;
    libraryTimer = null;
  }

  return {startLive, setLibraryInterval, refreshVisible, stop};
}
