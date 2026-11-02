# CUTROOM Direct Provider Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mandatory New API gateway form with CC Switch-style direct provider configuration while preserving optional New API routing and immutable task model bindings.

**Architecture:** Keep the existing provider-account → capability-profile → task-binding model. Store direct/gateway protocol metadata in the existing provider account `settings_json`, keep secrets only in Windows Credential Manager, and centralize token/endpoint resolution so probes and task execution follow the same rules.

**Tech Stack:** Python 3.11, SQLite migrations already at schema 27, Windows Credential Manager, browser-native JavaScript, Node test runner, TypeScript/ts-node.

## Global Constraints

- Default setup must not require New API or expose a channel ID.
- Provider fields follow CC Switch: name, preset, optional note, API Key, request URL, optional default model.
- One provider account may be reused by multiple capability profiles; one capability may have multiple profiles.
- API keys never enter SQLite, page responses, logs, task snapshots, or command arguments.
- Direct providers use the exact saved key; only New API with an explicit channel ID applies channel routing.
- Existing New API accounts and existing frozen task bindings remain executable.
- Profiles require a successful real check within 24 hours before new tasks may select them.
- No new third-party dependency and no paid video generation during connection tests.

---

### Task 1: Define provider presets and persist direct-account metadata

**Files:**
- Create: `media_catalog/provider_presets.py`
- Modify: `workbench/api.py`
- Test: `tests/test_workbench_api.py`
- Test: `tests/test_model_profiles.py`

**Interfaces:**
- Produces: `provider_preset(preset_id: str) -> dict`
- Produces: public account fields `preset`, `note`, `defaultModel`, `channelId`
- Consumes: existing `create_provider_account(db, *, account_id: str, name: str, provider: str, base_url: str, credential_ref: str, settings: Mapping | None = None) -> dict`

- [ ] **Step 1: Write failing API and model tests**

Add tests proving a direct account is accepted without `channelId`, the secret is absent from responses, and metadata survives a list/read round trip:

```python
result = api.dispatch("POST", "/api/settings/provider-accounts", {
    "name": "火山方舟直连",
    "preset": "volcengine_ark",
    "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
    "defaultModel": "ep-example",
    "note": "分镜与包装",
    "secret": "secret-direct",
})
self.assertEqual(result.status, 200)
self.assertEqual(result.body["preset"], "volcengine_ark")
self.assertIsNone(result.body["channelId"])
self.assertNotIn("secret", json.dumps(result.body))
```

