export const byId = (documentRef, id) => documentRef.getElementById(id);

export function renderChips(target, values, escape) {
  target.classList.remove("empty");
  target.innerHTML = values.map(([label, value]) => `<span class="chip">${escape(label)}: <b>${escape(value)}</b></span>`).join("");
}

export function updatePager(documentRef, previousId, labelId, nextId, page, pages, total, noun = "items") {
  byId(documentRef, previousId).disabled = page <= 1;
  byId(documentRef, nextId).disabled = page >= pages;
  byId(documentRef, labelId).textContent = `Page ${page} of ${pages} · ${total} ${noun}`;
}
