# 剪辑工作台 UI Systemization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变既有业务流程、数据语义和已跑通豆包配音能力的前提下，按已确认的方案 A 统一剪辑工作台七个页面的视觉系统、豆包凭据交互、状态反馈、响应式布局与无障碍体验，并发布 v0.9.0-beta.3。

**Architecture:** 保留原生 HTML/CSS/ES Module 和现有 Python API；前端通过语义化设计令牌、共享组件类和少量渲染辅助函数完成系统化，不引入新框架或外部依赖。豆包后端继续同时支持 API Key 与旧版 AppID/Access Token，前端只做渐进披露；保存检测按“保存凭据 → 检测一个默认配置 → 刷新音色目录”串行执行并持久显示结果。

**Tech Stack:** HTML、CSS、原生 JavaScript ES Module、Python 标准库 API、Node test runner、Python unittest、TypeScript、GitHub CLI。

**Spec:** `docs/superpowers/specs/2026-09-15-cutroom-ui-systemization-design.md`

## Global Constraints

- 现有 95 个 Python 分组测试、45 个 Web 测试、TypeScript 测试和类型检查都是硬门禁；不得删除测试或放宽断言来换取通过。
- 保留历史任务、旧配置、旧音色、AppID/Access Token、快照冻结和 Windows 凭据管理器语义。
- 豆包配置正常路径只展示 API Key、Access Key ID、Secret Access Key、服务版本；旧凭据只折叠，不删除、不清空。
- 不修改任务阶段、审核门禁、渲染参数、API payload、素材库目录和发布包白名单语义。
- 每个任务先写失败测试，再做最小实现，再跑相关回归；任何已有功能失败都先修复，不带病进入下一任务。

---

### Task 1: 冻结已跑通的豆包 V3 基线

**Files:**
- Modify: `README.md`
- Modify: `media_catalog/credentials.py`
- Modify: `media_catalog/model_profile_probe.py`
- Modify: `media_catalog/model_runtime.py`
- Modify: `media_catalog/tts_voice_catalog.py`
- Modify: `scripts/tts-provider.ts`
- Modify: `scripts/tts.ts`
- Modify: `workbench/api.py`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/help.html`
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/test_credentials.py`
- Modify: `tests/test_model_profile_probe.py`
- Modify: `tests/test_model_runtime.py`
- Modify: `tests/test_workbench_api.py`
- Modify: `tests/tts-provider.test.ts`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `release/RELEASE_NOTES_v0.9.0-beta.3.md`

- [ ] **Step 1: Review the baseline diff for accidental scope expansion**

Run: `git status --short`, `git diff --stat`, and `git diff --check`.
Expected: exactly the Beta 3 Doubao files above are modified; no secret, database, generated audio, ZIP, or unrelated file is present.

- [ ] **Step 2: Run focused Python baseline tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_credentials tests.test_model_profile_probe tests.test_model_runtime tests.test_workbench_api -v`
Expected: all tests pass.

- [ ] **Step 3: Run frontend and TypeScript baseline tests**

Run: `npm run test:ts`, `npm run test:web`, and `npx tsc --noEmit`.
Expected: all tests pass with zero failures.

- [ ] **Step 4: Verify the real Doubao happy path without logging credentials**

Use the existing saved credentials through the application API to refresh the voice catalog, synthesize one very short preview, probe the default profile, and verify valid audio bytes plus an available profile state. Do not print request headers or credential values.
Expected: catalog refresh succeeds, preview succeeds, and the profile error is cleared.

- [ ] **Step 5: Commit the stable feature baseline**

Stage only the files listed in this task and commit with `git commit -m "feat: support doubao api key tts"`.

### Task 2: 简化豆包凭据并折叠旧版兼容项

**Files:**
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/test_workbench_api.py`

- [ ] **Step 1: Write failing UI contract tests**

Add assertions that the normal Doubao form exposes API Key, Access Key ID, Secret Access Key and service version; AppID and Access Token live inside `<details class="legacy-tts-credentials">`; fixed API URL, resource ID, cluster, exact model ID and voice ID are absent from the normal Doubao flow.

- [ ] **Step 2: Run the targeted UI test and observe failure**

Run: `node --test tests/workbench-ui.test.mjs`.
Expected: new progressive-disclosure assertions fail before implementation.

