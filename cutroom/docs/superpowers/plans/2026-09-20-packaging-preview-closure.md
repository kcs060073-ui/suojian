# 剪辑工作台 Packaging Preview Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把包装确认从“素材 ID 列表”升级为与当前画面锁和包装方案摘要绑定的真实短视频预览，并接通方案编辑、个人模板和包装素材新增/更新检查。

**Architecture:** 新增独立的包装预览领域层，负责规范化输入、摘要、缓存记录、观看证据和确认门禁；渲染层复用正式数字人/广告渲染器，并以明确的 draft plan 和低成本输出配置运行，不写正式 QA 或审片快照。现有持久任务队列负责预览和素材检查，工作台 API/媒体路由只做任务归属、认证和投影，前端通过新的纯模型模块管理 stale/ready/viewed 状态。

**Tech Stack:** Python 3.11、SQLite migrations、`unittest`、FFmpeg/ffprobe、TypeScript/ts-node、Node test runner、原生 ES modules/DOM。

**Spec:** `docs/superpowers/specs/2026-09-20-packaging-preview-closure-design.md`

## Global Constraints

- 本计划不修改粗剪播放器、时间线、画面编辑或画面锁语义；只读取持久化 picture lock、场景卡和 canonical digest。
- 真实预览默认 8—12 秒、540×960、H.264/AAC；任务短于 8 秒时按真实时长结束，不循环伪造内容。
- 预览键固定为 `pictureLockDigest + planDigest + rendererVersion + previewProfile`；任何一项变化后旧预览不可确认。
- 已选择槽位必须出现在 `coverage_json`，或带明确的 `not_applicable` / `failed` 原因；不允许静默跳过。
- 浏览器和 API 不接受本机文件路径；媒体只通过任务 ID、方案 ID、预览 ID 或资产 ID 解析。
- 源素材与 manifest 只读；预览只写可重建缓存，素材检查不得自动修改核心 manifest。
- 授权 `prohibited` 或非商用素材阻断；`unknown` 只能出现在检查结果，不能成为正式方案候选。
- 预览不发起隐式付费 TTS，不创建正式 render snapshot、QA report、review candidate 或 packaging gate。
- 正式确认必须通过 `confirm_plan_after_preview()`；HTTP 与 CLI 不得直接调用低层 `confirm_plan()` 绕过观看门禁。
- 执行工作树必须从包含提交 `2b2aa34` 的主线创建；涉及 `app.mjs`、`styles.css`、`workbench/api.py`、`workbench/routes.py` 前，先吸收已完成的粗剪分支，解决一次共享文件冲突后再继续。

## Review Focus

- 预览排队后方案或画面锁被修改：worker 必须把结果记为 stale，不能把旧文件投影为当前预览；Task 2 与 Task 6 的竞态测试固定该行为。
- 缓存文件被删除、替换或截断：GET media 与确认都必须按签名失败；Task 1、Task 7 与 Task 8 覆盖 missing/tampered。
- 同一摘要被连续点击多次：只能有一个活动 job/一个 canonical artifact；Task 6 覆盖幂等和唯一约束。
- 任务不足 8 秒、没有任何可选槽位或某槽位不适用：仍生成真实短预览，并在 coverage 中解释；Task 3、Task 4 与 Task 5 覆盖。
- 个人/扩展目录包含符号链接逃逸、伪装扩展名或未知授权文件：检查结果显示 invalid/pending，但不能被目录接口内联或进入推荐；Task 9 与 Task 10 覆盖。

---

## File Structure

**Create**

- `media_catalog/migrations/033_packaging_preview_and_catalog_check.sql`：预览产物与素材检查结果的持久结构和唯一约束。
- `media_catalog/packaging_preview.py`：预览输入、摘要、状态机、观看证据、确认校验和安全媒体解析。
- `media_catalog/packaging_layers.py`：把一个包装方案编译成正式渲染器可消费的 BGM、水印、标题、贴纸、花字、音效和覆盖清单。
- `media_catalog/packaging_catalog_check.py`：核心、扩展和个人目录的受控差异检查。
- `src/mix-sfx.ts`：渲染器共享的定时音效校验与 FFmpeg filter 片段生成。
- `web-ui/public/packaging-preview.mjs`：预览、槽位、模板与确认按钮的纯状态模型。
- `web-ui/public/packaging-catalog.mjs`：素材检查结果和分类过滤的纯状态模型。
- `tests/test_packaging_preview.py`、`tests/test_packaging_catalog_check.py`：后端领域与 worker 测试。
- `tests/test_cli_packaging.py`：命令行包装确认必须绑定预览的回归测试。
- `tests/packaging-preview-ui.test.mjs`、`tests/render-mix-sfx.test.ts`：前端模型与渲染音效测试。

**Modify**

- `media_catalog/packaging_plan.py:353-403,573-640`：复用禁用/模板能力，公开模板列表并加入预览后确认编排。
- `media_catalog/render_mix_adapter.py:251-338,341-637,863-924`：接受显式 draft plan，复用包装层编译，并支持预览输出配置。
- `media_catalog/ad_actions.py:779-995`：广告路线构造显式 draft 包装预览，不写正式快照。
- `media_catalog/jobs.py:16-37`、`media_catalog/workbench_actions.py:793-825`：登记并绑定新 worker action。
- `scripts/render-mix-options.ts`、`scripts/render-mix.ts`、`scripts/render-ad.ts`、`src/types.ts`：短预览输出配置和定时音效。
- `workbench/api.py:75-104,207-250,1024-1136,1299-1425`：预览、观看、模板、素材检查接口与任务投影。
- `workbench/routes.py:12-15,59-85,195-330`：认证后的合成预览 Range 媒体路由。
- `media_catalog/cli.py:1540-1580`：包装确认命令要求 `--preview-id`。
- `web-ui/public/app.mjs:798-925,1655-1735`、`packaging-editor.mjs`、`libraries-view.mjs`、`styles.css`：真实播放器、方案编辑、模板和素材检查 UI。
- `web-ui/public/help.html:130-170`、`docs/user-guide/cutroom-user-guide.md:310-330`：同事使用说明。
- 现有包装、渲染、API、server、worker 与 web 测试文件：增加回归覆盖，不降低既有断言。

---

### Task 1: Persist packaging preview artifacts and catalog checks

**Files:**
- Create: `media_catalog/migrations/033_packaging_preview_and_catalog_check.sql`
- Create: `media_catalog/packaging_preview.py`
- Test: `tests/test_packaging_preview.py`

