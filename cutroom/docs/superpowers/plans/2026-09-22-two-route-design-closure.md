# Digital Human and Ad Route Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the remaining design gaps shared by the digital-human and ad-mix routes while keeping the park route unavailable.

**Architecture:** Extend the existing vanilla-JS workbench, `WorkbenchAPI`, SQLite ledger, and durable worker rather than creating another workflow. New behavior lives in focused domain modules; API projections remain the boundary between persisted facts and UI labels. Existing rough-cut, packaging-preview, candidate, review, rework, and publish-binding behavior remains authoritative.

**Tech Stack:** Python 3, SQLite migrations, Node.js ESM tests, vanilla HTML/CSS/JavaScript, FFmpeg/ffprobe, TypeScript render scripts.

**Spec:** `docs/superpowers/specs/2026-09-22-two-route-design-closure.md`

## Global Constraints

- Supported production routes in this plan are exactly `digital_human` and `ad_mix`; `park_video` remains unavailable.
- Unknown estimates remain `null`/`unknown`; no missing measurement is converted to zero or pass.
- AI/retrieval results are candidates and never overwrite a confirmed decision without an explicit apply action.
- Pausing is checkpoint-safe; paid/provider-unknown requests are never silently repeated.
- Publish output remains local and is atomically assembled; no external platform upload is added.
- Existing API fields keep their meaning; additions are backward compatible for browser and Agent MCP consumers.
- UI changes preserve the current Native workbench layout, keyboard focus, visible labels, loading feedback, and empty-state guidance.
- Human usability testing, on-site acceptance, park-route implementation, Git tags, releases, and installers are outside this plan.

## Review Focus

- Empty search/filter combinations show a useful empty state and never mutate the underlying task list; Task 1 pins this.
- Offline or changed task-scoped assets stay visible but cannot become replacement candidates; Tasks 2 and 3 pin this.
- Pausing a running paid job after dispatch never replays an unknown provider call; Task 4 pins this.
- Missing subtitle/loudness/provenance evidence blocks or reports unknown instead of passing; Task 5 pins this.
- Missing editable components are listed as `not_available` and never represented by fake files; Task 6 pins this.

---

### Task 1: Task discovery and complete preflight presentation

**Files:**
- Create: `web-ui/public/task-center-controls.mjs`
- Create: `web-ui/public/preflight-view.mjs`
- Modify: `web-ui/public/state.mjs`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/styles.css`
- Test: `tests/workbench-ui.test.mjs`

**Interfaces:**
- Consumes: existing task summary objects and existing `preflight_digital_task` / `preflight_ad_task` response contracts.
- Produces: `filterAndSortTasks(tasks, controls) -> Task[]` and `buildPreflightView(result) -> {metrics, confirmations, notices}`.

- [ ] **Step 1: Write failing task-center behavior tests**

```javascript
test("task controls search title/id, filter route/status, and sort newest", () => {
  const visible = filterAndSortTasks(FIXTURE_TASKS, {
    query: "ad-02", route: "ad_mix", status: "queued", sort: "updated_desc",
  });
  assert.deepEqual(visible.map((item) => item.taskId), ["ad-02"]);
});

