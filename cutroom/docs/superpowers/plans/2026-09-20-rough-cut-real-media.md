# Rough-Cut Real Media Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CUTROOM's rough-cut color placeholders with a secure, continuous real-media preview and wire every originally designed shot-level edit before picture lock.

**Architecture:** A new `media_catalog.rough_media` boundary resolves only task-authorized assets to cached previews or read-only source files. The authenticated workbench router streams those files, the task-detail API projects media descriptors without absolute paths, and a focused browser model drives a continuous scene sequencer, visual timeline, candidate previews, reorder, trim, and playhead split. Existing rough-cut transactions, undo/redo, and render semantics remain authoritative.

**Tech Stack:** Python 3 `unittest`, SQLite, existing `media_catalog` preview cache, `http.server` workbench router, browser-native `<img>`/`<video>`, JavaScript ES modules, Node test runner, existing headless Chrome layout harness.

**Spec:** `docs/superpowers/specs/2026-09-20-rough-cut-real-media-design.md`

## Global Constraints

- The original design at `docs/superpowers/specs/2026-09-05-colleague-video-workbench-design.md` remains the product contract.
- Original media is read-only; previews and proxies are rebuildable derivatives.
- The browser never submits a local path and no response exposes an absolute local path.
- Digital-human narration stays continuous and cannot be deleted, split, or reordered.
- Every rough-cut mutation previews its impact before applying and continues to use the existing undo/redo transaction log.
- The media endpoint accepts only task-authorized `image` and `video` assets and revalidates availability on every request.
- Missing media must be shown as an explicit blocking error, never as a success-colored placeholder.
- No Remotion dependency; rendering remains FFmpeg-based.

## Review Focus

- A task-scoped URL using an asset ID from another task must not reveal whether that file exists; Task 1 and Task 2 pin this to a safe rejection.
- A cached preview whose source signature is stale must never mask a changed source file; Task 1 tests cache invalidation and controlled source fallback.
- A source file that goes offline after the page loads must stop playback and block picture lock; Task 3 and Task 6 cover both server and UI behavior.
- A video source window shorter than its narrative slot must map playback exactly like the render adapter rather than drift into the next source segment; Task 4 pins the mapping.
- Reorder, trim, and split must preserve the narration axis and survive refresh/undo/redo; Task 5 and Task 7 test the API payloads and persisted result.

---

### Task 1: Task-Authorized Rough Media Resolver

**Files:**
- Create: `media_catalog/rough_media.py`
- Create: `tests/test_rough_media.py`
- Modify: `media_catalog/preview.py:45-72`

**Interfaces:**
- Consumes: `CatalogDatabase`, `preview_cache`, `analysis_runs(stage='preview')`, active task source scopes, current `scene_cards`, and registered `asset_locations`.
- Produces: `RoughMediaFile`, `RoughMediaError`, `resolve_rough_media(db, task_id, asset_id, purpose)`, `describe_rough_media(db, task_id, asset_id)`, and `assert_picture_media_available(db, task_id)`.

- [ ] **Step 1: Write resolver tests that fail because the module does not exist**

```python
# tests/test_rough_media.py
from media_catalog.rough_media import (
    RoughMediaError,
    assert_picture_media_available,
    describe_rough_media,
    resolve_rough_media,
)

def test_selected_scene_image_resolves_without_exposing_absolute_path(self):
    resolved = resolve_rough_media(self.db, "task-a", "image-a", "preview")
    self.assertEqual(resolved.media_kind, "image")
    self.assertEqual(resolved.delivery, "source")
    self.assertEqual(resolved.path.read_bytes(), b"image-a")
    descriptor = describe_rough_media(self.db, "task-a", "image-a")
    self.assertNotIn(str(self.library_root), str(descriptor))

def test_task_cannot_resolve_another_tasks_asset(self):
    with self.assertRaisesRegex(RoughMediaError, "ROUGH_MEDIA_NOT_ALLOWED"):
        resolve_rough_media(self.db, "task-a", "image-b", "preview")

def test_valid_cache_wins_and_stale_cache_falls_back_to_source(self):
    cached = resolve_rough_media(self.db, "task-a", "video-a", "preview")
    self.assertEqual(cached.delivery, "cache")
    self.source_video.write_bytes(b"changed-source")
    fallback = resolve_rough_media(self.db, "task-a", "video-a", "preview")
    self.assertEqual(fallback.delivery, "source")

def test_offline_or_escaped_asset_blocks_picture_lock(self):
    self.source_image.unlink()
    with self.assertRaisesRegex(RoughMediaError, "ROUGH_MEDIA_OFFLINE"):
        assert_picture_media_available(self.db, "task-a")
```