**Interfaces:**
- Consumes: `CatalogDatabase.migrate()` and existing `tasks` / `packaging_plans` rows.
- Produces: `create_preview_artifact()`, `get_preview_artifact()`, `find_reusable_preview()`, `set_preview_status()`, `finalize_preview_artifact()`, `mark_preview_viewed()`, `resolve_packaging_preview_media()`.

- [ ] **Step 1: Write the failing migration and repository tests**

```python
class PackagingPreviewRecordTests(unittest.TestCase):
    def test_migration_creates_preview_and_catalog_check_tables(self):
        names = {
            row[0] for row in self.db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("packaging_preview_artifacts", names)
        self.assertIn("packaging_catalog_checks", names)

    def test_same_canonical_input_has_one_active_artifact(self):
        first = create_preview_artifact(self.db, self.input)
        second = create_preview_artifact(self.db, self.input)
        self.assertEqual(first["preview_id"], second["preview_id"])

    def test_deleted_or_tampered_cache_is_not_resolved(self):
        preview = self.ready_preview(bytes_value=b"valid-mp4")
        Path(preview["absolute_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "PACKAGING_PREVIEW_MEDIA_CHANGED"):
            resolve_packaging_preview_media(self.db, preview["preview_id"], self.root)
```

- [ ] **Step 2: Run the tests and verify the schema/repository imports fail**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewRecordTests -v`

Expected: FAIL because migration 033 and `media_catalog.packaging_preview` do not exist.

- [ ] **Step 3: Add migration 033 with strict states and a canonical uniqueness key**

```sql
CREATE TABLE packaging_preview_artifacts(
  preview_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  plan_id TEXT NOT NULL REFERENCES packaging_plans(plan_id) ON DELETE CASCADE,
  canonical_key TEXT NOT NULL UNIQUE,
  picture_lock_digest TEXT NOT NULL,
  plan_digest TEXT NOT NULL,
  renderer_version TEXT NOT NULL,
  preview_profile TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('queued','rendering','ready','failed','stale','missing')),
  rel_path TEXT,
  output_signature_json TEXT,
  duration_seconds REAL,
  size_bytes INTEGER,
  coverage_json TEXT NOT NULL DEFAULT '[]',
  error_code TEXT,
  error_detail TEXT,
  viewed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE packaging_catalog_checks(
  check_id TEXT PRIMARY KEY,
  scope_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('queued','running','completed','failed')),
  result_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);
```

Do not add an `INSERT schema_migrations` line; `CatalogDatabase.migrate()` owns that write.

- [ ] **Step 4: Implement record transitions and signed media resolution**

```python
@dataclass(frozen=True)
class PreviewIdentity:
    task_id: str
    plan_id: str
    picture_lock_digest: str
    plan_digest: str
    renderer_version: str
    preview_profile: str = "portrait-540p-v1"

def preview_canonical_key(identity: PreviewIdentity) -> str:
    return canonical_digest(asdict(identity))

def mark_preview_viewed(db, task_id: str, preview_id: str) -> dict:
    with db.transaction(immediate=True) as connection:
        changed = connection.execute(
            "UPDATE packaging_preview_artifacts SET viewed_at=datetime('now'), "
            "updated_at=datetime('now') WHERE preview_id=? AND task_id=? AND status='ready'",
            (preview_id, task_id),
        ).rowcount
    if changed != 1:
        raise RuntimeError("PACKAGING_PREVIEW_NOT_READY: 预览尚未完成")
    return get_preview_artifact(db, preview_id)
```

Store `file_content_signature()` JSON when ready. Resolve only beneath `runtime/workbench/cache/packaging-previews`, compare the signature on every read, and convert a missing file to status `missing`.

- [ ] **Step 5: Run the record tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewRecordTests -v`

Expected: PASS.

- [ ] **Step 6: Commit the persistence layer**

```powershell
git add media_catalog/migrations/033_packaging_preview_and_catalog_check.sql media_catalog/packaging_preview.py tests/test_packaging_preview.py
git commit -m "feat: persist packaging preview artifacts"
```

---

### Task 2: Build canonical preview inputs and fail closed on stale plans

**Files:**
- Modify: `media_catalog/packaging_preview.py`
- Modify: `media_catalog/packaging_plan.py:437-575`
- Test: `tests/test_packaging_preview.py`

**Interfaces:**
- Consumes: `latest_gate(db, task_id, "picture_lock")`, `get_plan()`, `canonical_packaging_gate_payload()`.
- Produces: `PackagingPreviewInput`, `build_preview_input(db, task_id, plan_id)`, `current_preview_matches(db, preview)`.

- [ ] **Step 1: Add failing ownership, digest, short-task and stale-race tests**

```python
def test_preview_input_is_owned_and_digest_changes_with_plan(self):
    before = build_preview_input(self.db, "task-1", self.plan_id)
    replace_item(self.db, self.plan_id, "bgm", "bgm-b")
    after = build_preview_input(self.db, "task-1", self.plan_id)
    self.assertNotEqual(before.identity.plan_digest, after.identity.plan_digest)

def test_worker_result_is_stale_when_picture_lock_changes_during_render(self):
    preview = self.queued_preview()
    self.replace_picture_lock_digest("new-digest")
    self.assertFalse(current_preview_matches(self.db, preview))

def test_task_shorter_than_eight_seconds_keeps_real_duration(self):
    value = build_preview_input(self.db, "task-short", self.short_plan_id)
    self.assertEqual(value.duration_seconds, 5.25)
```

- [ ] **Step 2: Run the focused tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewInputTests -v`

Expected: FAIL because `build_preview_input()` is missing.

- [ ] **Step 3: Expose one read-only plan validator from `packaging_plan.py`**

```python
def validate_plan_for_render(db, plan_id: str, *, require_picture_lock: bool = True) -> dict:
    plan = get_plan(db, plan_id)
    if plan is None:
        raise KeyError(f"找不到包装方案：{plan_id}")
    with db.transaction() as connection:
        route = _require_canonical_route(connection, plan["task_id"], plan["route"])
        _validate_items(plan["items"], route, require_required=True)
        if require_picture_lock:
            _require_picture_lock(connection, plan["task_id"])
    return plan
```

Reuse the same license, file, route and required-slot validation used by confirmation.

- [ ] **Step 4: Implement canonical inputs and current-state comparison**

```python
@dataclass(frozen=True)
class PackagingPreviewInput:
    identity: PreviewIdentity
    duration_seconds: float
    task_type: str
    plan: dict
    coverage_targets: tuple[str, ...]

