export const SLOT_DEFINITIONS = Object.freeze([
  { key: "subtitle", label: "字幕", required: true },
  { key: "voice", label: "音色", required: false },
  { key: "bgm", label: "BGM", required: false },
  { key: "sfx", label: "音效", required: false },
  { key: "title_card", label: "文字卡", required: false },
  { key: "sticker", label: "贴纸", required: false },
  { key: "fancy_text", label: "花字", required: false },
  { key: "brand", label: "Logo / 水印", required: false },
]);

function categoryMap(catalog) {
  return new Map((catalog?.categories || []).map((category) => [category.key, category]));
}

export function canConfirmPackaging(current) {
  const preview = current?.preview;
  return Boolean(
    preview?.status === "ready"
      && preview.viewedAt
      && preview.planDigest === current?.planDigest
      && preview.pictureLockDigest === current?.pictureLockDigest
  );
}

export function buildPackagingPreviewModel(current = {}) {
  const preview = current.preview || null;
  const previewId = preview?.previewId || preview?.preview_id || null;
  const taskId = current.taskId || current.task_id || null;
  return Object.freeze({
    status: preview?.status || "not_requested",
    previewId,
    mediaUrl: previewId && taskId
      ? `/api/tasks/${encodeURIComponent(taskId)}/packaging-previews/${encodeURIComponent(previewId)}/media`
      : null,
    coverage: Object.freeze([...(preview?.coverage || [])]),
    canConfirm: canConfirmPackaging(current),
    canRequest: Boolean(current.planDigest && current.pictureLockDigest),
    disclaimer: "包装预览片段，不是最终成片",
  });
}

export function slotRows(items = [], catalog = {}) {
  const primaries = new Map(
    items
      .filter((item) => !item.is_backup && !item.isBackup)
      .map((item) => [item.slot, item]),
  );
  const categories = categoryMap(catalog);
  return SLOT_DEFINITIONS.map((definition) => {
    const item = primaries.get(definition.key) || null;
    const assetRef = item?.asset_ref || item?.assetRef || null;
    const assets = categories.get(definition.key)?.assets || [];
    const asset = assets.find((candidate) => (
      candidate.assetRef === assetRef || candidate.asset_ref === assetRef || candidate.id === assetRef
    )) || null;
    return Object.freeze({
      ...definition,
      selected: asset ? Object.freeze({ ...asset }) : null,
      assetRef,
      enabled: Boolean(item),
      canDisable: !definition.required,
      options: Object.freeze([...assets]),
    });
  });
}