- [ ] **Step 2: Run the resolver tests and verify the expected import failure**

Run: `.venv\Scripts\python -m unittest tests.test_rough_media -v`  
Expected: FAIL with `ModuleNotFoundError: No module named 'media_catalog.rough_media'`.

- [ ] **Step 3: Expose a public source-signature helper without changing preview generation behavior**

```python
# media_catalog/preview.py
def source_signature(db, asset_id: str) -> str:
    return _source_signature(db, asset_id)
```

Keep `_source_signature()` as the single implementation and make existing internal callers continue using it.

- [ ] **Step 4: Implement the resolver and explicit domain error**

```python
# media_catalog/rough_media.py
@dataclass(frozen=True)
class RoughMediaFile:
    path: Path
    media_kind: str
    delivery: str

class RoughMediaError(RuntimeError):
    def __init__(self, status: int, code: str, public_message: str):
        super().__init__(f"{code}: {public_message}")
        self.status = status
        self.code = code
        self.public_message = public_message

def resolve_rough_media(db, task_id: str, asset_id: str, purpose: str) -> RoughMediaFile:
    if purpose not in {"thumbnail", "preview"}:
        raise RoughMediaError(404, "ROUGH_MEDIA_UNSUPPORTED", "不支持该预览用途。")
    owned = _authorized_asset_ids(db, task_id)
    if asset_id not in owned:
        raise RoughMediaError(404, "ROUGH_MEDIA_NOT_ALLOWED", "当前任务不能访问该素材。")
    source, media_kind = _available_source(db, asset_id)
    cache_kind = "thumbnail" if purpose == "thumbnail" else "proxy"
    cached = _valid_cache_path(db, asset_id, cache_kind)
    if cached is not None:
        return RoughMediaFile(cached, media_kind, "cache")
    return RoughMediaFile(source, media_kind, "source")
```

Implement `_authorized_asset_ids()` as the union of current scene-card assets and `list_eligible_assets()` IDs. `_available_source()` must resolve both the library root and candidate path, reject symlink/root escape, require `state='available'`, require a file, and allow only `image` or `video`. `_valid_cache_path()` must keep the resolved file under `cache_root_for(db)`, require the stored preview source signature to equal `source_signature(db, asset_id)`, and ignore stale/missing cache rows.

`describe_rough_media()` returns only `media_kind`, `thumbnail_url`, `preview_url`, `preview_delivery`, `preview_available`, and `preview_problem`. `assert_picture_media_available()` resolves every current scene-card asset and reports the failing scene number in its public message.

- [ ] **Step 5: Run the resolver tests and verify they pass**

Run: `.venv\Scripts\python -m unittest tests.test_rough_media -v`  
Expected: all resolver tests PASS.

- [ ] **Step 6: Run existing preview-cache tests to prove no cache regression**

Run: `.venv\Scripts\python -m unittest tests.test_p1_closeout tests.test_database -v`  
Expected: PASS with no new warnings.

- [ ] **Step 7: Commit the resolver boundary**

```powershell
git add media_catalog/rough_media.py media_catalog/preview.py tests/test_rough_media.py
git commit -m "feat: resolve task-authorized rough media"
```

---

### Task 2: Authenticated Image and Video Streaming Routes

