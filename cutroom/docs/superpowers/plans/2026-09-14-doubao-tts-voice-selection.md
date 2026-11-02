# CUTROOM Doubao TTS Voice Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace manual Doubao model/voice IDs with an AK/SK-backed voice catalog, a readable settings selector, and a per-ad-task voice choice frozen into the task snapshot.

**Architecture:** Keep provider credentials local to each Windows machine. Use the official Volcengine Python SDK for the signed `ListSpeakers` control-plane request, normalize results into SQLite, and keep synthesis on the existing AppID/Access Token adapter. A TTS profile owns a service resource and common voice; an ad request may override that voice after server-side catalog validation, and the override receives a new snapshot digest.

**Tech Stack:** Python 3, SQLite migrations, `volcengine-python-sdk==5.0.48`, stdlib unittest/http server, native HTML/CSS/ES modules, Node test runner, TypeScript TTS adapter.

**Spec:** `docs/superpowers/specs/2026-09-14-doubao-tts-voice-selection-design.md`

## Global Constraints

- Work only on `codex/tts-voice-selection` in `.worktrees/colleague-workbench-phase1`; do not modify the dirty root checkout.
- Secret Access Key and TTS Access Token live only in Windows Credential Manager; never return them from APIs, store them in SQLite, log them, or package them.
- Each colleague computer receives credentials once locally; GitHub, release ZIPs, and copied SQLite files do not transfer secrets.
- Doubao normal UI never asks for a model ID, Resource ID, Cluster, or voice ID.
- Existing plain provider secrets, TTS profiles with `settings.voice`, and frozen task snapshots remain readable and executable.
- A task always uses its frozen exact voice and never silently switches voice on refresh or synthesis failure.
- No paid video generation is added or triggered by this work.

---

### Task 1: Local credential bundle and Doubao account fields

**Files:**
- Modify: `requirements.txt`
- Modify: `media_catalog/credentials.py`
- Modify: `media_catalog/provider_presets.py`
- Modify: `workbench/api.py`
- Test: `tests/test_credentials.py`
- Test: `tests/test_workbench_api.py`

**Interfaces:**
- Produces: `encode_provider_credentials(access_token: str, secret_access_key: str | None) -> str`
- Produces: `decode_provider_credentials(raw: str) -> dict[str, str | None]`
- Produces: Doubao account settings `access_key_id`, `tts_resource_id`; the single credential reference stores both secret values as a versioned JSON bundle.
- Preserves: legacy non-JSON provider secrets decode as `access_token` with no `secret_access_key`.

- [ ] **Step 1: Write failing credential compatibility tests**

```python
def test_provider_credential_bundle_keeps_two_secrets_out_of_repr(self):
    raw = encode_provider_credentials("tts-token", "cloud-sk")
    self.assertEqual(decode_provider_credentials(raw), {
        "access_token": "tts-token", "secret_access_key": "cloud-sk",
    })
    self.assertNotIn("tts-token", repr(MemoryCredentialBackend()))

def test_legacy_provider_secret_decodes_as_tts_access_token(self):
    self.assertEqual(decode_provider_credentials("legacy-token"), {
        "access_token": "legacy-token", "secret_access_key": None,
    })
```

- [ ] **Step 2: Run the credential tests and verify RED**

Run: `python -m unittest tests.test_credentials -v`

Expected: import failure because `encode_provider_credentials` and `decode_provider_credentials` do not exist.

- [ ] **Step 3: Implement the versioned bundle and official SDK dependency**

```python
def encode_provider_credentials(access_token: str, secret_access_key: str | None) -> str:
    return json.dumps({
        "schema": 1,
        "access_token": _required_secret(access_token),
        "secret_access_key": _optional_secret(secret_access_key),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def decode_provider_credentials(raw: str) -> dict[str, str | None]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict) or value.get("schema") != 1:
        return {"access_token": raw, "secret_access_key": None}
    return {
        "access_token": str(value.get("access_token") or "").strip() or None,
        "secret_access_key": str(value.get("secret_access_key") or "").strip() or None,
    }
```

Add `volcengine-python-sdk==5.0.48` to `requirements.txt`. Extend the Doubao preset with `supports_voice_catalog: True` and resource choices `seed-tts-1.0` / `seed-tts-2.0`.

- [ ] **Step 4: Write failing provider API tests**

Add an HTTP integration test that creates a Doubao account with `secret`, `secretAccessKey`, `accessKeyId`, `appId`, and `ttsResourceId`, then asserts the response includes only non-secret fields and that the stored credential decodes to both secrets. Add a legacy account test showing a missing catalog secret returns `voiceCatalogConfigured: false` rather than exposing the old token.