test("task controls preserve input and return an actionable empty state", () => {
  const result = buildTaskSearchResult(FIXTURE_TASKS, { query: "不存在" });
  assert.deepEqual(result.tasks, []);
  assert.match(result.emptyMessage, /清除筛选|换个关键词/);
  assert.equal(FIXTURE_TASKS.length, 3);
});
```

- [ ] **Step 2: Run the task-center tests and verify RED**

Run: `node --test --test-name-pattern="task controls" tests/workbench-ui.test.mjs`

Expected: FAIL because `task-center-controls.mjs` does not exist.

- [ ] **Step 3: Implement pure filtering/sorting and wire accessible controls**

```javascript
export function filterAndSortTasks(tasks, controls = {}) {
  const query = String(controls.query || "").trim().toLocaleLowerCase("zh-CN");
  const filtered = tasks.filter((task) => {
    const text = `${task.title} ${task.taskId}`.toLocaleLowerCase("zh-CN");
    return (!query || text.includes(query))
      && (!controls.route || task.route === controls.route)
      && (!controls.status || task.runStatus === controls.status);
  });
  return filtered.toSorted((left, right) =>
    controls.sort === "updated_asc"
      ? String(left.updatedAt || "").localeCompare(String(right.updatedAt || ""))
      : String(right.updatedAt || "").localeCompare(String(left.updatedAt || "")));
}
```

Add labeled search/select controls before the board/list switch, preserve visible focus, update board/list/quick-task views from the same filtered array, and show a reset action when no task matches.

- [ ] **Step 4: Write failing preflight projection tests**

```javascript
test("preflight view keeps unknown values and exposes all four confirmations", () => {
  const view = buildPreflightView(PREFLIGHT_WITH_UNKNOWN_ESTIMATE);
  assert.equal(view.metrics.machineTime.value, "暂无法估算");
  assert.equal(view.metrics.machineTime.status, "unknown");
  assert.equal(view.confirmations.length, 4);
  assert.match(view.notices.join(" "), /真实性|隐私|授权/);
});
```

- [ ] **Step 5: Run preflight tests and verify RED**

Run: `node --test --test-name-pattern="preflight view" tests/workbench-ui.test.mjs`

Expected: FAIL because `buildPreflightView` is missing.

- [ ] **Step 6: Implement and render complete preflight data**

`buildPreflightView` must render machine range, human range, disk bytes, API range and excluded costs, queue observation time/wait range, four confirmations, and safety notices. It must render unknown reasons verbatim and never use `|| 0` for nullable measurements.

- [ ] **Step 7: Run focused and full Web tests**

Run: `npm run test:web`

Expected: all Web tests pass.

- [ ] **Step 8: Commit Task 1**

```text
feat: complete task discovery and preflight views
```

### Task 2: Searchable shot-material index

**Files:**
- Create: `media_catalog/asset_index.py`
- Create: `web-ui/public/material-index.mjs`
- Modify: `workbench/api.py`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/styles.css`
- Test: `tests/test_workbench_api.py`
- Test: `tests/workbench-ui.test.mjs`

**Interfaces:**
- Consumes: `assets`, `asset_locations`, `libraries`, `preview_cache`, `analysis_runs`, and optional task source membership.
- Produces: `list_asset_index(db, *, task_id=None, query=None, library_id=None, media_kind=None, availability=None, limit=100, offset=0) -> dict` and `GET /api/assets`.

- [ ] **Step 1: Write failing backend index tests**

```python
def test_asset_index_filters_without_hiding_offline_records(self):
    result = list_asset_index(
        self.db, query="门头", media_kind="video", availability="offline"
    )
    self.assertEqual([item["assetId"] for item in result["items"]], ["offline-video"])
    self.assertFalse(result["items"][0]["online"])

def test_task_asset_index_excludes_other_task_sources(self):
    result = list_asset_index(self.db, task_id="task-1")
    self.assertNotIn("other-park-asset", {item["assetId"] for item in result["items"]})
```

- [ ] **Step 2: Run backend index tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api.WorkbenchApiTests.test_asset_index_filters_without_hiding_offline_records tests.test_workbench_api.WorkbenchApiTests.test_task_asset_index_excludes_other_task_sources -v`

Expected: FAIL because `media_catalog.asset_index` is missing.

- [ ] **Step 3: Implement the read-only asset index and API route**

Use parameterized SQL. Return `assetId`, `mediaKind`, `sizeBytes`, `fingerprintStatus`, `libraryId`, `libraryName`, `relativePath`, `availability`, `online`, `previewUrl`, `durationSeconds`, and `authorizationStatus`. Existing schemas without authorization metadata return `unknown`, not approved.

The API accepts `taskId`, `query`, `libraryId`, `mediaKind`, `availability`, `limit`, and `offset`; reject malformed limits with `ASSET_INDEX_QUERY_INVALID`.

- [ ] **Step 4: Write failing material-index UI tests**

```javascript
test("material index groups filters and explains offline results", () => {
  const view = buildMaterialIndexView(INDEX_FIXTURE, { availability: "offline" });
  assert.equal(view.items.length, 1);
  assert.match(view.items[0].statusLabel, /离线/);
  assert.equal(view.items[0].canSelect, false);
});
```

- [ ] **Step 5: Run UI test and verify RED**

Run: `node --test --test-name-pattern="material index" tests/workbench-ui.test.mjs`

Expected: FAIL because `material-index.mjs` does not exist.

- [ ] **Step 6: Implement the indexed asset view**

Keep the existing directory cards. Add an “素材索引” section with labeled search/type/directory/status controls, image/video preview where available, source and authorization labels, pagination, `aria-live` result count, and a useful empty state. Offline rows remain visible and disabled.

- [ ] **Step 7: Run focused and full tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api -v`

