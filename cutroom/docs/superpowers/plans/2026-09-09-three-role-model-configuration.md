# 剪辑工作台 Three-Role Model Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace five user-facing model capabilities with three real roles—planning, B-roll, and TTS—and make provider setup, probing, task selection, and runtime calls work end to end.

**Architecture:** New profiles and tasks use canonical `planning`, `broll`, and `tts` capabilities. Compatibility helpers let new tasks reuse one planning snapshot for copywriting/storyboard/packaging while old tasks keep their exact frozen bindings. A shared endpoint resolver normalizes API roots and full request URLs before both probes and runtime calls.

**Tech Stack:** Python 3.11, SQLite migrations, `unittest`, vanilla ES modules, Node test runner, CSS, Windows Credential Manager.

**Spec:** `docs/superpowers/specs/2026-09-09-three-role-model-configuration-design.md`

## Global Constraints

- Do not expose API keys, credential references, cookies, or task snapshot secrets in logs or responses.
- Do not install React or another UI framework; implement the researched Card, Badge, Combobox, and Alert patterns in existing HTML/CSS/JavaScript.
- Do not issue a paid video-generation request during configuration or tests.
- Preserve existing supplier accounts, keys, old profiles, and historical task bindings.
- New task selectors only list profiles whose real probe succeeded within 24 hours.

---

### Task 1: Shared provider endpoint resolution

**Files:**
- Create: `media_catalog/provider_endpoints.py`
- Modify: `media_catalog/model_profile_probe.py`
- Modify: `media_catalog/model_runtime.py`
- Test: `tests/test_model_profile_probe.py`
- Test: `tests/test_model_runtime.py`

**Interfaces:**
- Produces: `normalize_api_root(base_url: str) -> str` and `provider_endpoint(base_url: str, endpoint: str) -> str`.
- Consumers: model catalog, text probe, OpenAI speech probe, B-roll catalog probe, and runtime environment construction.

- [ ] **Step 1: Write failing URL-resolution tests**

```python
def test_zhipu_v4_root_does_not_gain_v1(self):
    self.assertEqual(
        provider_endpoint("https://open.bigmodel.cn/api/paas/v4", "chat/completions"),
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    )

def test_full_chat_url_is_normalized_before_models_request(self):
    self.assertEqual(
        provider_endpoint(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions", "models"
        ),
        "https://open.bigmodel.cn/api/paas/v4/models",
    )
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_model_profile_probe tests.test_model_runtime -v`

Expected: `/v4/v1/` or duplicate endpoint assertions fail.

- [ ] **Step 3: Implement one resolver and remove both old hard-coded suffix lists**

```python
_TERMINAL_ENDPOINTS = ("/chat/completions", "/models", "/audio/speech")
_VERSION_RE = re.compile(r"/v\d+$", re.IGNORECASE)

def normalize_api_root(base_url: str) -> str:
    base = _validated_http_url(base_url).rstrip("/")
    for suffix in _TERMINAL_ENDPOINTS:
        if base.lower().endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base if _VERSION_RE.search(base) else base + "/v1"

def provider_endpoint(base_url: str, endpoint: str) -> str:
    return normalize_api_root(base_url) + "/" + endpoint.strip("/")
```

- [ ] **Step 4: Run focused probe/runtime tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m unittest tests.test_model_profile_probe tests.test_model_runtime -v`

- [ ] **Step 5: Commit only Task 1 files**

```powershell
git add -- media_catalog/provider_endpoints.py media_catalog/model_profile_probe.py media_catalog/model_runtime.py tests/test_model_profile_probe.py tests/test_model_runtime.py
git commit -m "fix: normalize provider API endpoints"
```

### Task 2: Canonical three-role profiles with legacy compatibility

**Files:**
- Modify: `media_catalog/credentials.py`
- Modify: `media_catalog/model_profiles.py`
- Modify: `media_catalog/model_profile_probe.py`
- Test: `tests/test_model_profiles.py`
- Test: `tests/test_model_profile_probe.py`

**Interfaces:**
- Produces: `CANONICAL_MODEL_ROLES`, `compatible_profile_capabilities(role)`, and role-aware snapshot resolution.
- Consumers: settings bootstrap, task preflight, runtime bindings, and UI grouping.

- [ ] **Step 1: Write failing profile-role tests**

```python
def test_planning_profile_is_a_first_class_visible_capability(self):
    self.assertEqual(CAPABILITY_SPECS["planning"]["label"], "文案策划")

