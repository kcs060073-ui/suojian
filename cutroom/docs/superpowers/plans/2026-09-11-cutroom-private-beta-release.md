# CUTROOM Private Beta Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the verified CUTROOM code to a private GitHub repository and ship a reproducible Windows online-install Beta ZIP as prerelease `v0.9.0-beta.1`.

**Architecture:** A strict allowlist builder creates a release staging tree, adds generated runtime directories and a cryptographic manifest, then emits a ZIP and checksum. A double-click Windows installer provisions Python/Node/FFmpeg dependencies without bundling secrets or user data. GitHub stores source privately and serves the Beta ZIP as a private Release asset.

**Tech Stack:** Python 3.11, PowerShell 5.1+, Windows batch, Node.js LTS/npm, FFmpeg/ffprobe, Git/GitHub CLI.

## Global Constraints

- Repository is `qnwhageqlavs-jpg/cutroom-private` and must remain private.
- Version is `0.9.0-beta.1`; Git tag is `v0.9.0-beta.1`; Release is a prerelease.
- Do not create a public repository in this phase.
- Never include `.env`, credentials, runtime databases, cache, output videos, customer media, task scripts, `assets-local/`, `.git/`, `.worktrees/`, `.venv/`, or `node_modules/` in GitHub Release assets.
- The Beta installer may require internet access and Windows Package Manager; offline installation is out of scope.
- Existing user changes must be preserved.

---

### Task 1: Release-package contract and red tests

**Files:**
- Create: `tests/test_release_packaging.py`
- Create: `release/release-files.json`

**Interfaces:**
- Consumes: repository root and target version.
- Produces: an allowlist contract consumed by `scripts/build_release.py`.

- [ ] **Step 1: Write failing packaging tests**

Add `ReleasePackagingTests` covering these behaviors:

```python
def test_release_contract_includes_runtime_sources_and_user_entrypoints(self):
    contract = json.loads((ROOT / "release/release-files.json").read_text("utf-8"))
    self.assertIn("media_catalog", contract["directories"])
    self.assertIn("web-ui", contract["directories"])
    self.assertIn("安装CUTROOM.bat", contract["files"])

def test_builder_rejects_forbidden_paths_even_if_allowlisted(self):
    with self.assertRaisesRegex(ValueError, "forbidden"):
        validate_contract({"files": [".env"], "directories": []})

def test_build_emits_zip_manifest_and_checksum_without_private_data(self):
    result = build_release(ROOT, self.output, "0.9.0-beta.1", commit="test")
    self.assertTrue(result.zip_path.is_file())
    self.assertTrue(result.checksum_path.is_file())
    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
    self.assertIn("CUTROOM-v0.9.0-beta.1/release-manifest.json", names)
    self.assertFalse(any("/.env" in name or "/runtime/" in name for name in names))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_release_packaging
```

Expected: import failure for missing `scripts.build_release` or missing release contract.

- [ ] **Step 3: Add the minimal allowlist contract**

Create JSON with exact top-level files and runtime directories:

```json
{
  "files": [
    ".env.example",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "requirements.txt",
    "README.md",
    "广告出片指南.md",
    "安装CUTROOM.bat",
    "启动CUTROOM.bat",
    "THIRD_PARTY_NOTICES.md"
  ],
  "directories": [
    "media_catalog",
    "scripts",
    "web-ui",
    "workbench",
    "assets",
    "asset-packs",
    "docs/user-guide"
  ],
  "create_directories": [
    "runtime/workbench",
    "out",
    "assets-local",
    "public/digital_human",
    "public/scripts",
    "public/inserts",
    "public/ad_audio",
    "public/library"
  ]
}
```

- [ ] **Step 4: Commit the red-test contract**

```powershell
git add tests/test_release_packaging.py release/release-files.json
git commit -m "test: define CUTROOM release package contract"
```

---

### Task 2: Deterministic release builder

