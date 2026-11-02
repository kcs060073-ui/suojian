# Xinjiang Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复字幕同步和样式绑定，生成三条全新可追溯 Seedance B-roll，并产出停在人工审片门禁的 `p2-xinjiang-v2.mp4`。

**Architecture:** TypeScript 纯函数完成 Whisper 单调对齐和 ASS 生成；Python 从 confirmed packaging plan 生成任务专属 ASS 并绑定依赖快照；Seedance 显式批次限制三次 POST；v2 数据库通过 SQLite backup API 建立后改绑新素材。

**Tech Stack:** TypeScript 5.7、Node test runner、`opencc-js@1.4.1`、Python `unittest`、SQLite、FFmpeg/libass、Seedance。

## Global Constraints

- 只在 `codex/p1-p2-recovery` 改动；首轮 MP4、数据库、旧 B-roll、旧 ASS 不覆盖、不删除。
- coverage < `0.80`、cue 缺锚点、时间倒退或越界时失败关闭。
- `sub-modern-clean-v1`：阿里妈妈数黑、72/82、白字、重点 `#FD9437`、4px 描边、1px 阴影、`MarginV=430`、12 字/行、最多两行、`\fad(60,80)`。
- replace 模式；尾垫 ≤ `0.12s`；重叠取中点；“同样的逻辑”在 `2.40s ± 0.12s`。
- 仅三条 9:16、1080p、5 秒、无水印 Seedance；每条最多一次创建请求，失败停止且不回退。
- 新文件名含 run id，保存 model、task id、prompt、时间、SHA-256。
- 新 DB、`p2-xinjiang-v2`、`out/p2-xinjiang-v2.mp4`；QA 后停在 `awaiting_human_review`，审批和发布表为空。

---

### Task 1: 单调词级对齐

**Files:** Create `src/subtitle-alignment.ts`, `tests/subtitle-alignment.test.ts`; modify `package.json`, `package-lock.json`。

**Interface:**

```ts
export interface TimedWord { w: string; start: number; end: number }
export interface AlignedChar { scriptIndex: number; start: number; end: number; confidence: number }
export interface AlignmentResult { chars: AlignedChar[]; coverage: number }
export class AlignmentError extends Error {
  constructor(public readonly code: string, message: string) { super(code + ": " + message); }
}
export function alignScriptToWords(
  scriptText: string, words: TimedWord[], speed: number, leadSeconds: number,
): AlignmentResult;
```

- [ ] Install exact `opencc-js@1.4.1`.
- [ ] Write failing tests for traditional→simplified, `40%`↔`百分之四十` followed by stable “同样的逻辑”, low coverage, empty words and regressing timestamps.
- [ ] Run `node --test -r ts-node/register tests/subtitle-alignment.test.ts`; confirm missing-module RED.
- [ ] Implement OpenCC/full-width/Latin normalization, original-index retention, per-word character timing, deterministic Needleman–Wunsch costs 0/1/1/1, monotonic interpolation and `source/speed-lead`.
- [ ] Run focused test and `npm run test:ts`; commit `feat: align subtitles to whisper words`.

### Task 2: modern-clean ASS

**Files:** Create `src/subtitle-ass.ts`, `tests/subtitle-ass.test.ts`; modify `scripts/make-ass.ts`, `assets/manifests/subtitle-presets.json`。

**Interface:** `buildAssDocument(input): { ass: string, metadata: SubtitleMetadata }`; metadata includes schema 1, preset, coverage, speed, lead, cues, `assSha256`。

- [ ] Write failing tests for exact 72/82 styles, outline/shadow/margin/fade, 12-char/two-line wrap, midpoint overlap, no cumulative dwell and real anchor range 2.28–2.52s.
- [ ] Run focused test; confirm RED.
- [ ] Add `sub-modern-clean-v1` with all Global Constraints and commercial-approved font.
- [ ] Build replace-only cues from Task 1; tail pad ≤0.12s; midpoint split; throw `CUE_ANCHOR_MISSING`, `TIMELINE_REGRESSION`, `CUE_OUT_OF_RANGE`.
- [ ] Preserve legacy CLI and add `--script --words --output --metadata --preset --speed --lead --duration`; supplied missing preset is fatal.
- [ ] Run focused/full TS tests and real Xinjiang dry output; commit `feat: render modern task subtitles`.

### Task 3: confirmed preset → assemble

**Files:** Create `media_catalog/subtitles.py`, `tests/test_subtitle_binding.py`; modify `media_catalog/render_mix_adapter.py`, `media_catalog/assemble.py`, `media_catalog/cli.py` and their tests。

**Interface:**

```py
def compile_task_ass(
    project_root: Path, work_dir: Path, task_id: str, preset_id: str,
    script_path: Path, words_path: Path, *, runner=None,
) -> dict:
    """Return immutable ASS/metadata paths and verified signatures."""
```

