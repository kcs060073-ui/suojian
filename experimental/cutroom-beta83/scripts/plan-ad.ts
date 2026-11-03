/**
 * 实体信息流广告 · P2 逐句分镜规划器（第三条内容线，与 plan-brief 并存）。
 * 与 plan-brief 的根本区别：没有数字人底层，画面=素材墙到墙逐句切换，所以输出是**覆盖全文的 segments**，
 * 每段一句字幕 + 一段全屏画面；每句可标注一个原文重点词供包装字幕使用。
 *
 * 用法: ts-node scripts/plan-ad.ts <name> [ratio=9:16|16:9]
 *   读 public/scripts/<name>.script.txt → 产 public/scripts/<name>.adplan.json
 *   下游：P1 tts 按 segments[].text 逐句配音得时间表 → P3 按 query 配/生素材 → P4 拼接转场出片。
 *
 * 环境变量同 plan-brief（ARK_API_KEY / ARK_MODEL / ARK_BASE_URL，见 .env）。
 */
import "./load-env";
import * as fs from "fs";
import * as path from "path";
import { z } from "zod";
import { buildStoryboardUnits, validateStoryboardPhrases, materializeScriptGroups, type ScriptUnit } from "./script-segment-groups";
import { packagingSfxCandidates, packagingSfxPrompt,
  type PackagingSfxCandidate } from "./packaging-sfx-catalog";
import { requestStructuredPlanning } from "./planning-request-policy";

for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]) delete process.env[k];

const ROOT = path.resolve(__dirname, "..");
const SCRIPTS = path.join(ROOT, "public", "scripts");

const API_KEY = process.env.ARK_API_KEY || process.env.LLM_API_KEY || process.env.OPENAI_API_KEY;
const BASE_URL = process.env.ARK_BASE_URL || process.env.LLM_BASE_URL || "https://ark.cn-beijing.volces.com/api/v3";
const MODEL = process.env.ARK_MODEL || process.env.LLM_MODEL || "";

const AdPlanSchema = z.object({
  segments: z.array(
    z.object({
      text: z.string(),
      query: z.string(),
      prompt: z.string(),
      type: z.enum(["video", "image"]),
      highlight: z.string().optional(),
    })
  ),
  packaging: z.object({
    title: z.object({ text: z.string(), anchorText: z.string() }).optional(),
    fancyText: z.object({ text: z.string(), anchorText: z.string() }).optional(),
    sfx: z.object({ assetRef: z.string(), event: z.literal("title_card") }).optional(),
  }).optional(),
});
type AdPlan = z.infer<typeof AdPlanSchema>;

const GroupedAdPlanSchema = AdPlanSchema.omit({ segments: true }).extend({
  segments: z.array(AdPlanSchema.shape.segments.element.omit({ text: true }).extend({
    sourceUnitIds: z.array(z.number().int().positive()).min(1),
  }).strict()).min(1),
});

export function parseGroupedModelAdPlan(value: unknown, units: ScriptUnit[], fixed = false): AdPlan {
  const { segments, ...plan } = GroupedAdPlanSchema.parse(value);
  if (fixed && (segments.length !== units.length || segments.some((s, i) =>
    s.sourceUnitIds.length !== 1 || s.sourceUnitIds[0] !== i + 1))) {
    throw new Error("VOICE_PLAN_TIMING_MISMATCH: 已确认配音的分句不可合并或拆分");
  }
  const texts = materializeScriptGroups(units, segments.map((segment) => segment.sourceUnitIds));
  const parsed = AdPlanSchema.parse({ ...plan, segments: segments.map(({ sourceUnitIds, ...segment }, index) =>
    ({ ...segment, text: texts[index] })) });
  if (!fixed) validateStoryboardPhrases(parsed.segments);
  parsed.segments = normalizeAdHighlights(parsed.segments);
  return parsed;
}