Also prove `new_api` accepts an optional positive channel and rejects non-positive/non-numeric values only when supplied.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_workbench_api tests.test_model_profiles
```

Expected: direct provider request fails because the API still requires `provider`/channel-oriented input or omits the new public fields.

- [ ] **Step 3: Implement preset metadata and API validation**

Create an immutable preset table:

```python
PROVIDER_PRESETS = {
    "openai_compatible": {
        "label": "OpenAI 兼容",
        "protocol": "openai",
        "default_base_url": "",
    },
    "volcengine_ark": {
        "label": "火山方舟文本",
        "protocol": "openai",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
    "volcengine_seedance": {
        "label": "火山方舟视频生成",
        "protocol": "doubao_video",
        "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
    "doubao_tts": {
        "label": "豆包 TTS",
        "protocol": "volcengine_tts",
        "default_base_url": "https://openspeech.bytedance.com/api/v1/tts",
    },
    "new_api": {
        "label": "New API（高级兼容）",
        "protocol": "openai",
        "default_base_url": "http://127.0.0.1:3000",
    },
}
```

Normalize the POST body into account settings:

```python
settings = {
    "preset": preset_id,
    "protocol": preset["protocol"],
    "note": _optional_string(body, "note"),
    "default_model": _optional_string(body, "defaultModel"),
    "app_id": _optional_string(body, "appId"),
    "cluster": _optional_string(body, "cluster"),
}
if preset_id == "new_api" and channel_id is not None:
    settings["channel_id"] = channel_id
```

Keep `provider` in the database as the preset ID. Do not change schema 27.

When creating a capability profile, make `protocol` optional in the request and default it from the selected account's frozen `settings.protocol`. Continue accepting an explicit protocol from older clients.

- [ ] **Step 4: Expose credential-safe public metadata**

Update `_public_profile`, `get_provider_account`, and task snapshot construction so public account/profile data includes preset metadata but never `credentialRef` or secret values. Existing accounts with only `provider="new_api"` and `channel_id` must project correctly.

- [ ] **Step 5: Run tests and verify GREEN**

Run the Task 1 command again. Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add media_catalog/provider_presets.py media_catalog/model_profiles.py workbench/api.py tests/test_workbench_api.py tests/test_model_profiles.py
git commit -m "feat: add direct provider account presets"
```

---

### Task 2: Resolve direct and gateway credentials consistently

**Files:**
- Modify: `media_catalog/model_runtime.py`
- Modify: `media_catalog/model_profile_probe.py`
- Modify: `scripts/tts-provider.ts`
- Test: `tests/test_model_runtime.py`
- Test: `tests/test_model_profile_probe.py`
- Test: `tests/tts-provider.test.ts`

**Interfaces:**
- Produces: `provider_access_token(secret: str, provider: str, channel_id: object) -> str`
- Produces: identical credential routing for availability probes and frozen task execution
- Consumes: task snapshot fields `provider`, `accountSettings`, `profileSettings`, `baseUrl`, `model`

- [ ] **Step 1: Write failing direct-token tests**

```python
def test_direct_provider_uses_exact_api_key_without_channel_suffix(self):
    # Bind a volcengine_ark account with no channel_id.
    environment, _ = task_model_environment(
        self.db, "task-runtime", "storyboard",
        base_environment={},
        secret_reader=lambda _reference: "ark-direct-key",
    )
    self.assertEqual(environment["ARK_API_KEY"], "ark-direct-key")
```

Add probe assertions that direct text requests send `Authorization: Bearer ark-direct-key`, while existing New API account tests still send `Bearer sk-gateway-7`. Add a New API test without channel ID that uses the raw token.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_model_runtime tests.test_model_profile_probe
```

Expected: direct/no-channel paths fail with `MODEL_CHANNEL_NOT_PINNED` or “必须指定 New API 固定渠道 ID”.

- [ ] **Step 3: Centralize token routing**

Replace the unconditional channel requirement with:

```python
def provider_access_token(secret: str, provider: str, channel_id) -> str:
    value = secret.strip() if isinstance(secret, str) else ""
    if not value:
        raise TaskModelConfigurationError("MODEL_CREDENTIAL_MISSING: 模型凭据不存在")
    if provider != "new_api" or channel_id in (None, ""):
        return value
    channel = _positive_channel_id(channel_id)
    suffix = f"-{channel}"
    return value if value.endswith(suffix) else value + suffix
```

Use this resolver in both runtime and probe code. Keep `pinned_gateway_token` as a compatibility wrapper if existing imports require it.

- [ ] **Step 4: Add direct provider protocol environments**

Text and Seedance continue using existing ARK/SEEDANCE environment contracts with the exact direct key and normalized base URL. For direct Doubao TTS, read `app_id` and `cluster` from frozen account settings and emit:

```python
environment.update({
    "TTS_PROTOCOL": "volcengine",
    "TTS_ENDPOINT": exact_base_url,
    "TTS_APPID": app_id,
    "TTS_ACCESS_TOKEN": token,
    "TTS_CLUSTER": cluster or "volcano_tts",
    "TTS_VOICE_TYPE": voice,
})
```

Keep OpenAI-compatible TTS/New API on the existing `openai` speech contract.

- [ ] **Step 5: Make probes follow the same protocol**

Text uses `/chat/completions`; OpenAI-compatible TTS uses `/audio/speech`; direct Doubao TTS sends the existing Volcengine request shape and validates the JSON audio payload without persisting it; video checks `/models` and never submits a generation.

Map safe failures to stable codes:

```python
401, 403 -> MODEL_AUTH_FAILED
404 -> MODEL_OR_ENDPOINT_NOT_FOUND
timeout/URLError -> MODEL_NETWORK_UNREACHABLE
invalid response -> MODEL_PROTOCOL_MISMATCH
```

- [ ] **Step 6: Run Python and TypeScript tests**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_model_runtime tests.test_model_profile_probe
node --test -r ts-node/register tests/tts-provider.test.ts
```

Expected: direct, optional-channel New API, pinned-channel New API, OpenAI TTS, and Volcengine TTS tests all pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add media_catalog/model_runtime.py media_catalog/model_profile_probe.py scripts/tts-provider.ts tests/test_model_runtime.py tests/test_model_profile_probe.py tests/tts-provider.test.ts
git commit -m "feat: execute direct provider credentials"
```

---

### Task 3: Add credential-safe model discovery

**Files:**
- Modify: `media_catalog/model_profile_probe.py`
- Modify: `workbench/api.py`
- Test: `tests/test_model_profile_probe.py`
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Produces: `list_provider_models(db, account_id: str, *, secret_reader=read_provider_secret, timeout: float = 15.0) -> list[str]`
- Produces: `POST /api/settings/provider-accounts/{account_id}/models`

- [ ] **Step 1: Write failing model-list tests**

Use the existing bounded local HTTP server and assert:

```python
models = list_provider_models(
    self.db, "direct-account",
    secret_reader=lambda _reference: "direct-key",
)
self.assertEqual(models, ["model-a", "model-b"])
self.assertEqual(ProbeHandler.requests[-1]["authorization"], "Bearer direct-key")
```

Add API tests for unknown accounts, oversized/malformed responses, and response bodies containing no credential references.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest tests.test_model_profile_probe tests.test_workbench_api
```

Expected: import/route failure because model discovery does not exist.

- [ ] **Step 3: Implement bounded discovery**

Reuse `_request_json`, the provider token resolver, the 2 MiB response limit, and `/v1` normalization. Return sorted unique non-empty model IDs only:

```python
return sorted({
    item["id"].strip()
    for item in decoded.get("data", [])
    if isinstance(item, dict)
    and isinstance(item.get("id"), str)
    and item["id"].strip()
})
```

For provider presets without a model-directory contract, return a 409 API error instructing the user to enter the exact model ID manually.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Task 3 command again. Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add media_catalog/model_profile_probe.py workbench/api.py tests/test_model_profile_probe.py tests/test_workbench_api.py
git commit -m "feat: discover provider models safely"
```

---

### Task 4: Replace the New API-only form with CC Switch-style UI

**Files:**
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/settings-view.mjs`
- Modify: `web-ui/public/styles.css`
- Test: `tests/workbench-ui.test.mjs`
- Test: `tests/workbench-layout.test.mjs`

**Interfaces:**
- Consumes: bootstrap `providerAccounts` fields and provider preset metadata
- Consumes: `POST /api/settings/provider-accounts`
- Consumes: `POST /api/settings/provider-accounts/{accountId}/models`
- Produces: provider selection options for existing capability profile forms

- [ ] **Step 1: Write failing UI contract tests**

Assert the default provider form contains:

```javascript
assert.match(index, /name="preset"/);
assert.match(index, /name="secret"[^>]*placeholder="API Key/);
assert.match(index, /name="baseUrl"[^>]*placeholder="API 请求地址/);
assert.match(index, /name="defaultModel"/);
assert.match(index, /name="note"/);
assert.doesNotMatch(index, /name="channelId"[^>]*required/);
```

Add a DOM behavior test proving the channel field is hidden by default and appears only in the expanded advanced section for `new_api`.

- [ ] **Step 2: Run tests and verify RED**

```powershell
npm run test:web
```

Expected: tests fail because the current form hardcodes `new_api`, localhost:3000, required channel ID, and New API Token.

- [ ] **Step 3: Implement the provider form**

Use a preset select, CC Switch-style shared fields, and native `<details>` for advanced options:

```html
<select class="field" name="preset" required>
  <option value="openai_compatible">OpenAI 兼容</option>
  <option value="volcengine_ark">火山方舟文本</option>
  <option value="volcengine_seedance">火山方舟视频生成</option>
  <option value="doubao_tts">豆包 TTS</option>
  <option value="new_api">New API（高级兼容）</option>
</select>
<input class="field" name="name" placeholder="供应商名称，例如：火山方舟">
<input class="field" name="note" placeholder="备注（可选）">
<input class="field" name="secret" type="password" placeholder="API Key / Access Token">
<input class="field" name="baseUrl" placeholder="API 请求地址">
<input class="field" name="defaultModel" placeholder="默认模型（可选）">
<details class="provider-advanced">
  <summary>高级选项</summary>
  <input class="field" name="channelId" inputmode="numeric" placeholder="New API 固定渠道 ID（可选）">
  <input class="field" name="appId" placeholder="豆包 TTS AppID">
  <input class="field" name="cluster" placeholder="豆包 TTS Cluster（可选）">
</details>
```

Preset changes populate default endpoints only when the user has not manually changed the current value. They never populate or expose a secret.

- [ ] **Step 4: Render provider accounts and model discovery**

Each account row shows name, preset label, endpoint, optional default model, and “获取模型” where supported. Never render “未固定渠道” for ordinary direct accounts. Discovery results populate a selectable datalist while preserving manual model input.

- [ ] **Step 5: Preserve capability-profile selection**

Existing capability cards continue to create named profiles. When a provider has `defaultModel`, prefill the profile model field without forcing it; changing providers updates only an untouched/empty model field.

Remove the hardcoded hidden `protocol=openai` input. The server derives protocol from the selected provider preset; older callers may still send an explicit protocol.

- [ ] **Step 6: Run UI tests and verify GREEN**

```powershell
npm run test:web
```

Expected: all existing route/layout/model-profile tests and new direct-provider tests pass.

- [ ] **Step 7: Commit Task 4**

```powershell
git add web-ui/public/index.html web-ui/public/app.mjs web-ui/public/settings-view.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs
git commit -m "feat: add CC Switch style provider settings"
```

---

### Task 5: Update operations documentation and run end-to-end regression

**Files:**
- Modify: `docs/operations/model-profiles-new-api.md`
- Modify: `web-ui/README.md`
- Modify: `README.md`
- Test: full existing suites

**Interfaces:**
- Documents: direct setup examples for Ark text, Seedance, Doubao TTS, OpenAI-compatible endpoints, and optional New API
- Verifies: live `http://127.0.0.1:8765/#/settings`

- [ ] **Step 1: Rewrite setup instructions**

Lead with direct official API setup. Explain exact field meanings and move New API installation/channel routing into an optional advanced section. Include no real key, token, customer information, or fabricated model ID.

- [ ] **Step 2: Run full automated verification**

```powershell
npm run test:web
node --test --test-reporter=dot -r ts-node/register tests/*.test.ts
.venv\Scripts\python.exe -c "import unittest,sys; s=unittest.defaultTestLoader.discover('tests',pattern='test_*.py'); r=unittest.TextTestRunner(stream=sys.stdout,verbosity=0).run(s); print(f'COUNT={r.testsRun} FAILURES={len(r.failures)} ERRORS={len(r.errors)} SKIPPED={len(r.skipped)}'); raise SystemExit(0 if r.wasSuccessful() else 1)"
git diff --check
```

Expected: every command exits 0; Python reports zero failures/errors; Node reporters complete all declared tests.

- [ ] **Step 3: Verify the live page**

Request `http://127.0.0.1:8765/` and assert:

- direct provider preset control is present;
- API Key, API request URL, default model, and note fields are present;
- channel ID is not required and lives under advanced options;
- old “先添加本机 New API 网关账号” copy is absent.

- [ ] **Step 4: Update Session checkpoint**

Record implementation scope, exact test counts, live-page evidence, and the remaining external boundary: real provider connectivity cannot be claimed until the user supplies and tests their own account credentials.

- [ ] **Step 5: Commit Task 5**

```powershell
git add README.md web-ui/README.md docs/operations/model-profiles-new-api.md
git commit -m "docs: explain direct model provider setup"
```
