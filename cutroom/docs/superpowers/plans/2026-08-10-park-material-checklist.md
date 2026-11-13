# 项目素材极简检查表实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 生成一份供普通业务人员本地维护的单工作表 Excel，同时检查项目点位是否拍全、素材是否已整理可用。

**Architecture:** 使用 `@oai/artifact-tool` 创建一张 `项目素材检查表`。顶部是公式驱动的四项汇总，下面是 8 字段明细表；要求等级和两段状态使用下拉选项，状态使用条件颜色。最终工作簿保存到项目 `outputs/20260810-093912-codex-5ff8/`，构建脚本仅放在本次会话的可写临时目录。

**Tech Stack:** bundled Node.js、`@oai/artifact-tool` 2.8.6+、Excel `.xlsx`

## Global Constraints

- 设计事实源：`docs/superpowers/specs/2026-08-10-park-material-checklist-design.md`。
- 工作簿只能有一张业务工作表：`项目素材检查表`。
- 明细只能有 8 个字段：一级分类、点位/场景、要求等级、最低要求、拍摄状态、入库状态、素材位置、缺口/备注。
- 一级分类：项目形象、公共环境、产业空间、基础设施、交通区位、可组织场景。
- 要求等级：基础必备、有则必备、特色可选。
- 拍摄状态：未拍、部分完成、已拍全、不适用。
- 入库状态：未整理、整理中、已整理可用、不适用。
- 顶部完成率只计算基础必备项；基础必备项使用“不适用”仍计为缺口。
- 不增加专业摄影参数、逐文件元数据、云端功能或素材预览。

---

### Task 1: 创建工作簿数据与基础布局

**Files:**
- Create: `C:/Users/Tanikawa/.codex/visualizations/2026/08/10/019fe94e-4b46-7ac2-9718-d12472ca2cca/build-park-material-checklist.mjs`
- Create: `E:/我的AI项目/视频自动化剪辑/outputs/20260810-093912-codex-5ff8/园区素材检查表.xlsx`

**Interfaces:**
- Consumes: 设计说明第 4、7 节的字段与点位目录。
- Produces: `项目素材检查表` 工作表以及 A8:H58 左右的明细区域，确切末行由点位数量决定。

- [ ] **Step 1: 创建会话级依赖 Junction**

Run:

```powershell
New-Item -ItemType Junction -Path 'C:\Users\Tanikawa\.codex\visualizations\2026\08\10\019fe94e-4b46-7ac2-9718-d12472ca2cca\node_modules' -Target 'C:\Users\Tanikawa\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules'
```

Expected: `node_modules` 指向 loader 提供的依赖目录，不修改依赖源。

- [ ] **Step 2: 创建构建脚本并写入全部规范点位**

脚本必须使用以下启动和导出接口：

```js
import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("项目素材检查表");
sheet.showGridLines = false;

// rows 的每一项严格对应 8 个字段；点位内容逐条复制自设计说明第 7 节。
const rows = [
  ["项目形象", "项目门头全貌", "基础必备", "门头远景、近景各1段", "未拍", "未整理", "", ""],
  ["项目形象", "项目名称、LOGO、门牌", "基础必备", "清晰特写视频1段，照片1张", "未拍", "未整理", "", ""],
];

const outputDir = "E:/我的AI项目/视频自动化剪辑/outputs/20260810-093912-codex-5ff8";
await fs.mkdir(outputDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(`${outputDir}/项目素材检查表.xlsx`);
```

Expected: `rows` 最终包含设计说明第 7 节全部点位，且每行恰好 8 个值。

- [ ] **Step 3: 写入标题、说明、汇总标签、表头和明细**

使用块写入：