def test_one_planning_snapshot_can_be_selected_for_all_text_usages(self):
    snapshots = resolve_task_profile_snapshots(
        self.db, {"planning": "planning-1"}, required_capabilities=("planning",)
    )
    self.assertEqual(snapshots["planning"]["capability"], "planning")
```

Also assert that a legacy `copywriting` profile may be selected as `planning`, while new lists hide legacy text capabilities.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_model_profiles tests.test_model_profile_probe -v`

- [ ] **Step 3: Add canonical roles and compatibility mapping**

```python
CANONICAL_MODEL_ROLES = ("planning", "broll", "tts")
ROLE_COMPATIBILITY = {
    "planning": ("planning", "copywriting", "storyboard", "packaging", "general_text"),
    "broll": ("broll", "video_generation"),
    "tts": ("tts",),
}
```

Mark legacy capabilities `visible=False`. When binding a legacy profile under a canonical role, write the canonical role into the frozen snapshot and recalculate its digest; never update the source profile row.

- [ ] **Step 4: Make probes dispatch by canonical role**

Treat `planning` as text and `broll` as the existing non-paid video catalog check.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `.venv\Scripts\python.exe -m unittest tests.test_model_profiles tests.test_model_profile_probe -v`

- [ ] **Step 6: Commit only Task 2 files**

```powershell
git add -- media_catalog/credentials.py media_catalog/model_profiles.py media_catalog/model_profile_probe.py tests/test_model_profiles.py tests/test_model_profile_probe.py
git commit -m "feat: add canonical planning broll and tts roles"
```

### Task 3: New task binding and runtime reuse

**Files:**
- Modify: `media_catalog/digital_workflow.py`
- Modify: `media_catalog/ad_workflow.py`
- Modify: `media_catalog/model_profiles.py`
- Modify: `media_catalog/model_runtime.py`
- Modify: `media_catalog/workbench_actions.py`
- Modify: `scripts/plan_brief_runner.py`
- Test: `tests/test_digital_workflow.py`
- Test: `tests/test_ad_workflow.py`
- Test: `tests/test_model_runtime.py`
- Test: `tests/test_workbench_actions.py`
- Test: `tests/test_plan_brief_runner.py`

**Interfaces:**
- Produces: new-task requests with `model_profile_ids.planning`, optional `.broll`, and `.tts`.
- Produces: `get_task_profile_binding_for_usage(db, task_id, usage)` which checks an exact legacy binding first, then canonical role fallback.

- [ ] **Step 1: Write failing new-task and old-task compatibility tests**

```python
def test_digital_creation_freezes_one_planning_profile(self):
    self._configure_planning_profile("plan-1")
    with patch("media_catalog.task_sources.scan_library"):
        created = create_digital_task(
            self.db,
            self.request(model_profile_ids={"planning": "plan-1"}),
            self.root,
            dependencies=self.dependencies(),
        )
    self.assertIsNotNone(get_task_profile_binding(self.db, created["task_id"], "planning"))
    self.assertIsNone(get_task_profile_binding(self.db, created["task_id"], "storyboard"))

def test_storyboard_and_packaging_runtime_reuse_planning_binding(self):
    storyboard_env, _ = task_model_environment(
        self.db,
        "task-runtime",
        "storyboard",
        base_environment={},
        secret_reader=lambda _reference: "test-key",
    )
    packaging_env, _ = task_model_environment(
        self.db,
        "task-runtime",
        "packaging",
        base_environment={},
        secret_reader=lambda _reference: "test-key",
    )
    self.assertEqual(storyboard_env["ARK_MODEL"], packaging_env["ARK_MODEL"])
```