**Files:**
- Modify: `workbench/routes.py:38-112,195-345`
- Modify: `tests/test_workbench_server.py:543-569`

**Interfaces:**
- Consumes: `resolve_rough_media()` and existing `_send_media_file()`.
- Produces: `GET|HEAD /api/tasks/{task_id}/rough-media/{asset_id}/{thumbnail|preview}`.

- [ ] **Step 1: Add failing route tests for authentication, authorization, MIME, HEAD, and Range**

```python
def test_rough_media_requires_session_and_streams_task_image(self):
    self.seed_rough_media_task()
    path = "/api/tasks/task-a/rough-media/image-a/preview"
    status, _, body = self.request("GET", path)
    self.assertEqual(status, 403)
    self.assert_error(body, "AUTH_REQUIRED")
    cookie = self.bootstrap_cookie()
    status, headers, body = self.request("GET", path, headers={"Cookie": cookie})
    self.assertEqual(status, 200)
    self.assertEqual(headers["Content-Type"], "image/jpeg")
    self.assertEqual(body, self.image_bytes)

def test_rough_video_supports_head_and_byte_ranges(self):
    cookie = self.bootstrap_cookie()
    path = "/api/tasks/task-a/rough-media/video-a/preview"
    status, headers, body = self.request("GET", path, headers={"Cookie": cookie, "Range": "bytes=2-5"})
    self.assertEqual((status, body), (206, b"2345"))
    self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
    status, headers, body = self.request("HEAD", path, headers={"Cookie": cookie})
    self.assertEqual(status, 200)
    self.assertEqual(body, b"")

def test_rough_media_hides_assets_outside_the_task(self):
    cookie = self.bootstrap_cookie()
    status, _, body = self.request("GET", "/api/tasks/task-a/rough-media/image-b/preview", headers={"Cookie": cookie})
    self.assertEqual(status, 404)
    self.assertNotIn(str(self.library_root).encode(), body)
```

- [ ] **Step 2: Run route tests and verify the new path currently returns JSON 404**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_server.WorkbenchServerTests.test_rough_media_requires_session_and_streams_task_image tests.test_workbench_server.WorkbenchServerTests.test_rough_video_supports_head_and_byte_ranges tests.test_workbench_server.WorkbenchServerTests.test_rough_media_hides_assets_outside_the_task -v`  
Expected: FAIL because the router has no rough-media route.

- [ ] **Step 3: Add strict path matching and streaming**

```python
@staticmethod
def _rough_media_parts(path: str) -> tuple[str, str, str] | None:
    parts = path.split("/")
    if len(parts) != 7 or parts[:3] != ["", "api", "tasks"] or parts[4] != "rough-media":
        return None
    if parts[6] not in {"thumbnail", "preview"}:
        return None
    return tuple(unquote(value) for value in (parts[3], parts[5], parts[6]))

def _rough_media(self, handler, task_id: str, asset_id: str, purpose: str, *, head_only: bool) -> None:
    try:
        with CatalogDatabase.open(self.runtime.db_path) as db:
            media = resolve_rough_media(db, task_id, asset_id, purpose)
    except RoughMediaError as error:
        raise HttpError(error.status, error.code, error.public_message) from error
    self._send_media_file(handler, media.path, head_only=head_only)
```

Dispatch this route after session validation and before generic `/api/` JSON dispatch. Preserve `_send_media_file()` behavior for Range, disconnects, `no-store`, and `nosniff`.

- [ ] **Step 4: Re-run route tests and the existing review-media tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_server -v`  
Expected: all server tests PASS.

- [ ] **Step 5: Commit the streaming route**

```powershell
git add workbench/routes.py tests/test_workbench_server.py
git commit -m "feat: stream authenticated rough media"
```

---

### Task 3: Task Detail Media Descriptors and Server Picture-Lock Gate

**Files:**
- Modify: `workbench/api.py:1007-1023,1080-1105,1173-1202`
- Modify: `tests/test_workbench_api.py:83-116,190-215`