```js
sheet.getRange("A1:H1").merge();
sheet.getRange("A1").values = [["项目本地素材库检查表"]];
sheet.getRange("A2:H2").merge();
sheet.getRange("A2").values = [["一张表完成拍摄验收与入库验收；基础必备项必须拍全并整理可用。"]];
sheet.getRange("A7:H7").values = [["一级分类", "点位/场景", "要求等级", "最低要求", "拍摄状态", "入库状态", "素材位置", "缺口/备注"]];
sheet.getRangeByIndexes(7, 0, rows.length, 8).values = rows;
```

Expected: 标题在第 1 行，说明在第 2 行，汇总区使用第 4–5 行，表头在第 7 行，数据从第 8 行开始。

- [ ] **Step 4: 运行首次构建**

Run:

```powershell
& 'C:\Users\Tanikawa\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' 'C:\Users\Tanikawa\.codex\visualizations\2026\08\10\019fe94e-4b46-7ac2-9718-d12472ca2cca\build-park-material-checklist.mjs'
```

Expected: exit code 0，并生成目标 `.xlsx`。

### Task 2: 添加汇总公式、下拉、条件颜色与排版

**Files:**
- Modify: `C:/Users/Tanikawa/.codex/visualizations/2026/08/10/019fe94e-4b46-7ac2-9718-d12472ca2cca/build-park-material-checklist.mjs`
- Regenerate: `E:/我的AI项目/视频自动化剪辑/outputs/20260810-093912-codex-5ff8/园区素材检查表.xlsx`

**Interfaces:**
- Consumes: Task 1 的 `rows.length`、数据起始行 8 和结束行 `7 + rows.length`。
- Produces: 可编辑、可筛选、可量化检查的最终工作簿。

- [ ] **Step 1: 写入四项公式驱动汇总**

设 `endRow = 7 + rows.length`，使用有界公式：

```js
sheet.getRange("A4").values = [["基础必备项"]];
sheet.getRange("B4").formulas = [[`=COUNTIF(C8:C${endRow},"基础必备")`]];
sheet.getRange("C4").values = [["拍摄完成率"]];
sheet.getRange("D4").formulas = [[`=IFERROR(COUNTIFS(C8:C${endRow},"基础必备",E8:E${endRow},"已拍全")/B4,0)`]];
sheet.getRange("E4").values = [["入库完成率"]];
sheet.getRange("F4").formulas = [[`=IFERROR(COUNTIFS(C8:C${endRow},"基础必备",F8:F${endRow},"已整理可用")/B4,0)`]];
sheet.getRange("G4").values = [["当前缺口"]];
sheet.getRange("H4").formulas = [[`=COUNTIFS(C8:C${endRow},"基础必备",E8:E${endRow},"<>已拍全")+COUNTIFS(C8:C${endRow},"基础必备",E8:E${endRow},"已拍全",F8:F${endRow},"<>已整理可用")`]];
```

Expected: 初始状态下两项完成率为 0%，缺口等于未完成的基础必备行数；同一行不会重复计数。

- [ ] **Step 2: 添加数据验证与条件格式**

```js
sheet.getRange(`C8:C${endRow}`).dataValidation = { rule: { type: "list", values: ["基础必备", "有则必备", "特色可选"] } };
sheet.getRange(`E8:E${endRow}`).dataValidation = { rule: { type: "list", values: ["未拍", "部分完成", "已拍全", "不适用"] } };
sheet.getRange(`F8:F${endRow}`).dataValidation = { rule: { type: "list", values: ["未整理", "整理中", "已整理可用", "不适用"] } };

sheet.getRange(`E8:F${endRow}`).conditionalFormats.add("containsText", { text: "未", format: { fill: "#FDE2E2", font: { color: "#B42318" } } });
sheet.getRange(`E8:F${endRow}`).conditionalFormats.add("containsText", { text: "部分", format: { fill: "#FFF1CC", font: { color: "#8A5700" } } });
sheet.getRange(`E8:F${endRow}`).conditionalFormats.add("containsText", { text: "整理中", format: { fill: "#FFF1CC", font: { color: "#8A5700" } } });
sheet.getRange(`E8:F${endRow}`).conditionalFormats.add("containsText", { text: "已", format: { fill: "#DDF3E4", font: { color: "#166534" } } });
sheet.getRange(`E8:F${endRow}`).conditionalFormats.add("containsText", { text: "不适用", format: { fill: "#ECEFF3", font: { color: "#667085" } } });
```