- [ ] **Step 5: Run the provider API tests and verify RED**

Run: `python -m unittest tests.test_workbench_api.WorkbenchApiTests.test_doubao_account_stores_catalog_credentials_locally -v`

Expected: FAIL because the extra fields are ignored and `voiceCatalogConfigured` is absent.

- [ ] **Step 6: Implement account create/update behavior**

Add `PUT /api/settings/provider-accounts/{accountId}/credentials`. It accepts omitted secret fields to preserve the existing values, rewrites the local credential bundle, and updates only `access_key_id`, `app_id`, and `tts_resource_id` in `settings_json`. Return booleans `ttsConfigured` and `voiceCatalogConfigured`; never return either secret.

- [ ] **Step 7: Run Task 1 tests and commit**

Run: `python -m unittest tests.test_credentials tests.test_workbench_api -v`

Commit: `feat: store doubao catalog credentials locally`

---

### Task 2: Voice catalog schema, persistence, and legacy import

**Files:**
- Create: `media_catalog/migrations/030_tts_voice_catalog.sql`
- Create: `media_catalog/tts_voice_catalog.py`
- Modify: `media_catalog/database.py`
- Test: `tests/test_database.py`
- Create: `tests/test_tts_voice_catalog.py`

**Interfaces:**
- Produces: `replace_provider_voices(db, account_id: str, resource_id: str, voices: list[Mapping], *, observed_at: datetime | None = None) -> list[dict]`
- Produces: `mark_voice_catalog_failure(db, account_id: str, resource_id: str, code: str, message: str) -> dict`
- Produces: `list_provider_voices(db, account_id: str, resource_id: str, *, selectable_only: bool = False) -> list[dict]`
- Produces: `voice_catalog_state(db, account_id: str, resource_id: str) -> dict`
- Produces: `import_legacy_tts_voices(db) -> int`, called idempotently after SQL migrations.

- [ ] **Step 1: Write the failing migration test**

Update the expected migration range to `range(1, 31)` and assert `tts_voice_catalog` plus `tts_voice_catalog_state` exist with a composite primary key and foreign-key cascade to `model_provider_accounts`.

- [ ] **Step 2: Run the migration test and verify RED**

Run: `python -m unittest tests.test_database.DatabaseTests.test_migration_is_idempotent_and_enables_foreign_keys -v`

Expected: FAIL because migration 30 and both tables are absent.

- [ ] **Step 3: Add migration 030**

Create both tables with `CHECK` constraints for `source IN ('provider_catalog','legacy_saved')`, boolean `selectable`, and refresh status `never/success/failed`. Store labels, language, gender, emotions, trial URL, metadata, last-seen, and last-tested state; store no credentials.

- [ ] **Step 4: Write failing persistence tests**

```python
def test_successful_refresh_replaces_only_one_account_resource(self):
    replace_provider_voices(self.db, "doubao", "seed-tts-2.0", [{
        "voiceId": "voice-a", "displayName": "阳光青年 2.0",
        "resourceId": "seed-tts-2.0", "gender": "male",
    }], observed_at=self.now)
    replace_provider_voices(self.db, "doubao", "seed-tts-2.0", [{
        "voiceId": "voice-b", "displayName": "灿灿 2.0",
        "resourceId": "seed-tts-2.0", "gender": "female",
    }], observed_at=self.later)
    voices = list_provider_voices(self.db, "doubao", "seed-tts-2.0")
    self.assertEqual([(v["voiceId"], v["selectable"]) for v in voices], [
        ("voice-a", False), ("voice-b", True),
    ])

def test_failed_refresh_keeps_last_successful_catalog(self):
    replace_provider_voices(self.db, "doubao", "seed-tts-2.0", [self.voice])
    mark_voice_catalog_failure(self.db, "doubao", "seed-tts-2.0", "VOICE_CATALOG_AUTH_FAILED", "无权限")
    self.assertEqual(len(list_provider_voices(self.db, "doubao", "seed-tts-2.0")), 1)
```

Add a real migrated-DB test that seeds a legacy TTS profile containing `settings.voice`, calls `import_legacy_tts_voices` twice, and gets one `legacy_saved` row plus `default_voice_id` without modifying any task binding.

- [ ] **Step 5: Run persistence tests and verify RED**

Run: `python -m unittest tests.test_tts_voice_catalog -v`

Expected: import failure because `media_catalog.tts_voice_catalog` does not exist.

- [ ] **Step 6: Implement transactional catalog persistence and legacy import**