**Interfaces:**
- Consumes: `describe_rough_media()` and `assert_picture_media_available()`.
- Produces: media fields on every scene card/candidate and a server-authoritative picture-lock availability check.

- [ ] **Step 1: Add failing task-detail and lock-gate tests**

```python
def test_task_detail_projects_media_urls_without_absolute_paths(self):
    self.seed_rough_media_task()
    status, _, detail = self.request("GET", "/api/tasks/task-a")
    self.assertEqual(status, 200)
    scene = detail["sceneCards"][0]
    self.assertEqual(scene["media_kind"], "image")
    self.assertEqual(scene["preview_url"], "/api/tasks/task-a/rough-media/image-a/preview")
    self.assertEqual(scene["thumbnail_url"], "/api/tasks/task-a/rough-media/image-a/thumbnail")
    self.assertNotIn(str(self.library_root), json.dumps(detail, ensure_ascii=False))

def test_picture_lock_rejects_the_specific_offline_scene(self):
    self.seed_rough_media_task()
    self.image_path.unlink()
    status, _, payload = self.request("POST", "/api/tasks/task-a/picture/lock", {})
    self.assertEqual(status, 409)
    self.assertEqual(payload["error"]["code"], "ROUGH_MEDIA_OFFLINE")
    self.assertIn("第 1 镜头", payload["error"]["message"])
```

- [ ] **Step 2: Run the tests and verify descriptor and lock assertions fail**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api.WorkbenchApiTests.test_task_detail_projects_media_urls_without_absolute_paths tests.test_workbench_api.WorkbenchApiTests.test_picture_lock_rejects_the_specific_offline_scene -v`  
Expected: FAIL because media fields are absent and lock does not inspect rough media.

- [ ] **Step 3: Project descriptors onto copied dictionaries**

```python
def _with_rough_media(self, task_id: str, items: list[dict]) -> list[dict]:
    projected = []
    for item in items:
        copy = dict(item)
        asset_id = copy.get("asset_id")
        if asset_id:
            copy.update(describe_rough_media(self.db, task_id, asset_id))
        projected.append(copy)
    return projected
```

Use this helper for both `sceneCards` and `eligibleAssets`. A candidate that becomes unavailable remains listed with `preview_available=false` and a safe `preview_problem`; do not leak its absolute location.

- [ ] **Step 4: Enforce media availability immediately before picture lock**

```python
if key == ("picture", "lock"):
    assert_picture_media_available(self.db, task_id)
    if task.get("task_type") == "ad_mix":
        require_ad_picture_assets(self.db, task_id)
    return ApiResult(200, set_picture_lock(self.db, task_id, confirm_all=True))
```

Extend `translate_exception()` to preserve `RoughMediaError.status`, `.code`, and `.public_message`.

- [ ] **Step 5: Run task-detail, lock, and rough-cut API tests**

Run: `.venv\Scripts\python -m unittest tests.test_workbench_api tests.test_rough_cut -v`  
Expected: PASS.

- [ ] **Step 6: Commit descriptors and server gate**

```powershell
git add workbench/api.py tests/test_workbench_api.py
git commit -m "feat: expose rough media and guard picture lock"
```

---

### Task 4: Pure Continuous Preview and Editing Model

**Files:**
- Create: `web-ui/public/rough-preview.mjs`
- Modify: `web-ui/public/scene-editor.mjs:1-53`
- Modify: `tests/workbench-ui.test.mjs:947-983`

**Interfaces:**
- Consumes: scene cards containing narrative ranges, source windows, media URLs, and media kind.
- Produces: `sceneAtPlayhead()`, `sourceTimeAtPlayhead()`, `advancePlayhead()`, `moveCardId()`, `trimBounds()`, `pictureLockBlockers()`, and stricter `buildSceneEdit()` validation.

- [ ] **Step 1: Add failing pure-model tests**

```javascript
test("全局播放头定位场景并映射视频来源时间", () => {
  const cards = [
    { card_id: "a", narrative_start: 0, narrative_end: 4, enter_tc: 10, exit_tc: 14, media_kind: "video" },
    { card_id: "b", narrative_start: 4, narrative_end: 7, media_kind: "image" },
  ];
  assert.equal(sceneAtPlayhead(cards, 5).card_id, "b");
  assert.equal(sourceTimeAtPlayhead(cards[0], 2), 12);
  assert.deepEqual(advancePlayhead(cards, 3.9, 0.2), { playhead: 4.1, sceneId: "b", ended: false });
});

