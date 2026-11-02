/**
 * 实体信息流广告 · P2 逐句分镜规划器（第三条内容线，与 plan-brief 并存）。
 * 与 plan-brief 的根本区别：没有数字人底层，画面=素材墙到墙逐句切换，所以输出是**覆盖全文的 segments**，
 * 每段一句字幕 + 一段全屏画面；不产 keywords/punchLines/标题卡（广告字幕白字、封面另做）。
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
import OpenAI from "openai";
import { z } from "zod";
import {
  JSON_REPAIR_SYSTEM_PROMPT,
  parseStructuredOutputWithSingleRepair,
} from "./model-json-output";

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
    })
  ),
});
type AdPlan = z.infer<typeof AdPlanSchema>;

// ratio → 构图措辞，注入 prompt 规则（横竖版素材朝向不同）
const FRAMING: Record<string, string> = { "9:16": "竖屏 9:16", "16:9": "横屏 16:9" };

function buildSys(ratio: string): string {
  const framing = FRAMING[ratio] || FRAMING["9:16"];
  return `你是实体信息流广告的分镜师。给你一段广告口播文案，你要逐句拆成分镜，每句字幕配一段全屏画面（满屏素材逐句切换），供自动化流水线 TTS 配音 → 匹配/生成素材 → 拼接转场出片。

只输出一个 JSON 对象，不要任何额外文字或 markdown 代码块，结构严格如下：
{
  "segments": [
    {
      "text": string,   // 一句字幕，必须是文案中逐字出现的【连续原文子串】，按文案顺序、无遗漏无重叠地覆盖全文
      "query": string,  // 该句配的素材搜索词，2-6字名词，给图库检索用
      "prompt": string, // 给【Seedance 文生视频】的电影级画面描述，见下方要求
      "type": "video" | "image"  // video=动态空镜（默认优先）；image=画面本身无需运动时用静态图
    }
  ]
}

【拆句要求】
- 把整篇文案按顺序切成短句，每段 text 是原文的连续子串，逐字一致，不改写/不补字/不转繁体/不加引号。
- 全文必须被覆盖，前后相接、不跳句不漏句；标点可作为切分点，长句按语义切成多段。
- 优先按完整语义短语和自然停顿拆分，约 8~20 字；字幕可以显示两行，不得为了凑字数腰斩词组。
- 数字与单位、范围（如 11到12米、3到6个月）、项目专名、可租可售等固定短语必须保持完整。
- 不要把“的、和、与、可、能、还是”等连接成分孤立在句尾；不要生成仅标点或仅一两个字的分镜。句末标点仍按原文保留，显示时由字幕排版器隐藏，不能从文案或配音输入中删除。
- 一句一画面：每段都要有一个能落地的全屏视觉，不要给同一画面硬塞两句。

【query / prompt 写法要求（直接喂给 Seedance，决定画面质量，务必认真写）】
- 结构公式：主体 + 主体动态 + 场景环境 + 镜头语言 + 光影氛围 + 画面质感。一句话 30~60 字，聚焦一个画面。
- 必须有「运动」：① 主体动态（有在动的东西，纯静态会很呆）；② 镜头运动（推近/拉远/横移/上升/环绕/跟随 + 景别），5 秒一镜、慢而连续。
- 单镜头一镜到底，不要分镜切换/转场/「接着然后」——一个 prompt 只拍一个连贯画面、一个主体。
- 优先选能拍成具体实物的画面；抽象词「意象化」成具体空镜（如物流→货车港口、办公→写字楼会议、热闹→人群street）。
- 屏幕/手机界面/招牌/电子屏 等天然带文字的主体，优先换成无文字意象，避免生成乱码文字。
- 遇到具体专名/品牌/门店名，写泛化的代表性场景，不要还原真实品牌/招牌。
- ${framing} 构图，写实/纪实风格，光影写具体。画面里可以有人但只拍群像/远景/背影/手部，严禁可识别人脸特写；严禁画面内出现文字、字幕、logo、水印、明显畸变，也不要在 prompt 里写「文字/招牌/数字隐约可见」。

铁律：每段 segments[].text 必须是文案中逐字出现的连续原文子串，不得改写、补字、转繁体。query 和 prompt 不受此限，按上面要求自由撰写。`;
}

interface PlanAdArgs {
  name: string;
  ratio: string;
  explicitScript?: string;
  explicitOutput?: string;
}

function parseArgs(argv: string[]): PlanAdArgs {
  const explicit = argv.some((value) => value.startsWith("--"));
  if (!explicit) {
    const [name, ratio = "9:16", ...extra] = argv;
    if (!name || extra.length) throw new Error("用法: ts-node scripts/plan-ad.ts <name> [ratio=9:16|16:9]");
    return { name, ratio };
  }
  const allowed = new Set(["--script", "--ratio", "--output"]);
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
  const scriptText = fs.readFileSync(scriptPath, "utf8").replace(/\r/g, "").trim();

  const client = new OpenAI({ apiKey: API_KEY, baseURL: BASE_URL });
  console.log(`▶ 广告分镜 ${name}（ratio=${ratio} | ${MODEL} @ ${BASE_URL}）...`);
  const resp = await client.chat.completions.create({
    model: MODEL,
    temperature: 0.3,
    messages: [
      { role: "system", content: buildSys(ratio) },
      { role: "user", content: `广告文案如下（请按上述 JSON 逐句拆分输出）：\n\n${scriptText}` },
    ],
  });

  const raw = resp.choices?.[0]?.message?.content || "";
  let plan: AdPlan;
  try {
    plan = await parseStructuredOutputWithSingleRepair(
      raw,
      (value) => AdPlanSchema.parse(value),
      async (invalidOutput) => {
        console.warn("  ⚠ 首次输出不是有效 JSON，正在用同一规划模型执行一次格式修复...");
        const repairResp = await client.chat.completions.create({
          model: MODEL,
          temperature: 0,
          messages: [
            { role: "system", content: JSON_REPAIR_SYSTEM_PROMPT },
            { role: "user", content: invalidOutput.slice(0, 50_000) },
          ],
        });
        return repairResp.choices?.[0]?.message?.content || "";
      },
    );
  } catch (e) {
    console.error("✘ 模型输出无法解析为预期结构:", (e as Error).message);
    console.error("原始输出片段:", raw.slice(0, 400));
    process.exit(1);
  }

  const compact = (value: string) => value.replace(/\s/g, "");
  const covered = plan.segments.map((segment) => segment.text).join("");
  if (compact(covered) !== compact(scriptText)) {
    console.error("AD_COPY_COVERAGE_INVALID: 分镜文案没有逐字、按序、无遗漏地覆盖锁定原文");
    process.exit(1);
  }
  const outPath = explicitOutput
    ? path.resolve(explicitOutput)
    : path.join(SCRIPTS, name + ".adplan.json");
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify({ ratio, segments: plan.segments }, null, 2), "utf8");

  console.log("✔ adplan: " + plan.segments.length + " 段 | 覆盖 100%");
  console.log("  → " + outPath);
}

main().catch((e) => {
  console.error("✘ 广告分镜失败:", e?.message || e);
  process.exit(1);
});
