/**
 * 实体信息流广告 · 字幕样式（来自用户剪映工程参数）。
 * ⚠ 这些是剪映坐标/字号，P4 渲染时需换算为 ASS：
 *   - fontSize（剪映“号”）→ ASS Fontsize（按画布分辨率换算，渲染时定标）
 *   - posX/posY（剪映以画布中心为原点）→ ASS Alignment + MarginL/MarginV
 *   - shadowOpacity → ASS BackColour 的 alpha（&HAABBGGRR，AA=不透明度反码）+ BorderStyle/Shadow
 * 字体（得意黑 / 优设标题黑 / 特黑体）需放 assets/fonts/ 供 ffmpeg 烧录，否则回退系统字体。
 */
export interface AdSubtitleStyle {
  fontFamily: string[]; // 首选→回退
  fontSize: number; // 剪映“号”
  color: string; // 字色
  posX: number; // 剪映 X（画布中心=0）
  posY: number; // 剪映 Y（画布中心=0）
  shadowColor: string;
  shadowOpacity: number; // 0~1
}

// h=横版 16:9，v=竖版 9:16
export const AD_SUBTITLE: Record<"h" | "v", AdSubtitleStyle> = {
  h: { fontFamily: ["Alimama ShuHeiTi Bold", "阿里妈妈数黑"], fontSize: 8, color: "#FFFFFF", posX: 0, posY: -900, shadowColor: "#000000", shadowOpacity: 0.9 },
  v: { fontFamily: ["Alimama ShuHeiTi Bold", "阿里妈妈数黑"], fontSize: 12, color: "#FFFFFF", posX: 0, posY: -800, shadowColor: "#000000", shadowOpacity: 0.9 },
};
