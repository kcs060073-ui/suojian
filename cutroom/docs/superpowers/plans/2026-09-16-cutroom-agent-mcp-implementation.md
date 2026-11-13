# 剪辑工作台 Agent MCP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional local stdio MCP adapter that lets the current Codex model plan 剪辑工作台 videos without a 剪辑工作台 text-model API key, while preserving the standalone product, external production-service credentials, durable jobs, and four UI-only human gates.

**Architecture:** Add an `agent` orchestration mode and immutable Agent artifact ledger to the existing SQLite domain. A focused `AgentGateway` exposes a strict allowlist over the same task, workflow, and job domains used by the web workbench; standalone planning still calls the existing provider scripts, while Agent planning submits validated JSON into shared canonical finalizers. A small MCP Python SDK v2 server maps typed stdio tools to this gateway without opening a port or exposing credentials.

**Tech Stack:** Python 3.11, SQLite migrations, `unittest`, MCP Python SDK `mcp>=2,<3`, Node.js/TypeScript 5.7, vanilla ES modules, existing 剪辑工作台 Workbench API and durable worker.

**Spec:** `docs/superpowers/specs/2026-09-16-cutroom-agent-mcp-design.md`

## Global Constraints

- Existing tasks default to `standalone`; no historical task is silently converted to `agent`.
- `agent` tasks never fall back to 剪辑工作台 text/storyboard models when Agent input is absent or invalid.
- TTS, Seedance, and other external production capabilities keep their existing profile snapshots and credential flow.
- MCP is local `stdio` only and must never write protocol-unrelated output to stdout.
- MCP cannot approve brief lock, picture lock, packaging confirmation, or full review.
- MCP cannot read/write credentials, delete media, execute arbitrary commands, read arbitrary paths, or issue SQL supplied by callers.
- Existing `启动CUTROOM.bat`, `workbench/start-local.ps1`, CLI behavior, model settings, and standalone task execution remain functional.
- The workbench startup key, session cookies, provider secrets, and hidden Agent reasoning must never appear in MCP results or artifact rows.
- Paid TTS or video generation is only started by an explicit cost-confirmed task action; reads, reconnects, and artifact submission are non-billable.
- Original media remains read-only and every accepted asset reference must belong to the task's eligible material scope.

---

## File Map

- `media_catalog/migrations/031_agent_orchestration.sql`: additive task mode and immutable Agent artifact schema.
- `media_catalog/tasks.py`: validated orchestration mode persistence and projections.
- `media_catalog/agent_artifacts.py`: schema validation, content/input digests, idempotent immutable artifact storage.
- `media_catalog/agent_context.py`: minimal task planning context and deterministic input binding.
- `media_catalog/planning_finalize.py`: shared digital-human canonical planning finalizer.
- `media_catalog/ad_actions.py`: shared ad canonical finalizer and Agent-mode TTS-only branch.
- `scripts/ad_tts_runner.py`: credential-safe TTS runner that never invokes `plan-ad.ts`.
- `media_catalog/agent_gateway.py`: MCP-safe query/action façade and stable error/result envelope.
- `cutroom_mcp/server.py`: typed MCP SDK v2 tool registration.
- `scripts/cutroom_mcp.py`: stdio entry point with stderr-only diagnostics.
- `requirements-mcp.txt`: optional MCP dependency, separate from standalone installation.
- `scripts/install-codex-mcp.ps1`: explicit Codex registration helper; ordinary 剪辑工作台 install does not call it.
- `web-ui/public/task-create.mjs`, `web-ui/public/app.mjs`, `web-ui/public/task-workspace.mjs`: mode selection, source labeling, and Agent-waiting state.
- `workbench/api.py`: mode projection and rejection of provider-paid storyboard action for Agent-mode tasks.
- `README.md`, `web-ui/README.md`, `web-ui/public/help.html`, `docs/operations/media-catalog-agent-contract.md`, `docs/user-guide/cutroom-user-guide.md`: setup, operation, rollback, and user guidance.

### Task 1: Persist task orchestration mode and immutable Agent artifacts

**Files:**
- Create: `media_catalog/migrations/031_agent_orchestration.sql`
- Create: `media_catalog/agent_artifacts.py`
- Modify: `media_catalog/tasks.py`
- Modify: `tests/test_database.py`
- Create: `tests/test_agent_artifacts.py`

**Interfaces:**
- Produces: `create_task(..., orchestration_mode: str = "standalone") -> dict`.
- Produces: `save_agent_artifact(db, *, task_id: str, artifact_type: str, schema_version: int, content: dict, input_digest: str, source_agent: str, source_model_label: str | None) -> tuple[dict, bool]` where the boolean is `True` only for a new row.
- Produces: `get_agent_artifact(db, artifact_id: str) -> dict | None` and `list_agent_artifacts(db, task_id: str, artifact_type: str | None = None) -> list[dict]`.

- [ ] **Step 1: Write failing migration and domain tests**