def build_preview_input(db, task_id: str, plan_id: str) -> PackagingPreviewInput:
    plan = validate_plan_for_render(db, plan_id)
    if plan["task_id"] != task_id:
        raise PermissionError("PACKAGING_PLAN_NOT_OWNED: 方案不属于当前任务")
    picture = latest_gate(db, task_id, "picture_lock")
    if picture is None:
        raise RuntimeError("PICTURE_LOCK_MISSING: 必须先完成画面锁定")
    task_kind, timeline_duration = _task_preview_context(db, task_id)
    duration = min(12.0, timeline_duration)
    if duration <= 0:
        raise RuntimeError("PACKAGING_PREVIEW_TIMELINE_EMPTY: 当前任务没有可预览时长")
    identity = PreviewIdentity(
        task_id=task_id,
        plan_id=plan_id,
        picture_lock_digest=picture["gate_digest"],
        plan_digest=canonical_digest(canonical_packaging_gate_payload(plan)),
        renderer_version=PACKAGING_PREVIEW_RENDERER_VERSION,
    )
    slots = tuple(sorted({item["slot"] for item in plan["items"] if not item["is_backup"]}))
    return PackagingPreviewInput(identity, duration, task_kind, plan, slots)
```

Implement `_task_preview_context()` with one query over `tasks` and `MAX(scene_cards.narrative_end)`; accept only `digital_human` and `ad_mix`, and raise `UNSUPPORTED_ROUTE` for anything else.

- [ ] **Step 5: Run preview input and composition regression tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewInputTests tests.test_packaging_composition -v`

Expected: PASS.

- [ ] **Step 6: Commit the canonical input contract**

```powershell
git add media_catalog/packaging_preview.py media_catalog/packaging_plan.py tests/test_packaging_preview.py
git commit -m "feat: bind packaging previews to locked inputs"
```

---

### Task 3: Compile every packaging slot into shared render layers

**Files:**
- Create: `media_catalog/packaging_layers.py`
- Create: `src/mix-sfx.ts`
- Create: `tests/render-mix-sfx.test.ts`
- Modify: `src/types.ts:75-122`
- Modify: `scripts/render-mix.ts:32-40,279-438`
- Modify: `media_catalog/render_mix_adapter.py:251-338`
- Test: `tests/test_render_mix_adapter.py`

**Interfaces:**
- Consumes: validated plan items, task title/keywords/punch lines, preview duration.
- Produces: `PackagingTimingContext`, `compile_packaging_layers(db, plan_id, context) -> CompiledPackaging`, `MixSfx`.

- [ ] **Step 1: Write failing Python tests for all eight slots and coverage**

```python
def test_compiler_emits_real_layers_and_explicit_coverage(self):
    compiled = compile_packaging_layers(
        self.db,
        self.plan_id,
        PackagingTimingContext(10.0, "测试标题", "核心优势", "original"),
    )
    self.assertEqual(compiled.plan_fields["titles"][0]["text"], "测试标题")
    self.assertEqual(compiled.plan_fields["titles"][0]["presetId"], "card-data-emphasis")
    self.assertEqual(compiled.plan_fields["stickers"][0]["presetId"], "stk-check")
    self.assertEqual(compiled.plan_fields["fancyTexts"][0]["text"], "核心优势")
    self.assertEqual(compiled.plan_fields["sfx"][0]["src"], str(self.sfx_path))
    self.assertEqual(
        {item["slot"] for item in compiled.coverage},
        {"voice", "bgm", "sfx", "subtitle", "title_card", "sticker", "fancy_text", "brand"},
    )
```

Also add `test_no_optional_slots_produces_subtitle_only_coverage` and `test_original_voice_is_not_applicable_with_reason`.

- [ ] **Step 2: Write failing TypeScript tests for SFX validation and mixing**

```typescript
test("buildSfxFilters delays and mixes one bounded effect", () => {
  const result = buildSfxFilters([{ src: "hit.wav", start: 2.5, gain: 0.35 }], 3);
  assert.deepEqual(result.inputs, ["-i", "hit.wav"]);
  assert.match(result.filters.join(";"), /adelay=2500\|2500/);
  assert.equal(result.labels[0], "[sfx0]");
});
```

- [ ] **Step 3: Run both suites and verify missing compiler/types**

Run: `.venv\Scripts\python -m unittest tests.test_render_mix_adapter.PackagingLayerTests -v`

Run: `node --test -r ts-node/register tests/render-mix-sfx.test.ts`

Expected: FAIL because `packaging_layers.py` and `MixSfx` do not exist.

- [ ] **Step 4: Implement focused layer types and deterministic cue rules**

```python
@dataclass(frozen=True)
class PackagingTimingContext:
    duration_seconds: float
    title: str
    keyword: str
    voice_mode: str

@dataclass(frozen=True)
class CompiledPackaging:
    snapshot: dict
    plan_fields: dict
    coverage: list[dict]

def _cue(duration: float, preferred_start: float, length: float) -> tuple[float, float]:
    start = min(preferred_start, max(0.0, duration - min(length, duration)))
    return start, min(duration, start + length)
```

Use fixed rules: title card at `0.0—min(2.5,duration)`, sticker at the first valid 2-second body cue, fancy text at the next valid 2.5-second cue, and SFX at the title/sticker cue. `voice_mode="original"` yields `not_applicable`; cached TTS yields `rendered`; a selected voice without cache yields `failed`.

- [ ] **Step 5: Add `MixSfx` and mix it below narration but above BGM**

```typescript
export interface MixSfx {
  src: string;
  start: number;
  gain?: number;
}

export interface MixTitle {
  text: string;
  start: number;
  end: number;
  src?: string;
  presetId?: string;
}
```

Add `sfx?: MixSfx[]` to `MixPlan`. When `MixTitle.presetId` is present, `render-mix.ts` resolves the selected `visual-cards.json` item and uses its font, background and accent values for the title ASS style; an unknown preset fails instead of falling back to the fixed title style. Append SFX labels between narration/ambient and BGM, then use the existing limiter.

- [ ] **Step 6: Replace `_compile_packaging()` with the shared compiler**

The final renderer and preview renderer must receive the same `plan_fields`; only output profile and duration differ. Preserve dependency snapshots and file signatures.

- [ ] **Step 7: Run compiler, TypeScript and existing adapter tests**

Run: `.venv\Scripts\python -m unittest tests.test_render_mix_adapter -v`

Run: `node --test -r ts-node/register tests/render-mix-sfx.test.ts tests/render-mix-options.test.ts`

Expected: PASS.

- [ ] **Step 8: Commit shared packaging layers**

```powershell
git add media_catalog/packaging_layers.py media_catalog/render_mix_adapter.py src/types.ts src/mix-sfx.ts scripts/render-mix.ts tests/test_render_mix_adapter.py tests/render-mix-sfx.test.ts
git commit -m "feat: compile packaging slots into render layers"
```