Use one `BEGIN IMMEDIATE` transaction per successful refresh. Upsert current rows, mark missing provider rows unselectable, preserve `legacy_saved` rows, and update state only after all rows validate. In `CatalogDatabase.migrate`, call `import_legacy_tts_voices(self)` after applying migrations, guarded by table existence so injectable historical migration directories still work.

- [ ] **Step 7: Run Task 2 tests and commit**

Run: `python -m unittest tests.test_database tests.test_tts_voice_catalog tests.test_model_profiles -v`

Commit: `feat: persist doubao voice catalog`

---

### Task 3: Official ListSpeakers adapter and refresh service

**Files:**
- Create: `media_catalog/volcengine_voice_catalog.py`
- Modify: `media_catalog/tts_voice_catalog.py`
- Create: `tests/test_volcengine_voice_catalog.py`
- Modify: `tests/test_tts_voice_catalog.py`

**Interfaces:**
- Produces: `fetch_volcengine_voices(access_key_id: str, secret_access_key: str, resource_id: str, *, timeout: float = 15.0, client_factory=None) -> list[dict]`
- Produces: `refresh_provider_voice_catalog(db, account_id: str, *, secret_reader=read_provider_secret, fetcher=fetch_volcengine_voices) -> dict`
- Error: `VoiceCatalogError(code: str, safe_message: str)` contains no upstream credentials or raw signed headers.

- [ ] **Step 1: Write failing adapter normalization and pagination tests**

Use an injected fake client returning two official-shaped pages. Assert request bodies use literal `Page`, string `Limit`, and `ResourceIDs`; assert output maps `VoiceType`, `Name`, `ResourceID`, `Gender`, `Languages`, `Emotions`, `NormalLabels`, `TrialURL`, and `ShortTrialURL` to the normalized catalog contract. Assert page 2 is requested when `Total` exceeds page 1.

- [ ] **Step 2: Run adapter tests and verify RED**

Run: `python -m unittest tests.test_volcengine_voice_catalog -v`

Expected: import failure because the adapter module does not exist.

- [ ] **Step 3: Implement the SDK adapter**

The default factory constructs `volcenginesdkcore.Configuration` with `ak`, `sk`, `region='cn-beijing'`, and `debug=False`, then creates `volcenginesdkspeechsaasprod.SPEECHSAASPRODApi`. Call `list_speakers(ListSpeakersRequest(...), _request_timeout=timeout)`. Keep SDK imports inside the default factory so unit tests do not require network or live credentials.

- [ ] **Step 4: Write failing refresh-service tests**

Seed a real account whose local credential bundle contains both secrets. Inject a fetcher that captures only the four non-network arguments, return two voices, and assert SQLite/state output. Add missing-AK, missing-SK, HTTP/auth, malformed-response, and resource-mismatch cases; every error assertion uses the safe error code/message and proves raw secrets are absent.

- [ ] **Step 5: Run refresh tests and verify RED**

Run: `python -m unittest tests.test_tts_voice_catalog -v`

Expected: FAIL because `refresh_provider_voice_catalog` is absent.

- [ ] **Step 6: Implement refresh orchestration**

Read `access_key_id` and `tts_resource_id` from account settings, decode the credential bundle for SK, call the adapter, reject voices whose normalized `resourceId` differs, persist success transactionally, and persist only safe failure state. Do not fall back to another resource or a built-in voice.

- [ ] **Step 7: Run Task 3 tests and commit**

Run: `python -m unittest tests.test_volcengine_voice_catalog tests.test_tts_voice_catalog -v`

Commit: `feat: fetch doubao voices with official sdk`

---

### Task 4: Voice APIs and TTS profile semantics

**Files:**
- Modify: `workbench/api.py`
- Modify: `media_catalog/model_profile_probe.py`
- Modify: `media_catalog/model_runtime.py`
- Modify: `media_catalog/model_profiles.py`
- Modify: `workbench/routes.py`
- Test: `tests/test_workbench_api.py`
- Test: `tests/test_model_profile_probe.py`
- Test: `tests/test_model_runtime.py`
- Test: `tests/test_model_profiles.py`

**Interfaces:**
- Adds: `GET /api/settings/provider-accounts/{accountId}/voices`
- Adds: `POST /api/settings/provider-accounts/{accountId}/voices/refresh`
- Adds: `GET /api/settings/model-profiles/{profileId}/voices`
- Adds: `POST /api/settings/model-profiles/{profileId}/voice-test` returning `previewUrl`.
- Doubao profile POST consumes `accountId`, `displayName`, `resourceId`, `defaultVoiceId`; server derives internal `model=resourceId` and mirrors the common voice into `settings.voice` for backward compatibility.

- [ ] **Step 1: Write failing route and profile-validation tests**

