const SORTS = new Set(["updated_desc", "updated_asc", "title_asc"]);

function normalizedControls(controls = {}) {
  return {
    query: String(controls.query || "").trim().toLocaleLowerCase("zh-CN"),
    route: String(controls.route || ""),
    status: String(controls.status || ""),
    sort: SORTS.has(controls.sort) ? controls.sort : "updated_desc",
  };
}

function compareTasks(sort) {
  if (sort === "updated_asc") {
    return (left, right) => String(left.updatedAt || "").localeCompare(String(right.updatedAt || ""));
  }
  if (sort === "title_asc") {
    return (left, right) => String(left.title || "").localeCompare(String(right.title || ""), "zh-CN");
  }
  return (left, right) => String(right.updatedAt || "").localeCompare(String(left.updatedAt || ""));
}

export function filterAndSortTasks(tasks, controls = {}) {
  const normalized = normalizedControls(controls);
  return (Array.isArray(tasks) ? tasks : [])
    .filter((task) => {
      const text = `${task?.title || ""} ${task?.taskId || ""}`.toLocaleLowerCase("zh-CN");
      return (!normalized.query || text.includes(normalized.query))
        && (!normalized.route || task?.route === normalized.route)
        && (!normalized.status || task?.runStatus === normalized.status);
    })
    .slice()
    .sort(compareTasks(normalized.sort));
}

export function buildTaskSearchResult(tasks, controls = {}) {
  const visible = filterAndSortTasks(tasks, controls);
  return {
    tasks: visible,
    emptyMessage: visible.length
      ? ""
      : "没有符合条件的任务，请换个关键词或清除筛选。",
  };
}