```python
def test_migration_31_defaults_existing_tasks_to_standalone(self):
    columns = {row[1] for row in db.connection.execute("PRAGMA table_info(tasks)")}
    self.assertIn("orchestration_mode", columns)
    self.assertEqual(
        db.connection.execute(
            "SELECT orchestration_mode FROM tasks WHERE task_id='legacy-task'"
        ).fetchone()[0],
        "standalone",
    )

def test_same_content_and_input_digest_is_idempotent(self):
    first, created = save_agent_artifact(
        self.db, task_id="agent-task", artifact_type="digital_storyboard",
        schema_version=1, content={"brief": {"broll": []}, "plan": {}},
        input_digest="input-1", source_agent="codex",
        source_model_label="current-agent-model",
    )
    second, created_again = save_agent_artifact(
        self.db, task_id="agent-task", artifact_type="digital_storyboard",
        schema_version=1, content={"plan": {}, "brief": {"broll": []}},
        input_digest="input-1", source_agent="codex",
        source_model_label="current-agent-model",
    )
    self.assertTrue(created)
    self.assertFalse(created_again)
    self.assertEqual(first["artifact_id"], second["artifact_id"])
```

- [ ] **Step 2: Run the focused tests and confirm the red state**

Run: `.venv\Scripts\python.exe -m unittest tests.test_database tests.test_agent_artifacts -v`

Expected: FAIL because migration 31, `orchestration_mode`, and `media_catalog.agent_artifacts` do not exist.

- [ ] **Step 3: Add the additive schema and task-mode validation**

```sql
ALTER TABLE tasks ADD COLUMN orchestration_mode TEXT NOT NULL DEFAULT 'standalone'
  CHECK(orchestration_mode IN ('standalone', 'agent'));

CREATE TABLE agent_artifacts(
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  artifact_type TEXT NOT NULL CHECK(artifact_type IN ('digital_storyboard', 'ad_storyboard')),
  schema_version INTEGER NOT NULL CHECK(schema_version > 0),
  content_json TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  source_agent TEXT NOT NULL,
  source_model_label TEXT,
  status TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted', 'superseded')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  confirmed_at TEXT,
  UNIQUE(task_id, artifact_type, input_digest, content_digest)
);
CREATE INDEX idx_agent_artifacts_task_type_created
  ON agent_artifacts(task_id, artifact_type, created_at, artifact_id);
INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(31, datetime('now'));
```

In `tasks.py`, reject modes outside `{"standalone", "agent"}`, include the field in `INSERT`, and add it to `list_tasks`. In `agent_artifacts.py`, compute `content_digest` with the existing canonical JSON convention (`sort_keys=True`, compact separators, UTF-8 SHA-256), generate `artifact_id` as `artifact_` plus `uuid.uuid4().hex`, and return parsed `content` without exposing raw `content_json`.

- [ ] **Step 4: Run migration and artifact tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_database tests.test_agent_artifacts -v`

Expected: PASS, including schema versions `1..31`, historical default preservation, foreign-key cascade, immutable insert, and idempotent duplicate submission.

- [ ] **Step 5: Commit the database boundary**

```powershell
git add media_catalog/migrations/031_agent_orchestration.sql media_catalog/tasks.py media_catalog/agent_artifacts.py tests/test_database.py tests/test_agent_artifacts.py
git commit -m "feat: persist 剪辑工作台 agent orchestration artifacts"
```

### Task 2: Make task preflight and creation mode-aware without changing standalone behavior

**Files:**
- Modify: `media_catalog/digital_workflow.py`
- Modify: `media_catalog/ad_workflow.py`
- Modify: `workbench/api.py`
- Modify: `tests/test_digital_workflow.py`
- Modify: `tests/test_ad_workflow.py`
- Modify: `tests/test_workbench_api.py`

**Interfaces:**
- Consumes: `create_task(..., orchestration_mode=...)` from Task 1.
- Produces: `request["orchestration_mode"]` normalized to `standalone` or `agent` during preflight and frozen on task creation.
- Produces: `_selected_model_capabilities(selections, orchestration_mode)` where Agent mode removes text-only capabilities but retains `tts`, `video_generation`, and `broll` when selected.

- [ ] **Step 1: Add failing mode-aware preflight tests**

```python
def test_agent_digital_preflight_does_not_require_planning_profile(self):
    request = self.valid_request()
    request.update({"orchestration_mode": "agent", "model_profile_ids": {}})
    result = preflight_digital_task(self.db, request, self.root, dependencies=self.deps)
    self.assertFalse(any(item["code"] == "MODEL_PROFILE_UNAVAILABLE" for item in result["blocking"]))

def test_agent_ad_creation_freezes_tts_but_not_planning(self):
    request = self.valid_request()
    request.update({
        "orchestration_mode": "agent",
        "model_profile_ids": {"tts": self.tts_profile_id},
        "tts_voice_id": self.voice_id,
    })
    created = create_ad_task(self.db, request, self.root)
    bindings = list_task_profile_bindings(self.db, created["task_id"])
    self.assertEqual(created["task"]["orchestration_mode"], "agent")
    self.assertEqual([item["capability"] for item in bindings], ["tts"])
```

Also snapshot the current standalone preflight and task binding assertions so the existing planning/storyboard/packaging selections remain unchanged.

- [ ] **Step 2: Run the focused workflow tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_digital_workflow tests.test_ad_workflow tests.test_workbench_api -v`