Create a catalog with two voices and assert cached GET makes no upstream call, refresh POST calls the injected service boundary, profile voice GET returns only matching selectable voices, and a forged voice/resource/account is rejected with `VOICE_NOT_SELECTABLE`. Assert Doubao profile creation succeeds without a client-supplied model/voice ID and stores the exact selected catalog voice.

- [ ] **Step 2: Run API tests and verify RED**

Run: `python -m unittest tests.test_workbench_api -v`

Expected: 404/validation failures for missing voice routes and required model.

- [ ] **Step 3: Implement cached, refresh, and profile APIs**

Route by exact path segments. Filter provider accounts by preset before voice operations. Keep generic/OpenAI TTS profile POST behavior unchanged. Return safe catalog state and voice metadata only.

- [ ] **Step 4: Write failing probe/runtime compatibility tests**

Assert a new Doubao profile can probe from `default_voice_id`, the runtime decodes the bundled TTS access token rather than passing JSON to synthesis, and an old plain credential plus `settings.voice` still produces the original `TTS_ACCESS_TOKEN` and `TTS_VOICE_TYPE`.

- [ ] **Step 5: Run probe/runtime tests and verify RED**

Run: `python -m unittest tests.test_model_profile_probe tests.test_model_runtime -v`

Expected: new bundle is treated as the token or default voice is considered missing.

- [ ] **Step 6: Implement probe/runtime bundle decoding and preview output**

Extract a shared bounded TTS probe that accepts an explicit voice. Save generated MP3 under `runtime/workbench/voice-previews/{profileId}/{sha256(voiceId)}.mp3` using atomic replacement. Add a loopback-only preview GET route with strict profile/hash path validation and `audio/mpeg`; never serve arbitrary paths.

- [ ] **Step 7: Run Task 4 tests and commit**

Run: `python -m unittest tests.test_workbench_api tests.test_model_profile_probe tests.test_model_runtime tests.test_model_profiles -v`

Commit: `feat: expose selectable tts voices`

---

### Task 5: Per-task voice validation and immutable snapshot override

**Files:**
- Modify: `media_catalog/model_profiles.py`
- Modify: `media_catalog/ad_workflow.py`
- Modify: `media_catalog/ad_actions.py`
- Test: `tests/test_model_profiles.py`
- Test: `tests/test_ad_workflow.py`
- Test: `tests/test_ad_actions.py`

**Interfaces:**
- Produces: `override_snapshot_profile_settings(snapshot: Mapping, updates: Mapping) -> dict`
- Produces: `resolve_task_tts_voice(db, snapshot: Mapping, voice_id: str) -> dict`
- Ad preflight/create consumes top-level `tts_voice_id` when route is `ad_mix`.

- [ ] **Step 1: Write failing snapshot digest test**

Resolve a valid TTS snapshot, override `voice`, `voice_name`, and `resource_id`, save it, then assert `get_task_profile_binding` accepts the new digest and the original snapshot object is unchanged. Tampering with the frozen voice must still trigger integrity failure.

- [ ] **Step 2: Run the digest test and verify RED**

Run: `python -m unittest tests.test_model_profiles -v`

Expected: import failure because `override_snapshot_profile_settings` does not exist.

- [ ] **Step 3: Implement immutable override and digest recomputation**

Deep-copy the snapshot through JSON serialization, update only `profileSettings`, remove `checkedAt` and the old digest from the digest payload, and calculate the same canonical SHA-256 format used by `_binding_snapshot`.

- [ ] **Step 4: Write failing ad preflight/create tests**

Add literal cases for missing `tts_voice_id`, a voice belonging to another account/resource, an unselectable voice, and a valid non-common voice. The valid case must assert the task binding and `ad-settings.json` contain the requested exact voice while the profile common voice remains unchanged.

- [ ] **Step 5: Run ad workflow tests and verify RED**

Run: `python -m unittest tests.test_ad_workflow -v`

Expected: missing voice currently passes and valid override still freezes the profile common voice.

- [ ] **Step 6: Implement server-side voice selection**

During both preflight and create, resolve model snapshots first, validate `tts_voice_id` against the chosen TTS snapshot’s account/resource catalog, then override and recompute the TTS snapshot. Include `tts_voice_id` and `tts_voice_name` in the task brief/settings for display, but make execution read only the frozen binding.

- [ ] **Step 7: Run Task 5 tests and commit**

Run: `python -m unittest tests.test_model_profiles tests.test_ad_workflow tests.test_ad_actions -v`

Commit: `feat: freeze per-task tts voice`

---

### Task 6: Settings and task creation user interface