- [ ] **Step 3: Lock backend compatibility with tests**

Add API assertions that omitted legacy secrets preserve stored values and that both API Key and AppID/Access Token configurations remain readable by the runtime.

- [ ] **Step 4: Implement provider-specific progressive disclosure**

Update static and generated credential forms so Doubao shows the four normal fields, places legacy fields in the collapsed details element, preserves saved masked values, and leaves non-Doubao provider forms unchanged.

- [ ] **Step 5: Run focused regressions**

Run: `node --test tests/workbench-ui.test.mjs` and `.venv\Scripts\python.exe -m unittest tests.test_workbench_api tests.test_credentials -v`.
Expected: all targeted tests pass.

- [ ] **Step 6: Commit**

Run: `git add web-ui/public/index.html web-ui/public/app.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/test_workbench_api.py` then `git commit -m "ui: simplify doubao credential setup"`.

### Task 3: 将“保存并检测”改成可理解的分阶段反馈

**Files:**
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/test_workbench_api.py`

- [ ] **Step 1: Write failing interaction tests**

Test one-click ordering `PUT credentials → test one selected/default profile → refresh voices`, duplicate-click prevention, button busy labels, persistent `role="status"` result, stale-error clearing, and a notice that detection synthesizes a very short phrase.

- [ ] **Step 2: Run tests and confirm RED**

Run: `node --test tests/workbench-ui.test.mjs`.
Expected: the new staged-feedback assertions fail.

- [ ] **Step 3: Implement staged state and rendering**

Add `providerCredentialChecks[accountId]`, use the existing busy-button helper or add `setButtonBusy(button, busy, label)`, test only the selected/default Doubao profile, refresh the catalog, then show a persistent success/partial/failure panel plus a concise toast.

- [ ] **Step 4: Guard failure branches**

Ensure saved credentials remain saved when testing or catalog refresh fails, the UI tells the user which stage failed, and repeated clicks cannot start concurrent requests.

- [ ] **Step 5: Run focused and full Web regressions**

Run: `node --test tests/workbench-ui.test.mjs`, `.venv\Scripts\python.exe -m unittest tests.test_workbench_api -v`, and `npm run test:web`.
Expected: all tests pass, including existing task, draft, voice, library and review-gate behavior.

- [ ] **Step 6: Commit**

Run: `git add web-ui/public/app.mjs web-ui/public/index.html web-ui/public/styles.css tests/workbench-ui.test.mjs tests/test_workbench_api.py` then `git commit -m "ui: clarify doubao save and detection feedback"`.

### Task 4: 建立方案 A 的全局设计令牌和组件基线

**Files:**
- Modify: `web-ui/public/styles.css`
- Modify: `web-ui/public/index.html`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/workbench-layout.test.mjs`

- [ ] **Step 1: Write failing design-system tests**

Assert semantic color, typography, spacing, radius, shadow and motion tokens; minimum 44px interactive controls; shared section-card, status-badge and inline-feedback classes; visible focus treatment; dark-mode token parity.

- [ ] **Step 2: Confirm RED**

Run: `node --test tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs`.
Expected: new token/component requirements fail.

- [ ] **Step 3: Implement tokens without changing business selectors**

Refactor CSS variables into semantic warm-gray/orange tokens and map existing selectors onto the shared component primitives. Keep existing element IDs, data attributes and JavaScript hooks unchanged.

- [ ] **Step 4: Normalize controls and motion**

Apply 44px minimum target sizes, consistent typography and spacing, subtle transitions, `:focus-visible`, and `prefers-reduced-motion` handling.

- [ ] **Step 5: Run regressions and commit**

Run: `node --test tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs` then `npm run test:web`.
Expected: all pass.

Stage with `git add web-ui/public/styles.css web-ui/public/index.html tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs`, then commit with `git commit -m "ui: establish cutroom design system"`.

### Task 5: 加固三栏壳层与五档响应式布局

**Files:**
- Modify: `web-ui/public/styles.css`
- Modify: `web-ui/public/index.html`
- Modify: `tests/workbench-layout.test.mjs`
- Modify: `tests/workbench-ui.test.mjs`

- [ ] **Step 1: Add failing viewport tests**