Expected: FAIL because Agent mode still validates and binds planning profiles and is not persisted.

- [ ] **Step 3: Implement explicit capability filtering**

```python
def _orchestration_mode(request: Mapping[str, object]) -> str:
    value = request.get("orchestration_mode", "standalone")
    if value not in {"standalone", "agent"}:
        raise ValueError("ORCHESTRATION_MODE_INVALID: 编排模式必须是 standalone 或 agent")
    return str(value)

def _agent_external_capabilities(selections: object, *, include_tts: bool) -> tuple[str, ...]:
    if not isinstance(selections, Mapping):
        return ("tts",) if include_tts else ()
    capabilities = ["tts"] if include_tts else []
    for name in ("video_generation", "broll"):
        if selections.get(name):
            capabilities.append(name)
    return tuple(capabilities)
```

Call the current `_selected_model_capabilities` unchanged for `standalone`. For `agent`, resolve and save only the returned external capabilities. Pass the normalized mode to `create_task`. Include `orchestrationMode` in workbench task summaries while preserving the existing snake_case field in task detail.

- [ ] **Step 4: Run workflow/API regression tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_digital_workflow tests.test_ad_workflow tests.test_workbench_api tests.test_model_profiles tests.test_model_runtime -v`

Expected: PASS; standalone fixtures retain their original model bindings, Agent digital tasks need no planning profile, and Agent ad tasks retain the selected TTS snapshot.

- [ ] **Step 5: Commit the mode-aware creation path**

```powershell
git add media_catalog/digital_workflow.py media_catalog/ad_workflow.py workbench/api.py tests/test_digital_workflow.py tests/test_ad_workflow.py tests/test_workbench_api.py
git commit -m "feat: add agent task orchestration mode"
```

### Task 3: Build deterministic, minimal Agent planning context

**Files:**
- Create: `media_catalog/agent_context.py`
- Create: `tests/test_agent_context.py`

**Interfaces:**
- Produces: `build_agent_context(db, task_id: str, project_root: Path) -> dict`.
- Produces: `current_agent_input_digest(db, task_id: str, project_root: Path) -> str`.
- Context keys: `task`, `script`, `workflow`, `gates`, `materials`, `planningSchema`, `inputDigest`; no credentials, arbitrary absolute source paths, cookies, or hidden logs.

- [ ] **Step 1: Write failing context and redaction tests**

```python
def test_context_contains_locked_script_scoped_assets_and_stable_digest(self):
    context = build_agent_context(self.db, self.task_id, self.root)
    self.assertEqual(context["task"]["orchestrationMode"], "agent")
    self.assertEqual(context["script"]["text"], "锁定文案")
    self.assertEqual(context["materials"][0]["assetId"], self.asset_id)
    self.assertEqual(context["inputDigest"], build_agent_context(self.db, self.task_id, self.root)["inputDigest"])

def test_context_rejects_unlocked_or_standalone_tasks_and_has_no_secret_fields(self):
    with self.assertRaisesRegex(RuntimeError, "AGENT_MODE_REQUIRED"):
        build_agent_context(self.db, self.standalone_task_id, self.root)
    serialized = json.dumps(build_agent_context(self.db, self.task_id, self.root))
    self.assertNotRegex(serialized.lower(), r"api[_-]?key|token|credential|cookie|startup")
```

- [ ] **Step 2: Run the context tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_agent_context -v`

Expected: FAIL with missing `media_catalog.agent_context`.

- [ ] **Step 3: Implement context construction and its input binding**

```python
def _input_binding(task: dict, script_sha256: str, source_snapshot_ids: list[str], assets: list[dict]) -> dict:
    return {
        "task_id": task["task_id"],
        "route": task["task_type"],
        "orchestration_mode": task["orchestration_mode"],
        "script_sha256": script_sha256,
        "source_snapshot_ids": source_snapshot_ids,
        "assets": [
            {"asset_id": item["asset_id"], "media_kind": item["media_kind"], "relative_path": item["relative_path"]}
            for item in assets
        ],
    }
```

Require the brief gate and the material-sync stage before returning planning context. Resolve only the task-local locked script under `runtime/workbench/tasks/<task_id>`, reuse `list_eligible_assets`, sort every list deterministically, cap the material list at 500 entries, and expose task-relative media references rather than library mount roots.

