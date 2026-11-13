# P1 收口与数字人 P2 最小真实闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留 WorkBuddy 已落盘能力，修复 P1 关键验收缺口，并让一条数字人任务真正从 SQLite 素材 ID、场景卡和包装方案驱动 `render-mix.ts`，经过自动 QA、人工完整观看门禁后生成完整发布包。

**Architecture:** SQLite 继续作为任务、素材和人工决定的唯一主账本；新增薄适配层把确认后的场景卡和包装方案编译成 `MixPlan`，通过显式路径调用现有 FFmpeg 渲染引擎。渲染前后均使用可追溯门禁，生成物放在 `runtime/`、`out/`、`deliverables/`，不改写原片和现有 `public/scripts/*.plan.json`。

**Tech Stack:** Python 3.11、SQLite、`unittest`、TypeScript 5.7、ts-node、FFmpeg/ffprobe、现有 `media_catalog` 与 `render-mix.ts`。

## Global Constraints

- 面向 Codex / WorkBuddy 对话入口，不建设浏览器操作页，不新增大模型 API。
- 正式存储目标只有本机硬盘和移动硬盘；移动硬盘仍按 `local` 登记，NAS 不属于验收门禁。
- 原片只读；不得覆盖现有原片、已有文案、已有 plan 或历史成片。
- SQLite 是主账本，JSON 只作为渲染输入和发布导出。
- 同一时间只运行一个代理、识别或渲染重任务。
- 四道人工门禁必须可追溯：文案锁定、画面锁定、包装确认、完整观看。
- P3 不扩展；词法匹配、去重和脚本原型不进入本轮完成口径。
- 当前未连接移动硬盘，本轮只完成本机真实验证与换挂载路径自动测试；移动硬盘现场验收必须保持未完成状态。
- 所有新功能和缺陷修复遵循 TDD：测试先失败，再写最小实现，再跑全量回归。

---

### Task 1: 建立可恢复的真实 Git 基线和隔离分支

**Files:**
- Modify: `.gitignore`
- Create: `.git/`（本地仓库元数据，不进入提交）
- Create: `.worktrees/p1-p2-recovery/`（隔离开发目录）

**Interfaces:**
- Consumes: 当前真实项目目录。
- Produces: `main` 基线提交和 `codex/p1-p2-recovery` 隔离工作分支。

- [ ] **Step 1: 完善忽略规则**

在 `.gitignore` 追加：

```gitignore
__pycache__/
*.py[cod]
.superpowers/
assets-local/
runtime/
deliverables/
outputs/
/_*.png
/_*.txt
```

保留 `assets/` 核心包装资产和字体；不得使用全局 `*.png`，避免把贴纸资产排除。

- [ ] **Step 2: 建立本地仓库并检查拟提交文件**

```powershell
git init -b main
git status --short --ignored
git check-ignore .env runtime/media-catalog/catalog.sqlite public/digital_human/xinjiang.mp4 .worktrees
```

Expected: 密钥、运行库、大媒体和旧 worktree 均为 ignored；`media_catalog/`、`scripts/`、`src/`、`docs/`、`assets/` 可进入基线。

- [ ] **Step 3: 建立基线提交**

```powershell
git add -A
git diff --cached --check
git commit -m "chore: establish local project baseline"
```

- [ ] **Step 4: 创建隔离工作分支**

```powershell
git worktree add .worktrees/p1-p2-recovery -b codex/p1-p2-recovery
```

Expected: 后续实现只在该 worktree 进行，`main` 保留 WorkBuddy 原始落盘基线。

---

### Task 2: 恢复当前版本测试基线

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_backup.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_database.py`
- Create: `tests/test_errors.py`
- Create: `tests/test_estimate.py`
- Create: `tests/test_jobs.py`
- Create: `tests/test_libraries.py`
- Create: `tests/test_models.py`
- Create: `tests/test_probe.py`
- Create: `tests/test_scanner.py`
- Create: `tests/test_tools.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `.worktrees/p0-media-catalog/tests/` 遗留 P0 测试。
- Produces: 根项目可直接运行的当前测试基线。

- [ ] **Step 1: 恢复 P0 测试源码，不复制 `__pycache__`**

```powershell
New-Item -ItemType Directory -Path tests
Copy-Item -LiteralPath ..\p0-media-catalog\tests\*.py -Destination .\tests
```