Cover 375, 768, 1024, 1440 and 1920 pixel widths. Assert no document-level horizontal overflow, four navigation routes remain reachable, rail/content collapse predictably, and only dense task boards may scroll internally.

- [ ] **Step 2: Run layout tests and confirm failure**

Run: `node --test tests/workbench-layout.test.mjs`.
Expected: at least one new breakpoint or overflow assertion fails.

- [ ] **Step 3: Implement responsive shell rules**

Retain the desktop three-column structure, compact the rail and secondary column at tablet widths, stack page sections on mobile, and preserve theme/view/sidebar localStorage behavior.

- [ ] **Step 4: Verify all breakpoints and existing navigation**

Run: `node --test tests/workbench-layout.test.mjs tests/workbench-ui.test.mjs`.
Expected: all viewport and navigation tests pass.

- [ ] **Step 5: Commit**

Run: `git add web-ui/public/styles.css web-ui/public/index.html tests/workbench-layout.test.mjs tests/workbench-ui.test.mjs` then `git commit -m "ui: harden responsive workbench shell"`.

### Task 6: 统一七个页面的信息层级和状态语言

**Files:**
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/ad-route-ui.test.mjs`

- [ ] **Step 1: Write failing page-hierarchy tests**

For tasks, task detail, create task, mirror library, packaging library, AI settings and storage/cache, assert one page title, one concise description, a consistent action region, semantic status badges and inline feedback regions.

- [ ] **Step 2: Confirm RED**

Run: `node --test tests/workbench-ui.test.mjs tests/ad-route-ui.test.mjs`.
Expected: new hierarchy/status assertions fail.

- [ ] **Step 3: Introduce small rendering helpers**

Add shared page-header, status-badge and inline-feedback render helpers only where dynamic markup needs them. Preserve all task payloads, model-role IDs, stage transitions, review gates, library actions and advertising route behavior.

- [ ] **Step 4: Apply the shared hierarchy to all seven pages**

Reduce duplicate explanatory copy, keep primary actions visually dominant, move fields classified by the approved design as advanced behind disclosure, and retain every existing control and workflow.

- [ ] **Step 5: Run business regressions**

Run: `npm run test:web`, `npm run test:ts`, and `.venv\Scripts\python.exe -m unittest tests.test_workbench_api tests.test_model_runtime -v`.
Expected: all pass.

- [ ] **Step 6: Commit**

Run: `git add web-ui/public/index.html web-ui/public/app.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/ad-route-ui.test.mjs` then `git commit -m "ui: unify workbench page hierarchy"`.

### Task 7: 补齐键盘、屏幕阅读器和状态可见性

**Files:**
- Modify: `web-ui/public/index.html`
- Modify: `web-ui/public/app.mjs`
- Modify: `web-ui/public/styles.css`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/workbench-layout.test.mjs`

- [ ] **Step 1: Write failing accessibility tests**

Assert page regions use `aria-labelledby`, form controls have labels/help, asynchronous actions expose `aria-busy`, results use polite `aria-live`, keyboard focus remains visible, and reduced-motion users receive no nonessential transitions.

- [ ] **Step 2: Confirm RED**

Run: `node --test tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs`.
Expected: new accessibility assertions fail.

- [ ] **Step 3: Implement semantic and focus fixes**

Add accessible labels, descriptions, status announcements and busy-state attributes without changing control IDs or click handlers. Ensure light and dark status colors remain distinguishable.

- [ ] **Step 4: Run full frontend regression and commit**

Run: `npm run test:web` and `npx tsc --noEmit`.
Expected: all tests and type checks pass.

Stage with `git add web-ui/public/index.html web-ui/public/app.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs`, then commit with `git commit -m "ui: improve workbench accessibility"`.

### Task 8: 更新安装、帮助和发布说明

**Files:**
- Modify: `README.md`
- Modify: `web-ui/public/help.html`
- Modify: `release/RELEASE_NOTES_v0.9.0-beta.3.md`
- Modify: `docs/superpowers/specs/2026-09-14-doubao-tts-voice-selection-design.md`
- Modify: `tests/workbench-ui.test.mjs`

- [ ] **Step 1: Add failing copy-contract tests**