---

### Task 4: Add bounded preview output profiles to the digital renderer

**Files:**
- Modify: `src/types.ts:114-130`
- Modify: `scripts/render-mix-options.ts`
- Modify: `scripts/render-mix.ts:35-75,99-145,290-438`
- Modify: `media_catalog/render_mix_adapter.py:341-637,863-924`
- Modify: `tests/render-mix-options.test.ts`
- Modify: `tests/test_render_mix_adapter.py`

**Interfaces:**
- Consumes: `MixOutputProfile`, explicit `plan_id`, locked task media.
- Produces: `build_packaging_preview_mix_job(db, task_id, plan_id, work_dir, output_path, duration_seconds, subtitle_sources)`.

- [ ] **Step 1: Add failing option and explicit-draft tests**

```typescript
test("preview output flags are bounded", () => {
  const options = parseRenderMixOptions([
    "task-1", "--plan", "plan.json", "--output", "preview.mp4",
    "--max-duration", "10", "--width", "540", "--height", "960", "--crf", "28",
  ], ROOT);
  assert.deepEqual(options.outputProfile, { maxDuration: 10, width: 540, height: 960, crf: 28 });
});
```

```python
def test_preview_job_uses_draft_plan_without_packaging_gate(self):
    job = build_packaging_preview_mix_job(
        self.db, "task-1", self.draft_plan_id, self.work, self.output, 10.0,
        subtitle_sources=self.subtitle_sources,
    )
    self.assertEqual(job["packaging"]["plan_id"], self.draft_plan_id)
    self.assertEqual(job["plan"]["output"]["maxDuration"], 10.0)
    self.assertIsNone(latest_gate(self.db, "task-1", "packaging"))
```

- [ ] **Step 2: Run option and adapter tests**

Run: `node --test -r ts-node/register tests/render-mix-options.test.ts`

Run: `.venv\Scripts\python -m unittest tests.test_render_mix_adapter.PackagingPreviewMixJobTests -v`

Expected: FAIL because output profile and explicit draft plan are unsupported.

- [ ] **Step 3: Add strict output-profile parsing**

```typescript
export interface MixOutputProfile {
  maxDuration?: number;
  width?: number;
  height?: number;
  crf?: number;
}
```

Accept only duration in `(0,12]`, even positive dimensions no larger than `1080×1920`, and CRF `18—32`. Final defaults remain `1080×1920`, unlimited duration and CRF 20.

- [ ] **Step 4: Refactor the Python adapter around one internal builder**

```python
def _build_render_mix_job(
    db, task_id: str, output_path: Path, ass_path: Path | None, *,
    packaging_plan_id: str | None, require_packaging_gate: bool,
    output_profile: dict | None, allow_existing_output: bool = False,
) -> dict:
    packaging = _resolve_packaging_selection(
        db, task_id, packaging_plan_id,
        require_packaging_gate=require_packaging_gate,
    )
    return _assemble_mix_job_payload(
        db, task_id, output_path, ass_path, packaging, output_profile,
        allow_existing_output=allow_existing_output,
    )
```

`build_render_mix_job()` passes `require_packaging_gate=True`; `build_packaging_preview_mix_job()` passes the explicit draft plan and `False`. Both still require current brief and picture locks.

- [ ] **Step 5: Make `render-mix.ts` honor the profile**

Use profile width/height in every scale/crop and ASS PlayRes calculation. Add `-t <maxDuration>` before the output path and use profile CRF. Keep `+faststart`, `yuv420p` and AAC.

- [ ] **Step 6: Run TypeScript and adapter regression suites**

Run: `node --test -r ts-node/register tests/render-mix-options.test.ts tests/render-mix-source-window.test.ts tests/render-mix-sfx.test.ts`

Run: `.venv\Scripts\python -m unittest tests.test_render_mix_adapter tests.test_subtitle_binding -v`

Expected: PASS.

- [ ] **Step 7: Commit the digital preview profile**

```powershell
git add src/types.ts scripts/render-mix-options.ts scripts/render-mix.ts media_catalog/render_mix_adapter.py tests/render-mix-options.test.ts tests/test_render_mix_adapter.py
git commit -m "feat: render bounded digital packaging previews"
```

---

### Task 5: Add the route-aware preview renderer, including ads

**Files:**
- Modify: `media_catalog/packaging_preview.py`
- Modify: `media_catalog/ad_actions.py:779-995`
- Modify: `scripts/render-ad.ts:70-105,146-253`
- Test: `tests/test_packaging_preview.py`
- Test: `tests/test_ad_actions.py`

**Interfaces:**
- Consumes: `PackagingPreviewInput`, digital preview mix builder, ad render artifacts.
- Produces: `render_packaging_preview(db, preview_id, project_root, runner) -> dict`.

- [ ] **Step 1: Write failing route dispatch and real-output tests**

```python
def test_digital_preview_dispatches_to_mix_renderer(self):
    result = render_packaging_preview(self.db, self.preview_id, self.root, self.runner)
    self.assertEqual(result["route"], "digital_human")
    self.assertEqual(result["profile"], "portrait-540p-v1")

def test_ad_preview_uses_existing_voice_and_never_calls_tts(self):
    with patch("media_catalog.tts.synthesize") as paid_tts:
        result = render_packaging_preview(self.db, self.ad_preview_id, self.root, self.runner)
    paid_tts.assert_not_called()
    self.assertIn("voice", {item["slot"] for item in result["coverage"]})
```

Use a tiny synthetic video/audio fixture and verify with ffprobe: codec `h264`, audio `aac`, width `540`, height `960`, duration no more than 12.1 seconds.

- [ ] **Step 2: Run the route tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewRenderTests tests.test_ad_actions.AdPackagingPreviewTests -v`

Expected: FAIL because route-aware rendering is missing.

- [ ] **Step 3: Add bounded flags to `render-ad.ts`**

Extend explicit invocation with `--max-duration`, `--width`, `--height`, and `--crf`. Trim segment definitions before constructing xfade offsets; do not render the full video and trim afterward. Read BGM, subtitle preset and compiled SFX/visual fields from the explicit ad render JSON.

- [ ] **Step 4: Implement route-aware rendering and the post-render stale check**

```python
def render_packaging_preview(db, preview_id: str, project_root: Path, runner) -> dict:
    preview = get_preview_artifact(db, preview_id)
    current = build_preview_input(db, preview["task_id"], preview["plan_id"])
    if preview_canonical_key(current.identity) != preview["canonical_key"]:
        return set_preview_status(
            db, preview_id, "stale", error_code="PACKAGING_PREVIEW_STALE"
        )
    renderers = {
        "digital_human": render_digital_packaging_preview,
        "ad_mix": render_ad_packaging_preview,
    }
    renderer = renderers[current.task_type]
    result = renderer(db, current, Path(project_root), runner)
    if not current_preview_matches(db, preview):
        return set_preview_status(
            db, preview_id, "stale", error_code="PACKAGING_PREVIEW_STALE"
        )
    return finalize_preview_artifact(db, preview_id, result)
