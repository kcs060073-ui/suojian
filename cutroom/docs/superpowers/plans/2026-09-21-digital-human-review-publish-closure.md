# 剪辑工作台 Digital-Human Review and Publish Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有数字人工作台后半程增加可追溯候选版本、分级 QA、分类返工、局部回退和候选绑定发布包，完成“精剪—审片—返工—新版本—发布”闭环。

**Architecture:** 保留现有 `render_snapshots`、`qa_reports`、真实观看区间、审批与发布包校验，在其上增加 `candidate_versions` 和 `rework_requests` 编排层。服务端是门禁和版本关系的唯一解释者；前端只消费任务投影，通过纯模型模块展示历史候选、QA 分级、返工影响与发布包过期状态。

**Tech Stack:** Python 3.11、SQLite migrations、`unittest`、原生 HTTP workbench API、Node test runner、原生 ES modules/DOM、FFmpeg/ffprobe。

**Spec:** `docs/superpowers/specs/2026-09-21-digital-human-review-publish-closure-design.md`

## Global Constraints

- 保留现有全局导航、任务上下文栏、工作区骨架、主要视觉语言以及前半程阶段页面，只定向改造后半程工作区。
- 候选视频或渲染输入变化必须创建新候选版本；仅视频字节未变化的 QA 重跑可增加报告修订。
- 新候选版本不得继承旧版本的 QA、观看进度、批准结果或发布状态。
- 旧候选、旧 QA、旧审片记录和旧发布包只读保留，不覆盖、不删除。
- 阻断 QA、未完整观看、候选签名变化或上游输入失效时禁止批准与正式发布。
- 分类返工只失效目标阶段及其下游当前门禁，不删除历史证据。
- 保存返工单不执行回退；执行返工也不隐式调用付费 AI、外部生成或外部发布服务。
- 所有状态写操作必须在服务端校验任务归属、候选签名、当前门禁和幂等键。
- 继续使用现有 FFmpeg 渲染链，不引入 Remotion，不改变粗剪和包装闭环语义。
- 真实人工 100% 观看不得由自动测试伪造；自动化只验证门禁，最终真实任务由人工完成观看。

## Review Focus

- 两个标签页对同一返工单重复确认：只执行一次回退，只产生一个后继候选；Task 3 和 Task 5 覆盖幂等与竞态。
- QA 阻断候选没有 `qa_snapshot_id`：仍能出现在候选历史并显示问题，但不能开始完整审片；Task 2、Task 5 和 Task 6 覆盖。
- 旧候选的视频、观看会话或批准记录被误用于新候选：服务端拒绝，前端不解锁按钮；Task 2、Task 5 和 Task 6 覆盖。
- 已发布候选被返工：旧发布包保留且标记过期，新候选批准前不能复用旧包；Task 4 和 Task 5 覆盖。
- 候选文件被替换、删除或路径越界：历史媒体读取、审批和发布均失败关闭；Task 2、Task 4 和 Task 5 覆盖。

---

## File Structure

**Create**

- `media_catalog/migrations/035_candidate_versions_and_rework.sql`：候选版本、返工单以及发布包候选绑定结构。
- `media_catalog/candidate_versions.py`：候选创建、QA 绑定、历史投影、签名校验和媒体解析。
- `media_catalog/rework.py`：分类映射、影响预览、驳回建单、幂等确认和工作流回退。
- `web-ui/public/review-closure.mjs`：候选历史、QA 分组、返工表单和按钮门禁的纯状态模型。
- `tests/test_candidate_versions.py`：候选版本与媒体签名测试。
- `tests/test_rework_requests.py`：返工映射、原子驳回、回退和幂等测试。
- `tests/review-closure-ui.test.mjs`：后半程纯模型和浏览器源码回归测试。
- `docs/validation/2026-09-21-digital-human-review-publish-closure-acceptance.md`：自动化证据与人工验收清单。

**Modify**

- `media_catalog/assemble.py:207-237`：在渲染完成和 QA 结果产生时登记候选版本。
- `media_catalog/snapshots.py:65-216`：把候选 ID、版本号附加到可信候选投影。
- `media_catalog/reviews.py:43-130`：提供同一事务内写入审片决定的内部接口，供分类驳回复用。
- `media_catalog/workflow.py:220-401`：支持把返工目标阶段设为 ready、其下游设为 invalidated，并按发布包生命周期投影。
- `media_catalog/publish.py:230-464`：把发布包绑定候选版本并暴露 `current/expired` 状态。
- `workbench/api.py:1000-1190`：候选列表、QA、返工预览/保存/执行和绑定发布动作。
- `workbench/routes.py:55-105,273-308`：认证后的候选版本媒体 Range 路由。
- `web-ui/public/review-view.mjs`：候选感知的审片门禁与媒体 URL。
- `web-ui/public/publish-view.mjs`：候选标签和过期状态投影。
- `web-ui/public/app.mjs:1550-1750`：拆分精剪候选、自动 QA、完整审片和发布区，增加分类返工对话框。
- `web-ui/public/styles.css`：候选历史、QA 分组、返工摘要和过期发布包样式。
- `tests/test_full_review.py`、`tests/test_publish_package.py`、`tests/test_workbench_api.py`、`tests/workbench-ui.test.mjs`：增加跨模块回归覆盖。