- [ ] Write failing tests: confirmed preset controls ASS; wrong/missing sidecar raises `ASS_PRESET_MISMATCH`; snapshot binds preset, coverage, script, words, ASS, metadata, font; mutations fail verification.
- [ ] Run `py -3 -m unittest tests.test_subtitle_binding tests.test_render_mix_adapter tests.test_assemble -v`; confirm RED.
- [ ] Resolve local node/ts-node with `shell=False`; clear proxies/TS_NODE overrides; write `runtime/jobs/<task>/subtitles.<digest>.ass`; validate schema/preset/coverage/SHA/source signatures; immutable mismatch raises `ASS_CONTENT_MISMATCH`.
- [ ] Extend `compile_render_mix_job`/`assemble` with `subtitle_sources` and runner injection. A confirmed subtitle item requires sources or matching sidecar.
- [ ] Add `assemble --script --words`; reject partial pair and pairing with `--ass`.
- [ ] Run focused tests, `py -3 -m unittest discover -s tests -q`, `npm run test:ts`; commit `feat: bind confirmed subtitle preset`.

### Task 4: bounded Seedance provenance

**Files:** Create `src/broll-generation.ts`, `tests/broll-generation.test.ts`; modify `scripts/generate-broll.ts`。

**Interface:**

```ts
export interface BrollRequest { key: string; prompt: string }
export interface BrollProvenanceItem {
  key: string; prompt: string; model: string; taskId: string;
  file: string; createdAt: string; sha256: string;
}
export async function runGenerationBatch(
  requests: BrollRequest[], options: GenerationOptions,
): Promise<{ schemaVersion: 1; runId: string; items: BrollProvenanceItem[] }>;
```

- [ ] Write failing injected tests: exactly 3 POSTs; second failure stops at 2; request count 2/4 fails; names unique; provenance complete.
- [ ] Run focused test; confirm RED.
- [ ] Require three unique keys; one POST each; abort on create/poll/download/probe/hash failure; exclusive `seed_<run-id>_<01..03>_<slug>.mp4`; explicit mode never updates old manifest.
- [ ] Add `--requests --run-id --output-dir --manifest`; remove duplicate load-env; preserve legacy mode.
- [ ] Run focused/full TS tests; commit `feat: constrain seedance broll generation`.

### Task 5: isolated v2 candidate

**Files:** Create `media_catalog/v2_candidate.py`, `tests/test_v2_candidate.py`。

**Interface:**

```py
def prepare_v2_candidate(
    db, source_task_id: str, target_task_id: str,
    generated_manifest: Path, generated_library: Path, *,
    script_path: Path, words_path: Path,
) -> dict:
    """Create a fail-closed v2 task without mutating the source task."""
```

- [ ] Write failing tests for successful clone, incomplete manifest, hash mismatch, old SHA reuse, existing target and rollback from partial failure.
- [ ] Run `py -3 -m unittest tests.test_v2_candidate -v`; confirm RED.
- [ ] Validate exactly three new files/hashes; register/scan local library; create target brief with subtitle sources; lock brief; reuse only digital human; add 3 new assets; copy narrative windows; confirm cards; picture-lock; copy packaging except subtitle; add `sub-modern-clean-v1` and confirm.
- [ ] Assert source task/assets unchanged and failed runs leave no usable target.
- [ ] Run focused/full Python tests; commit `feat: prepare isolated v2 candidates`.

### Task 6: real generation, render and review gate

**Files:** Create git-ignored runtime request/provenance/ASS/v2 DB, `out/p2-xinjiang-v2.mp4`, `docs/validation/2026-08-24-xinjiang-v2-acceptance.md`。

- [ ] Preflight exactly `xinjiang-wind-farm`, `modern-data-center`, `xinjiang-computing-park`. Verify key/model declarations without values, absent outputs, and 9:16/1080p/5s/no-watermark.
- [ ] Run explicit generator once with unique run id. On failure do not invoke again without new authorization.
- [ ] FFprobe all 3; verify dimensions/duration/readability, unique hashes, no equality with 3 old hashes, and unchanged first-round files.
- [ ] Call `backup_database(source, runtime/media-catalog/v2)`, then `prepare_v2_candidate`; verify source DB bytes/old task/snapshots unchanged.
- [ ] Run assemble with new DB/task, real script/words and output path.
- [ ] Require `awaiting_human_review`, QA `passed`, blockers `0`, `go=false`.
- [ ] Fresh verify `npm run test:ts`, `py -3 -m unittest discover -s tests -q`, `git diff --check`; final H.264/AAC 1080×1920 24–60fps, aligned starts, snapshot preset/coverage, 3 new IDs, empty approvals/publish records.
- [ ] Extract first-anchor and 3 B-roll-window frames. Record commands, hashes, provider task IDs, QA/snapshot IDs and “等待 Peter 人工完整审片”; do not commit videos, DBs, URLs, credentials or runtime manifests.
- [ ] Commit `docs: record xinjiang v2 review candidate`.