```

Keep the stale output only inside disposable cache. Never attach it to the current task projection.

- [ ] **Step 5: Run route and existing ad tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewRenderTests tests.test_ad_actions -v`

Expected: PASS.

- [ ] **Step 6: Commit route-aware rendering**

```powershell
git add media_catalog/packaging_preview.py media_catalog/ad_actions.py scripts/render-ad.ts tests/test_packaging_preview.py tests/test_ad_actions.py
git commit -m "feat: render packaging previews for digital and ad routes"
```

---

### Task 6: Execute preview work through the durable queue

**Files:**
- Modify: `media_catalog/jobs.py:16-37`
- Modify: `media_catalog/workbench_actions.py:793-825`
- Modify: `media_catalog/packaging_preview.py`
- Modify: `tests/test_workbench_worker.py`
- Modify: `tests/test_workbench_actions.py`
- Test: `tests/test_packaging_preview.py`

**Interfaces:**
- Consumes: `enqueue_job()`, `render_packaging_preview()`.
- Produces: job types `packaging_preview`, `packaging_catalog_check`; action `run_packaging_preview_action(db, job, root, runner)`.

- [ ] **Step 1: Write failing idempotency, FIFO and stale-completion tests**

```python
def test_repeated_preview_request_reuses_one_job_and_artifact(self):
    first = request_preview(self.db, "task-1", self.plan_id)
    second = request_preview(self.db, "task-1", self.plan_id)
    self.assertEqual(first["previewId"], second["previewId"])
    self.assertEqual(first["jobId"], second["jobId"])
    self.assertEqual(self.job_count("packaging_preview"), 1)

def test_worker_never_marks_changed_input_ready(self):
    job = self.claim_preview_job()
    self.change_plan_after_claim()
    result = self.handlers["packaging_preview"](job)
    self.assertEqual(result["result"]["status"], "stale")
```

- [ ] **Step 2: Run worker/action tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewQueueTests tests.test_workbench_worker tests.test_workbench_actions -v`

Expected: FAIL because job types and handlers do not exist.

- [ ] **Step 3: Register both jobs as heavy, local and non-paid**

Add `packaging_preview` and `packaging_catalog_check` to `JobManager.HEAVY_TYPES`, but not `PAID_OR_GENERATIVE_TYPES`. Preserve the single-heavy-job gate.

- [ ] **Step 4: Implement idempotent preview requests**

```python
def request_preview(db, task_id: str, plan_id: str) -> dict:
    value = build_preview_input(db, task_id, plan_id)
    artifact = create_preview_artifact(db, value.identity)
    active = active_job_for_preview(db, artifact["preview_id"])
    if artifact["status"] == "ready" and current_preview_matches(db, artifact):
        return preview_request_projection(artifact, None)
    if active is not None:
        return preview_request_projection(artifact, active)
    job_id = next_preview_job_id(db, artifact["preview_id"])
    enqueue_job(
        db,
        job_id=job_id,
        job_type="packaging_preview",
        task_id=task_id,
        payload={"preview_id": artifact["preview_id"]},
        retry_policy="explicit",
    )
    return preview_request_projection(artifact, get_job(db, job_id))
```

`next_preview_job_id()` uses the artifact ID for the first request and a bounded numeric retry suffix for a terminal retry. It never creates two active jobs for one canonical artifact.

- [ ] **Step 5: Bind actions and run queue regressions**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_preview.PackagingPreviewQueueTests tests.test_workbench_worker tests.test_workbench_actions -v`

Expected: PASS.

- [ ] **Step 6: Commit durable execution**

```powershell
git add media_catalog/jobs.py media_catalog/workbench_actions.py media_catalog/packaging_preview.py tests/test_packaging_preview.py tests/test_workbench_worker.py tests/test_workbench_actions.py
git commit -m "feat: queue packaging preview rendering"
```

---

### Task 7: Enforce viewed-preview confirmation and expose templates

**Files:**
- Modify: `media_catalog/packaging_preview.py`
- Modify: `media_catalog/packaging_plan.py:353-403,573-640`
- Modify: `media_catalog/cli.py:1540-1580`
- Modify: `workbench/api.py:75-104,207-250,1024-1136`
- Modify: `workbench/routes.py:99-108`
- Create: `tests/test_cli_packaging.py`
- Modify: `tests/test_packaging_composition.py`
- Modify: `tests/test_workbench_api.py`

**Interfaces:**
- Consumes: `assert_preview_confirmable()`, existing `confirm_plan()`, `save_personal_template()`.
- Produces: `confirm_plan_after_preview()`, `list_personal_templates()`, preview/template API endpoints.

- [ ] **Step 1: Write failing confirmation and template API tests**

```python
def test_confirm_requires_current_viewed_preview(self):
    status, _, payload = self.request(
        "POST", f"/api/tasks/task-1/packaging/{self.plan_id}/confirm", {}
    )
    self.assertEqual(status, 409)
    self.assertEqual(payload["error"]["code"], "PACKAGING_PREVIEW_REQUIRED")

def test_viewed_current_preview_can_confirm(self):
    preview = self.seed_ready_preview(viewed=True)
    status, _, payload = self.request(
        "POST", f"/api/tasks/task-1/packaging/{self.plan_id}/confirm",
        {"previewId": preview["preview_id"]},
    )
    self.assertEqual(status, 200)
    self.assertEqual(payload["status"], "confirmed")
    self.assertEqual(self.latest_packaging_gate()["confirmation_evidence"]["preview_id"], preview["preview_id"])

def test_confirm_rejects_not_viewed_stale_and_missing_previews(self):
    cases = (
        (self.seed_ready_preview(viewed=False), "PACKAGING_PREVIEW_NOT_VIEWED"),
        (self.seed_stale_preview(), "PACKAGING_PREVIEW_STALE"),
        (self.seed_missing_preview(), "PACKAGING_PREVIEW_MEDIA_MISSING"),
    )
    for preview, code in cases:
        with self.subTest(code=code):
            status, _, payload = self.request(
                "POST",
                f"/api/tasks/task-1/packaging/{self.plan_id}/confirm",
                {"previewId": preview["preview_id"]},
            )
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["code"], code)

def test_save_and_list_personal_template(self):
    status, _, template = self.request(
        "POST", f"/api/tasks/task-1/packaging/{self.plan_id}/save-template",
        {"templateName": "稳重招商"},
    )
    self.assertEqual(status, 200)
    self.assertEqual(template["status"], "template")
```