// Highlight is optional visual packaging, never pronunciation or source copy.
export function normalizeAdHighlights<T extends { text: string; highlight?: string }>(segments: T[]): T[] {
  return segments.map(segment => {
    const highlight = segment.highlight?.trim() || "";
    return { ...segment, highlight: highlight && Array.from(highlight).length <= 8
      && segment.text.includes(highlight) ? highlight : "" };
  });
}

export function validateAdCopy<T extends { text: string }>(script: string, segments: readonly T[]): T[] {
  const copied = segments.map((segment) => ({ ...segment }));
  const joined = copied.map((segment) => segment.text).join("");
  const suffix = script.slice(joined.length);
  const invalidSegment = !copied.length || copied.some((segment) => !segment.text.trim());
  if (!invalidSegment && script.startsWith(joined) && /^\s+$/.test(suffix)) {
    copied[copied.length - 1].text += suffix;
  }
  if (invalidSegment || copied.map((segment) => segment.text).join("") !== script) {
    throw new Error("AD_COPY_COVERAGE_INVALID: 分镜文案没有逐字、按序、无遗漏地覆盖锁定原文");
  }
  return copied;
}

// ratio → 构图措辞，注入 prompt 规则（横竖版素材朝向不同）
const FRAMING: Record<string, string> = { "9:16": "竖屏 9:16", "16:9": "横屏 16:9" };

function buildSys(ratio: string, sfxCandidates: PackagingSfxCandidate[]): string {
  const framing = FRAMING[ratio] || FRAMING["9:16"];
  return `你是实体信息流广告的分镜师。给你一段广告口播文案，你要逐句拆成分镜，每句字幕配一段全屏画面（满屏素材逐句切换），供自动化流水线 TTS 配音 → 匹配/生成素材 → 拼接转场出片。

只输出一个 JSON 对象，不要任何额外文字或 markdown 代码块，结构严格如下：
{
  "segments": [
    {
      "sourceUnitIds": number[], // 本镜头对应的连续原文单元编号；字幕由程序按编号还原，不输出 text
      "query": string,  // 该句配的素材搜索词，2-6字名词，给图库检索用
      "prompt": string, // 给【Seedance 文生视频】的电影级画面描述，见下方要求
      "type": "video" | "image"  // video=动态空镜（默认优先）；image=画面本身无需运动时用静态图
      "highlight": string  // 字幕要突出显示的 2~6 字原文连续子串；确无重点词则为空字符串
    }
  ],
  "packaging": {
    "title": {"text": string, "anchorText": string},
    "fancyText": {"text": string, "anchorText": string},
    "sfx": {"assetRef": string, "event": "title_card"}
  }
}

【拆句要求】
- 输入是按原文顺序编号的 sourceUnits；每段仅返回 sourceUnitIds，不要输出 text 或复制原文。
- 所有 sourceUnitIds 展平后必须恰好等于输入的全部编号，按序、无遗漏、无重复；每组至少一个连续编号，不得拆开单元或自造编号。
- 原文单元已经按分句边界拆开。按语义组织便于选材的短句，通常8至25字，最多40字；不要把多项卖点重新合成一个长段。只有表达同一件事的相邻短单元才可合并，不能按全文只分开头与结尾。
- 每个短句分别给出具体画面需求，文案段与素材文件不是一一对应；原素材只取适合的区间，不要求整条使用。
- 每段都要有一个能落地的全屏视觉；字幕长度由下游按完整原文拆短句处理。
- highlight 必须逐字取自本组原文，优先数据、地点或业务关键名词；不要新造词或选标点。

【包装语义建议】
- title.text 是不超过 40 字的广告主题标题；title.anchorText 必须是某一段 segments[].text 中逐字出现、全篇只出现一次的连续原文，指定标题出现的句子。
- fancyText.text 是该句值得强调的不超过 40 字原文连续子串；fancyText.anchorText 同样指定该句，必须全篇唯一。
- 只能依据文案判断语义，不要编造配音时间、镜头位置或画面内容；无法找到明确原文锚点时省略相应字段。
- sfx 可省略；仅在已选标题卡的入场确实需要音效时，从下面已登记音效中选 assetRef。它跟随标题事件，由程序按真实配音、镜头和源文件长度定时间，不要编造秒数。

${packagingSfxPrompt(sfxCandidates)}

【query / prompt 写法要求（直接喂给 Seedance，决定画面质量，务必认真写）】
- 结构公式：主体 + 主体动态 + 场景环境 + 镜头语言 + 光影氛围 + 画面质感。一句话 30~60 字，聚焦一个画面。
- 必须有「运动」：① 主体动态（有在动的东西，纯静态会很呆）；② 镜头运动（推近/拉远/横移/上升/环绕/跟随 + 景别），5 秒一镜、慢而连续。
- 单镜头一镜到底，不要分镜切换/转场/「接着然后」——一个 prompt 只拍一个连贯画面、一个主体。
- 优先选能拍成具体实物的画面；抽象词「意象化」成具体空镜（如物流→货车港口、办公→写字楼会议、热闹→人群street）。
- 屏幕/手机界面/招牌/电子屏 等天然带文字的主体，优先换成无文字意象，避免生成乱码文字。
- 遇到具体专名/品牌/门店名，写泛化的代表性场景，不要还原真实品牌/招牌。
- ${framing} 构图，写实/纪实风格，光影写具体。画面里可以有人但只拍群像/远景/背影/手部，严禁可识别人脸特写；严禁画面内出现文字、字幕、logo、水印、明显畸变，也不要在 prompt 里写「文字/招牌/数字隐约可见」。

铁律：只用 sourceUnitIds 指定原文，全部编号必须按序、无遗漏、无重复地覆盖；不得输出或改写字幕。query 和 prompt 按上面要求自由撰写。`;
}

