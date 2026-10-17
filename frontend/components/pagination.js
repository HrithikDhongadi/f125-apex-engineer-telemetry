export function pageData(items, page, size = 10) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(Math.max(1, page), pages);
  const start = (current - 1) * size;
  return {current, pages, start, items: items.slice(start, start + size)};
}