Run: `npm run test:web`

Expected: both suites pass.

- [ ] **Step 8: Commit Task 2**

```text
feat: add searchable shot material index
```

### Task 3: Scene recommendation rejection, correction, and local re-ranking

**Files:**
- Create: `media_catalog/migrations/036_two_route_closure.sql`
- Create: `media_catalog/storyboard_feedback.py`
- Create: `web-ui/public/storyboard-feedback.mjs`
- Modify: `workbench/api.py`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/styles.css`
- Test: `tests/test_storyboard_feedback.py`
- Test: `tests/workbench-ui.test.mjs`

**Interfaces:**
- Consumes: task-scoped scene cards and the Task 2 asset index.
- Produces: `record_feedback(...)`, `rerank_scene_candidates(...)`, `apply_feedback_candidate(...)`, and task routes `/storyboard/feedback`, `/storyboard/rerank`, `/storyboard/apply-candidate`.

- [ ] **Step 1: Write failing migration and feedback tests**

```python
def test_reject_records_reason_without_changing_scene_card(self):
    before = get_scene_card(self.db, "card-1")
    feedback = record_feedback(self.db, "task-1", "card-1", "rejected", "不要夜景")
    self.assertEqual(feedback["decision"], "rejected")
    self.assertEqual(get_scene_card(self.db, "card-1")["asset_id"], before["asset_id"])

def test_rerank_uses_only_online_assets_inside_frozen_task_scope(self):
    ranked = rerank_scene_candidates(self.db, "task-1", "card-1", "改用白天厂房")
    self.assertEqual([item["assetId"] for item in ranked], ["day-factory"])
```

- [ ] **Step 2: Run feedback tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_storyboard_feedback -v`

Expected: FAIL because the module/table does not exist.

- [ ] **Step 3: Add append-only feedback schema and domain behavior**

Migration 036 creates `storyboard_feedback(feedback_id, task_id, card_id, decision, instruction, original_asset_id, selected_asset_id, candidates_json, created_at, applied_at)` with decision restricted to `rejected/correction/reranked/applied`.

`rerank_scene_candidates` tokenizes the narration, system suggestion, and correction, then ranks only available assets in the task source snapshot. It returns reason, source directory, relative path, preview URL, and warnings. It cannot mutate the scene card.

`apply_feedback_candidate` uses the existing rough-cut edit/impact path so picture-lock invalidation and version history remain centralized.

- [ ] **Step 4: Write failing API/UI tests**

```javascript
test("scene feedback exposes reject correction and apply as separate actions", () => {
  const view = buildStoryboardFeedbackView(SCENE_WITH_CANDIDATES);
  assert.equal(view.actions.map((item) => item.key).join(","), "reject,rerank,apply");
  assert.match(view.candidates[0].reason, /旁白|纠正要求/);
});
```

- [ ] **Step 5: Run API/UI tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api -v`

Run: `node --test --test-name-pattern="scene feedback" tests/workbench-ui.test.mjs`

Expected: FAIL on missing endpoints/helper.

- [ ] **Step 6: Wire the inspector interaction**

Add visible “拒绝当前推荐” and “按要求重荐” controls to the scene inspector. Require a non-empty correction for reranking, display source/reason/warnings before apply, and show success/error feedback. Applying a candidate goes through impact confirmation and never silently overwrites the current scene.

- [ ] **Step 7: Run focused and full tests**

Run: `.venv\Scripts\python -m unittest tests.test_storyboard_feedback tests.test_rough_cut tests.test_workbench_api -v`

Run: `npm run test:web`

Expected: all pass.

- [ ] **Step 8: Commit Task 3**

```text
feat: add scene recommendation correction workflow
```

### Task 4: Checkpoint-safe pause and honest job ETA

**Files:**
- Extend: `media_catalog/migrations/036_two_route_closure.sql`
- Create: `media_catalog/job_timing.py`
- Modify: `media_catalog/workbench_worker.py`
- Modify: `media_catalog/digital_workflow.py`
- Modify: `workbench/api.py`
- Create: `web-ui/public/job-status.mjs`
- Modify: `web-ui/public/app.mjs`
- Test: `tests/test_workbench_worker.py`
- Test: `tests/test_workbench_api.py`
- Test: `tests/workbench-ui.test.mjs`

**Interfaces:**
- Produces: `request_job_pause(db, job_id)`, extended `resume_job`, `job_timing(db, job) -> dict`, `POST /api/jobs/:id/pause`, and public fields `pauseRequested`, `canPause`, `estimatedStartAt`, `estimatedRemainingSeconds`, `estimateStatus`, `observedAt`.

- [ ] **Step 1: Write failing pause-state tests**

```python
def test_queued_job_pauses_immediately_and_resume_requeues_at_tail(self):
    request_job_pause(self.db, "queued-1")
    self.assertEqual(get_job(self.db, "queued-1")["public_status"], "paused")
    resume_job(self.db, "queued-1")
    self.assertEqual(get_job(self.db, "queued-1")["queue_position"], 2)