- [ ] **Step 4: Run context and source-boundary tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_agent_context tests.test_task_sources tests.test_task5_fail_closed -v`

Expected: PASS, including stable digest changes after script/source changes and rejection of out-of-scope assets.

- [ ] **Step 5: Commit the context boundary**

```powershell
git add media_catalog/agent_context.py tests/test_agent_context.py
git commit -m "feat: expose bounded agent planning context"
```

### Task 4: Extract the digital-human canonical planning finalizer

**Files:**
- Create: `media_catalog/planning_finalize.py`
- Modify: `media_catalog/workbench_actions.py`
- Modify: `tests/test_workbench_actions.py`
- Create: `tests/test_planning_finalize.py`

**Interfaces:**
- Consumes: accepted content shaped as `{"brief": dict, "plan": dict}`.
- Produces: `finalize_digital_storyboard(db, *, task_id: str, project_root: Path, content: dict, input_binding: dict, input_digest: str, source: str) -> dict`.
- `source` is exactly `standalone_model` or `agent_artifact` and is recorded in `recommendation.json` plus the action result.

- [ ] **Step 1: Write failing finalizer parity tests**

```python
def test_agent_and_standalone_inputs_produce_same_scene_cards(self):
    agent_result = finalize_digital_storyboard(
        self.db, task_id=self.task_id, project_root=self.root,
        content=self.content, input_binding=self.binding,
        input_digest=self.input_digest, source="agent_artifact",
    )
    self.assertEqual(agent_result["scene_card_count"], 2)
    self.assertEqual(json.loads(self.metadata.read_text())["source"], "agent_artifact")

def test_invalid_anchor_writes_neither_files_cards_nor_stage(self):
    content = copy.deepcopy(self.content)
    content["brief"]["broll"][0]["anchorText"] = "不在锁定文案中的文字"
    with self.assertRaisesRegex(RuntimeError, "STORYBOARD_OUTPUT_INVALID"):
        finalize_digital_storyboard(...)
    self.assertFalse(self.brief_path.exists())
    self.assertEqual(list_scene_cards(self.db, self.task_id), [])
```

- [ ] **Step 2: Run the focused finalizer tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_planning_finalize -v`

Expected: FAIL because the finalizer is still embedded in `recommend_storyboard`.

- [ ] **Step 3: Move validation and canonical writes behind one transaction-safe function**

```python
def finalize_digital_storyboard(db, *, task_id, project_root, content, input_binding, input_digest, source):
    if source not in {"standalone_model", "agent_artifact"}:
        raise ValueError("STORYBOARD_SOURCE_INVALID")
    brief = validate_digital_storyboard_content(content)
    cards, missing = build_digital_storyboard_cards(db, task_id, script, words, brief)
    write_storyboard_files_atomically(storyboard_dir, content, {**input_binding, "input_digest": input_digest, "source": source})
    persist_scene_cards_idempotently(db, task_id, cards)
    complete_stage(db, task_id, "ai_storyboard", input_digest=input_digest, details={
        "scene_card_count": len(cards), "missing_count": len(missing), "source": source,
    })
    return {"reused": reused, "scene_card_count": len(cards), "missing_count": len(missing), "brief_path": str(brief_path.resolve()), "source": source}
```

Keep `_anchor_time`, source scoping, exact existing-row comparison, and atomic part-file behavior unchanged. Refactor `recommend_storyboard` so the provider call only produces `content`, then calls this function. No Agent branching belongs in the standalone provider invocation.

- [ ] **Step 4: Run finalizer and existing action tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_planning_finalize tests.test_workbench_actions tests.test_render_mix_adapter -v`

Expected: PASS; existing standalone runner arguments and result paths are unchanged, while direct Agent content produces the same canonical scene-card ledger.

- [ ] **Step 5: Commit the shared digital finalizer**

```powershell
git add media_catalog/planning_finalize.py media_catalog/workbench_actions.py tests/test_planning_finalize.py tests/test_workbench_actions.py
git commit -m "refactor: share digital storyboard finalization"
```

### Task 5: Separate Agent ad planning from paid TTS execution

**Files:**
- Create: `scripts/ad_tts_runner.py`
- Modify: `media_catalog/ad_actions.py`
- Modify: `tests/test_ad_actions.py`
- Create: `tests/test_ad_tts_runner.py`

**Interfaces:**
- Produces: `finalize_ad_storyboard(db, *, task_id: str, project_root: Path, plan: dict, input_binding: dict, input_digest: str, runner, source: str) -> dict`.
- Produces CLI: `ad_tts_runner.py --project-root ROOT --node NODE --ts-node BIN --catalog-db DB --task-id ID --plan-input PLAN --timing-output JSON --voice-output MP3 --segments-dir DIR`.
- The runner loads only the frozen `tts` capability and invokes `scripts/tts.ts`; it never loads a text capability and never invokes `scripts/plan-ad.ts`.

- [ ] **Step 1: Write failing command-boundary and ad-finalizer tests**

```python
def test_agent_ad_action_never_invokes_plan_ad_or_text_model(self):
    result = finalize_ad_storyboard(
        self.db, task_id=self.task_id, project_root=self.root,
        plan=self.plan, input_binding=self.binding, input_digest="digest",
        runner=self.runner, source="agent_artifact",
    )
    command = self.runner.commands[0]
    self.assertIn("ad_tts_runner.py", " ".join(command))
    self.assertNotIn("plan-ad.ts", " ".join(command))
    self.assertNotIn("--text-capability", command)
    self.assertEqual(result["source"], "agent_artifact")

def test_tts_runner_environment_contains_tts_but_no_general_text_secret(self):
    with patch("subprocess.run", return_value=CompletedProcess([], 0)) as run:
        self.assertEqual(main(self.argv), 0)
    environment = run.call_args.kwargs["env"]
    self.assertIn("TTS_ACCESS_TOKEN", environment)
    self.assertNotIn("ARK_API_KEY", environment)