Assert user-facing docs consistently state: API Key is required for synthesis/preview/test; AK/SK is required for automatic catalog refresh; AppID/Access Token is optional legacy compatibility; save-and-detect synthesizes a very short phrase.

- [ ] **Step 2: Confirm RED**

Run: `node --test tests/workbench-ui.test.mjs`.
Expected: documentation contract assertions fail until copy is updated.

- [ ] **Step 3: Update docs and superseded design notes**

Update README, in-app help and Beta 3 notes. Mark the old voice-selection spec credential/setup sections as superseded by the approved 2026-09-15 systemization design; do not rewrite its historical implementation record.

- [ ] **Step 4: Verify and commit**

Run: `node --test tests/workbench-ui.test.mjs` and `git diff --check`.
Expected: pass with no whitespace errors.

Stage with `git add README.md web-ui/public/help.html release/RELEASE_NOTES_v0.9.0-beta.3.md docs/superpowers/specs/2026-09-14-doubao-tts-voice-selection-design.md tests/workbench-ui.test.mjs`, then commit with `git commit -m "docs: update beta 3 setup and ui guidance"`.

### Task 9: 全量验证、隔离安装、GitHub 合并与发布

**Files:**
- Verify: all tracked files changed since `405b033`
- Generate: `release/dist/CUTROOM-v0.9.0-beta.3-windows-x64.zip`
- Generate: `release/dist/CUTROOM-v0.9.0-beta.3-windows-x64.zip.sha256`

- [ ] **Step 1: Run the complete automated verification suite**

Run:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
npm run test:ts
npm run test:web
npx tsc --noEmit
git diff --check
```

Expected: every test passes, TypeScript reports no errors, and the diff has no whitespace errors.

- [ ] **Step 2: Exercise existing business flows against a copied database**

Create a temporary directory with `New-Item`, copy the current workbench database into it, start the server against that copy, and verify task list/detail, task creation draft, stage/review gates, model roles, mirror library, packaging library, storage/cache and theme/sidebar preferences. Never mutate or delete the live database.
Expected: all prior workflows behave as before.

- [ ] **Step 3: Run real Doubao acceptance without exposing secrets**

Using saved Windows credentials, refresh the voice catalog, preview two available voices, test one default profile, and create/freeze a task snapshot that retains its chosen voice after the account catalog changes.
Expected: all four checks pass and no credential value appears in logs or JSON responses.

- [ ] **Step 4: Perform code review and address findings**

Review the complete branch diff for regressions, secret exposure, duplicated requests, weak error branches, responsive overflow and release-package safety. Fix every critical or high-confidence issue, then rerun the complete suite from Step 1.

- [ ] **Step 5: Build the deterministic release artifacts**

Run:

```powershell
.venv\Scripts\python.exe scripts\build_release.py --root . --output release\dist --version 0.9.0-beta.3
.venv\Scripts\python.exe -m unittest tests.test_release_packaging -v
```

Expected: the ZIP and `.zip.sha256` are produced by the allowlist builder and packaging tests pass.

- [ ] **Step 6: Inspect the ZIP for forbidden content**

List the ZIP entries and scan names/content for `.env`, databases, runtime data, generated audio/video, credential strings, `.git`, node_modules and local caches.
Expected: none are present; `release-manifest.json` records version `0.9.0-beta.3` and the final source commit.

- [ ] **Step 7: Verify isolated installation and startup**

Expand the ZIP into a newly created temporary directory, run `安装CUTROOM.bat` and `启动CUTROOM.bat` from that directory with bounded process execution, verify HTTP 200 at the local workbench URL, then run the smoke flow against a copied database.
Expected: a colleague can install and start the package without relying on the repository checkout.

- [ ] **Step 8: Push, review, merge and release**

Push `codex/tts-voice-refresh-fix`, create a pull request to `main`, wait for GitHub checks, merge only when green, then tag the merged `origin/main` commit as `v0.9.0-beta.3`. Create the GitHub release from `release/RELEASE_NOTES_v0.9.0-beta.3.md` and upload both generated artifacts.
Expected: the tag, release notes, ZIP and checksum all point to the same merged commit.

- [ ] **Step 9: Final verification from GitHub**

Download the published checksum text through GitHub, compare it with the local artifact hash, and verify the release download link in README resolves.
Expected: hashes match and the colleague download path is live.