---

### Task 1: Persist candidate versions and rework requests

**Files:**
- Create: `media_catalog/migrations/035_candidate_versions_and_rework.sql`
- Create: `media_catalog/candidate_versions.py`
- Test: `tests/test_candidate_versions.py`
- Test: `tests/test_workbench_schema.py`

**Interfaces:**
- Consumes: `CatalogDatabase.migrate()`, existing `tasks`, `render_snapshots`, `qa_reports`, and `publish_records`.
- Produces: `ensure_candidate_version(db, task_id, render_snapshot, *, change_summary=None) -> dict`, `attach_candidate_qa(db, candidate_id, qa_result, qa_snapshot_id=None) -> dict`, `get_candidate(db, task_id, candidate_id) -> dict | None`, `list_candidates(db, task_id) -> list[dict]`, `candidate_for_snapshot(db, task_id, snapshot_id) -> dict | None`.

- [ ] **Step 1: Write failing schema and repository tests**

```python
def test_migration_creates_candidate_and_rework_tables(self):
    names = {
        row[0] for row in self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    self.assertIn("candidate_versions", names)
    self.assertIn("rework_requests", names)

def test_same_mix_snapshot_reuses_candidate_and_versions_are_monotonic(self):
    first = ensure_candidate_version(self.db, "task-1", self.mix("mix-1", "d1"))
    replay = ensure_candidate_version(self.db, "task-1", self.mix("mix-1", "d1"))
    second = ensure_candidate_version(self.db, "task-1", self.mix("mix-2", "d2"))
    self.assertEqual(first["candidate_id"], replay["candidate_id"])
    self.assertEqual([first["version_no"], second["version_no"]], [1, 2])

def test_blocked_qa_is_visible_without_becoming_reviewable(self):
    candidate = ensure_candidate_version(self.db, "task-1", self.mix("mix-1", "d1"))
    updated = attach_candidate_qa(self.db, candidate["candidate_id"], {
        "report_id": "qa-1", "status": "blocked", "blocking_count": 1,
        "issues": [{"severity": "blocking", "check": "black", "detail": "黑帧"}],
    })
    self.assertEqual(updated["status"], "qa_blocked")
    self.assertIsNone(updated["qa_snapshot_id"])
```

- [ ] **Step 2: Run the tests and verify they fail for missing migration/module**

Run: `.venv\Scripts\python -m unittest tests.test_candidate_versions tests.test_workbench_schema -v`

Expected: FAIL because migration 035 and `media_catalog.candidate_versions` do not exist.

- [ ] **Step 3: Add migration 035 with strict keys and lifecycle states**

```sql
CREATE TABLE candidate_versions(
  candidate_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  version_no INTEGER NOT NULL CHECK(version_no > 0),
  source_candidate_id TEXT REFERENCES candidate_versions(candidate_id),
  rework_request_id TEXT,
  mix_snapshot_id TEXT NOT NULL UNIQUE REFERENCES render_snapshots(snapshot_id),
  qa_snapshot_id TEXT UNIQUE REFERENCES render_snapshots(snapshot_id),
  qa_report_id TEXT REFERENCES qa_reports(report_id),
  input_digest TEXT NOT NULL,
  output_path TEXT NOT NULL,
  output_signature_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN(
    'rendered','qa_blocked','awaiting_review','rejected','approved','published'
  )),
  change_summary TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id, version_no)
);

CREATE TABLE rework_requests(
  rework_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  source_candidate_id TEXT NOT NULL REFERENCES candidate_versions(candidate_id),
  result_candidate_id TEXT REFERENCES candidate_versions(candidate_id),
  category TEXT NOT NULL CHECK(category IN(
    'script','material','rhythm','subtitle','audio','packaging','technical'
  )),
  notes TEXT NOT NULL,
  position_seconds REAL CHECK(position_seconds IS NULL OR position_seconds >= 0),
  qa_issue_ref TEXT,
  target_stage TEXT NOT NULL,
  impact_json TEXT NOT NULL,
  cost_status TEXT NOT NULL DEFAULT 'none'
    CHECK(cost_status IN('none','confirmation_required','approved')),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN('draft','pending_confirmation','executing','applied','failed','cancelled')),
  idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(task_id, idempotency_key)
);

ALTER TABLE publish_records ADD COLUMN candidate_id TEXT
  REFERENCES candidate_versions(candidate_id);
ALTER TABLE publish_records ADD COLUMN package_status TEXT NOT NULL DEFAULT 'current'
  CHECK(package_status IN('current','expired'));
```

