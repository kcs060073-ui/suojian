# QA Context Corrections Implementation Plan

> **For agentic workers:** Implement each task with a failing regression test before changing production code.

**Goal:** Make release QA respect owned source material and intentional still images while preserving measurable audio safety.

**Architecture:** Preserve the existing task-specific authorization attestation. Add an explicit reusable self-produced declaration for the indexed fingerprint of every asset in a locked picture, and let the candidate snapshot carry its approved status. Filter freeze detections only when the detected interval lies inside an image scene. Treat nonblocking warnings as present QA evidence. Keep the true-peak limit and make the ad renderer output at 48 kHz, which passed a probe on the affected candidate audio.

**Tech Stack:** SQLite migrations, Python media catalog and QA, TypeScript/FFmpeg renderer, vanilla JS workbench.

**Spec:** `docs/superpowers/specs/2026-09-22-two-route-design-closure.md`, especially section 6, corrected by Peter's 2026-09-23 requirement that self-shot images and intentional still scenes not be mislabeled.

## Global Constraints

- Never infer commercial authorization from a local file path alone.
- Reusable declarations name exact asset IDs and indexed fingerprints; a changed fingerprint requires a new declaration.
- Missing release-critical QA checks remain blocking; only explicit warnings may be reviewable.
- The true-peak release limit stays at -1.0 dBFS.
- Existing task-specific attestation, picture-lock invalidation, and historic candidates remain intact.

## Review Focus

- New assets added to an approved library must still be unknown until explicitly declared.
- Modified bytes under an existing asset ID must invalidate reusable approval.
- A frozen video segment in a mixed image/video cut must still warn.
- Missing freeze/clipping QA evidence must still block even when warnings are reviewable.
- Existing task-specific approval must remain task-specific and not silently become reusable.

### Task 1: Reusable declaration for self-produced shots

**Files:** `media_catalog/migrations/039_self_produced_assets.sql`, `media_catalog/material_authorization.py`, `media_catalog/asset_index.py`, `media_catalog/qa_evidence.py`, `workbench/api.py`, `web-ui/public/app.mjs`, focused Python and Web tests.

**Interfaces:** Add `confirm_self_produced_material(db, task_id, confirmed_by)` that approves exactly the locked scene assets and stores their current quick fingerprints. `shot_authorization` and task-scoped asset index read these records only while fingerprints match. Keep `confirm_material_authorization` as a task-only option.

- [x] Write a test showing reusable approval on another task, fingerprint invalidation, and task-only approval remaining local.
- [x] Run the test and verify the expected failure.
- [x] Add the migration, model logic, API action, and explicit self-shot workbench control.
- [x] Run focused Python and Web tests and check the affected task path.

### Task 2: Image-aware freeze checks and honest warning gates

**Files:** `media_catalog/qa.py`, `media_catalog/qa_evidence.py`, focused QA tests.

**Interfaces:** `_check_freeze` receives image time ranges from the immutable candidate scene snapshot. It ignores a freeze interval only when image ranges cover it completely. `qa_evidence_complete` accepts present nonblocking freeze/clipping warnings but still requires every release-critical check.

- [x] Write tests for an all-image candidate, a mixed candidate with a frozen video interval, and warning-only release QA.
- [x] Run each test and verify the expected failure.
- [x] Implement interval-aware detection and warning-gate correction.
- [x] Run focused QA and route tests.

### Task 3: Final audio sample rate and real-candidate verification

**Files:** `scripts/render-ad.ts`, focused renderer test and validation note.

**Interfaces:** FFmpeg final AAC output uses 48 kHz after the existing loudness control. Do not change the release true-peak threshold.

- [x] Write a renderer regression test for 96 kHz voice input and final output true peak.
- [x] Run it and verify the expected failure.
- [x] Set the final audio sample rate to 48 kHz.
- [x] Render the affected task into an isolated output path and measure integrated loudness and true peak.

### Task 4: Closure

- [x] Run relevant Python, Node, and TypeScript checks with a writable test environment.
- [x] Review the diff and update the user guide for the new self-shot action and QA warning semantics.
- [x] Report test evidence and any limitations; do not change the production SQLite or approve the user's task on their behalf.