**Files:**
- Modify: `web-ui/public/settings-view.mjs`
- Modify: `web-ui/public/task-create.mjs`
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/ad-route-ui.test.mjs`

**Interfaces:**
- Produces: `providerSupportsCapability(account, capability) -> boolean`
- Produces: `voiceOptionPresentation(voice) -> { label: string, detail: string }`
- `buildCreateRequest(values)` includes `tts_voice_id` for `ad_mix`.
- UI state adds `providerVoices`, `profileVoices`, and in-flight refresh/test sets keyed by ID.

- [ ] **Step 1: Write failing pure UI tests**

Assert TTS account filtering excludes a `volcengine_ark` text account, Doubao voice presentation uses `displayName` rather than `voiceId`, ad draft normalization preserves `tts_voice_id`, and `buildCreateRequest` emits the exact selected value. Assert missing task voice produces a client-side error before preflight.

- [ ] **Step 2: Run UI tests and verify RED**

Run: `npm run test:web`

Expected: helper/field assertions fail because the UI has no voice catalog state or task voice field.

- [ ] **Step 3: Implement provider and profile UI**

Add labeled AK/SK, AppID/Access Token, and service-version fields for Doubao. Existing accounts show configuration-state badges plus “补充/更新凭据” and “刷新音色”. In the Doubao TTS card replace model/voice text inputs with a searchable `<select>` of readable names, metadata, trial audio when present, and “试听/设为常用”. Keep advanced manual fields only for non-catalog TTS providers.

- [ ] **Step 4: Implement the ad-task voice selector**

Split the existing “配音模型与音色” field into “配音服务” and an always-visible “本条视频音色” field for `ad_mix`. On profile change fetch compatible cached voices, select the profile common voice when valid, allow changing it, and provide audio playback/test. Do not render either field on the digital-human route.

- [ ] **Step 5: Run UI tests and commit**

Run: `npm run test:web`

Commit: `feat: select voices by name in workbench`

---

### Task 7: Documentation, upgrade path, and release verification

**Files:**
- Modify: `docs/operations/model-profiles-new-api.md`
- Modify: `docs/user-guide/cutroom-user-guide.md`
- Modify: `README.md`
- Modify: `package.json`
- Modify: `release/release-files.json` only if the existing contract does not already include migrations/docs/requirements.
- Modify: release tests selected by `tests/test_release_packaging.py`

**Interfaces:**
- Documents: where the administrator obtains AK/SK, four-field first-machine setup, per-task voice selection, local-only credential boundary, safe upgrade/DB copy, and permission troubleshooting.
- Releases: `v0.9.0-beta.2`; never overwrite or retag `v0.9.0-beta.1`.

- [ ] **Step 1: Update user and operator documentation**

Replace every instruction telling ordinary users to enter a voice ID. State that each PC needs one administrator setup, credentials remain local, and copied ZIP/SQLite files never contain secrets. Include explicit error guidance for missing AK/SK, unauthorized ListSpeakers, missing service version, empty catalog, and failed voice preview.

- [ ] **Step 2: Update version and release assertions**

Set `package.json` to `0.9.0-beta.2`. Extend release tests to assert migration 030, official SDK requirement, updated docs, and all web assets are in the ZIP while runtime DBs and credentials are absent.

- [ ] **Step 3: Run focused and full verification**

Run:

```powershell
python -m unittest tests.test_database tests.test_credentials tests.test_tts_voice_catalog tests.test_volcengine_voice_catalog tests.test_model_profiles tests.test_model_profile_probe tests.test_model_runtime tests.test_workbench_api tests.test_ad_workflow tests.test_ad_actions tests.test_release_packaging -v
npm run test:web
npm run test:ts
npx tsc --noEmit
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/build_release.py --root . --output release/dist --version 0.9.0-beta.2
```

- [ ] **Step 4: Perform isolated upgrade and package smoke tests**

Use a copied legacy database, start the extracted ZIP on an unused loopback port, verify migration 30 and legacy voice import, verify the UI contains all three model sections plus named voice selection, and verify no secret files/values appear in the package manifest or extracted tree. Do not use the sole production database for this test.

- [ ] **Step 5: Commit release-ready changes**

Commit: `docs: prepare cutroom beta 2 voice selection`

- [ ] **Step 6: Complete the branch**

Invoke `superpowers:verification-before-completion`, then `superpowers:requesting-code-review`, then `superpowers:finishing-a-development-branch`. Push the branch, create tag `v0.9.0-beta.2`, publish a new GitHub Release with ZIP and SHA-256 artifacts, and verify the remote tag, release assets, checksums, and clean extracted startup before telling the user to upgrade.