Add indexes on `(task_id, version_no DESC)`, `(task_id, status, created_at)` and `(task_id, source_candidate_id, status)`. `CatalogDatabase.migrate()` owns the schema version insert.

- [ ] **Step 4: Implement candidate repository normalization and idempotency**

```python
def ensure_candidate_version(db, task_id, render_snapshot, *, change_summary=None):
    mix_id = _required(render_snapshot, "mix_snapshot_id")
    with db.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM candidate_versions WHERE mix_snapshot_id=?", (mix_id,)
        ).fetchone()
        if existing is not None:
            return _public_candidate(connection, existing)
        version_no = connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM candidate_versions WHERE task_id=?",
            (task_id,),
        ).fetchone()[0]
        candidate_id = f"candidate-{uuid.uuid4().hex[:16]}"
        connection.execute(
            """INSERT INTO candidate_versions(
              candidate_id,task_id,version_no,mix_snapshot_id,input_digest,
              output_path,output_signature_json,status,change_summary
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id, task_id, version_no, mix_id,
                _required(render_snapshot, "dependency_digest"),
                _required(render_snapshot, "output_path"),
                json.dumps(_required(render_snapshot, "output_signature"), sort_keys=True),
                "rendered", change_summary,
            ),
        )
    return get_candidate(db, task_id, candidate_id)
```

`attach_candidate_qa()` must require the report belongs to the same task and output path. `status='awaiting_review'` requires both a passed report and a QA snapshot; a blocked report stays `qa_blocked` with a null QA snapshot.

- [ ] **Step 5: Run candidate/schema tests**

Run: `.venv\Scripts\python -m unittest tests.test_candidate_versions tests.test_workbench_schema -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add media_catalog/migrations/035_candidate_versions_and_rework.sql media_catalog/candidate_versions.py tests/test_candidate_versions.py tests/test_workbench_schema.py
git commit -m "feat: persist candidate versions and rework requests"
```

### Task 2: Bind render, QA, history, and media integrity to candidates

**Files:**
- Modify: `media_catalog/assemble.py:207-237`
- Modify: `media_catalog/snapshots.py:65-216`
- Modify: `media_catalog/candidate_versions.py`
- Modify: `workbench/routes.py:273-308`
- Test: `tests/test_candidate_versions.py`
- Test: `tests/test_full_review.py`
- Test: `tests/test_workbench_server.py`

**Interfaces:**
- Consumes: Task 1 candidate repository and existing `record_render_dependency_snapshot()` / `record_qa_review_snapshot()`.
- Produces: `record_candidate_qa(db, task_id, render_snapshot, qa_result, qa_snapshot_id=None) -> dict`, `resolve_candidate_media(db, task_id, candidate_id) -> Path`, candidate-enriched `latest_review_candidate()`.

- [ ] **Step 1: Write failing lifecycle and media tests**

```python
def test_passed_qa_attaches_snapshot_and_latest_candidate_identity(self):
    result = self.finish_render_qa(status="passed")
    current = latest_review_candidate(self.db, "task-1")
    self.assertEqual(current["candidate_id"], result["candidate_id"])
    self.assertEqual(current["version_label"], "candidate-v1")
    self.assertEqual(current["status"], "awaiting_review")

def test_new_candidate_does_not_reuse_completed_watch(self):
    old = self.create_passed_candidate("old.mp4", "digest-1")
    self.complete_watch(old)
    new = self.create_passed_candidate("new.mp4", "digest-2")
    self.assertIsNone(latest_watch_session(
        self.db, "task-1", new["qa_snapshot_id"], new["qa_report_id"]
    ))

def test_history_media_rejects_tampered_or_cross_task_candidate(self):
    candidate = self.create_passed_candidate("one.mp4", "digest-1")
    Path(candidate["output_path"]).write_bytes(b"tampered")
    with self.assertRaisesRegex(RuntimeError, "CANDIDATE_MEDIA_CHANGED"):
        resolve_candidate_media(self.db, "task-1", candidate["candidate_id"])
```

- [ ] **Step 2: Run targeted tests and verify candidate lifecycle assertions fail**

Run: `.venv\Scripts\python -m unittest tests.test_candidate_versions tests.test_full_review tests.test_workbench_server -v`

Expected: FAIL because render/QA does not create candidate rows and history media resolution is absent.

- [ ] **Step 3: Register every render result before QA gating**