```

- [ ] **Step 2: Run ad tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_ad_actions tests.test_ad_tts_runner -v`

Expected: FAIL because ad planning and TTS are still coupled in `ad_pipeline_runner.py`.

- [ ] **Step 3: Implement the TTS-only runner and source-aware finalizer**

```python
with CatalogDatabase.open(args.catalog_db) as db:
    environment, _tts = task_model_environment(
        db, args.task_id, "tts", base_environment=legacy_environment(root)
    )
for key in ("ARK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"):
    environment.pop(key, None)
command = [
    str(args.node.resolve()), str(args.ts_node.resolve()), str((root / "scripts" / "tts.ts").resolve()),
    "--plan", str(args.plan_input.resolve()), "--speed", args.speed,
    "--timing-output", str(args.timing_output.resolve()),
    "--voice-output", str(args.voice_output.resolve()),
    "--segments-dir", str(args.segments_dir.resolve()),
]
```

Extract the current post-plan validation, asset matching, card persistence, metadata, and stage completion into `finalize_ad_storyboard`. The standalone path still calls `ad_pipeline_runner.py` and then the finalizer. The Agent path requires an accepted artifact, writes its validated `adplan.json`, invokes only `ad_tts_runner.py`, then calls the same finalizer. Require explicit `cost_status="approved"` on the durable job before any TTS command starts.

- [ ] **Step 4: Run ad and model-runtime tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_ad_actions tests.test_ad_tts_runner tests.test_model_runtime tests.test_workbench_worker -v`

Expected: PASS; standalone still invokes planning plus TTS, Agent ad invokes only TTS, and failure before TTS leaves `ai_storyboard` incomplete.

- [ ] **Step 5: Commit the split ad execution path**

```powershell
git add scripts/ad_tts_runner.py media_catalog/ad_actions.py tests/test_ad_actions.py tests/test_ad_tts_runner.py
git commit -m "feat: finalize agent ad plans with external TTS only"
```

### Task 6: Add the strict Agent Gateway

**Files:**
- Create: `media_catalog/agent_gateway.py`
- Create: `tests/test_agent_gateway.py`
- Modify: `workbench/api.py`
- Modify: `tests/test_workbench_api.py`

**Interfaces:**
- Produces: `AgentGateway(db, project_root: Path, workbench_origin: str = "http://127.0.0.1:8765")`.
- Produces methods `get_system_status`, `list_tasks`, `get_task`, `get_agent_context`, `get_job`, `get_result_links`, `preflight_task`, `create_task`, `submit_planning_artifact`, `start_task_action`, `cancel_job`, `resume_job`; every method returns `{"ok": bool, "data": dict | list | None, "error": dict | None}`.
- Allowed actions: `sync_sources`, `recommend_storyboard`, `render_and_qa`, `build_publish_package`. No gate action is present.

- [ ] **Step 1: Write failing permission, idempotency, and no-fallback tests**

```python
def test_gateway_has_no_gate_approval_or_credential_methods(self):
    forbidden = {"lock_brief", "lock_picture", "confirm_packaging", "approve_review", "save_credentials"}
    self.assertTrue(forbidden.isdisjoint(dir(self.gateway)))

def test_invalid_artifact_does_not_advance_or_fallback(self):
    result = self.gateway.submit_planning_artifact(
        task_id=self.task_id, artifact_type="digital_storyboard", schema_version=1,
        input_digest="stale", content=self.valid_content,
        source_agent="codex", source_model_label="current-agent-model",
    )
    self.assertFalse(result["ok"])
    self.assertEqual(result["error"]["code"], "AGENT_INPUT_STALE")
    self.assertEqual(self.provider_runner.commands, [])
    self.assertNotEqual(project_workflow(self.db, self.task_id)["current_stage"], "rough_cut")
```

Add tests that reject standalone tasks, unsupported artifact types, content over 1 MiB, strings over 20,000 characters, an out-of-scope asset ID, arbitrary actions, and a paid action without `confirm_cost=True`.

- [ ] **Step 2: Run gateway tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_agent_gateway tests.test_workbench_api -v`

Expected: FAIL with missing `AgentGateway`.

- [ ] **Step 3: Implement the allowlisted façade and stable errors**

```python
ALLOWED_ACTIONS = frozenset({
    "sync_sources", "recommend_storyboard", "render_and_qa", "build_publish_package",
})
PAID_ACTIONS = frozenset({"recommend_storyboard"})

def _failure(code: str, message: str, *, retryable: bool = False, next_action: str | None = None, fields: list[str] | None = None) -> dict:
    return {"ok": False, "data": None, "error": {
        "code": code, "message": message, "retryable": retryable,
        "nextAction": next_action, "fields": fields or [],
    }}
```

Use the domain functions from Tasks 1–5 and existing worker functions; do not accept a database path, project root, shell command, or output path from tool arguments. `submit_planning_artifact` compares the supplied digest to `current_agent_input_digest` before inserting. On a new digital artifact it calls the digital finalizer; on an ad artifact it records the plan and returns `nextAction="start_recommend_storyboard_for_tts"`. `get_result_links` returns `taskUrl="http://127.0.0.1:8765/#/tasks/<urlencoded-id>"` plus verified files under the task/out roots, never `/auth/bootstrap`.