interface PlanAdArgs {
  name: string;
  ratio: string;
  explicitScript?: string;
  explicitOutput?: string;
  fixedSegments?: string;
}

function parseArgs(argv: string[]): PlanAdArgs {
  const explicit = argv.some((value) => value.startsWith("--"));
  if (!explicit) {
    const [name, ratio = "9:16", ...extra] = argv;
    if (!name || extra.length) throw new Error("用法: ts-node scripts/plan-ad.ts <name> [ratio=9:16|16:9]");
    return { name, ratio };
  }
  const allowed = new Set(["--script", "--ratio", "--output", "--fixed-segments"]);
  const values = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith("--") || !value || value.startsWith("--")) {
      throw new Error("显式调用必须提供 --script、--ratio 和 --output");
    }
    if (!allowed.has(flag)) throw new Error(`未知参数：${flag}`);
    values.set(flag, value);
  }
  const explicitScript = values.get("--script");
  const ratio = values.get("--ratio");
  const explicitOutput = values.get("--output");
  if (!explicitScript || !ratio || !explicitOutput) {
    throw new Error("显式调用必须提供 --script、--ratio 和 --output");
  }
  return {
    name: path.basename(explicitScript).replace(/\.script\.txt$/i, "") || "ad-task",
    ratio,
    explicitScript,
    explicitOutput,
    fixedSegments: values.get("--fixed-segments"),
  };
}