**Files:**
- Create: `scripts/build_release.py`
- Modify: `tests/test_release_packaging.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `release/release-files.json`.
- Produces: `BuildResult(zip_path: Path, checksum_path: Path, manifest_path: Path)` and CLI `python scripts/build_release.py --version VERSION --output release/dist`.

- [ ] **Step 1: Implement path validation and staging**

`validate_contract()` resolves every allowlisted path beneath the repository root and rejects components matching this fixed forbidden set:

```python
FORBIDDEN_PARTS = {
    ".git", ".worktrees", ".venv", "node_modules", "runtime", "out",
    "assets-local", "__pycache__",
}
FORBIDDEN_NAMES = {".env"}
```

The exception is `create_directories`, which creates empty runtime/output/input directories in the staging tree and writes `.gitkeep` markers so ZIP preserves them.

- [ ] **Step 2: Implement manifest, ZIP and checksum**

For each staged file, record POSIX relative path, byte size and lowercase SHA-256. Sort paths lexicographically and write UTF-8 JSON with `ensure_ascii=False`. ZIP entries must be rooted at `CUTROOM-v{version}/`. Write an external checksum line:

```text
<lowercase sha256>  CUTROOM-v0.9.0-beta.1-windows-x64.zip
```

- [ ] **Step 3: Keep build output outside Git**

Append to `.gitignore`:

```gitignore
# Local release build artifacts
/release/dist/
```

- [ ] **Step 4: Run targeted tests and verify GREEN**

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_release_packaging
```

Expected: all release packaging tests pass.

- [ ] **Step 5: Commit the builder**

```powershell
git add scripts/build_release.py tests/test_release_packaging.py .gitignore
git commit -m "build: add deterministic private beta packager"
```

---

### Task 3: Online installer and runtime dependency contract

**Files:**
- Create: `requirements.txt`
- Create: `release/install.ps1`
- Create: `安装CUTROOM.bat`
- Create: `启动CUTROOM.bat`
- Create: `THIRD_PARTY_NOTICES.md`
- Modify: `tests/test_release_packaging.py`

**Interfaces:**
- Consumes: Windows x64, internet access, `winget`, release tree.
- Produces: `.venv`, `node_modules`, runtime directories and a working double-click launcher.

- [ ] **Step 1: Extend red tests for installer behavior**

Tests must assert that the installer contains exact prerequisite IDs and install commands:

```python
self.assertIn("Python.Python.3.11", install_script)
self.assertIn("OpenJS.NodeJS.LTS", install_script)
self.assertIn("Gyan.FFmpeg", install_script)
self.assertIn("-m venv", install_script)
self.assertIn("-m pip install -r", install_script)
self.assertIn("npm ci", install_script)
```

Also assert that `requirements.txt` pins direct dependencies:

```text
faster-whisper==1.2.1
numpy==2.4.6
Pillow==12.2.0
```

- [ ] **Step 2: Run and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_release_packaging
```

Expected: missing installer/launcher/requirements assertions fail.

- [ ] **Step 3: Implement the installer**

`release/install.ps1` must:

1. Require 64-bit Windows and PowerShell 5.1+.
2. Resolve Python 3.11, Node/npm, FFmpeg and ffprobe.
3. When a prerequisite is missing, use `winget install --exact --id <ID> --accept-package-agreements --accept-source-agreements`.
4. Refresh process PATH from machine and user environment after each installation.
5. Create `.venv`, install `requirements.txt`, run `npm ci`, create all runtime/input directories and verify imports/tools.
6. Exit non-zero with a concise Chinese error if any step fails.
7. Never read or write API keys.

`安装CUTROOM.bat` calls the PowerShell installer with `-ExecutionPolicy Bypass`. `启动CUTROOM.bat` starts `workbench.server` with the local `.venv`, opens `http://127.0.0.1:8765/`, and fails with a Chinese message when installation is incomplete.

- [ ] **Step 4: Add third-party notices**

Include the exact Scott Buckley credit block from `asset-packs/open-commercial-2026-09/ATTRIBUTION.md`, plus Kenney CC0 and OFL font notices and paths to bundled license copies.

- [ ] **Step 5: Run targeted tests and commit**

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_release_packaging
git add requirements.txt release/install.ps1 安装CUTROOM.bat 启动CUTROOM.bat THIRD_PARTY_NOTICES.md tests/test_release_packaging.py
git commit -m "feat: add Windows beta installer and launcher"
```

---

### Task 4: Version and user-facing release documentation

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `README.md`
- Modify: `docs/user-guide/cutroom-user-guide.md`
- Create: `release/RELEASE_NOTES_v0.9.0-beta.1.md`
- Modify: `tests/test_release_packaging.py`

**Interfaces:**
- Consumes: installer behavior and release limitations.
- Produces: one consistent Beta version and user instructions.

- [ ] **Step 1: Add red consistency tests**

Verify package files both declare `0.9.0-beta.1`, release notes contain `Beta` and `普通同事电脑`, and the user guide contains `安装CUTROOM.bat`, `启动CUTROOM.bat`, `升级`, `回退`, `卸载`, and `署名`.

- [ ] **Step 2: Run and verify RED**

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_release_packaging
```

- [ ] **Step 3: Update version and documentation**

Describe exactly:

- Extract ZIP into a user-writable local directory.
- Run installer once, then use the launcher.
- Configure model credentials through CUTROOM only; do not edit `.env`.
- Upgrade by keeping `assets-local/` and runtime database backup, then installing the new version in a new directory.
- Roll back by reopening the previous version directory and restoring its database backup.
- Uninstall by closing CUTROOM and deleting its extracted directory; user media libraries remain untouched.
- Scott Buckley music requires publication credit; public distribution is not part of this Beta.

- [ ] **Step 4: Run targeted tests and commit**

```powershell
.venv\Scripts\python.exe -m unittest -v tests.test_release_packaging
git add package.json package-lock.json README.md docs/user-guide/cutroom-user-guide.md release/RELEASE_NOTES_v0.9.0-beta.1.md tests/test_release_packaging.py
git commit -m "docs: prepare CUTROOM v0.9 beta release"
```

---

### Task 5: Full verification and isolated package smoke test

**Files:**
- Generated only: `release/dist/*` (ignored)
- Create: `docs/validation/2026-09-11-cutroom-v0.9.0-beta.1-release.md`

**Interfaces:**
- Consumes: verified source tree.
- Produces: release evidence and distributable assets.

- [ ] **Step 1: Run complete regression suites**

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
npm run test:ts
npm run test:web
npx tsc --noEmit
.venv\Scripts\python.exe -m compileall -q media_catalog workbench scripts
.venv\Scripts\python.exe scripts\media_catalog.py assets validate
git diff --check
```

Every command must exit 0.

- [ ] **Step 2: Run repository and candidate secret scan**

Reject tracked or staged files under forbidden paths and reject high-confidence credential patterns such as private key headers and live API-token assignments. `.env.example` placeholder values are allowed; actual `.env` is not.

- [ ] **Step 3: Build release assets**

```powershell
.venv\Scripts\python.exe scripts\build_release.py --version 0.9.0-beta.1 --output release\dist
```

- [ ] **Step 4: Verify extracted runtime tree**

Extract the ZIP into a newly created temporary directory. Recompute the ZIP and package manifest hashes, validate manifests, then start the extracted `workbench.server` on an unused loopback port using the verified local Python runtime and request `/`. Stop only the exact spawned process tree.

- [ ] **Step 5: Write validation report and commit**

Record command counts, exit codes, archive size/hash, included/excluded categories, smoke-test port/status and the unfulfilled clean-colleague-PC gate.

```powershell
git add docs/validation/2026-09-11-cutroom-v0.9.0-beta.1-release.md
git commit -m "test: record CUTROOM v0.9 beta release validation"
```

---

### Task 6: Private GitHub repository and prerelease

**Files:**
- No source-file changes expected.

**Interfaces:**
- Consumes: clean verified commit and `release/dist` assets.
- Produces: private GitHub repository, main branch, tag and prerelease.

- [ ] **Step 1: Create the private repository**

```powershell
gh repo create qnwhageqlavs-jpg/cutroom-private --private --description "CUTROOM 本地视频制作工作台" --disable-issues --disable-wiki
git remote add origin https://github.com/qnwhageqlavs-jpg/cutroom-private.git
```

- [ ] **Step 2: Integrate the verified branch into main**

Fast-forward when possible; otherwise create a normal merge commit without discarding either branch. Re-run `git status --short`, critical tests and `git diff --check` after integration.

- [ ] **Step 3: Push source and tag**

```powershell
git push -u origin main
git tag -a v0.9.0-beta.1 -m "CUTROOM v0.9.0-beta.1"
git push origin v0.9.0-beta.1
```

- [ ] **Step 4: Create prerelease and upload assets**

```powershell
gh release create v0.9.0-beta.1 `
  release/dist/CUTROOM-v0.9.0-beta.1-windows-x64.zip `
  release/dist/CUTROOM-v0.9.0-beta.1-SHA256SUMS.txt `
  --repo qnwhageqlavs-jpg/cutroom-private `
  --title "CUTROOM v0.9.0-beta.1" `
  --notes-file release/RELEASE_NOTES_v0.9.0-beta.1.md `
  --prerelease
```

- [ ] **Step 5: Read back immutable evidence**

```powershell
gh repo view qnwhageqlavs-jpg/cutroom-private --json visibility,defaultBranchRef,url
gh release view v0.9.0-beta.1 --repo qnwhageqlavs-jpg/cutroom-private --json isPrerelease,tagName,targetCommitish,url,assets
git ls-remote --heads --tags origin
```

Verify private visibility, `main`, prerelease state, tag alignment, both assets, byte sizes and local checksum.