In `WorkbenchAPI._task_route`, reject `/storyboard/recommend` for `agent` tasks unless the queued path is the internal Agent-TTS flow; the web button therefore cannot accidentally invoke a text provider for Agent tasks.

- [ ] **Step 4: Run gateway, gate, job, and API tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_agent_gateway tests.test_workbench_api tests.test_full_review tests.test_packaging_composition tests.test_workbench_worker -v`

Expected: PASS; all four gate APIs remain web-only, and gateway reads/actions return bounded stable envelopes.

- [ ] **Step 5: Commit the gateway**

```powershell
git add media_catalog/agent_gateway.py workbench/api.py tests/test_agent_gateway.py tests/test_workbench_api.py
git commit -m "feat: add gate-safe 剪辑工作台 agent gateway"
```

### Task 7: Expose the gateway as an optional stdio MCP server

**Files:**
- Create: `requirements-mcp.txt`
- Create: `cutroom_mcp/__init__.py`
- Create: `cutroom_mcp/server.py`
- Create: `scripts/cutroom_mcp.py`
- Create: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `AgentGateway` from Task 6.
- Produces: `create_mcp_server(gateway: AgentGateway) -> MCPServer`.
- Produces exactly twelve tools named in the design: six query tools and six write/action tools.

- [ ] **Step 1: Add the optional SDK requirement and failing in-memory MCP tests**

`requirements-mcp.txt`:

```text
mcp>=2,<3
```

```python
class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_discovery_is_exact_and_gate_safe(self):
        async with Client(create_mcp_server(self.gateway)) as client:
            tools = await client.list_tools()
        names = {item.name for item in tools.tools}
        self.assertEqual(names, {
            "cutroom_get_system_status", "cutroom_list_tasks", "cutroom_get_task",
            "cutroom_get_agent_context", "cutroom_get_job", "cutroom_get_result_links",
            "cutroom_preflight_task", "cutroom_create_task",
            "cutroom_submit_planning_artifact", "cutroom_start_task_action",
            "cutroom_cancel_job", "cutroom_resume_job",
        })

    async def test_structured_tool_result_preserves_gateway_error(self):
        async with Client(create_mcp_server(self.gateway)) as client:
            result = await client.call_tool("cutroom_get_task", {"task_id": "missing"})
        self.assertEqual(result.structured_content["error"]["code"], "TASK_NOT_FOUND")
```

- [ ] **Step 2: Install the optional dependency and verify the tests are red for missing server code**

Run: `.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt`

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_server -v`

Expected: dependency installs, then tests FAIL because `cutroom_mcp.server` is absent.

- [ ] **Step 3: Register typed tools and a clean stdio entry point**

```python
from mcp.server import MCPServer

def create_mcp_server(gateway: AgentGateway) -> MCPServer:
    server = MCPServer("cutroom")

    @server.tool()
    def cutroom_get_task(task_id: str) -> dict:
        """Return one 剪辑工作台 task without credentials or raw logs."""
        return gateway.get_task(task_id)

    @server.tool()
    def cutroom_submit_planning_artifact(
        task_id: str, artifact_type: str, schema_version: int,
        input_digest: str, content: dict, source_agent: str = "codex",
        source_model_label: str | None = None,
    ) -> dict:
        return gateway.submit_planning_artifact(
            task_id=task_id, artifact_type=artifact_type,
            schema_version=schema_version, input_digest=input_digest,
            content=content, source_agent=source_agent,
            source_model_label=source_model_label,
        )

    return server
```

Register the other ten methods with explicit scalar/list/dict type hints and docstrings. `scripts/cutroom_mcp.py` resolves the project root from its own file, opens only `runtime/workbench/catalog.sqlite`, creates the gateway, and calls `server.run(transport="stdio")`. Configure Python logging to stderr and do not use `print()`.

- [ ] **Step 4: Run MCP discovery, validation, and stdout-cleanliness tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_server tests.test_agent_gateway -v`

Expected: PASS, including exact tool names, structured content, invalid enum rejection, bounded list limits, and no non-protocol stdout.

- [ ] **Step 5: Commit the MCP adapter**

```powershell
git add requirements-mcp.txt cutroom_mcp scripts/cutroom_mcp.py tests/test_mcp_server.py
git commit -m "feat: expose 剪辑工作台 through local stdio MCP"
```

### Task 8: Make Agent mode explicit in the existing workbench UI

**Files:**
- Modify: `web-ui/public/task-create.mjs`
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/task-workspace.mjs`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/workbench-layout.test.mjs`

**Interfaces:**
- Produces create payload `orchestration_mode: "standalone" | "agent"`.
- Agent mode omits text-planning profile IDs, retains TTS/video-generation profile IDs, labels planning source as `当前 Agent`, and renders `等待 Agent 提交规划` instead of a provider-paid recommendation button.

- [ ] **Step 1: Write failing UI request and workspace tests**

```javascript
test("agent create request omits planning profile but retains ad TTS", () => {
  const request = buildCreateRequest({
    ...validAdValues, orchestration_mode: "agent",
    planning_profile_id: "planning-profile", tts_profile_id: "tts-profile",
  });
  assert.equal(request.orchestration_mode, "agent");
  assert.deepEqual(request.model_profile_ids, { tts: "tts-profile" });
});