```python
def _finish_render_qa(db, task_id: str, render_snapshot: dict) -> dict:
    rendered = Path(render_snapshot["output_path"]).resolve()
    qa_result = run_qa(db, task_id, str(rendered))
    candidate = ensure_candidate_version(db, task_id, render_snapshot)
    if qa_result["status"] != "passed" or not _qa_has_video_evidence(qa_result):
        candidate = attach_candidate_qa(db, candidate["candidate_id"], qa_result)
        return _result(
            task_id, "render", "qa_blocked", gate_status(db, task_id),
            blocked_by=["qa_passed"], qa={
                "report_id": qa_result["report_id"],
                "status": qa_result["status"],
                "blocking": qa_result["blocking_count"],
            },
            snapshots=render_snapshot, candidate=candidate,
            output_path=str(rendered),
            message="渲染完成但 QA 存在阻断，不能进入人工审片。",
        )
    snapshots = record_qa_review_snapshot(db, task_id, render_snapshot, qa_result)
    candidate = attach_candidate_qa(
        db, candidate["candidate_id"], qa_result,
        qa_snapshot_id=snapshots["qa_snapshot_id"],
    )
    return _result(
        task_id, "render", "awaiting_human_review", gate_status(db, task_id),
        blocked_by=["human_reviewed"], qa={
            "report_id": qa_result["report_id"], "status": qa_result["status"],
            "blocking": qa_result["blocking_count"],
        },
        snapshots={**snapshots, **candidate}, candidate=candidate,
        output_path=str(rendered),
        message="渲染和 QA 已完成，等待人工完整审片。",
    )
```

Keep existing status/message contracts. Do not create a QA snapshot for a blocking report.

- [ ] **Step 4: Enrich trusted candidate projection and add signed history media route**

`latest_review_candidate()` looks up `candidate_versions.mix_snapshot_id` and adds `candidate_id`, `version_no`, `version_label`, `status`, `source_candidate_id`, and `change_summary`. `resolve_candidate_media()` requires task ownership, containment beneath the recorded task output directory, existence, and exact stored `file_content_signature()`.

Extend the authenticated route to accept both:

```text
/api/tasks/<task-id>/review-media
/api/tasks/<task-id>/review-media/<candidate-id>
```

The legacy URL resolves the current reviewable candidate; the versioned URL resolves the exact read-only history item.

- [ ] **Step 5: Run lifecycle/media tests**

Run: `.venv\Scripts\python -m unittest tests.test_candidate_versions tests.test_full_review tests.test_workbench_server -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add media_catalog/assemble.py media_catalog/snapshots.py media_catalog/candidate_versions.py workbench/routes.py tests/test_candidate_versions.py tests/test_full_review.py tests/test_workbench_server.py
git commit -m "feat: bind render and QA to candidate history"
```

### Task 3: Create categorized rework and local workflow rollback

**Files:**
- Create: `media_catalog/rework.py`
- Modify: `media_catalog/reviews.py:43-130`
- Modify: `media_catalog/workflow.py:220-270`
- Test: `tests/test_rework_requests.py`
- Test: `tests/test_review_decisions.py`

**Interfaces:**
- Consumes: candidate repository, `WORKFLOW_STAGES`, task stage rows, and candidate-bound review validation.
- Produces: `preview_rework_impact(category: str, *, technical_scope: str | None = None) -> dict`, `reject_with_rework(db, task_id: str, candidate_id: str, *, reviewed_by: str, category: str, notes: str, position_seconds: float | None = None, qa_issue_ref: str | None = None, idempotency_key: str) -> dict`, `get_rework_request(db, task_id: str, rework_id: str) -> dict | None`, `list_rework_requests(db, task_id: str) -> list[dict]`, `execute_rework(db, task_id: str, rework_id: str, *, idempotency_key: str) -> dict`.

- [ ] **Step 1: Write failing category, atomicity, and idempotency tests**

```python
def test_categories_map_to_smallest_safe_boundary(self):
    expected = {
        "script": "brief_lock", "material": "ai_storyboard",
        "rhythm": "rough_cut", "subtitle": "packaging_confirm",
        "audio": "packaging_confirm", "packaging": "packaging_confirm",
        "technical": "fine_render",
    }
    self.assertEqual(
        {key: preview_rework_impact(key)["target_stage"] for key in expected},
        expected,
    )

def test_reject_creates_decision_and_rework_atomically(self):
    result = reject_with_rework(
        self.db, "task-1", self.candidate["candidate_id"],
        reviewed_by="Peter", category="subtitle", notes="字幕慢半拍",
        position_seconds=12.5, idempotency_key="reject-1",
    )
    self.assertEqual(result["review"]["decision"], "rejected")
    self.assertEqual(result["rework"]["status"], "pending_confirmation")
    self.assertEqual(result["rework"]["target_stage"], "packaging_confirm")

def test_execute_is_idempotent_and_invalidates_only_downstream(self):
    first = execute_rework(self.db, "task-1", self.rework_id, idempotency_key="run-1")
    replay = execute_rework(self.db, "task-1", self.rework_id, idempotency_key="run-1")
    self.assertEqual(first["rework_id"], replay["rework_id"])
    states = self.stage_states()
    self.assertEqual(states["packaging_confirm"], "ready")
    self.assertEqual(states["fine_render"], "invalidated")
    self.assertEqual(states["picture_lock"], "completed")
```

- [ ] **Step 2: Run tests and verify missing rework service fails**

Run: `.venv\Scripts\python -m unittest tests.test_rework_requests tests.test_review_decisions -v`

