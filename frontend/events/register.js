export function createEventRegistry(documentRef = document) {
  const cleanup = [];

  function on(target, eventName, listener, options) {
    target.addEventListener(eventName, listener, options);
    cleanup.push(() => target.removeEventListener(eventName, listener, options));
  }

  function onAll(targets, eventName, listener) {
    for (const target of targets) on(target, eventName, listener);
  }

  function onId(id, eventName, listener, options) {
    const target = documentRef.getElementById(id);
    if (!target) throw new Error(`Missing event target #${id}`);
    on(target, eventName, listener, options);
  }

  function onSelectorAll(selector, eventName, listener, options) {
    for (const target of documentRef.querySelectorAll(selector)) on(target, eventName, listener, options);
  }

  function dispose() {
    while (cleanup.length) cleanup.pop()();
  }

  return {on, onAll, onId, onSelectorAll, dispose};
}
