import * as fs from "fs";
import * as path from "path";

export interface PackagingSfxCandidate {
  id: string;
  name: string;
  kind: string;
}

/** Only offer registered, licensed, locally present sounds to the planning model. */
export function packagingSfxCandidates(root: string, route: string): PackagingSfxCandidate[] {
  let manifest: unknown;
  try {
    manifest = JSON.parse(fs.readFileSync(
      path.join(root, "assets", "manifests", "sfx.json"), "utf8"));
  } catch {
    return [];
  }
  if (!manifest || typeof manifest !== "object" || !Array.isArray((manifest as { items?: unknown }).items)) {
    return [];
  }
  const allowedRoots = ["assets", "asset-packs"].map((part) => path.resolve(root, part));
  const candidates: PackagingSfxCandidate[] = [];
  for (const value of (manifest as { items: unknown[] }).items) {
    if (!value || typeof value !== "object") continue;
    const item = value as Record<string, unknown>;
    if (typeof item.id !== "string" || !item.id.trim()
      || item.license !== "commercial-approved"
      || typeof item.file !== "string" || !item.file.trim()) continue;
    if (Array.isArray(item.default_for) && item.default_for.length
      && !item.default_for.includes(route)) continue;
    const source = path.resolve(root, item.file);
    if (!allowedRoots.some((base) => source.startsWith(base + path.sep))) continue;
    try {
      if (!fs.statSync(source).isFile()) continue;
    } catch {
      continue;
    }
    candidates.push({ id: item.id, name: String(item.name || item.id),
      kind: String(item.effect_kind || item.kind || "sfx") });
  }
  return candidates;
}

export function packagingSfxPrompt(candidates: PackagingSfxCandidate[]): string {
  if (!candidates.length) return "当前没有可用音效；省略 packaging.sfx。";
  return `可选本地音效（只能填写所列 assetRef，不要编造文件）：\n${candidates
    .map((item) => `- ${item.id}：${item.name}（${item.kind}）`).join("\n")}`;
}