Expected: FAIL because `media_catalog.rework` and atomic categorized rejection are absent.

- [ ] **Step 3: Implement literal category mappings and impact payloads**

```python
REWORK_BOUNDARIES = {
    "script": ("brief_lock", True),
    "material": ("ai_storyboard", True),
    "rhythm": ("rough_cut", False),
    "subtitle": ("packaging_confirm", False),
    "audio": ("packaging_confirm", False),
    "packaging": ("packaging_confirm", False),
    "technical": ("fine_render", False),
}

def preview_rework_impact(category, *, technical_scope=None):
    if category not in REWORK_BOUNDARIES:
        raise ValueError("INVALID_REWORK_CATEGORY: 未知返工分类")
    target, may_cost = REWORK_BOUNDARIES[category]
    if category == "technical" and technical_scope == "qa_only":
        target = "auto_qa"
    index = WORKFLOW_STAGES.index(target)
    return {
        "category": category,
        "target_stage": target,
        "preserved_stages": list(WORKFLOW_STAGES[:index]),
        "invalidated_stages": list(WORKFLOW_STAGES[index + 1:]),
        "rerun_stages": list(WORKFLOW_STAGES[index:]),
        "cost_confirmation_required": may_cost,
    }
```

- [ ] **Step 4: Implement atomic reject/create and confirmed rollback**

Extract a private `insert_review_decision(connection, db, task_id: str, snapshot_id: str, qa_report_id: str, decision: str, reviewed_by: str, notes: str | None, watch_session_id: str | None) -> dict` from `record_review()` so `reject_with_rework()` can validate the exact candidate, insert the immutable rejection, and insert one rework row inside one `BEGIN IMMEDIATE` transaction.

`execute_rework()` must:

1. Re-read the pending rework under `BEGIN IMMEDIATE`.
2. Return the existing applied row for the same idempotency key.
3. Refuse a different execution key after application.
4. Refuse `cost_status='confirmation_required'` until it is explicitly approved.
5. Set the target stage to `ready` and every downstream stage to `invalidated` with the rework ID as reason.
6. Mark the source candidate `rejected` and the rework `applied`.
7. Never enqueue rendering or call an external provider.

- [ ] **Step 5: Run rework/review tests**

Run: `.venv\Scripts\python -m unittest tests.test_rework_requests tests.test_review_decisions -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add media_catalog/rework.py media_catalog/reviews.py media_catalog/workflow.py tests/test_rework_requests.py tests/test_review_decisions.py
git commit -m "feat: add categorized review rework"
```

### Task 4: Bind publish packages to candidates and expire them on rework

**Files:**
- Modify: `media_catalog/publish.py:230-464`
- Modify: `media_catalog/rework.py`
- Modify: `media_catalog/workflow.py:320-401`
- Test: `tests/test_publish_package.py`
- Test: `tests/test_publish_reentry.py`
- Test: `tests/test_rework_requests.py`

**Interfaces:**
- Consumes: Task 1 `candidate_versions`, Task 3 confirmed rework, existing `build_publish_package()` content idempotency.
- Produces: candidate-bound publish records and `list_publish()` fields `candidate_id`, `candidate_label`, `package_status`, `is_current`.

- [ ] **Step 1: Write failing publish binding and expiry tests**

```python
def test_publish_record_binds_exact_approved_candidate(self):
    package = self.build_package()
    row = self.db.connection.execute(
        "SELECT candidate_id,package_status FROM publish_records WHERE record_id=?",
        (package["record_id"],),
    ).fetchone()
    self.assertEqual(row["candidate_id"], self.candidate["candidate_id"])
    self.assertEqual(row["package_status"], "current")

def test_confirmed_rework_expires_but_does_not_delete_old_package(self):
    package = self.build_package()
    self.execute_packaging_rework()
    listed = list_publish(self.db, "task-1")
    self.assertEqual(listed[0]["record_id"], package["record_id"])
    self.assertEqual(listed[0]["package_status"], "expired")
    self.assertTrue(all(Path(path).is_file() for path in listed[0]["files"]))

def test_old_package_cannot_be_reused_for_new_candidate(self):
    old = self.build_package()
    self.create_and_approve_new_candidate()
    new = self.build_package()
    self.assertNotEqual(old["record_id"], new["record_id"])
    self.assertNotEqual(old["candidate_id"], new["candidate_id"])
```

- [ ] **Step 2: Run publish tests and verify candidate columns/expiry assertions fail**

Run: `.venv\Scripts\python -m unittest tests.test_publish_package tests.test_publish_reentry tests.test_rework_requests -v`

Expected: FAIL because publish records are not candidate-bound and rework does not expire packages.

- [ ] **Step 3: Include candidate identity in publish digest, package manifest, and insert**