test("agent storyboard stage exposes waiting copy and no paid text action", () => {
  const state = buildStoryboardActionState({
    task: { orchestration_mode: "agent" }, workflow: { current_stage: "ai_storyboard" },
  });
  assert.equal(state.label, "等待 Agent 提交规划");
  assert.equal(state.allowPaidTextRecommendation, false);
});
```

- [ ] **Step 2: Run web tests red**

Run: `node --test --test-concurrency=1 tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs`

Expected: FAIL because task drafts and create requests have no orchestration mode.

- [ ] **Step 3: Implement the two-mode UI without removing standalone controls**

```javascript
const orchestrationMode = String(values.orchestration_mode || "standalone");
const modelProfileIds = orchestrationMode === "agent"
  ? {}
  : { planning: String(values.planning_profile_id || "") };
if (route === "ad_mix") modelProfileIds.tts = String(values.tts_profile_id || "");
return { ...shared, orchestration_mode: orchestrationMode, model_profile_ids: modelProfileIds };
```

Add a creation choice with plain labels `剪辑工作台自己规划` and `当前 Agent 规划`. Keep standalone as the default. Hide only text-planning selectors in Agent mode; never hide external TTS/video-generation selectors. In the task workspace, show a persistent mode badge and artifact source. At an Agent planning wait, show the task ID plus instructions to return to Codex; do not render the paid text recommendation button. All four existing confirmation buttons remain unchanged.

- [ ] **Step 4: Run UI and API tests green**

Run: `node --test --test-concurrency=1 tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs tests/ad-route-ui.test.mjs`

Run: `.venv\Scripts\python.exe -m unittest tests.test_workbench_api -v`

Expected: PASS for standalone and Agent creation, mode restoration from drafts, Agent waiting copy, and existing gate controls.

- [ ] **Step 5: Commit the UI mode**

```powershell
git add web-ui/public/task-create.mjs web-ui/public/index.html web-ui/public/app.mjs web-ui/public/task-workspace.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs
git commit -m "feat: surface 剪辑工作台 agent mode in workbench"
```

### Task 9: Add explicit Codex registration, documentation, and rollback

**Files:**
- Create: `scripts/install-codex-mcp.ps1`
- Create: `scripts/uninstall-codex-mcp.ps1`
- Modify: `release/install.ps1`
- Modify: `README.md`
- Modify: `web-ui/README.md`
- Modify: `web-ui/public/help.html`
- Modify: `docs/operations/media-catalog-agent-contract.md`
- Modify: `docs/user-guide/cutroom-user-guide.md`
- Modify: `tests/test_release_packaging.py`
- Create: `tests/test_mcp_install_scripts.py`

**Interfaces:**
- Registration script accepts `-ConfigPath` and `-DryRun`; it writes one `cutroom` stdio server entry using absolute Python/script paths.
- Uninstall removes only the exact `cutroom` entry and does not delete the database, tasks, model profiles, or credentials.
- `release/install.ps1` installs only standalone dependencies; it prints the optional MCP command but never runs it automatically.

- [ ] **Step 1: Write failing installer safety tests**

```python
def test_normal_installer_does_not_register_mcp(self):
    text = INSTALLER.read_text(encoding="utf-8")
    self.assertNotIn("install-codex-mcp.ps1\"", text)
    self.assertIn("requirements.txt", text)

def test_uninstaller_does_not_delete_cutroom_data(self):
    text = UNINSTALLER.read_text(encoding="utf-8").lower()
    self.assertNotIn("remove-item $projectroot", text)
    self.assertNotIn("catalog.sqlite", text)
```

- [ ] **Step 2: Run installer/release tests red**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_install_scripts tests.test_release_packaging -v`

Expected: FAIL because the optional install/uninstall scripts do not exist.

- [ ] **Step 3: Implement dry-run-first configuration scripts and documentation**

The install script must: verify `.venv\Scripts\python.exe`, verify `scripts\cutroom_mcp.py`, verify `import mcp`, parse the target config as JSON/TOML according to the repository's documented Codex Desktop format, preserve unrelated entries, and atomically replace the config only after `-DryRun` output is accepted. The uninstall script removes only server ID `cutroom`. Neither script starts 剪辑工作台, modifies model profiles, or reads credentials.

Document this exact operating contract:

```text
独立模式：剪辑工作台自己调用已配置的文案/分镜模型。
Agent 模式：当前 Codex 模型完成文案与分镜；剪辑工作台不调用文本模型。
两种模式：豆包 TTS、Seedance 等生产服务仍使用剪辑工作台各自凭据。
关闭 MCP：只移除 Codex 中的 cutroom 服务项，现有剪辑工作台和任务继续可用。
```

Update the media catalog Agent contract from CLI-first to MCP-first for normal Agent control, while retaining CLI for diagnosis and batch use. Add the task URL form `http://127.0.0.1:8765/#/tasks/<task-id>` and state that an existing workbench session is required; never document a startup-key URL.

