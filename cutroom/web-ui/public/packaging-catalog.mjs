const STATUS_ORDER = Object.freeze({
  invalid: 0,
  missing: 1,
  updated: 2,
  added: 3,
  unchanged: 4,
});

export function buildCatalogCheckModel(value = {}) {
  const counts = {
    invalid: Number(value.counts?.invalid || 0),
    missing: Number(value.counts?.missing || 0),
    updated: Number(value.counts?.updated || 0),
    added: Number(value.counts?.added || 0),
    unchanged: Number(value.counts?.unchanged || 0),
  };
  const items = [...(value.items || [])]
    .map((item) => Object.freeze({ ...item }))
    .sort((left, right) => (
      (STATUS_ORDER[left.status] ?? 99) - (STATUS_ORDER[right.status] ?? 99)
      || String(left.relativePath || "").localeCompare(String(right.relativePath || ""))
    ));
  return Object.freeze({
    status: value.status || "not_checked",
    checkId: value.checkId || value.check_id || null,
    counts: Object.freeze(counts),
    items: Object.freeze(items),
    hasProblems: counts.invalid + counts.missing + counts.updated > 0,
  });
}