```python
candidate = latest_review_candidate(db, task_id)
candidate_version = candidate_for_snapshot(db, task_id, candidate["qa_snapshot_id"])
request["candidate_id"] = candidate_version["candidate_id"]

package_check.update({
    "candidate_id": candidate_version["candidate_id"],
    "candidate_label": candidate_version["version_label"],
})

connection.execute(
    """INSERT INTO publish_records(
      record_id,task_id,platform,title,description,tags,cover_path,
      sensitive_hits,status,candidate_id,package_status
    ) VALUES(?,?,?,?,?,?,?,?,?,?, 'current')""",
    values,
)
```

The reuse path must verify both `candidate_id` and stored file signatures. `list_publish()` joins candidate versions and exposes labels without trusting filesystem names.

- [ ] **Step 4: Expire packages transactionally when confirmed rework changes content**

Inside `execute_rework()` update only packages bound to the source candidate:

```sql
UPDATE publish_records
SET package_status='expired'
WHERE task_id=? AND candidate_id=? AND package_status='current';
```

The workflow projector treats only `package_status='current'` on the currently approved candidate as completed.

- [ ] **Step 5: Run publish/rework tests**

Run: `.venv\Scripts\python -m unittest tests.test_publish_package tests.test_publish_reentry tests.test_rework_requests -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add media_catalog/publish.py media_catalog/rework.py media_catalog/workflow.py tests/test_publish_package.py tests/test_publish_reentry.py tests/test_rework_requests.py
git commit -m "feat: bind publish packages to approved candidates"
```

### Task 5: Expose candidate, QA, rework, and publish contracts through the workbench API

**Files:**
- Modify: `workbench/api.py:1000-1190`
- Modify: `media_catalog/candidate_versions.py`
- Modify: `media_catalog/rework.py`
- Test: `tests/test_workbench_api.py`
- Test: `tests/test_review_api_regressions.py`

**Interfaces:**
- Consumes: Tasks 1-4 repository/service functions.
- Produces: task detail fields `candidateVersions`, `currentCandidate`, `reworkRequests`; POST actions `/review/rework/preview`, `/review/rework`, `/review/rework/<id>/execute`; candidate-explicit review and publish validation.

- [ ] **Step 1: Write failing API contract tests against real database state**

```python
def test_task_detail_projects_candidate_history_qa_and_expired_packages(self):
    status, _, detail = self.request("GET", "/api/tasks/task-1")
    self.assertEqual(status, 200)
    self.assertEqual(detail["currentCandidate"]["versionLabel"], "candidate-v2")
    self.assertEqual(detail["candidateVersions"][0]["qa"]["blockingCount"], 0)
    self.assertEqual(detail["publishPackages"][0]["packageStatus"], "expired")

def test_rework_preview_save_and_execute_are_distinct(self):
    preview = self.request("POST", "/api/tasks/task-1/review/rework/preview", {
        "category": "subtitle",
    })[2]
    self.assertEqual(preview["targetStage"], "packaging_confirm")
    saved = self.request("POST", "/api/tasks/task-1/review/rework", {
        "candidateId": self.candidate_id, "category": "subtitle",
        "notes": "字幕慢半拍", "positionSeconds": 12.5,
        "reviewedBy": "Peter", "idempotencyKey": "reject-1",
    })[2]
    self.assertEqual(saved["rework"]["status"], "pending_confirmation")
    self.assertNotEqual(self.stage("fine_render"), "invalidated")
    executed = self.request(
        "POST", f"/api/tasks/task-1/review/rework/{saved['rework']['reworkId']}/execute",
        {"idempotencyKey": "run-1"},
    )[2]
    self.assertEqual(executed["status"], "applied")

def test_candidate_mismatch_cannot_approve_or_publish(self):
    status, _, body = self.request("POST", "/api/tasks/task-1/review/approve", {
        "candidateId": self.old_candidate_id,
        "snapshotId": self.new_snapshot_id,
        "qaReportId": self.new_qa_id,
        "watchSessionId": self.old_watch_id,
        "reviewedBy": "Peter",
    })
    self.assertEqual((status, body["error"]["code"]), (409, "CANDIDATE_BINDING_MISMATCH"))
```