Add an old-task test where exact `storyboard` and `packaging` bindings still win over canonical fallback.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_digital_workflow tests.test_ad_workflow tests.test_model_runtime tests.test_workbench_actions tests.test_plan_brief_runner -v`

- [ ] **Step 3: Require only canonical roles for new tasks**

Digital-human: required `planning`; optional `broll`. Ad mix: required `planning` and `tts`; optional `broll`.

- [ ] **Step 4: Resolve execution usage through exact-first compatibility**

```python
USAGE_ROLE = {
    "copywriting": "planning",
    "storyboard": "planning",
    "packaging": "planning",
    "video_generation": "broll",
}
```

`task_model_environment(db, task_id, "storyboard", base_environment={}, secret_reader=reader)` accepts a frozen `planning` snapshot, exports the same text environment, and reports both requested usage and bound role without altering the snapshot.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run the command from Step 2 and require zero failures.

- [ ] **Step 6: Commit only Task 3 files**

```powershell
git add -- media_catalog/digital_workflow.py media_catalog/ad_workflow.py media_catalog/model_profiles.py media_catalog/model_runtime.py media_catalog/workbench_actions.py scripts/plan_brief_runner.py tests/test_digital_workflow.py tests/test_ad_workflow.py tests/test_model_runtime.py tests/test_workbench_actions.py tests/test_plan_brief_runner.py
git commit -m "feat: reuse planning model across task text stages"
```

### Task 4: Safe configure-test API and profile deletion

**Files:**
- Modify: `media_catalog/model_profiles.py`
- Modify: `workbench/api.py`
- Test: `tests/test_model_profiles.py`
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Produces: `DELETE /api/settings/model-profiles/{profile_id}` with `409 MODEL_PROFILE_IN_USE`.
- Produces: `POST /api/settings/model-profiles/{profile_id}/probe` returning the updated public profile and safe error details.
- Keeps all secret material inside the credential backend.

- [ ] **Step 1: Write failing deletion and error-detail API tests**

```python
def test_unused_profile_can_be_deleted_but_bound_profile_is_blocked(self):
    unused = self._create_profile("profile-unused", "model-unused")
    self.assertEqual(
        delete_model_profile(self.db, unused["profileId"]),
        {"profileId": "profile-unused", "deleted": True},
    )
    bound = self._create_profile("profile-bound", "model-bound")
    mark_profile_check(self.db, bound["profileId"], available=True)
    bind_task_profiles(
        self.db,
        "task-1",
        {"general_text": bound["profileId"]},
        required_capabilities=("general_text",),
    )
    with self.assertRaises(ModelProfileInUse):
        delete_model_profile(self.db, bound["profileId"])

def test_probe_failure_returns_safe_profile_error_details(self):
    self.assertEqual(response.status, 422)
    self.assertEqual(response.json["error"]["code"], "MODEL_OR_ENDPOINT_NOT_FOUND")
    self.assertNotIn("credentialRef", response.text)
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_model_profiles tests.test_workbench_api -v`

- [ ] **Step 3: Implement guarded profile deletion and actionable probe responses**

Return 409 when `task_model_bindings` references the profile. Return safe `lastErrorCode`, `lastErrorMessage`, and `lastCheckedAt` so the UI can show the next action.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the command from Step 2.

- [ ] **Step 5: Commit only Task 4 files**

```powershell
git add -- media_catalog/model_profiles.py workbench/api.py tests/test_model_profiles.py tests/test_workbench_api.py
git commit -m "feat: make model profile lifecycle actionable"
```

### Task 5: Three-card settings UI and direct fields

**Files:**
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/settings-view.mjs`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/styles.css`
- Test: `tests/workbench-ui.test.mjs`
- Test: `tests/workbench-layout.test.mjs`

**Interfaces:**
- Consumes: canonical roles and safe probe responses from Tasks 2 and 4.
- Produces: provider connection cards and three role cards with searchable model selection, inline status, retry, default, and delete actions.

- [ ] **Step 1: Write failing DOM/source tests**

Assert that the settings page contains exactly three role cards labelled `文案策划`, `B-roll 画面`, and `配音`; contains no visible legacy capability cards or `高级选项`; and exposes inline `未检测/检测中/可用/不可用` states.

- [ ] **Step 2: Run web tests and confirm RED**

Run: `node --test tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs`

- [ ] **Step 3: Replace the stacked forms with card-based configuration**

Use semantic form groups and existing element helpers. Provider cards show name, protocol label, URL, default model, and actions. Role cards show one compact editor with provider select, searchable model input/datalist, auto-generated name, `保存并检测`, and existing profiles below it.

- [ ] **Step 4: Remove the advanced disclosure**

Delete the `<details>` control. Render `channelId` only when preset is `new_api`; render `appId` and `cluster` only when preset is `doubao_tts`. These fields are ordinary labelled fields in the active form.

- [ ] **Step 5: Render actionable status and preserve input after failure**

On probe failure, keep the selected account/model and show the safe backend message inside the profile card. On success, refresh bootstrap and mark the profile available without a page reload.

- [ ] **Step 6: Run web tests and confirm GREEN**

Run: `npm run test:web`

- [ ] **Step 7: Commit only Task 5 files**

```powershell
git add -- web-ui/public/index.html web-ui/public/settings-view.mjs web-ui/public/app.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs
git commit -m "feat: redesign model settings around three roles"
```

### Task 6: Three-role task creation UI

**Files:**
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/task-create.mjs`
- Modify: `web-ui/public/app.mjs`
- Test: `tests/workbench-ui.test.mjs`
- Test: `tests/ad-route-ui.test.mjs`