def test_running_paid_job_pause_waits_for_checkpoint_without_resetting_outcome(self):
    request_job_pause(self.db, "paid-running")
    self.assertTrue(get_job(self.db, "paid-running")["pause_requested"])
    self.assertEqual(read_payload(self.db, "paid-running")["outcome_status"], "in_progress")
```

- [ ] **Step 2: Run pause tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_worker.WorkbenchWorkerTests.test_queued_job_pauses_immediately_and_resume_requeues_at_tail tests.test_workbench_worker.WorkbenchWorkerTests.test_running_paid_job_pause_waits_for_checkpoint_without_resetting_outcome -v`

Expected: FAIL because pause controls do not exist.

- [ ] **Step 3: Implement persisted control state**

Migration 036 creates `job_controls(job_id PRIMARY KEY, state CHECK active/pause_requested/paused, requested_at, paused_at)`. Claim queries exclude non-active controls. Queued pause removes the queue entry and marks paused. Running pause marks `pause_requested`; after `_persist_action_result`, the worker moves the job to a paused checkpoint instead of completing it. Resume rejects provider outcome `unknown`, clears the control, and appends a fresh queue entry.

- [ ] **Step 4: Write failing ETA tests**

```python
def test_eta_uses_completed_same_type_samples_and_progress(self):
    timing = job_timing(self.db, get_job(self.db, "render-running"), observed_at=NOW)
    self.assertEqual(timing["estimate_status"], "estimated")
    self.assertEqual(timing["estimated_remaining_seconds"], 30)

def test_eta_is_unknown_without_valid_samples(self):
    timing = job_timing(self.db, get_job(self.db, "new-kind"), observed_at=NOW)
    self.assertIsNone(timing["estimated_start_at"])
    self.assertEqual(timing["estimate_status"], "unknown")
```

- [ ] **Step 5: Run ETA tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_worker -v`

Expected: FAIL because `job_timing` is missing.

- [ ] **Step 6: Implement robust duration estimates and API projection**

Use the median of up to 20 positive completed durations for the same job type. Running remaining time uses progress ratio when `progress_total > 0`; queued start sums the running remainder and estimates for active jobs ahead. Invalid/missing samples return unknown. Add pause dispatch and precise `canPause/canResume/canCancel` flags.

- [ ] **Step 7: Write failing job-status UI test, then implement**

```javascript
test("job status distinguishes pause requested paused and unknown ETA", () => {
  assert.match(buildJobStatus(PAUSE_REQUESTED).statusDetail, /安全步骤/);
  assert.equal(buildJobStatus(UNKNOWN_ETA).remainingLabel, "剩余时间待估算");
});
```

Run: `node --test --test-name-pattern="job status" tests/workbench-ui.test.mjs`

Expected RED, then add Pause/Resume/Cancel controls with busy states and `aria-live` progress. Run `npm run test:web` and expect PASS.

- [ ] **Step 8: Commit Task 4**

```text
feat: add safe pause and job timing estimates
```

### Task 5: Subtitle, loudness, fingerprint, source, and authorization QA

**Files:**
- Create: `media_catalog/qa_evidence.py`
- Modify: `media_catalog/qa.py`
- Modify: `media_catalog/assemble.py`
- Modify: `media_catalog/ad_actions.py`
- Modify: `media_catalog/workbench_actions.py`
- Test: `tests/test_qa_evidence.py`
- Test: `tests/test_qa_release_gate.py`
- Test: `tests/test_ad_actions.py`

**Interfaces:**
- Produces: `collect_qa_evidence(db, task_id, candidate_inputs)`, `check_subtitle_evidence(...)`, `check_loudness(...)`, and extended `run_qa(..., evidence=None)`.

- [ ] **Step 1: Write failing subtitle-evidence tests**

```python
def test_subtitle_outside_video_range_blocks(self):
    issues = check_subtitle_evidence(self.ass_path, video_duration=10.0, frame_size=(1080, 1920))
    self.assertIssue(issues, "subtitle_timing", "blocking")