- [ ] **Step 2: Run API tests and verify missing fields/routes fail**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api tests.test_review_api_regressions -v`

Expected: FAIL because candidate history and rework routes are absent.

- [ ] **Step 3: Add public projections with camelCase boundary fields**

Candidate payloads include:

```json
{
  "candidateId": "candidate-0000000000000002",
  "versionNo": 2,
  "versionLabel": "candidate-v2",
  "sourceCandidateId": "candidate-0000000000000001",
  "status": "awaiting_review",
  "changeSummary": "修正字幕时间",
  "mediaUrl": "/api/tasks/task-1/review-media/candidate-0000000000000002",
  "qa": {
    "reportId": "qa-000000000002",
    "status": "passed",
    "revisionNo": 1,
    "blockingCount": 0,
    "warningCount": 1,
    "issues": []
  }
}
```

Do not expose absolute output paths. Normalize malformed legacy `report_json` into a safe unavailable QA projection instead of crashing task detail.

- [ ] **Step 4: Add rework routes and candidate-explicit gate checks**

`/review/rework/preview` is read-like and writes nothing. `/review/rework` records the rejection and pending request. `/review/rework/<id>/execute` applies the precomputed impact only after checking request ownership and idempotency.

Require `candidateId` on the enhanced approve, rework and publish paths. Before delegating, verify it matches the supplied snapshot/report and the current candidate; legacy approve payloads remain accepted only when they unambiguously refer to the current candidate.

- [ ] **Step 5: Run API tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api tests.test_review_api_regressions -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```bash
git add workbench/api.py media_catalog/candidate_versions.py media_catalog/rework.py tests/test_workbench_api.py tests/test_review_api_regressions.py
git commit -m "feat: expose candidate review closure API"
```

### Task 6: Upgrade the review and publish workspace UI

**Files:**
- Create: `web-ui/public/review-closure.mjs`
- Create: `tests/review-closure-ui.test.mjs`
- Modify: `web-ui/public/review-view.mjs`
- Modify: `web-ui/public/publish-view.mjs`
- Modify: `web-ui/public/app.mjs:1550-1750`
- Modify: `web-ui/public/styles.css`
- Modify: `package.json`
- Test: `tests/workbench-ui.test.mjs`

**Interfaces:**
- Consumes: Task 5 camelCase task detail and rework routes.
- Produces: `buildCandidateHistory(detail, selectedId)`, `groupQaIssues(candidate)`, `buildReworkDraft(candidate, values)`, `reviewGate(candidate, watchSession, workflow)`, `buildPublishPackageView(value, currentCandidateId)`.

- [ ] **Step 1: Write failing pure-model and DOM-source behavior tests**

```javascript
test("QA blocked candidate is visible but cannot start or approve review", () => {
  const candidate = {
    candidateId: "c1", versionLabel: "candidate-v1", status: "qa_blocked",
    qa: { blockingCount: 1, warningCount: 0, issues: [
      { severity: "blocking", check: "black", detail: "黑帧", startSeconds: 3 },
    ] },
  };
  assert.deepEqual(reviewGate(candidate, null, {}), {
    canStartWatch: false, canApprove: false, blockingReason: "QA_BLOCKED",
  });
  assert.equal(groupQaIssues(candidate).blocking.length, 1);
});

test("old candidate stays read-only and uses versioned media URL", () => {
  const history = buildCandidateHistory({
    currentCandidate: { candidateId: "c2" },
    candidateVersions: [
      { candidateId: "c2", versionLabel: "candidate-v2", mediaUrl: "/c2" },
      { candidateId: "c1", versionLabel: "candidate-v1", mediaUrl: "/c1" },
    ],
  }, "c1");
  assert.equal(history.selected.readOnly, true);
  assert.equal(history.selected.mediaUrl, "/c1");
});