```python
def test_cli_packaging_confirm_requires_preview_id(self):
    result = run_cli(["packaging-confirm", "--db", str(self.db_path), "plan-1"])
    self.assertNotEqual(result.returncode, 0)
    self.assertIn("PACKAGING_PREVIEW_REQUIRED", result.stderr)
```

In `tests/test_cli_packaging.py`, define the runner explicitly:

```python
def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/media_catalog.py", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
```

- [ ] **Step 2: Run API and composition tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api.WorkbenchAPITests.test_confirm_requires_current_viewed_preview tests.test_workbench_api.WorkbenchAPITests.test_viewed_current_preview_can_confirm tests.test_packaging_composition -v`

Expected: FAIL because confirmation ignores previews and templates are not exposed.

- [ ] **Step 3: Implement one formal confirmation orchestration**

```python
def confirm_plan_after_preview(db, task_id: str, plan_id: str, preview_id: str) -> dict:
    preview = assert_preview_confirmable(db, task_id, plan_id, preview_id)
    result = confirm_plan(
        db,
        plan_id,
        evidence={"preview_id": preview_id, "viewed_at": preview["viewed_at"]},
    )
    return {**result, "preview_id": preview_id}
```

Extend `confirm_plan(db, plan_id, *, evidence=None)` so the gate digest remains the canonical plan digest while `details_json` also records `confirmation_evidence`. Existing internal fixtures may omit evidence, but remove all direct production calls: Workbench API and CLI must call `confirm_plan_after_preview()`.

- [ ] **Step 4: Add preview, viewed, template and confirm API routes**

Implement:

```text
POST /api/tasks/{task_id}/packaging/{plan_id}/preview
GET  /api/tasks/{task_id}/packaging/{plan_id}/preview
POST /api/tasks/{task_id}/packaging-previews/{preview_id}/viewed
POST /api/tasks/{task_id}/packaging/{plan_id}/save-template
GET  /api/packaging-templates?route={route}
POST /api/tasks/{task_id}/packaging/{plan_id}/confirm
```

Extend `WorkbenchAPI.dispatch(..., query: dict[str, list[str]] | None = None)` and have `WorkbenchRouter` pass `parse_qs(parsed.query)`. Reject missing/unsupported routes instead of returning all templates.

- [ ] **Step 5: Require `--preview-id` in the CLI confirmation command**

The parser fails before database writes when omitted and prints `PACKAGING_PREVIEW_REQUIRED` in its public error.

- [ ] **Step 6: Run API, CLI and packaging tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api tests.test_packaging_composition tests.test_cli_packaging -v`

Expected: PASS.

- [ ] **Step 7: Commit confirmation and template APIs**

```powershell
git add media_catalog/packaging_preview.py media_catalog/packaging_plan.py media_catalog/cli.py workbench/api.py workbench/routes.py tests/test_cli_packaging.py tests/test_packaging_composition.py tests/test_workbench_api.py
git commit -m "feat: require viewed packaging previews"
```

---

### Task 8: Serve generated previews through authenticated Range media

**Files:**
- Modify: `workbench/routes.py:59-85,195-330`
- Modify: `media_catalog/packaging_preview.py`
- Modify: `tests/test_workbench_server.py`

**Interfaces:**
- Consumes: `resolve_packaging_preview_media(db, preview_id, project_root)`.
- Produces: `GET|HEAD /api/tasks/{task_id}/packaging-previews/{preview_id}/media`.

- [ ] **Step 1: Write failing auth, Range, ownership, missing and tampered tests**

```python
def test_packaging_preview_media_supports_head_and_range(self):
    preview = self.seed_preview_media(b"0123456789")
    response = self.http("GET", preview["mediaUrl"], headers={"Range": "bytes=2-5"})
    self.assertEqual(response.status, 206)
    self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
    self.assertEqual(response.body, b"2345")

def test_preview_media_rejects_wrong_task_and_changed_file(self):
    preview = self.seed_preview_media(b"valid")
    wrong = preview["mediaUrl"].replace("task-1", "task-2")
    self.assertEqual(self.http("GET", wrong).status, 404)
    Path(preview["path"]).write_bytes(b"tampered")
    response = self.http("GET", preview["mediaUrl"])
    self.assertEqual(response.json["error"]["code"], "PACKAGING_PREVIEW_MEDIA_CHANGED")
```

- [ ] **Step 2: Run server tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_server.WorkbenchPackagingPreviewMediaTests -v`

Expected: FAIL because the route is missing.

- [ ] **Step 3: Add the exact route matcher and secure resolver**

Match only `/api/tasks/{task_id}/packaging-previews/{preview_id}/media`, require the local session before database access, verify preview task ownership, then call existing `_send_media_file()`. Only `.mp4` with a ready record and valid signature may be served.

- [ ] **Step 4: Run all server tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_server -v`

Expected: PASS.

- [ ] **Step 5: Commit secure preview delivery**

```powershell
git add workbench/routes.py media_catalog/packaging_preview.py tests/test_workbench_server.py
git commit -m "feat: stream authenticated packaging previews"
```

---

### Task 9: Detect packaging asset additions and updates without mutating manifests

**Files:**
- Create: `media_catalog/packaging_catalog_check.py`
- Create: `tests/test_packaging_catalog_check.py`
- Modify: `media_catalog/workbench_actions.py:793-825`
- Modify: `workbench/api.py:207-250,1299-1425`
- Modify: `workbench/routes.py:232-257`
- Modify: `tests/test_workbench_server.py`

**Interfaces:**
- Consumes: packaging categories, personal packaging root, allowed core/extension roots, durable queue.
- Produces: `request_packaging_catalog_check()`, `run_packaging_catalog_check()`, `latest_packaging_catalog_check()`.

- [ ] **Step 1: Write failing difference and safety tests**