- [ ] **Step 2: 运行遗留测试记录唯一失败**

```powershell
python -m unittest discover -s tests -v
```

Expected: 52 项中只有 `test_status_json_has_stable_keys` 因新增 `p1` 字段失败。

- [ ] **Step 3: 把状态契约改成向后兼容断言**

测试必须断言 P0 固定键仍存在，并允许新增阶段键：

```python
required = {"libraries", "assets", "jobs", "warnings"}
self.assertTrue(required.issubset(payload))
self.assertIn("p1", payload)
```

- [ ] **Step 4: 运行测试、编译和类型检查**

```powershell
python -m unittest discover -s tests -v
python -m compileall -q media_catalog scripts tests
npx tsc --noEmit
```

Expected: 52/52 通过，编译退出码 0。

- [ ] **Step 5: 提交测试基线**

```powershell
git add tests
git commit -m "test: restore media catalog regression suite"
```

---

### Task 3: 修复 P1 按需代理、安全缓存清理和时间码校验

**Files:**
- Modify: `media_catalog/preview.py`
- Modify: `media_catalog/cache.py`
- Modify: `media_catalog/timecode.py`
- Modify: `media_catalog/cli.py`
- Create: `tests/test_p1_closeout.py`

**Interfaces:**
- Produces: `DEFAULT_LIBRARY_KINDS`、失败关闭的 `clean_cache()`、包含代理时长差的 `verify_timecode_consistency()`。

- [ ] **Step 1: 写按需代理失败测试**

```python
def test_library_preview_defaults_do_not_generate_proxy():
    self.assertEqual(DEFAULT_LIBRARY_KINDS, {"thumbnail", "contact_sheet", "audio_preview"})
```

运行该单测，Expected: `ImportError` 或默认集合不存在。

- [ ] **Step 2: 最小实现批量默认类型**

```python
DEFAULT_LIBRARY_KINDS = {"thumbnail", "contact_sheet", "audio_preview"}
```

`generate_previews(..., kinds=None)` 使用该集合；单素材 `generate_asset_previews(..., kinds=None)` 保留全部类型，用户明确选单素材时仍可按需生成 proxy。

- [ ] **Step 3: 写缓存删除失败测试**

测试构造一条真实 `preview_cache` 记录，让 `Path.unlink` 抛 `PermissionError`，断言：

```python
self.assertEqual(result["failed"][0]["rel_path"], "locked.proxy.mp4")
self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM preview_cache").fetchone()[0], 1)
```

运行该单测，Expected: 当前实现错误删除数据库记录。

- [ ] **Step 4: 最小实现失败关闭清理**

`clean_cache()` 只有物理文件成功删除或原本不存在时才删 DB 行；删除异常进入 `failed`，不计入 `freed_bytes`，CLI 显示失败数量并返回非零退出码。

- [ ] **Step 5: 写代理时长一致性测试**

使用注入的 `probe_duration(path)` 返回代理时长，断言原片 10.0 秒、代理 10.8 秒时：

```python
self.assertFalse(result["ok"])
self.assertIn("代理与原片时长偏差", result["problems"][0])
```

运行该单测，Expected: 当前函数只检查 segment 边界，测试失败。

- [ ] **Step 6: 最小实现代理时长校验**

扩展接口：

```python
def verify_timecode_consistency(
    db,
    asset_id: str,
    *,
    probe_duration: Callable[[Path], float | None] | None = None,
    tolerance_sec: float = 0.25,
) -> dict:
```

读取 `preview_cache(kind='proxy')` 和 DB 同级 `cache/` 文件；生产路径用 ffprobe 获取代理时长。偏差超过 `max(tolerance_sec, 原片时长*0.005)` 时阻断。

- [ ] **Step 7: 验证 P1 收口项**

```powershell
python -m unittest tests.test_p1_closeout -v
python -m unittest discover -s tests -v
```

另用 `p1-verify.sqlite` 对一个已有 proxy 做真实时间码检查；缓存真实清理只在新建临时 DB/临时目录验证，不清理现有 P1 缓存。

- [ ] **Step 8: 提交 P1 收口修复**

```powershell
git add media_catalog/preview.py media_catalog/cache.py media_catalog/timecode.py media_catalog/cli.py tests/test_p1_closeout.py
git commit -m "fix: close critical P1 preview and cache gaps"
```

---

### Task 4: 让 `render-mix` 支持显式任务路径