async function main() {
  let args: PlanAdArgs;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error((error as Error).message);
    process.exit(1);
    return;
  }
  const { name, ratio, explicitScript, explicitOutput } = args;
  if (!FRAMING[ratio]) {
    console.error(`未知 ratio「${ratio}」，可选: 9:16 | 16:9`);
    process.exit(1);
  }
  if (!API_KEY) {
    console.error("缺 ARK_API_KEY（或 LLM_API_KEY）。见 .env。");
    process.exit(1);
  }
  if (!MODEL) {
    console.error("缺 ARK_MODEL（或 LLM_MODEL）。见 .env。");
    process.exit(1);
  }
  const scriptPath = explicitScript ? path.resolve(explicitScript) : path.join(SCRIPTS, name + ".script.txt");
  if (!fs.existsSync(scriptPath)) {
    console.error(`缺文案: ${scriptPath}`);
    process.exit(1);
  }
  const scriptText = fs.readFileSync(scriptPath, "utf8").replace(/\r\n?/g, "\n");
  const fixed: unknown = args.fixedSegments ? JSON.parse(fs.readFileSync(path.resolve(args.fixedSegments), "utf8")) : null;
  if (args.fixedSegments && (!Array.isArray(fixed) || !fixed.length || fixed.some(t => typeof t !== "string" || !t.trim())
    || fixed.join("") !== scriptText)) throw new Error("VOICE_PLAN_TIMING_MISMATCH: 实测分句与原文不符");
  const sourceUnits: ScriptUnit[] = Array.isArray(fixed) ? fixed.map((text, i) => ({id:i+1,text})) : buildStoryboardUnits(scriptText);

  const sfxCandidates = packagingSfxCandidates(ROOT, "信息流广告");
  console.log(`▶ 广告分镜 ${name}（ratio=${ratio} | ${MODEL} @ ${BASE_URL}）...`);
  let plan: AdPlan;
  try {
    plan = await requestStructuredPlanning({ apiKey: API_KEY, baseURL: BASE_URL,
      model: MODEL, messages: [
        { role: "system", content: buildSys(ratio, sfxCandidates) + (args.fixedSegments ? "\n【已确认声音】每个 sourceUnitIds 必须只有一个编号，逐项对应已实测配音分句；禁止合并或拆分。该模式不适用上述8至25字或40字上限，不得为了短句修改已实测边界。" : "") },
        { role: "user", content: `广告原文单元如下（请按上述 JSON 返回分组编号）：\n\n${JSON.stringify({ sourceUnits })}` },
      ] }, (value) => parseGroupedModelAdPlan(value, sourceUnits, Boolean(args.fixedSegments)));
  } catch (e) {
    console.error("✘ 规划请求失败或输出无效（未自动重试或付费修复）:", (e as Error).message);
    process.exitCode = 1;
    return;
  }

  plan.segments = validateAdCopy(scriptText, plan.segments);
  if (!args.fixedSegments) validateStoryboardPhrases(plan.segments);
  plan.segments = normalizeAdHighlights(plan.segments);
  // Packaging is optional evidence. An invalid model suggestion never invalidates
  // a fully covered storyboard and never turns into a guessed timeline instance.
  const packaging = plan.packaging;
  if (packaging) {
    for (const key of ["title", "fancyText"] as const) {
      const item = packaging[key];
      if (!item) continue;
      const text = item.text.trim();
      const anchor = item.anchorText.trim();
      const matching = plan.segments.filter((part) => part.text.includes(anchor));
      if (!text || Array.from(text).length > 40 || !anchor
        || scriptText.split(anchor).length !== 2 || matching.length !== 1
        || (key === "fancyText" && !matching[0].text.includes(text))) {
        delete packaging[key];
      } else {
        item.text = text;
        item.anchorText = anchor;
      }
    }
    if (packaging.sfx && (!packaging.title
      || !sfxCandidates.some((candidate) => candidate.id === packaging.sfx?.assetRef))) {
      delete packaging.sfx;
    }
  }
  const outPath = explicitOutput
    ? path.resolve(explicitOutput)
    : path.join(SCRIPTS, name + ".adplan.json");
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify({ ratio, segments: plan.segments,
    ...(packaging ? { packaging } : {}) }, null, 2), "utf8");

  console.log("✔ adplan: " + plan.segments.length + " 段 | 覆盖 100%");
  console.log("  → " + outPath);
}

if (require.main === module) {
  main().catch((e) => {
    console.error("✘ 广告分镜失败:", e?.message || e);
    process.exitCode = 1;
  });
}