```python
def test_check_reports_added_updated_missing_invalid_and_unchanged(self):
    result = run_packaging_catalog_check(self.db, self.root)
    self.assertEqual(
        {item["status"] for item in result["items"]},
        {"added", "updated", "missing", "invalid", "unchanged"},
    )

def test_unknown_license_and_symlink_escape_never_become_selectable(self):
    result = run_packaging_catalog_check(self.db, self.root)
    risky = {item["relativePath"]: item for item in result["items"]}
    self.assertFalse(risky["stickers/inbox/unknown.png"]["selectable"])
    self.assertEqual(
        risky["stickers/inbox/escape.png"]["reasonCode"],
        "PATH_OUTSIDE_PACKAGING_ROOT",
    )

def test_check_does_not_change_manifest_bytes(self):
    before = self.manifest.read_bytes()
    run_packaging_catalog_check(self.db, self.root)
    self.assertEqual(self.manifest.read_bytes(), before)

def test_registered_video_asset_has_a_streamable_preview(self):
    category = next(item for item in self.catalog()["categories"] if item["key"] == "sticker")
    video = next(item for item in category["assets"] if item["assetRef"] == "animated-sticker")
    self.assertEqual(video["previewKind"], "video")
    self.assertTrue(video["previewUrl"].endswith("/preview"))
```