Expected: 下拉选项完整；红、黄、绿、灰状态可快速识别。

- [ ] **Step 3: 设置表格、冻结窗格与专业排版**

```js
const table = sheet.tables.add(`A7:H${endRow}`, true, "ParkMaterialChecklist");
table.style = "TableStyleMedium2";
table.showFilterButton = true;
sheet.freezePanes.freezeRows(7);
sheet.getRange(`A7:H${endRow}`).format.wrapText = true;
sheet.getRange(`A1:A${endRow}`).format.columnWidth = 14;
sheet.getRange(`B1:B${endRow}`).format.columnWidth = 24;
sheet.getRange(`C1:C${endRow}`).format.columnWidth = 13;
sheet.getRange(`D1:D${endRow}`).format.columnWidth = 42;
sheet.getRange(`E1:F${endRow}`).format.columnWidth = 13;
sheet.getRange(`G1:G${endRow}`).format.columnWidth = 34;
sheet.getRange(`H1:H${endRow}`).format.columnWidth = 30;
```

标题使用深绿色，输入区域使用浅色底，汇总卡片突出完成率和缺口；百分比使用 `0%` 格式。

- [ ] **Step 4: 重新运行构建**

Run: 与 Task 1 Step 4 相同。

Expected: exit code 0，最终工作簿覆盖输出文件。

### Task 3: 内容、公式与视觉验收

**Files:**
- Verify: `E:/我的AI项目/视频自动化剪辑/outputs/20260810-093912-codex-5ff8/园区素材检查表.xlsx`
- Create temporary preview: `C:/Users/Tanikawa/.codex/visualizations/2026/08/10/019fe94e-4b46-7ac2-9718-d12472ca2cca/园区素材检查表-preview.png`

**Interfaces:**
- Consumes: Task 2 的最终工作簿。
- Produces: 公式、状态选项和视觉布局均通过检查的交付文件。

- [ ] **Step 1: 在构建脚本中检查关键范围**

```js
const inspect = await workbook.inspect({
  kind: "table",
  range: `项目素材检查表!A1:H${endRow}`,
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 8,
});
console.log(inspect.ndjson);
```

Expected: 标题、四项公式、8 个表头和首批点位均正确。

- [ ] **Step 2: 扫描公式错误**

```js
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);
```

Expected: 0 个公式错误。

- [ ] **Step 3: 渲染整张工作表并保存预览**

```js
const preview = await workbook.render({
  sheetName: "项目素材检查表",
  autoCrop: "all",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  "C:/Users/Tanikawa/.codex/visualizations/2026/08/10/019fe94e-4b46-7ac2-9718-d12472ca2cca/园区素材检查表-preview.png",
  new Uint8Array(await preview.arrayBuffer()),
);
```

Expected: 标题、汇总、表头和全部行均可见；无文字截断、空白默认工作表或越界内容。

- [ ] **Step 4: 打开预览图做视觉检查并修复**

检查：列宽、换行、颜色、表头、汇总卡片、分类边界和长文本。若发现严重缺陷，只修对应格式并重新运行 Task 2 Step 4 和本任务的三项检查。

- [ ] **Step 5: 最终文件确认**

Run:

```powershell
Get-Item -LiteralPath 'E:\我的AI项目\视频自动化剪辑\outputs\20260810-093912-codex-5ff8\园区素材检查表.xlsx' | Select-Object FullName,Length,LastWriteTime
```

Expected: 文件存在、大小大于 0、更新时间为本次执行时间。
