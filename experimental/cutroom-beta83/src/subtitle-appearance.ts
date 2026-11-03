/** Shared ASS transformation: applied only to subtitle styles, never card layers. */
export type SubtitleAppearance = {
  fontSize?: number; fontFamily?: string; fontFile?: string; primaryHex?: string; highlightHex?: string;
  outlineHex?: string; outlineWidth?: number; backgroundHex?: string;
  backgroundOpacity?: number; x?: number; y?: number; width?: number;
};
const color = (hex: string, alpha = 0) => {
  if (!/^#[0-9a-f]{6}$/i.test(hex)) throw new Error("SUBTITLE_APPEARANCE_INVALID: color");
  return `&H${Math.round(alpha).toString(16).padStart(2,"0")}${hex.slice(5,7)}${hex.slice(3,5)}${hex.slice(1,3)}&`.toUpperCase();
};
export function applySubtitleAppearance(ass: string, value: SubtitleAppearance | undefined, w: number, h: number): string {
  if (!value || Object.keys(value).length === 0) return ass;
  // ASS positions and margins use script resolution, independently of video output size.
  w = Number(/^PlayResX:\s*(\d+)/mi.exec(ass)?.[1]) || w;
  h = Number(/^PlayResY:\s*(\d+)/mi.exec(ass)?.[1]) || h;
  const original: Record<string,string> = {};
  const backgrounds = new Set<string>();
  const lines = ass.split(/\r?\n/).flatMap(line => {
    if (!line.startsWith("Style: ")) return [line];
    const fields = line.slice(7).split(",");
    if (!["Default", "Punch"].includes(fields[0])) return [line];
    original[fields[0]] = fields[3];
    if (value.fontFamily) {
      if (/[\r\n,{}\\]/.test(value.fontFamily)) throw new Error("SUBTITLE_APPEARANCE_INVALID: font");
      fields[1] = value.fontFamily;
    }
    if (value.fontSize !== undefined) fields[2] = String(value.fontSize);
    if (value.primaryHex) fields[3] = fields[4] = color(value.primaryHex);
    if (value.outlineHex) fields[5] = color(value.outlineHex);
    if (value.outlineWidth !== undefined) fields[16] = String(value.outlineWidth);
    if (value.backgroundOpacity !== undefined) { fields[15] = "1"; fields[17] = "0"; }
    if (value.x !== undefined || value.y !== undefined) fields[18] = "2";
    if (value.width !== undefined) {
      const center = value.x ?? .5;
      fields[19] = String(Math.round(w * Math.max(.02,center-value.width/2)));
      fields[20] = String(Math.round(w * Math.max(.02,1-center-value.width/2)));
    }
    const result = [`Style: ${fields.join(",")}`];
    if ((value.backgroundOpacity ?? 0) > 0) {
      const bg = [...fields]; bg[0] += "AppearanceBox"; bg[15] = "3"; bg[16] = "8"; bg[17] = "0";
      bg[3] = bg[4] = color("#000000",255);
      bg[5] = bg[6] = color(value.backgroundHex ?? "#000000", (1-value.backgroundOpacity!)*255);
      backgrounds.add(fields[0]); result.push(`Style: ${bg.join(",")}`);
    }
    return result;
  });
  return lines.flatMap(line => {
    if (!line.startsWith("Dialogue: ")) return [line];
    const match = /^(Dialogue: \d+,[^,]+,[^,]+,)([^,]+)(,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,)(.*)$/.exec(line);
    if (!match || !Object.hasOwn(original,match[2])) return [line];
    let text = match[4];
    const originalPrimary = original[match[2]].replace(/&$/,'').toUpperCase();
    text = text.replace(/\\(?:1?c)(&H[0-9A-F]+&?)/gi, (_all, old) => {
      const target = old.replace(/&$/,'').toUpperCase() === originalPrimary ? value.primaryHex : value.highlightHex;
      return `\\c${target ? color(target) : old}`;
    });
    const position = value.x !== undefined || value.y !== undefined
      ? `{\\an2\\pos(${Math.round((value.x ?? .5)*w)},${Math.round((value.y ?? .9)*h)})}` : '';
    const foreground = `${match[1]}${match[2]}${match[3]}${position}${text}`;
    if (!backgrounds.has(match[2])) return [foreground];
    return [`${match[1]}${match[2]}AppearanceBox${match[3]}${position}{\\1a&HFF&}${text}`, foreground];
  }).join("\n");
}