- [ ] **Step 2: Run catalog check tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_catalog_check -v`

Expected: FAIL because the checker does not exist.

- [ ] **Step 3: Implement controlled roots, relative results and quick fingerprints**

```python
ALLOWED_SUFFIXES = {
    "audio": {".mp3", ".wav", ".ogg", ".m4a", ".aac"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif"},
    "video": {".mp4", ".mov", ".webm"},
    "font": {".otf", ".ttf"},
}

def relative_entry(layer_root: Path, candidate: Path) -> str:
    resolved_root = layer_root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("PATH_OUTSIDE_PACKAGING_ROOT")
    return resolved.relative_to(resolved_root).as_posix()
```

Compare manifest signature metadata when present; otherwise compare the previous completed check's size, mtime and quick fingerprint. Decode failures are `invalid`, not `added`.

- [ ] **Step 4: Extend registered-asset previews to safe video files**

In `_packaging_catalog()`, classify `.mp4`, `.mov` and `.webm` as `previewKind="video"`. In `WorkbenchRouter._packaging_preview()`, allow those suffixes only after `resolve_manifest_asset_path()` containment succeeds, and reuse `_send_media_file()` so video Range requests work. Do not add unknown inbox files to this route.

- [ ] **Step 5: Queue the check and expose latest results**

Implement:

```text
POST /api/packaging-catalog/check
GET  /api/packaging-catalog/checks/latest
```

Deduplicate one active check for the same scope digest. Return counts and relative item projections; never return absolute paths.

- [ ] **Step 6: Run catalog, action, API and media tests**

Run: `.venv\Scripts\python -m unittest tests.test_packaging_catalog_check tests.test_workbench_actions tests.test_workbench_api tests.test_workbench_server -v`

Expected: PASS.

- [ ] **Step 7: Commit packaging catalog checks**

```powershell
git add media_catalog/packaging_catalog_check.py media_catalog/workbench_actions.py workbench/api.py workbench/routes.py tests/test_packaging_catalog_check.py tests/test_workbench_actions.py tests/test_workbench_api.py tests/test_workbench_server.py
git commit -m "feat: check packaging asset changes"
```

---

### Task 10: Build pure UI models for previews, slots, templates and checks

**Files:**
- Create: `web-ui/public/packaging-preview.mjs`
- Create: `web-ui/public/packaging-catalog.mjs`
- Create: `tests/packaging-preview-ui.test.mjs`
- Modify: `web-ui/public/packaging-editor.mjs`
- Modify: `web-ui/public/libraries-view.mjs`

**Interfaces:**
- Consumes: API projections for plans, templates, preview, jobs, catalog categories and checks.
- Produces: `buildPackagingPreviewModel()`, `canConfirmPackaging()`, `slotRows()`, `buildCatalogCheckModel()`.

- [ ] **Step 1: Write failing pure model tests**

```javascript
test("confirm is enabled only for the current viewed ready preview", () => {
  const current = {
    planDigest: "plan-a",
    pictureLockDigest: "picture-a",
    preview: {
      status: "ready",
      viewedAt: "2026-09-20T12:00:00Z",
      planDigest: "plan-a",
      pictureLockDigest: "picture-a",
    },
  };
  assert.equal(buildPackagingPreviewModel(current).canConfirm, true);
  assert.equal(
    buildPackagingPreviewModel({ ...current, planDigest: "plan-b" }).canConfirm,
    false,
  );
});

test("slot rows include sfx and disable only optional slots", () => {
  const rows = slotRows([], CATALOG);
  assert.equal(rows.find((row) => row.key === "sfx").label, "音效");
  assert.equal(rows.find((row) => row.key === "subtitle").canDisable, false);
  assert.equal(rows.find((row) => row.key === "bgm").canDisable, true);
});
```

- [ ] **Step 2: Run the UI model tests**

Run: `node --test tests/packaging-preview-ui.test.mjs`

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement normalized, immutable UI models**

```javascript
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

export function canConfirmPackaging(current) {
  const preview = current?.preview;
  return Boolean(
    preview?.status === "ready" &&
    preview.viewedAt &&
    preview.planDigest === current.planDigest &&
    preview.pictureLockDigest === current.pictureLockDigest
  );
}
```

Catalog check model orders problems `invalid → missing → updated → added → unchanged` and exposes counts without interpreting absolute paths.

- [ ] **Step 4: Run pure UI and existing UI tests**

Run: `node --test tests/packaging-preview-ui.test.mjs tests/workbench-ui.test.mjs`

Expected: PASS.

- [ ] **Step 5: Commit UI models**

```powershell
git add web-ui/public/packaging-preview.mjs web-ui/public/packaging-catalog.mjs web-ui/public/packaging-editor.mjs web-ui/public/libraries-view.mjs tests/packaging-preview-ui.test.mjs
git commit -m "feat: model packaging preview UI states"
```

---

### Task 11: Integrate the real preview and composition controls into the workbench

**Files:**
- Modify: `web-ui/public/app.mjs:798-925,1655-1735`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/workbench-layout.test.mjs`

**Interfaces:**
- Consumes: Task 7 API routes, Task 8 media URL, Task 10 pure models.
- Produces: complete packaging confirmation and packaging catalog interactions.

- [ ] **Step 1: Before shared-file edits, integrate the finished rough-cut branch**

Run: `git log --all --oneline --decorate -20`

Expected: identify the completed rough-cut commit/branch. Merge or rebase it into the packaging worktree, then run `npm run test:web` and require PASS. If rough cut is not complete, stop this task after Tasks 1—10; do not implement against stale `app.mjs`.

- [ ] **Step 2: Write failing DOM tests for the real player and controls**

```javascript
test("packaging workspace renders real video and blocks confirm until viewed", async () => {
  await openPackagingTask({ preview: readyPreview({ viewedAt: null }) });
  const video = document.querySelector("video[data-packaging-preview]");
  assert.equal(
    video.getAttribute("src"),
    "/api/tasks/task-1/packaging-previews/pv-1/media",
  );
  assert.equal(
    document.querySelector("[data-action='confirm-packaging']").disabled,
    true,
  );
  video.dispatchEvent(new Event("ended"));
  await flushRequests();
  assert.deepEqual(lastRequest(), {
    method: "POST",
    path: "/api/tasks/task-1/packaging-previews/pv-1/viewed",
    body: {},
  });
});

test("draft controls can disable sfx and save a template", async () => {
  await openPackagingTask({ selectedSlot: "sfx" });
  document.querySelector("[data-action='disable-packaging-slot']").click();
  assert.equal(lastRequest().body.enabled, false);
  document.querySelector("[data-action='save-packaging-template']").click();
  assert.equal(lastRequest().body.templateName, "我的包装方案");
});
```

- [ ] **Step 3: Run web tests and verify the current list-only preview fails**

Run: `npm run test:web`

Expected: FAIL because there is no real packaging video or viewing gate.

- [ ] **Step 4: Replace `renderPlanInspector()` with a real media state machine**

Render exactly one state: not generated, queued/rendering, ready, stale, or failed/missing. Only ready renders `<video controls preload="metadata">`. At 90% progress or `ended`, POST viewed once per preview ID and reload task detail.

The confirm button posts:

```javascript
await taskPost(`/packaging/${encodeURIComponent(plan.plan_id)}/confirm`, {
  previewId: model.preview.previewId,
});
```

Derive enabled state from `canConfirmPackaging(model)` after every task reload.

- [ ] **Step 5: Add composition controls and personal templates**

Include `sfx` in the selector. Show “关闭此项” only for optional slots and send `{planId, slot, enabled:false}`. Save-template requires a non-empty name. Display templates as an immutable group whose action clones into the task.

- [ ] **Step 6: Add asset cards and catalog check results**

Render per-asset audio/image/video controls from `previewUrl`. Add “检查新增与更新”, show durable job state, then latest grouped differences. Render `relativePath` only; discard absolute path fields from malformed responses.

- [ ] **Step 7: Add representative viewport assertions**

At desktop width, assert player, plan groups and inspector fit the two-column workspace. At 390px, assert controls stack, video remains 9:16, and confirm/save/disable buttons remain reachable without horizontal overflow.

- [ ] **Step 8: Run all web tests**

Run: `npm run test:web`

Expected: PASS.

- [ ] **Step 9: Commit the workbench UI**

```powershell
git add web-ui/public/app.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs
git commit -m "feat: show real packaging previews in workbench"
```

---

### Task 12: Update guidance and run end-to-end verification

**Files:**
- Modify: `web-ui/public/help.html:130-170`
- Modify: `docs/user-guide/cutroom-user-guide.md:310-330`
- Create: `docs/validation/2026-09-20-packaging-preview-closure-acceptance.md`
- Modify tests only if verification reveals a real defect; do not weaken expectations.

**Interfaces:**
- Consumes: completed backend, renderer, API and UI.
- Produces: colleague-facing instructions and reproducible acceptance evidence.

- [ ] **Step 1: Write the colleague workflow**

Document this exact order:

```text
选择方案 → 单项试听/查看 → 必要时复制并替换/关闭 → 生成真实预览
→ 看完预览 → 确认方案 → 可选保存为个人模板 → 正式渲染
```

State that stale/failed/missing previews must be regenerated and catalog checking never auto-enables unknown-license files.

- [ ] **Step 2: Run focused Python suites**

Run:

```powershell
.venv\Scripts\python -m unittest tests.test_packaging_preview tests.test_packaging_catalog_check tests.test_packaging_composition tests.test_render_mix_adapter tests.test_ad_actions tests.test_workbench_api tests.test_workbench_server tests.test_workbench_actions tests.test_workbench_worker -v
```

Expected: PASS with zero failures and zero errors.

- [ ] **Step 3: Run TypeScript and web suites**

Run:

```powershell
npm run test:ts
npm run test:web
node --test tests/packaging-preview-ui.test.mjs
```

Expected: PASS.

- [ ] **Step 4: Run the full Python regression suite**

Run: `.venv\Scripts\python -m unittest discover -s tests -p "test_*.py" -v`

Expected: PASS. Record exact test count and elapsed time in the acceptance document.

- [ ] **Step 5: Perform real digital-human acceptance**

Using one real task with a current picture lock:

1. Generate previews for three recommendations and verify visible/audible differences.
2. Clone one, replace BGM, disable SFX or sticker, and verify the old preview becomes stale.
3. Verify confirmation fails before playback and succeeds after 90%/ended.
4. Save the plan as a personal template and clone it into a second same-route task.
5. Render the confirmed plan and compare subtitle, BGM, brand and at least one visual layer with the preview.

Record task ID, plan ID, preview ID, digests, duration, codec, coverage and final render snapshot ID; omit secrets and private absolute paths.

- [ ] **Step 6: Perform minimal ad and catalog acceptance**

Generate one real ad packaging preview without a new TTS call, complete viewing and enter formal render. Then add one file, modify one file and remove one registered file in a disposable personal packaging test directory; verify `added / updated / missing` and remove only the disposable files afterward.

- [ ] **Step 7: Check the branch diff and commit documentation**

Run: `git diff --check`

Run: `git status --short`

Run: `git log --oneline --decorate -15`

Expected: no whitespace errors; only intended acceptance/help changes remain before commit.

```powershell
git add web-ui/public/help.html docs/user-guide/cutroom-user-guide.md docs/validation/2026-09-20-packaging-preview-closure-acceptance.md
git commit -m "docs: validate packaging preview closure"
```

- [ ] **Step 8: Request whole-branch review before integration**

Use `superpowers:requesting-code-review`. Review against both spec and plan, emphasizing preview/final parity, stale races, path containment, unknown-license handling and confirmation bypasses.

---

## Completion Conditions

- Real digital and ad preview media has been produced and ffprobe-verified.
- The exact viewed preview is bound to confirmation; stale/missing/tampered previews fail closed.
- Every selected slot is rendered or explicitly explained in coverage.
- Final rendering and preview share the same packaging layer compiler.
- Optional disable, SFX selection, personal template save/clone and blank composition are usable in the workbench.
- Packaging catalog checks detect changes without mutating manifests or enabling unknown-license files.
- Targeted, full Python, TypeScript and web suites pass.
- Real-task acceptance evidence is saved without sensitive paths or credentials.
- Rough-cut behavior and tests remain intact after shared-file integration.