- [ ] **Step 4: Run documentation and release tests green**

Run: `.venv\Scripts\python.exe -m unittest tests.test_mcp_install_scripts tests.test_release_packaging -v`

Expected: PASS; normal installer remains standalone-only and MCP removal preserves all 剪辑工作台 data.

- [ ] **Step 5: Commit setup and user guidance**

```powershell
git add scripts/install-codex-mcp.ps1 scripts/uninstall-codex-mcp.ps1 release/install.ps1 README.md web-ui/README.md web-ui/public/help.html docs/operations/media-catalog-agent-contract.md docs/user-guide/cutroom-user-guide.md tests/test_mcp_install_scripts.py tests/test_release_packaging.py
git commit -m "docs: add optional Codex MCP setup and rollback"
```

### Task 10: Verify standalone regression and perform controlled Codex acceptance

**Files:**
- Create: `docs/validation/2026-09-16-cutroom-agent-mcp-acceptance.md`
- Modify only if policy requires: `package.json`
- Modify only if packaging manifest requires the new files: `release/release-files.json`

**Interfaces:**
- Produces an evidence report with commands, timestamps, task IDs, artifact IDs, digests, gate states, and paid-service status.
- Does not trigger TTS or Seedance unless Peter separately confirms that paid call during acceptance.

- [ ] **Step 1: Run all automated suites from a clean process**

Run:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
npm run test:ts
npm run test:web
git status --short
```

Expected: every Python, TypeScript, and web test passes; only intentional implementation/doc changes are present.

- [ ] **Step 2: Run a no-MCP standalone smoke test**

Start the current workbench with `workbench/start-local.ps1`, create/read a `standalone` task through the normal UI/API test harness, verify model settings remain available, verify the existing `recommend_storyboard` runner command still contains `plan_brief_runner.py` or `ad_pipeline_runner.py`, and record the result. Do not alter or delete existing user tasks.

- [ ] **Step 3: Register and discover the MCP server in Codex Desktop**

Run the install script in `-DryRun`, inspect that the command points to this checkout's `.venv\Scripts\python.exe` and `scripts\cutroom_mcp.py`, then run the real registration after approval. Restart/reload Codex MCP discovery, call `cutroom_get_system_status`, and record the discovered twelve tool names without recording configuration secrets.

- [ ] **Step 4: Execute a non-paid Agent-mode planning acceptance**

Create an Agent-mode digital-human fixture/task with no text-provider credential in the test environment, complete brief lock and material sync through the existing UI/API fixture, call `cutroom_get_agent_context`, generate a schema-valid `digital_storyboard` artifact with the current Agent model, submit it twice, and verify:

```text
first submission: created=true, workflow advances after canonical finalization
second submission: created=false, same artifactId
provider command log: no plan-brief.ts, plan-ad.ts, ARK_API_KEY, LLM_API_KEY, or OPENAI_API_KEY
human gates: picture_lock=false, packaging_confirmed=false, human_reviewed=false
task URL: opens #/tasks/<task-id> in the existing authenticated workbench session
```

- [ ] **Step 5: Verify interruption, disablement, and paid-service hold**

Stop the MCP process after the artifact is stored, restart it, read the same task/artifact/job state, then unregister the MCP entry and confirm the standalone workbench still starts and lists both historical and new tasks. For an Agent ad fixture, stop at `nextAction=start_recommend_storyboard_for_tts`; record that no TTS/Seedance request ran without explicit paid confirmation.

- [ ] **Step 6: Write the acceptance report and version/manifest changes justified by repository policy**

The report must include separate verdicts for: MCP discovery, no-text-key Agent planning, standalone regression, four gate preservation, right-pane task URL, interruption/resume, disablement, secret redaction, and paid-service hold. If the repository packages this branch as the next beta, update `package.json` from `0.9.0-beta.4` to the approved next version and list every added MCP file in `release/release-files.json`; otherwise state that versioning was intentionally left unchanged because no release was cut.

- [ ] **Step 7: Commit acceptance evidence**

```powershell
git add docs/validation/2026-09-16-cutroom-agent-mcp-acceptance.md package.json release/release-files.json
git commit -m "test: validate 剪辑工作台 Codex MCP workflow"
```

If `package.json` and `release/release-files.json` were intentionally unchanged, omit them from `git add`.

---

## Self-Review Record

- Spec coverage: Tasks 1–10 cover data compatibility, mode isolation, Agent context/artifacts, shared digital and ad finalizers, external-service credentials, strict MCP tools, UI-only gates, right-pane links, recovery, optional setup, rollback, automation, and real acceptance.
- Placeholder scan: the implementation tasks contain explicit paths, signatures, commands, expected failures, expected successes, and commit boundaries; no deferred feature or unspecified handler remains.
- Type consistency: `orchestration_mode`, artifact type names, gateway envelopes, tool names, `inputDigest`, and finalizer source values are fixed once above and reused consistently.
- Scope: MCP transport, domain gateway, canonical planning, workbench projection, and optional setup form one end-to-end product slice; none is independently useful without the others, so they remain one implementation plan with reviewable commits.