**Files:**
- Modify: `scripts/render-mix.ts`
- Create: `scripts/render-mix-options.ts`
- Create: `tests/render-mix-options.test.ts`
- Modify: `package.json`

**Interfaces:**
- Produces: `parseRenderMixOptions(argv)` 和向后兼容的显式参数：`--input`、`--plan`、`--ass`、`--output`、`--asset-root`、`--dry-run`。

- [ ] **Step 1: 写参数解析失败测试**

```typescript
test("explicit task paths override legacy name paths", () => {
  const options = parseRenderMixOptions([
    "task-a", "1.25", "--input", "D:/input.mp4", "--plan", "D:/plan.json",
    "--ass", "D:/sub.ass", "--output", "D:/final.mp4", "--asset-root", "D:/assets"
  ], "E:/project");
  assert.equal(options.inputVideo, path.resolve("D:/input.mp4"));
  assert.equal(options.outputVideo, path.resolve("D:/final.mp4"));
});
```

Run: `node --test -r ts-node/register tests/render-mix-options.test.ts`

Expected: module/function missing.

- [ ] **Step 2: 实现独立参数解析器**

默认仍映射到 `public/digital_human/<name>.mp4`、`public/scripts/<name>.plan.json`、`public/scripts/<name>.ass`、`out/<name>.mp4`；显式参数只覆盖对应路径。

- [ ] **Step 3: 改造渲染器使用解析结果**

`resolveAsset(src)` 顺序：存在的绝对路径 → `assetRoot/src` → 旧 `public/inserts/src` → `public/src`。保留旧 `npm run mix <name>` 完全兼容。

- [ ] **Step 4: 运行单测和 dry-run 集成验证**

```powershell
npm run test:ts
npx tsc --noEmit
$env:PATH="E:\我的AI项目\视频自动化剪辑\runtime\ffmpeg\ffmpeg-master-latest-win64-gpl\bin;$env:PATH"
npx ts-node scripts/render-mix.ts xinjiang 1.25 --input "E:\我的AI项目\视频自动化剪辑\public\digital_human\xinjiang.mp4" --plan "E:\我的AI项目\视频自动化剪辑\public\scripts\xinjiang.plan.json" --ass "E:\我的AI项目\视频自动化剪辑\public\scripts\xinjiang.ass" --output "E:\我的AI项目\视频自动化剪辑\out\p2-dry-run.mp4" --asset-root "E:\我的AI项目\视频自动化剪辑\public" --dry-run
```

Expected: dry-run 打印的输入、plan 和 output 全部来自显式路径，不生成视频。

- [ ] **Step 5: 提交渲染入口改造**

```powershell
git add scripts/render-mix.ts scripts/render-mix-options.ts tests/render-mix-options.test.ts package.json
git commit -m "feat: support explicit render mix task paths"
```

---

### Task 5: 从素材 ID 编译并执行数字人渲染任务

**Files:**
- Create: `media_catalog/render_mix_adapter.py`
- Modify: `media_catalog/assemble.py`
- Modify: `media_catalog/cli.py`
- Modify: `media_catalog/snapshots.py`
- Create: `media_catalog/migrations/010_review_and_render_provenance.sql`
- Create: `tests/test_render_mix_adapter.py`
- Create: `tests/test_assemble.py`

**Interfaces:**
- Produces:

```python
def compile_render_mix_job(db, task_id: str, work_dir: Path, output_path: Path, ass_path: Path | None) -> dict: ...
def run_render_mix(job: dict, *, runner=subprocess.run, env: dict | None = None) -> Path: ...
```

- [ ] **Step 1: 写“无真实素材不得渲染”失败测试**

分别断言以下情况阻断：0 `task_assets`、0 `scene_cards`、场景卡未确认、picture lock 已变更、包装方案未确认、没有 `role='digital_human'`。

- [ ] **Step 2: 写 MixPlan 编译失败测试**

创建数字人素材和一张 `broll全屏` 卡片，断言：

```python
self.assertEqual(job["plan"]["inserts"][0]["src"], str(broll_path.resolve()))
self.assertEqual(job["plan"]["inserts"][0]["start"], 6.0)
self.assertEqual(job["plan"]["inserts"][0]["end"], 9.0)
self.assertEqual(job["plan"]["inserts"][0]["audio"], "mute")
```

