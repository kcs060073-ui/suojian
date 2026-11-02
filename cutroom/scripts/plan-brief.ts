/**
 * P2 AI 规划器：读文案，调大模型（OpenAI 兼容接口，默认火山方舟 Ark）自动产出剪辑 brief。
 *
 * 输出两份：
 *   public/scripts/<name>.brief.json  —— AI 完整规划（keywords / punchLines / title / broll 建议）
 *   public/scripts/<name>.plan.json   —— 可立即渲染的子集（speed + 开场标题卡；B-roll 待 P3 配素材）
 *
 * 下游：make-ass.ts 读 brief 的 keywords/punchLines；compile-plan/render-mix 走 plan；
 *       brief.broll[] 是 P3「素材匹配」的工作队列（每条带 query 搜索词 + anchorText 锚点）。
 *
 * 用法: ts-node scripts/plan-brief.ts <name> [speed=1.25] [profile=zhaoshang|caishui]
 *   profile 切换内容画像（招商 / 财税），第 3/4 参数任意顺序，或环境变量 PLAN_PROFILE。
 *
 * 环境变量（火山方舟 / 任意 OpenAI 兼容）：
 *   ARK_API_KEY   （或 LLM_API_KEY / OPENAI_API_KEY）  必填，你的 key
 *   ARK_MODEL     （或 LLM_MODEL）                      必填，模型名或推理接入点ID（如 doubao-… / ep-…）
 *   ARK_BASE_URL  （或 LLM_BASE_URL）  选填，默认 https://ark.cn-beijing.volces.com/api/v3
 *     —— 火山「coding plan」要用 https://ark.cn-beijing.volces.com/api/coding/v3（注意是 /coding/v3）
 *     —— 换 DeepSeek 官方: https://api.deepseek.com  | 通义: https://dashscope.aliyuncs.com/compatible-mode/v1
 *   注：deepseek-v4-pro 等部分模型不支持 response_format，本脚本已不强加，靠 prompt+宽松解析兜底。
 * 系统死代理会拦截 API，脚本内已清空 *_PROXY。
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

export interface PlanBriefOptions {
  name: string | null;
  scriptPath: string;
  briefPath: string;
  planPath: string;
  speed: number;
  profile: string;
}

function requiredValue(args: string[], index: number, flag: string): string {
  const value = args[index + 1];
  if (!value || value.startsWith("--")) throw new Error(`缺少 ${flag} 的值`);
  return value;
}

export function parsePlanBriefOptions(
  args: string[],
  root: string = ROOT,
): PlanBriefOptions {
  const explicit = args.some((item) => item.startsWith("--"));
  let speed = 1.25;
  let profile = process.env.PLAN_PROFILE || "zhaoshang";
  if (!explicit) {
    const name = args[0];
    if (!name) throw new Error("用法: plan-brief <name> [speed] [profile]");
    for (const value of args.slice(1)) {
      if (/^[\d.]+$/.test(value)) speed = Number(value);
      else profile = value;
    }
    if (!Number.isFinite(speed) || speed <= 0) throw new Error("speed 必须是正数");
    const scripts = path.join(root, "public", "scripts");
    return {
      name,
      scriptPath: path.join(scripts, `${name}.script.txt`),
      briefPath: path.join(scripts, `${name}.brief.json`),
      planPath: path.join(scripts, `${name}.plan.json`),
      speed,
      profile,
    };
  }

  if (args.length > 0 && !args[0].startsWith("--")) {
    throw new Error("旧名称参数与显式路径参数不能混用");
  }
  let scriptPath: string | null = null;
  let briefPath: string | null = null;
  let planPath: string | null = null;
  for (let index = 0; index < args.length; index += 1) {
    const flag = args[index];
    if (!flag.startsWith("--")) throw new Error(`意外参数: ${flag}`);
    const value = requiredValue(args, index, flag);
    if (flag === "--script") scriptPath = path.resolve(root, value);
    else if (flag === "--brief-output") briefPath = path.resolve(root, value);
    else if (flag === "--plan-output") planPath = path.resolve(root, value);
    else if (flag === "--speed") speed = Number(value);
    else if (flag === "--profile") profile = value;
    else throw new Error(`未知参数: ${flag}`);
    index += 1;
  }
  if (!scriptPath || !briefPath || !planPath) {
    throw new Error("显式模式必须同时提供 --script、--brief-output 和 --plan-output");
  }
  if (!Number.isFinite(speed) || speed <= 0) throw new Error("speed 必须是正数");
  return { name: null, scriptPath, briefPath, planPath, speed, profile };
}

// 后置校验用的结构（不依赖模型 json_schema 支持，统一走 JSON mode + 这里兜底）
const BriefSchema = z.object({
  keywords: z.array(z.string()),
  punchLines: z.array(z.string()),
  title: z.object({ text: z.string(), durationSec: z.number() }),
  broll: z.array(
    z.object({
      anchorText: z.string(),
      query: z.string(),
      prompt: z.string(),
      kind: z.enum(["broll", "image"]),
      durationSec: z.number(),
    })
  ),
});
type Brief = z.infer<typeof BriefSchema>;

// ── 内容画像：双线共存。zhaoshang=招商/园区/产业；caishui=财税/税务/财务咨询。
//    JSON 结构(HEAD) 与 铁律(TAIL) 两条线共用；只切换「锚点选取要求」和 prompt 的「领域示例」。
//    选择：CLI 第 3/4 参数任意位置给 profile 名，或环境变量 PLAN_PROFILE；默认 zhaoshang（不破老流程）。

const SYS_HEAD = `你是短视频混剪的剪辑策划。给你一段数字人口播文案，你要按固定剪辑规范产出结构化 brief，供自动化流水线生成带样式字幕、开场标题卡和 B-roll 插入点。

只输出一个 JSON 对象，不要任何额外文字或 markdown 代码块，结构严格如下：
{
  "keywords": string[],        // 需橘黄高亮的重点词，每个必须是文案中逐字出现的原文（高亮目标见下方画像）。不要纳入纯数字或数字+单位（程序单独处理）。最多 6 个，按重要性排序、同类概念只留代表，宁缺毋滥——高亮过密等于没重点。
  "punchLines": string[],      // 1-3 句金句，整屏大字呈现。必须是文案中的完整原文句子（含标点），逐字一致。
  "title": { "text": string, "durationSec": number },  // 开场标题卡：一句话概括主题，≤12字；durationSec 一般 3
  "broll": [                   // 适合插入画面的锚点，按文案顺序，这是配素材的工作队列；数量随内容弹性，详见下方「锚点选取要求」
    {
      "anchorText": string,    // 文案中的一段原文子串，逐字一致，用于定位插入时间
      "query": string,         // 简短素材搜索词（给图库检索用），2-6字名词
      "prompt": string,        // 给【视频生成模型】的电影级画面描述，详见下方要求
      "kind": "broll" | "image", // broll=全屏空镜（盖住素体）；image=画中画小图（不挡脸）
      "durationSec": number    // 展示秒数，一般 2-4
    }
  ]
}`;

// prompt 写法要求：两条线共用（只是末尾的领域示例各自不同，由 profile.examples 拼接）
const PROMPT_RULES = `【prompt 写法要求（直接喂给 Seedance 文生视频，决定画面质量，务必认真写）】
- 结构公式：主体 + 主体动态 + 场景环境 + 镜头语言 + 光影氛围 + 画面质感。一句话 30~60 字，聚焦一个画面，别堆砌。
- 必须有「运动」（视频的灵魂，分两层都要写）：
  · 主体动态：画面里要有在动的东西——云层流动 / 指示灯闪烁 / 手部翻动 / 人群走动 / 机械运转；纯静态画面会很呆。
  · 镜头运动：用专业运镜词——缓慢推近 / 拉远 / 横移 / 上升 / 环绕 / 跟随，配景别（远景 / 全景 / 中景）；5 秒一镜，运动要慢而连续。
- 单镜头一镜到底，不要分镜切换、不要转场、不要「接着/然后」——一个 prompt 只拍一个连贯画面、一个主体。
- 遇到具体专名/型号/企业名，写泛化的代表性场景，不要试图还原真实型号/特定建筑/品牌——模型造不出特定真实物（例：写「大型现代化总装厂房内的白色装备矗立」，而不是某个具体型号名）。
- 竖屏 9:16 构图，写实/纪实风格。光影写具体：黄昏金色逆光 / 冷色调工业光 / 晨雾柔光 / 暖色台灯光。质感词可选：电影质感、纪录片、浅景深、胶片颗粒、超写实。
- 画面里可以有人（群像/远景/背影/手部更稳），但严禁锁定单个可识别人脸特写；同样严禁画面内出现文字、字幕、logo、水印、明显畸变——也不要在 prompt 里写「企业标识/品牌招牌/文字/数字隐约可见」这类描述，那等于让模型去生成文字 logo。`;

const SYS_TAIL = `铁律：keywords、punchLines、broll.anchorText 都必须是文案中逐字出现的原文子串，不得改写、补字、转繁体。query 和 prompt 不受此限，按上面要求自由撰写。`;

interface Profile {
  label: string;
  anchorRules: string; // 【锚点选取要求】整块
  examples: string; // 接在 PROMPT_RULES 末尾的领域示例两行
}

const PROFILES: Record<string, Profile> = {
  zhaoshang: {
    label: "招商/园区/产业",
    anchorRules: `keywords 高亮目标：地点、园区/机构名、专有项目名、龙头企业名。

【锚点(anchorText/query)选取要求】
- 优先选能拍成具体实物画面的锚点：厂房产线、火箭/卫星/发动机、实验室科研、园区航拍、精密加工、机械臂等。
- 部分高频抽象词可「意象化」成具体空镜，别直接丢：
  · 物流/运输/供应链 → 集装箱货车在高速公路行驶 / 港口龙门吊装卸集装箱 / 货运列车驶过
  · 营商环境/政务服务/办事效率 → 明亮的政务服务大厅办事窗口（远景，无人脸特写）
  · 人才/招聘/用工 → 招聘会人群 / 工程师在实验室操作设备
  · 产业集群/产业链/配套 → 产业园区航拍俯瞰全景 / 入夜城市天际线灯火
  · 人工智能/算力/智能制造/具身智能 → 数据中心成排服务器机柜指示灯闪烁 / 人形机器人在测试场行走 / 工厂机械臂自动化作业
- 真拍不出的抽象词直接跳过、不配画面：成本核算、出让金公式、税务稽查、研发费用占比、知识产权、各类风险条款、各类基金与券（并购基金、耐心资本、算力券）、增长极、红利、"船票"。
- 含「人」的场景可选，但只拍群像/远景/背影/手部（求职人群、工程师、产线工人、商务握手特写），不锁定单个可识别人物。
- broll 数量随内容定：一般 3-6 处；纯方法论/政策/风险类几乎无实物可拍时，少到 1-2 处甚至 0 都行，绝不硬凑——凑出来的尴尬空镜比留白更糟。
- broll=全屏空镜（园区航拍、厂房产线，盖住数字人）；image=不挡脸的小实物特写（卫星终端、发动机部件）。`,
    examples: `- 示例：query=「数据中心 机房」→ prompt=「现代化数据中心机房内成排服务器机柜，蓝色指示灯不断闪烁，冷色调工业灯光，镜头沿走廊缓慢推近，浅景深，电影质感，写实」。
- 示例：query=「风力发电」→ prompt=「戈壁草原上成排白色风力发电机叶片缓缓转动，黄昏金色逆光，云层流动，航拍缓慢横移俯瞰，大气壮阔，纪录片质感，写实」。`,
  },
  caishui: {
    label: "财税/税务/财务咨询",
    anchorRules: `keywords 高亮目标：税种、政策/法规/优惠名称、风险点、关键名词（增值税、留抵退税、加计扣除、稽查、滞纳金、汇算清缴、税收优惠…）。注意合规：不高亮也不鼓励「返还/财政奖励」这类敏感承诺表述。

【锚点(anchorText/query)选取要求】
- 财税内容偏抽象（政策/税务/合规/筹划），实物锚点天然少。优先选能落到具体画面的：财务办公场景、税务服务大厅、单据道具、企业经营实景。
- 高频财税抽象词「意象化」成具体空镜：
  · 税务稽查/风险/补税/处罚 → 明亮的税务服务大厅远景办事窗口 / 财务人员对着电脑翻账的背影 / 一摞文件被手翻阅
  · 报税/申报/汇算清缴 → 办公桌上键盘敲击的手部特写 / 计算器与报表桌面（不出现可读文字数字）
  · 节税/筹划/降本/优惠 → 计算器与财务报表桌面特写 / 繁忙的工厂产线或门店等实体经营场景
  · 发票/合同/账册/印章 → 桌面单据特写、签字的手、盖章落下的瞬间（不出现可读文字）
  · 异地布局/选址/迁移 → 厂房园区航拍 / 城市天际线（与招商可借用）
  · 企业经营/现金流/营收 → 写字楼玻璃幕墙仰拍 / 商务团队会议室远景 / 繁忙产线
- 屏幕/手机界面/电子税务局/店铺招牌/电子屏 等天然带文字的主体，优先换成无文字意象（如机房服务器蓝光数据流、抽象光效流动、仓库货架），避免 Seedance 生成乱码文字。
- 真拍不出的直接跳过、不配画面：具体税率/比例/金额计算、法条编号、抵扣公式、"返还/财政奖励"这类敏感表述。
- 含「人」的场景可选，但只拍群像/远景/背影/手部（财务人员背影、会议室远景、签字盖章的手），不锁定单个可识别人物。
- broll 数量随内容定：财税片普遍偏少，一般 2-4 处；纯政策条款/风险清单几乎无实物可拍时，1-2 处甚至 0 都行，绝不硬凑。
- broll=全屏空镜（税务大厅、写字楼、厂房，盖住数字人）；image=不挡脸的道具小特写（计算器、印章、文件、报表）。`,
    examples: `- 示例：query=「税务大厅」→ prompt=「明亮宽敞的税务服务大厅，一排办事窗口，少量办事群众走动的远景背影，自然采光，镜头缓慢横移推进，景深柔和，写实纪实风格」。
- 示例：query=「财务报表」→ prompt=「木质办公桌上摊开的财务报表与计算器，一只手缓缓翻动纸页，暖色台灯光，浅景深，镜头缓慢推近，写实，质感细腻」。`,
  },
};

function buildSys(profile: string): string {
  const p = PROFILES[profile] || PROFILES.zhaoshang;
  return [SYS_HEAD, p.anchorRules, PROMPT_RULES + "\n" + p.examples, SYS_TAIL].join("\n\n");
}

async function main() {
  const options = parsePlanBriefOptions(process.argv.slice(2), ROOT);
  const { name, scriptPath, briefPath, planPath, speed, profile } = options;
  if (!PROFILES[profile]) {
    console.error(`未知 profile「${profile}」，可选: ${Object.keys(PROFILES).join(" | ")}`);
    process.exit(1);
  }
  const SYS = buildSys(profile);
  if (!API_KEY) {
    console.error("缺 ARK_API_KEY（或 LLM_API_KEY）环境变量。火山方舟控制台获取。");
    process.exit(1);
  }
  if (!MODEL) {
    console.error("缺 ARK_MODEL（或 LLM_MODEL）：填模型名或推理接入点ID，如 doubao-… / ep-…");
    process.exit(1);
  }
  if (!fs.existsSync(scriptPath)) {
    console.error(`缺文案: ${scriptPath}`);
    process.exit(1);
  }
  const scriptText = fs.readFileSync(scriptPath, "utf8").replace(/\r/g, "").trim();

  const client = new OpenAI({ apiKey: API_KEY, baseURL: BASE_URL });

  console.log(`▶ 规划 ${name}（profile=${profile}·${PROFILES[profile].label} | ${MODEL} @ ${BASE_URL}）...`);
  // 注：不强加 response_format（部分 Ark 模型如 deepseek-v4-pro 不支持 json_object）；
  // 靠 system 强约束「只输出 JSON」+ parseJsonLoose 去围栏/截取 + zod 校验兜底。
  const resp = await client.chat.completions.create({
    model: MODEL,
    temperature: 0.3,
    messages: [
      { role: "system", content: SYS },
      { role: "user", content: `文案如下（请按上述 JSON 结构输出）：\n\n${scriptText}` },
    ],
  });

  const raw = resp.choices?.[0]?.message?.content || "";
  let brief: Brief;
  try {
    brief = await parseStructuredOutputWithSingleRepair(
      raw,
      (value) => BriefSchema.parse(value),
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

  // 校验锚点确实是原文子串，过滤掉对不上的（模型偶发补字/转写）。
  // 引号归一（全角/半角 “”‘’ "' 等长度1:1替换）后再比对，避免模型换引号导致金句/关键词被误丢；
  // 命中后回填文案中的原始子串（保留原引号），保证下游 make-ass 高亮 / compile-plan 的 timeOf 精确匹配。
  const normQuotes = (s: string) =>
    s.replace(/[“”„‟″‶"]/g, '"').replace(/[‘’‚‛′‵']/g, "'");
  const normScript = normQuotes(scriptText);
  const findInScript = (s: string): string | null => {
    if (scriptText.includes(s)) return s;
    const ns = normQuotes(s);
    const idx = normScript.indexOf(ns);
    return idx >= 0 ? scriptText.slice(idx, idx + ns.length) : null; // 同长度，索引对齐，切出原始引号版本
  };
  const sift = (arr: string[]) => {
    const kept: string[] = [];
    const bad: string[] = [];
    for (const s of arr) {
      const r = findInScript(s);
      if (r !== null) kept.push(r);
      else bad.push(s);
    }
    return { kept, bad };
  };

  const KW_MAX = 6; // 关键词硬上限（SYS 已要求 ≤6，这里兜底）
  const kw = sift(brief.keywords);
  const punch = sift(brief.punchLines);
  const dedupKw = [...new Set(kw.kept)]; // 去重，保留模型给出的重要性顺序
  const overflowKw = dedupKw.slice(KW_MAX);
  brief.keywords = dedupKw.slice(0, KW_MAX);
  brief.punchLines = punch.kept;
  // 锚点也走引号归一回填，保证 timeOf 能在文案里精确定位
  brief.broll = brief.broll.flatMap((b) => {
    const a = findInScript(b.anchorText);
    return a !== null ? [{ ...b, anchorText: a }] : [];
  });

  if (kw.bad.length) console.warn(`  ⚠ 丢弃对不上原文的关键词: ${kw.bad.join("、")}`);
  if (overflowKw.length) console.warn(`  ⚠ 关键词超 ${KW_MAX} 个，裁掉: ${overflowKw.join("、")}`);
  if (punch.bad.length) console.warn(`  ⚠ 丢弃对不上原文的金句: ${punch.bad.join(" / ")}`);

  fs.mkdirSync(path.dirname(briefPath), { recursive: true });
  fs.mkdirSync(path.dirname(planPath), { recursive: true });
  fs.writeFileSync(briefPath, JSON.stringify(brief, null, 2), "utf8");

  // 可立即渲染的 plan.json：倍速 + 文字标题卡（无需素材）。B-roll 留待 P3 配素材并入。
  const plan = {
    speed,
    titles: brief.title?.text ? [{ text: brief.title.text, start: 0, end: brief.title.durationSec || 3 }] : [],
    inserts: [] as unknown[],
  };
  fs.writeFileSync(planPath, JSON.stringify(plan, null, 2), "utf8");

  console.log(
    `✔ brief: 关键词 ${brief.keywords.length} | 金句 ${brief.punchLines.length} | B-roll ${brief.broll.length} 处`
  );
  console.log(`  标题卡：「${brief.title.text}」`);
  console.log(`  → ${briefPath} + ${planPath}（B-roll 待 P3 配素材）`);
}

if (require.main === module) {
  main().catch((e) => {
    console.error("✘ 规划失败:", e?.message || e);
    process.exit(1);
  });
}
