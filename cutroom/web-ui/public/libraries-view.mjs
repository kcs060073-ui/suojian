export const PACKAGING_CATEGORIES = Object.freeze([
  { key: "subtitle", label: "字幕", group: "subtitle" },
  { key: "voice", label: "音色", group: "voice" },
  { key: "bgm", label: "BGM", group: "audio" },
  { key: "sfx", label: "音效", group: "audio" },
  { key: "title_card", label: "文字卡", group: "visual" },
  { key: "sticker", label: "贴纸", group: "visual" },
  { key: "fancy_text", label: "花字", group: "visual" },
  { key: "brand", label: "Logo / 水印", group: "brand" },
]);

export const PACKAGING_GROUPS = Object.freeze([
  { key: "all", label: "全部包装素材" },
  { key: "subtitle", label: "字幕与字体" },
  { key: "voice", label: "配音与音色" },
  { key: "audio", label: "BGM 与音效" },
  { key: "visual", label: "视觉包装" },
  { key: "brand", label: "Logo 与水印" },
]);

export function filterPackagingCategories(categories, group) {
  if (!group || group === "all") return [...(categories || [])];
  return (categories || []).filter((item) => item.group === group);
}

export function packagingAvailabilityLabel(status) {
  if (status === "available") return "文件可用";
  if (status === "external") return "由服务配置提供";
  return "文件缺失";
}

export function groupShotLibraries(libraries) {
  const shots = (libraries || []).filter((item) => item.purpose !== "packaging");
  return {
    primary: shots.filter((item) => item.purpose === "task_specific"),
    generic: shots.filter((item) => item.purpose === "generic"),
    packaging: [],
  };
}

export function buildLibraryRegistration(values) {
  return {
    name: String(values?.name || "").trim(),
    mountPath: String(values?.mountPath || "").trim(),
    purpose: String(values?.purpose || "task_specific"),
    kind: "local",
    isDefaultGeneric:
      values?.purpose === "generic" && Boolean(values?.isDefaultGeneric),
  };
}

export function authorizationLabel(status) {
  return status === "approved" ? "商用授权已确认" : "授权待核验";
}
export { buildCatalogCheckModel } from "./packaging-catalog.mjs";