包装方案中的 BGM 和品牌项写入实际 `MixPlan`；字幕预设、贴纸、花字等没有时码/文本的选择只写入 job/snapshot 的 packaging 元数据，不得编造展示时间或文案。所有引用文件必须存在且授权不是 `prohibited`。

- [ ] **Step 3: 最小实现任务编译器**

编译器只读 SQLite，写 `runtime/jobs/<task>/render-mix.plan.json`，不得修改 `public/scripts/<name>.plan.json`。`task_assets.role='digital_human'` 决定底层口播，场景卡决定 inserts，确认包装方案决定 BGM/贴纸/花字/品牌。

- [ ] **Step 4: 写并实现渲染命令测试**

注入 fake runner，断言命令包含显式 `--input/--plan/--output/--asset-root`，失败退出码或输出文件缺失均抛出稳定错误。

- [ ] **Step 5: 改造 assemble 为两段式闭环**

第一段：验证前三道门禁 → 编译任务 → 调用真实渲染 → 记录依赖快照 → QA → 返回 `awaiting_human_review`。

第二段：用户对指定 render snapshot + QA report 写入 `review_approvals` 后再次调用 → 校验 approval、QA、输出内容和完整依赖快照绑定一致 → 生成发布包。仅修改任务级 `reviewed` 状态不得绕过门禁。

旧 `--video` 仅保留为“导入外部已有视频做 QA”，返回值不得再出现“端到端验收通过”。新增 `--render`、`--ass`、`--output` 参数。

- [ ] **Step 6: 运行适配层与编排测试**

```powershell
python -m unittest tests.test_render_mix_adapter tests.test_assemble -v
python -m unittest discover -s tests -v
```

- [ ] **Step 7: 提交真实编排能力**

```powershell
git add media_catalog/render_mix_adapter.py media_catalog/assemble.py media_catalog/cli.py media_catalog/snapshots.py tests/test_render_mix_adapter.py tests/test_assemble.py
git commit -m "feat: render digital human tasks from catalog assets"
```

---

### Task 6: 补齐 QA、人工审片门禁和完整发布包

**Files:**
- Modify: `media_catalog/qa.py`
- Modify: `media_catalog/publish.py`
- Modify: `media_catalog/assemble.py`
- Modify: `media_catalog/cli.py`
- Modify: `media_catalog/migrations/010_review_and_render_provenance.sql`
- Create: `tests/test_qa_release_gate.py`
- Create: `tests/test_publish_package.py`

**Interfaces:**
- Produces: 工具不可用时失败关闭的 QA、与渲染快照绑定的人工审片记录、包含成片/封面候选/QA/发布文案的完整目录。

- [ ] **Step 1: 写 QA 工具失败和视频规格失败测试**

断言 ffmpeg/ffprobe 不可用、成片不存在、不可解码、不是 9:16、宽高低于 1080×1920 时均为 blocking；不能再以 warn 形式通过。

- [ ] **Step 2: 最小实现技术 QA**

在现有黑帧/冻结/静音/削波前增加 ffprobe JSON 检查：视频流存在、H.264/HEVC 可接受、9:16 容差 0.02、分辨率至少 1080×1920、fps 24–60、音频流存在。工具执行失败和解析失败均阻断。

- [ ] **Step 3: 写完整发布包失败测试**

调用 `build_publish_package(..., video_path, qa_report)` 后断言以下文件全部存在：

```text
video-final.mp4
cover-01.jpg
cover-02.jpg
cover-03.jpg
publish-copy.md
publish-check.json
qa-report.md
```

当前实现只输出平台 JSON/MD，测试应失败。

- [ ] **Step 4: 最小实现发布包**

复制已通过 QA 的成片为 `video-final.mp4`；用 ffmpeg 在 15%、50%、85% 时间点抽三张无新增字幕的关键帧候选；把 QA 摘要写入 `qa-report.md`，发布文案与敏感命中写入固定文件。用户使用外部封面生成器时，三个关键帧就是交接输入，不自动调用外部服务。

- [ ] **Step 5: 增加人工完整观看记录**

沿用 Task 5 已建立的迁移表：