test("rework draft carries category position and explicit execute semantics", () => {
  assert.deepEqual(buildReworkDraft({ candidateId: "c2" }, {
    category: "subtitle", notes: "字幕慢半拍", positionSeconds: 12.5,
  }), {
    candidateId: "c2", category: "subtitle", notes: "字幕慢半拍",
    positionSeconds: 12.5,
  });
});
```

Also assert `app.mjs` renders the four headings `精剪候选版本`, `自动 QA`, `完整审片`, `发布包`, calls the preview route before execution, and exposes separate buttons `保存返工单` / `确认返工并回到对应阶段`.

- [ ] **Step 2: Run web tests and verify module/import behavior fails**

Run: `node --test --test-concurrency=1 tests/review-closure-ui.test.mjs tests/workbench-ui.test.mjs`

Expected: FAIL because `review-closure.mjs` and the new workspace behavior do not exist.

- [ ] **Step 3: Implement pure UI models**

```javascript
export function reviewGate(candidate, watchSession, workflow = {}) {
  if (!candidate) return { canStartWatch: false, canApprove: false, blockingReason: "NO_CANDIDATE" };
  if (candidate.qa?.blockingCount > 0 || candidate.status === "qa_blocked") {
    return { canStartWatch: false, canApprove: false, blockingReason: "QA_BLOCKED" };
  }
  const completed = Boolean(watchSession?.completedAt && watchSession?.canApprove);
  return {
    canStartWatch: candidate.status === "awaiting_review",
    canApprove: candidate.status === "awaiting_review" && completed,
    blockingReason: completed ? null : "FULL_WATCH_REQUIRED",
  };
}
```

`groupQaIssues()` must normalize unknown severities to warnings. `buildCandidateHistory()` marks every non-current candidate read-only. `buildReworkDraft()` rejects blank notes, negative positions and unsupported categories before network calls.

- [ ] **Step 4: Refactor the existing combined panel into four status sections**

Keep the existing player and playback watch controller. Add:

- Candidate selector with version, source, timestamp and read-only history badge.
- Blocking/warning/passed QA groups and seek buttons using issue time ranges.
- Candidate-specific watch progress and candidate ID in approve payload.
- Rework dialog with category, notes, current playback position, impact preview, preserved/invalidated lists and cost warning.
- Separate save and execute buttons with pending state and duplicate-click disabling.
- Publish rows with candidate label and `已过期` badge; only current approved candidate enables the publish form.

Use existing `taskPost()`, `showActionError()` and render refresh flow. A failed request keeps the form values and offers retry.

- [ ] **Step 5: Register the new web test and run UI suites**

Add `tests/review-closure-ui.test.mjs` to `npm run test:web`.

Run: `npm run test:web`

Expected: PASS with the new review closure tests and existing workbench/ad UI tests.

- [ ] **Step 6: Commit Task 6**

```bash
git add web-ui/public/review-closure.mjs web-ui/public/review-view.mjs web-ui/public/publish-view.mjs web-ui/public/app.mjs web-ui/public/styles.css tests/review-closure-ui.test.mjs tests/workbench-ui.test.mjs package.json
git commit -m "feat: add candidate review and rework workspace"
```

### Task 7: Add end-to-end regression evidence and acceptance handoff

**Files:**
- Modify: `tests/test_workbench_actions.py`
- Modify: `tests/test_workbench_api.py`
- Modify: `tests/workbench-ui.test.mjs`
- Create: `docs/validation/2026-09-21-digital-human-review-publish-closure-acceptance.md`

**Interfaces:**
- Consumes: Tasks 1-6 complete backend/API/UI closure.
- Produces: one automated local closure scenario and a truthful manual acceptance checklist for the real digital-human task.

- [ ] **Step 1: Write the failing automated closure scenario**

```python
def test_digital_human_rework_creates_isolated_second_candidate_and_package(self):
    first = self.render_passed_candidate(input_digest="digest-v1")
    first_watch = self.complete_realistic_test_watch(first)
    self.approve(first, first_watch)
    first_package = self.publish(first)

    request = self.reject_and_execute(
        first, category="subtitle", notes="字幕时间需调整",
        reject_key="reject-v1", execute_key="execute-v1",
    )
    second = self.render_passed_candidate(input_digest="digest-v2")

    self.assertEqual(second["source_candidate_id"], first["candidate_id"])
    self.assertIsNone(self.watch_for(second))
    self.assertEqual(self.package(first_package)["package_status"], "expired")
    with self.assertRaisesRegex(RuntimeError, "FULL_WATCH_REQUIRED"):
        self.approve(second, first_watch)
```

This test may use the existing server-observed watch helper to exercise the automated gate, but the acceptance document must state that it is not human evidence.

- [ ] **Step 2: Run the focused closure suites and verify the scenario fails before final wiring**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_actions tests.test_workbench_api tests.test_candidate_versions tests.test_rework_requests tests.test_full_review tests.test_publish_package -v`

Expected: FAIL on any missing source-candidate linkage, watch isolation, package expiry, or API refresh wiring.

- [ ] **Step 3: Complete only the missing integration wiring exposed by the scenario**

The second candidate must claim exactly one latest applied rework with `result_candidate_id IS NULL` inside the candidate creation transaction, set `source_candidate_id` and `rework_request_id`, then update that request's `result_candidate_id`. If no applied request exists, create an independent candidate with null lineage. Do not infer lineage from version numbers alone.

- [ ] **Step 4: Write the acceptance report with separate automated and human evidence**

The report records exact commands/results for Python, TypeScript and Web suites. Its manual section remains unchecked until a person performs:

```text
打开 candidate-v1 → 完整观看 → 分类驳回
→ 确认局部返工 → 产生 candidate-v2
→ 确认 v2 观看进度为 0 → 完整观看 v2
→ 批准 → 生成并检查绑定 v2 的发布包
```

Do not mark human viewing complete from unit tests or database writes.

- [ ] **Step 5: Run all project verification**

Run:

```powershell
.venv\Scripts\python -m unittest discover -s tests -p "test_*.py" -v
npm run test:ts
npm run test:web
npx tsc --noEmit
```

Expected: all Python, TypeScript and Web tests PASS; TypeScript compilation exits 0.

- [ ] **Step 6: Commit Task 7**

```bash
git add tests/test_workbench_actions.py tests/test_workbench_api.py tests/workbench-ui.test.mjs docs/validation/2026-09-21-digital-human-review-publish-closure-acceptance.md
git commit -m "test: verify digital-human review publish closure"
```

---

## Final Review and Delivery

- Generate the executing-plans review package from the branch merge base through `HEAD`.
- Request one fresh whole-branch code review against the design, this plan, Review Focus items, and execution ledger.
- Fix Critical/Important findings once with RED→GREEN tests and full-suite verification; record Minor findings without opportunistic scope expansion.
- Use `superpowers:finishing-a-development-branch` after review and verification. Do not merge, push, publish externally, delete worktrees, or run a paid generation step without the corresponding user authorization.