def test_missing_required_subtitle_evidence_does_not_pass(self):
    issues = check_subtitle_evidence(None, video_duration=10.0, frame_size=(1080, 1920))
    self.assertIssue(issues, "subtitle_evidence", "blocking")
```

- [ ] **Step 2: Run subtitle tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_qa_evidence.QAEvidenceTests.test_subtitle_outside_video_range_blocks tests.test_qa_evidence.QAEvidenceTests.test_missing_required_subtitle_evidence_does_not_pass -v`

Expected: FAIL because `qa_evidence` is missing.

- [ ] **Step 3: Implement ASS/SRT structural, timing, line, and safe-area checks**

Parse timestamps and ASS style/margins without rendering assumptions. Validate non-negative monotonic intervals, cue end within video duration tolerance, maximum two display lines, non-empty text, and vertical margins inside the configured phone safe region. Malformed or absent required evidence is blocking.

- [ ] **Step 4: Write failing loudness tests**

```python
def test_loudness_parses_ebur128_and_blocks_outside_policy(self):
    issue = check_loudness(FFMPEG_EBUR128_MINUS_31, target_lufs=-16, tolerance_lu=4)
    self.assertEqual(issue[0]["severity"], "blocking")

def test_unreadable_loudness_is_unknown_not_passed(self):
    issue = check_loudness("", target_lufs=-16, tolerance_lu=4)
    self.assertFalse(issue[0]["ok"])
    self.assertEqual(issue[0]["severity"], "blocking")
```

- [ ] **Step 5: Run loudness tests and verify RED, then implement**

Run: `.venv\Scripts\python -m unittest tests.test_qa_evidence -v`

Expected RED, then implement FFmpeg `ebur128=framelog=verbose` parsing for integrated LUFS and true peak with route-neutral policy from the rule payload.

- [ ] **Step 6: Write failing provenance tests**

```python
def test_changed_fingerprint_and_unknown_authorization_block(self):
    issues = collect_qa_evidence(self.db, "task-1", self.changed_inputs)
    self.assertIssue(issues, "material_fingerprint", "blocking")
    self.assertIssue(issues, "material_authorization", "blocking")

def test_other_library_source_cannot_satisfy_task_provenance(self):
    issues = collect_qa_evidence(self.db, "task-1", self.out_of_scope_inputs)
    self.assertIssue(issues, "material_scope", "blocking")
```

- [ ] **Step 7: Run provenance tests and verify RED, then integrate**

Compare actual files with frozen dependency signatures and task source snapshot membership. Packaging authorization uses the confirmed packaging plan; shot authorization defaults to unknown unless evidence explicitly establishes it. Pass evidence from both route assemblers to `run_qa`.

Run: `.venv\Scripts\python -m unittest tests.test_qa_evidence tests.test_qa_release_gate tests.test_ad_actions -v`

Expected: PASS.

- [ ] **Step 8: Commit Task 5**

```text
feat: complete release qa evidence checks
```

### Task 6: Cover choice, tags, and editable delivery package

**Files:**
- Create: `media_catalog/editable_delivery.py`
- Modify: `media_catalog/publish.py`
- Modify: `media_catalog/assemble.py`
- Modify: `media_catalog/ad_actions.py`
- Modify: `media_catalog/workbench_actions.py`
- Modify: `workbench/api.py`
- Modify: `web-ui/public/review-closure.mjs`
- Test: `tests/test_publish_package.py`
- Test: `tests/test_ad_actions.py`
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Produces: `build_delivery_manifest(...)`, expanded `build_publish_package(..., cover_choice="candidate-1", delivery_type="standard")`, and publish request fields `coverChoice`, `externalCoverPath`, `deliveryType`.