**Interfaces:**
- Consumes: available canonical role profiles from bootstrap.
- Produces: `planning_profile_id`, `broll_profile_id`, and `tts_profile_id` draft fields and canonical API payloads.

- [ ] **Step 1: Write failing task-create tests**

```javascript
assert.deepEqual(buildModelProfileIds({
  planning_profile_id: "planning-1",
  broll_profile_id: "broll-1",
}), { planning: "planning-1", broll: "broll-1" })
```

Assert that digital-human shows planning and optional B-roll only, while ad mix shows planning, TTS, and optional B-roll.

- [ ] **Step 2: Run focused web tests and confirm RED**

Run: `node --test tests/workbench-ui.test.mjs tests/ad-route-ui.test.mjs`

- [ ] **Step 3: Replace five selectors and migrate local drafts**

When loading an existing draft, prefer `planning_profile_id`; otherwise map the first legacy text selector to planning. Prefer `broll_profile_id`; otherwise map `video_generation_profile_id`. Do not change already-created tasks.

- [ ] **Step 4: Run focused and full web tests and confirm GREEN**

Run: `npm run test:web`

- [ ] **Step 5: Commit only Task 6 files**

```powershell
git add -- web-ui/public/index.html web-ui/public/task-create.mjs web-ui/public/app.mjs tests/workbench-ui.test.mjs tests/ad-route-ui.test.mjs
git commit -m "feat: select three model roles for new tasks"
```

### Task 7: End-to-end verification and live service reload

**Files:**
- Modify only if evidence reveals a defect: files already listed above
- Test: all relevant Python, Node, TypeScript, and live HTTP checks

**Interfaces:**
- Verifies the complete path: stored provider → model catalog → real probe → available selector → frozen task binding → actual text runtime environment.

- [ ] **Step 1: Run all model/task/web regressions**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_model_profiles tests.test_model_profile_probe tests.test_model_runtime tests.test_digital_workflow tests.test_ad_workflow tests.test_workbench_actions tests.test_plan_brief_runner tests.test_workbench_api -v
npm run test:web
npm test
```

- [ ] **Step 2: Run syntax and patch checks**

```powershell
.venv\Scripts\python.exe -m compileall -q media_catalog workbench scripts
node --check web-ui/public/app.mjs
git diff --check
```

- [ ] **Step 3: Reload only the verified 剪辑工作台 8765 process tree**

Resolve listener PID and command line, require the expected worktree path and `-m workbench.server --port 8765`, stop only matching PIDs, then start the existing `CutroomLocal` scheduled task. Verify one listener and HTTP 200.

- [ ] **Step 4: Probe the existing 智谱1 profiles through production code**

Use the stored credential reference without printing it. Confirm the resolved URL is `https://open.bigmodel.cn/api/paas/v4/chat/completions`, the safe result is available for at least one selected GLM model, and no secret appears in stdout or SQLite.

- [ ] **Step 5: Verify task visibility and runtime reuse without a paid call**

Create a temporary test task in a temporary database using a frozen planning profile, assert both storyboard and packaging runtime consumers resolve the same profile/model, then remove the temporary database. Do not create a paid B-roll video.

- [ ] **Step 6: Record results and preserve unrelated work**

Update the active Shared Memory Hub session checkpoint, rebuild the session index, and leave unrelated dirty files untouched.