```sql
CREATE TABLE IF NOT EXISTS review_approvals(
  approval_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  qa_report_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
  decided_by TEXT,
  notes TEXT,
  decided_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

CLI：`review approve --task --snapshot --qa --by --notes`。发布必须读取与当前 render snapshot、QA report 匹配的 approved 记录；仅把 `tasks.status` 改成 reviewed 不足以绕过门禁。

- [ ] **Step 6: 运行 QA/发布/全量测试**

```powershell
python -m unittest tests.test_qa_release_gate tests.test_publish_package tests.test_assemble -v
python -m unittest discover -s tests -v
npx tsc --noEmit
```

- [ ] **Step 7: 提交交付门禁**

```powershell
git add media_catalog media_catalog/migrations/010_review_and_render_provenance.sql tests
git commit -m "feat: enforce reviewed QA release packages"
```

---

### Task 7: 用真实 `xinjiang` 任务验收并更新团队文档

**Files:**
- Create: `docs/validation/p1-closeout-p2-digital-human-acceptance.md`
- Modify: `docs/validation/p1-media-catalog-acceptance.md`
- Modify: `README.md`
- Modify: `docs/user-guide/video-production-user-guide.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: `runtime/media-catalog/p1-verify.sqlite` 的副本、`public/digital_human/xinjiang.mp4`、真实 B-roll、现有 `xinjiang.ass`。
- Produces: 一条可追溯数字人验收任务、真实新渲染成片、完整发布包和不夸大的验收报告。

- [ ] **Step 1: 建立独立验收数据库**

通过 SQLite backup API 把 `p1-verify.sqlite` 复制为 `p2-digital-human-acceptance.sqlite`，不得直接修改原验证库。查询并登记 `xinjiang.mp4` 与三条 B-roll 的真实 asset_id；建立 task、task_assets、scene_cards 和 confirmed packaging_plan。

- [ ] **Step 2: 运行前三道人工门禁和真实渲染**

```powershell
python scripts/media_catalog.py --db runtime/media-catalog/p2-digital-human-acceptance.sqlite assemble --task p2-xinjiang --render --ass public/scripts/xinjiang.ass --output out/p2-xinjiang.mp4 --json
```

Expected: 真实调用 `render-mix` 生成新的 `out/p2-xinjiang.mp4`，QA 0 阻断，状态为 `awaiting_human_review`，尚不生成发布包。

- [ ] **Step 3: 完整观看并落人工决定**

人工检查字幕、镜头时间、BGM 是否压人声、黑帧/卡顿/断句；确认后运行 `review approve`。如果发现问题，记录 rejected 并修复，不能跳过。

- [ ] **Step 4: 生成并核验完整发布包**

再次运行 assemble，Expected: `deliverables/publish/p2-xinjiang/` 含 final video、3 张关键帧、QA、发布文案和检查 JSON，且 DB 可追溯到素材 ID、场景卡、包装方案、render snapshot、QA 与 review approval。

- [ ] **Step 5: 全量完成前验证**

```powershell
python -m unittest discover -s tests -v
python -m compileall -q media_catalog scripts tests
npm run test:ts
npx tsc --noEmit
python scripts/media_catalog.py --db runtime/media-catalog/p2-digital-human-acceptance.sqlite assets validate --json
```

另用 ffprobe 核验成片编码、1080×1920、音视频流、时长，并列出发布包文件及 SHA-256。

- [ ] **Step 6: 更新文档口径**

P1 报告改为“软件核心能力完成，本机验收通过，移动硬盘/普通同事现场验收待完成”；P2 只写“数字人路线最小真实闭环通过”，项目路线仍未验收，P3 仍暂停。

- [ ] **Step 7: 提交验收与文档**

```powershell
git add README.md AGENTS.md docs
git commit -m "docs: record verified digital human vertical slice"
```

---

## 最终完成门禁

- 根项目具有持久化 Git 基线；实现工作位于独立分支。
- 当前版本 Python/TypeScript 测试全部通过。
- 批量预览默认不再全库生成 proxy；缓存删除失败不会丢账；代理与原片时长偏差可阻断。
- `assemble --render` 必须从真实 asset_id、scene_cards 和 confirmed packaging_plan 编译输入并调用 `render-mix`。
- 没有 scene card、素材、画面锁、包装确认、QA 或人工完整观看记录时，任何一步都不能写“端到端验收通过”。
- 发布包包含成片、3 张封面关键帧、发布文案、合规检查和 QA 报告。
- 数字人真实样片通过后只能声明“数字人 P2 最小闭环通过”；项目 P2、移动硬盘现场 P1、普通同事实测和 P3 均保持待验收。