- [ ] **Step 1: Write failing cover-choice and tags tests**

```python
def test_selected_cover_and_tags_are_bound_in_manifest_and_record(self):
    package = self.build(cover_choice="candidate-2", tags=["招商", "产业"])
    manifest = self.read_check(package)
    self.assertEqual(Path(package["cover_path"]).name, "cover-02.jpg")
    self.assertEqual(manifest["cover"]["mode"], "candidate")
    self.assertEqual(manifest["tags"], ["招商", "产业"])

def test_pending_cover_is_explicit_and_external_cover_is_copied(self):
    pending = self.build(cover_choice="pending")
    self.assertTrue(Path(pending["package_dir"], "cover-pending.json").is_file())
```

- [ ] **Step 2: Run cover tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_publish_package -v`

Expected: FAIL because cover choice is ignored and pending mode is unsupported.

- [ ] **Step 3: Implement cover modes and dynamic package manifest**

Support `candidate-1/2/3`, `external`, and `pending`. Validate external images before staging. Persist the actual selected/pending path in `publish_records.cover_path`. `publish-check.json` lists all actual files and signatures; reuse compares the complete request including cover mode and tags.

- [ ] **Step 4: Write failing editable delivery tests**

```python
def test_editable_delivery_copies_available_components_and_marks_missing(self):
    package = self.build(delivery_type="editable")
    manifest = json.loads(Path(package["package_dir"], "delivery-manifest.json").read_text("utf-8"))
    self.assertEqual(manifest["components"]["subtitles"]["status"], "included")
    self.assertEqual(manifest["components"]["sfx_stem"]["status"], "not_available")
    self.assertFalse(Path(package["package_dir"], "audio-sfx.wav").exists())

def test_editable_delivery_rejects_component_outside_trusted_roots(self):
    with self.assertRaisesRegex(ValueError, "EDITABLE_COMPONENT_UNTRUSTED"):
        self.build(delivery_type="editable", component_override="C:/outside/file.ass")
```

- [ ] **Step 5: Run editable tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_publish_package -v`

Expected: FAIL because editable delivery does not exist.

- [ ] **Step 6: Implement manifest-driven editable delivery**

Resolve the base video, route voice/original audio, available BGM/effect stems, ASS/SRT, and material timecodes from the approved candidate snapshot and frozen task artifacts. Copy only existing trusted files. Record `included` with signature or `not_available` with a reason. Never synthesize empty files. Assemble in staging and atomically rename with the standard package.

- [ ] **Step 7: Thread new fields through both route actions and API**

Include cover choice, external cover, delivery type, and tags in action input digests so retry/reuse cannot return a package with different publication settings.

Run: `.venv\Scripts\python -m unittest tests.test_publish_package tests.test_ad_actions tests.test_workbench_api -v`

Expected: PASS.

- [ ] **Step 8: Commit Task 6**

```text
feat: add cover selection and editable delivery
```

### Task 7: Publish and job-control UI completion

**Files:**
- Create: `web-ui/public/publish-controls.mjs`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/styles.css`
- Test: `tests/review-closure-ui.test.mjs`
- Test: `tests/workbench-ui.test.mjs`

**Interfaces:**
- Consumes: Task 4 public job fields and Task 6 publish request/package fields.
- Produces: `buildPublishFormValue(form)`, `buildCoverCandidates(candidate)`, and accessible job/publish controls.

- [ ] **Step 1: Write failing publish-control tests**

```javascript
test("publish form keeps tags cover mode and delivery type", () => {
  const value = normalizePublishDraft({
    tags: "招商, 产业，项目", coverChoice: "candidate-2", deliveryType: "editable",
  });
  assert.deepEqual(value.tags, ["招商", "产业", "项目"]);
  assert.equal(value.coverChoice, "candidate-2");
  assert.equal(value.deliveryType, "editable");
});