test("短来源窗口按渲染规则循环且不漂移", () => {
  const card = { narrative_start: 0, narrative_end: 8, enter_tc: 20, exit_tc: 23, media_kind: "video", playback_policy: "loop" };
  assert.equal(sourceTimeAtPlayhead(card, 5), 22);
});

test("排序和锁定门禁保持显式", () => {
  const cards = [
    { card_id: "a", fixed_anchor: false },
    { card_id: "voice", fixed_anchor: true },
    { card_id: "c", fixed_anchor: false },
  ];
  assert.deepEqual(moveCardId(cards, "c", 0), ["c", "voice", "a"]);
  assert.deepEqual(pictureLockBlockers([{ index: 1, preview_available: false, preview_problem: "素材离线" }]), ["第 1 镜头：素材离线"]);
});

test("裁剪和拆分拒绝非有限值与端点", () => {
  assert.throws(() => buildSceneEdit("trim", selected, { enter_tc: NaN, exit_tc: 4 }), /有效/);
  assert.throws(() => buildSceneEdit("split", selected, { playhead: selected.narrativeStart }), /内部/);
});
```

- [ ] **Step 2: Run web unit tests and verify missing exports fail**

Run: `npm run test:web`  
Expected: FAIL because `rough-preview.mjs` and its exports do not exist.

- [ ] **Step 3: Implement the deterministic model**

```javascript
export function sceneAtPlayhead(cards, playhead) {
  const time = Number(playhead);
  return (cards || []).find((card, index, all) => {
    const start = Number(card.narrative_start);
    const end = Number(card.narrative_end);
    return time >= start && (time < end || (index === all.length - 1 && time === end));
  }) || null;
}