test("cover candidates expose actual candidate frames and one selected state", () => {
  const covers = buildCoverCandidates({ mediaUrl: "/media/candidate.mp4", duration: 100 }, "candidate-2");
  assert.deepEqual(covers.map((item) => item.time), [15, 50, 85]);
  assert.equal(covers.filter((item) => item.selected).length, 1);
});
```

- [ ] **Step 2: Run UI tests and verify RED**

Run: `node --test --test-name-pattern="publish form|cover candidates" tests/review-closure-ui.test.mjs`

Expected: FAIL because `publish-controls.mjs` is missing.

- [ ] **Step 3: Implement labeled publish controls and frame previews**

Render platform, title, description, comma/Chinese-comma tag input, cover candidate radio cards using the approved candidate video at 15/50/85%, external cover picker, explicit pending-cover option, and standard/editable delivery choice. The submit button sends the exact normalized payload and shows busy/error/success states.

- [ ] **Step 4: Integrate pause/ETA controls in task recovery and service status**

Show current phase, numeric progress where known, queue position, estimated start/remaining label, observed time, Pause/Resume/Cancel buttons from server capabilities, and “当前安全步骤结束后暂停” when requested. Do not rely on color alone.

- [ ] **Step 5: Verify UI behavior and accessibility**

Run: `npm run test:web`

Expected: all Web tests pass. Verify every new control uses native button/input/select, visible label, focus indication, at least 44px interactive height, `aria-busy` during requests, and an explanatory empty/error state.

- [ ] **Step 6: Commit Task 7**

```text
feat: complete publish and job control ui
```

### Task 8: Two-route integration, regression gates, and documentation

**Files:**
- Create: `tests/test_two_route_design_closure.py`
- Modify: `tests/ad-route-ui.test.mjs`
- Modify: `README.md`
- Create: `docs/validation/2026-09-22-two-route-design-closure.md`

**Interfaces:**
- Consumes: all earlier task interfaces.
- Produces: an automated closure report that states software coverage and preserves the excluded human/field/park gates.

- [ ] **Step 1: Write failing end-to-end contract tests**

```python
def test_digital_and_ad_routes_share_required_workbench_capabilities(self):
    for route in ("digital_human", "ad_mix"):
        result = exercise_route_fixture(route)
        self.assertEqual(result["gates"], EXPECTED_FOUR_GATES)
        self.assertTrue(result["qaEvidenceComplete"])
        self.assertIn(result["deliveryType"], {"standard", "editable"})

def test_park_route_remains_unavailable(self):
    response = self.api.dispatch("POST", "/api/tasks/preflight", PARK_REQUEST)
    self.assertEqual(response.status, 400)
    self.assertEqual(response.body["code"], "ROUTE_UNAVAILABLE")
```

- [ ] **Step 2: Run integration tests and verify RED**

Run: `.venv\Scripts\python -m unittest tests.test_two_route_design_closure -v`

Expected: RED until every shared capability and park guard is connected.

- [ ] **Step 3: Complete only the missing integration wiring**

Fix route adapters or API projections revealed by the test. Do not open or implement the park route. Record any plan ruling in the execution ledger before changing the planned interface.

- [ ] **Step 4: Run all verification commands**

Run: `.venv\Scripts\python -m unittest discover -s tests -p "test_*.py" -v`

Run: `npm run test:ts`

Run: `npm run test:web`

Run: `npx tsc --noEmit`

Run: `.venv\Scripts\python -m compileall media_catalog workbench scripts`

Run: `.venv\Scripts\python -m unittest tests.test_release_packaging -v`

Expected: every command exits 0 with no unreported failing test.

- [ ] **Step 5: Run clean-checkout smoke verification**

Create a temporary clean worktree from the feature HEAD, run database migration tests plus the Python, TypeScript, Web, and release-package commands there, then remove only that verified temporary worktree.

- [ ] **Step 6: Write the validation report and README boundary**

The report lists commands and counts from fresh output, completed digital/ad capabilities, and explicit remaining items: park route, human usability testing, colleague field acceptance, and real-site validation. README must not claim the whole 剪辑工作台 design is complete.

- [ ] **Step 7: Commit Task 8**

```text
test: verify two-route design closure
```

- [ ] **Step 8: Request whole-branch review, fix Important findings with TDD, and integrate**

Run a fresh-context whole-branch review against the spec and plan. Reproduce every Important finding with a failing test, fix it, and rerun affected plus full suites. Then fast-forward local `main` to the reviewed feature branch, rerun a proportional post-merge smoke check, and push `main` to `origin`.