export function sourceTimeAtPlayhead(card, playhead) {
  const offset = Math.max(0, Number(playhead) - Number(card.narrative_start));
  const start = Number.isFinite(Number(card.enter_tc)) ? Number(card.enter_tc) : Number(card.narrative_start);
  const end = Number.isFinite(Number(card.exit_tc)) && Number(card.exit_tc) > start ? Number(card.exit_tc) : Number(card.narrative_end);
  const length = Math.max(0, end - start);
  if (!length) return start;
  return card.playback_policy === "loop" ? start + (offset % length) : Math.min(start + offset, end);
}
```

Implement the remaining exports without DOM access. `moveCardId()` must return every existing ID exactly once and merge the reordered visual IDs back into the original slots so every `fixed_anchor` stays at its original index. `trimBounds()` must reject windows outside the known source range or shorter than the narrative duration unless the playback policy explicitly allows loop. `pictureLockBlockers()` must include missing asset IDs, unsupported media kinds, unavailable previews, and safe problem text.

Extend `selectScene()` to expose `mediaKind`, `previewUrl`, `thumbnailUrl`, `previewAvailable`, `previewProblem`, numeric narrative/source ranges, and playback policy. Validate finite trim/split values in `buildSceneEdit()`.

- [ ] **Step 4: Run web unit tests and verify the pure model passes**

Run: `npm run test:web`  
Expected: PASS.

- [ ] **Step 5: Commit the browser model**

```powershell
git add web-ui/public/rough-preview.mjs web-ui/public/scene-editor.mjs tests/workbench-ui.test.mjs
git commit -m "feat: model continuous rough preview"
```

---

### Task 5: Real Main Preview, Timeline Thumbnails, and Candidate Gallery

**Files:**
- Modify: `web-ui/public/app.mjs:629-745,782-791`
- Modify: `web-ui/public/styles.css:757-803`
- Modify: `tests/workbench-layout.test.mjs`

**Interfaces:**
- Consumes: Task 3 media descriptors and Task 4 playhead functions.
- Produces: real `<img>`/`<video>` preview, continuous play/pause/scrub behavior, real timeline thumbnails, and preview-before-replace candidate cards.

- [ ] **Step 1: Add a failing headless-browser rough-cut fixture**

Build a fixture in `tests/workbench-layout.test.mjs` with one image scene, one video scene, and two eligible candidates. Execute the real rendering functions and assert:

```javascript
assert.equal(actual.stageImageSrc, "/api/tasks/task-a/rough-media/image-a/preview");
assert.equal(actual.timelineThumbnailCount, 2);
assert.equal(actual.candidateThumbnailCount, 2);
assert.equal(actual.selectCount, 0);
assert.equal(actual.replacementCalls, 0);
assert.equal(actual.activeCandidateLabel, "候选/园区全景.jpg");
```

The fixture first clicks a candidate card and proves no replacement API call occurs, then clicks the explicit replace button and proves the impact request contains only the selected scene and candidate asset.

- [ ] **Step 2: Run the browser test and verify it fails on the color placeholder/dropdown UI**

Run: `node --test --test-concurrency=1 tests/workbench-layout.test.mjs`  
Expected: FAIL because the stage has no real media element, timeline has no images, and replacement is still a `<select>`.

- [ ] **Step 3: Render media elements from descriptors**

```javascript
function roughMediaElement(scene, { thumbnail = false } = {}) {
  const url = thumbnail ? scene.thumbnail_url : scene.preview_url;
  if (!scene.preview_available || !url) return roughMediaError(scene);
  const media = document.createElement(scene.media_kind === "video" && !thumbnail ? "video" : "img");
  media.className = thumbnail ? "rough-thumbnail" : "rough-media";
  media.src = url;
  if (media instanceof HTMLVideoElement) {
    media.preload = "metadata";
    media.playsInline = true;
  } else {
    media.alt = scene.relative_path || "当前镜头画面";
  }
  return media;
}
```

Replace `portrait-frame` with the media element and a visible loading/error state. Keep narration as an overlay, not as a replacement for the picture. Attach video `loadedmetadata`, `timeupdate`, `ended`, and `error` events to the global playhead; use a requestAnimationFrame clock for image duration. Play/pause and range input must drive the same playhead state.

- [ ] **Step 4: Render real timeline and visual candidates**

Change `sceneCardButton()` to append `roughMediaElement(card, {thumbnail:true})`. Replace the `<select>` in `renderSceneInspector()` with candidate buttons containing thumbnails and metadata. Store the previewed candidate separately from the adopted scene; only the explicit replacement button calls `showImpact(buildSceneEdit("replace", ...))`.

- [ ] **Step 5: Add focused styles without changing the workbench shell**

```css
.rough-media { width: min(100%, 360px); max-height: 58vh; aspect-ratio: 9 / 16; object-fit: contain; background: #080b09; }
.rough-thumbnail { width: 100%; height: 64px; object-fit: cover; border-radius: 7px; background: #111512; }
.candidate-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.candidate-card[aria-pressed="true"] { border-color: var(--hot); box-shadow: 0 0 0 2px color-mix(in srgb, var(--hot) 20%, transparent); }
.rough-media-error { display: grid; place-items: center; min-height: 260px; padding: 24px; color: #fff; background: #381d19; text-align: center; }
```

Preserve the 760px narrow-layout behavior and local horizontal timeline scrolling.

- [ ] **Step 6: Re-run all web tests**

Run: `npm run test:web`  
Expected: PASS, including the real-media browser fixture.

- [ ] **Step 7: Commit real media rendering**

```powershell
git add web-ui/public/app.mjs web-ui/public/styles.css tests/workbench-layout.test.mjs
git commit -m "feat: show real media in rough cut"
```

---

### Task 6: Reorder, Trim, Real Playhead Split, and Lock Blockers

**Files:**
- Modify: `web-ui/public/app.mjs:716-791,2860-2925`
- Modify: `web-ui/public/styles.css:780-803`
- Modify: `tests/workbench-ui.test.mjs`
- Modify: `tests/workbench-layout.test.mjs`

**Interfaces:**
- Consumes: `moveCardId()`, `trimBounds()`, `pictureLockBlockers()`, current global playhead, and existing `showImpact()`.
- Produces: mouse/keyboard reorder, source trim inputs, actual-playhead split, and explicit lock blocking UI.

- [ ] **Step 1: Add failing interaction tests**

In the headless fixture, assert the exact payloads after user actions:

```javascript
assert.deepEqual(actual.reorderEdit, { action: "reorder", card_ids: ["c", "a", "b"] });
assert.deepEqual(actual.trimEdit, { action: "trim", card_id: "a", enter_tc: 11, exit_tc: 15 });
assert.deepEqual(actual.splitEdit, { action: "split", card_id: "a", playhead: 2.75 });
assert.equal(actual.lockDisabled, true);
assert.match(actual.lockReason, /第 2 镜头.*素材离线/);
```

Also assert that drag reorder and “左移/右移” produce the same `card_ids`, and that the digital-human anchor exposes none of delete/split/reorder controls that would move the voice axis.

- [ ] **Step 2: Run web tests and verify the missing controls fail**

Run: `npm run test:web`  
Expected: FAIL because app.mjs has midpoint split, no trim/reorder UI, and an always-enabled lock button.

- [ ] **Step 3: Wire reorder with keyboard parity**

Use draggable timeline cards for pointer users and visible move-left/move-right buttons for keyboard users. Both paths must call:

```javascript
const ordered = moveCardId(cards.map((card) => card.card_id), selected.id, targetIndex);
await showImpact(buildSceneEdit("reorder", selected, { card_ids: ordered }));
```

Do not mutate `detail.sceneCards` before the impact is applied and task detail is reloaded.

- [ ] **Step 4: Wire source trim and actual-playhead split**

Render numeric/time inputs initialized from `enter_tc` and `exit_tc`. Validate with `trimBounds()` before calling `showImpact(buildSceneEdit("trim", ...))`. Rename the split button to “在播放头拆分” and pass the current global playhead; reject playheads outside the selected editable scene.

- [ ] **Step 5: Disable lock with explicit blockers**

```javascript
const blockers = pictureLockBlockers(cards);
lock.disabled = blockers.length > 0;
lock.title = blockers.join("；");
if (blockers.length) toolbar.append(element("p", "picture-lock-blockers", blockers.join("；")));
```

The server check from Task 3 remains authoritative if media disappears after render.

- [ ] **Step 6: Run web tests and backend edit regressions**

Run: `npm run test:web`  
Expected: PASS.  
Run: `.venv\Scripts\python -m unittest tests.test_rough_cut tests.test_render_mix_adapter -v`  
Expected: PASS.

- [ ] **Step 7: Commit the complete rough-cut controls**

```powershell
git add web-ui/public/app.mjs web-ui/public/styles.css tests/workbench-ui.test.mjs tests/workbench-layout.test.mjs
git commit -m "feat: complete rough-cut editing controls"
```

---

### Task 7: Full Regression and Real-Task Acceptance

**Files:**
- Create: `docs/validation/2026-09-20-rough-cut-real-media-acceptance.md`
- Modify: `docs/user-guide/cutroom-user-guide.md:345-350`
- Modify: `docs/validation/2026-09-06-cutroom-v4-fidelity-matrix.md:12`

**Interfaces:**
- Consumes: completed backend and frontend implementation plus the current 9-scene/91-candidate task.
- Produces: reproducible automated results, paired visual evidence, and corrected acceptance status.

- [ ] **Step 1: Run the focused suites before the full suite**

Run: `.venv\Scripts\python -m unittest tests.test_rough_media tests.test_workbench_server tests.test_workbench_api tests.test_rough_cut tests.test_render_mix_adapter -v`  
Expected: PASS.  
Run: `npm run test:web`  
Expected: PASS.

- [ ] **Step 2: Run the project-wide suites**

Run: `.venv\Scripts\python -m unittest discover -s tests -p "test_*.py"`  
Expected: zero failures and zero errors.  
Run: `npm run test:ts`  
Expected: zero failures.  
Run: `npm run test:web`  
Expected: zero failures.

- [ ] **Step 3: Start the real workbench and verify the current task without mutating its original media**

Use the repository's existing workbench launch command and the current task database. In the UI:

1. Open all nine scenes and record that main preview, timeline thumbnail, and source metadata agree.
2. Play continuously from the first to the last scene.
3. Preview three candidates without applying; verify no database edit log is added.
4. Apply one replacement, one reorder, one trim, and one non-midpoint split through impact confirmation.
5. Refresh, undo, redo, and confirm the state persists.
6. Temporarily make a copied acceptance asset unavailable, prove lock is blocked, restore it, and prove the block clears. Do not rename or modify the user's only source file.
7. Lock the isolated acceptance copy, render it, and compare the output shot use/order with the locked scene cards.

- [ ] **Step 4: Capture paired evidence and write the acceptance report**

The report must record commit, database copy, task ID, commands, pass/fail counts, screenshots for main preview/timeline/candidates/offline blocker, applied edit IDs, rendered candidate signature, and any remaining limitation. It must explicitly retract the old “rough-cut fidelity passed” claim for the placeholder implementation and replace it only if real pixels and real edits pass.

- [ ] **Step 5: Update the user guide and fidelity matrix**

The user guide must tell users they can see and play real material in rough cut, preview a candidate before replacement, and cannot lock while any scene is unavailable. The fidelity matrix must link the new paired screenshots and acceptance report rather than relying on the old color-block screenshots.

- [ ] **Step 6: Re-run documentation and working-tree checks**

Run: `git diff --check`  
Expected: no output.  
Run: `git status --short`  
Expected: only the intended implementation, test, user-guide, and validation files before commit.

- [ ] **Step 7: Commit the verified delivery**

```powershell
git add docs/user-guide/cutroom-user-guide.md docs/validation/2026-09-06-cutroom-v4-fidelity-matrix.md docs/validation/2026-09-20-rough-cut-real-media-acceptance.md
git commit -m "docs: verify real-media rough cut"
```

---

## Spec Coverage Check

- Real current-shot pixels, continuous rough-v1 playback, and real timeline thumbnails: Tasks 3–5.
- Visual candidate preview before replace, with no mutation until confirmation: Task 5.
- Reorder, source trim, actual-playhead split, delete, undo, and redo: Tasks 4 and 6, backed by the existing rough-cut transaction engine.
- Task-scoped media authorization, cache/source selection, Range streaming, source-change detection, and path-escape rejection: Tasks 1 and 2.
- Frontend and server picture-lock blockers for offline or unsupported scenes: Tasks 3 and 6.
- Narrow-layout usability, full automated regression, real 9-scene/91-candidate validation, and locked-render parity: Task 7.
- Original-media read-only behavior and no general-model upload: Global Constraints plus Tasks 1, 2, and 7.

---

## Final Completion Gate

Do not claim this feature complete until all of the following are simultaneously true:

- Focused Python and web suites pass.
- Full Python, TypeScript, and web suites pass.
- The real task displays actual pixels for all nine selected scenes.
- Candidate preview does not mutate selection before confirmation.
- Reorder, trim, non-midpoint split, undo, redo, and refresh persistence are demonstrated.
- Offline media blocks picture lock in both UI and API.
- The locked scene-card order matches the rendered output.
- The acceptance report and fidelity matrix contain current evidence rather than placeholder-era screenshots.
