"""Build a compiled CUTROOM Windows package; no private development sources.

Python 3.11 is required. Small .py/.ts compatibility loaders remain public.
Python bytecode and transpiled JavaScript are reversible, not source encryption.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

PY_LOADER = """# CUTROOM-COMPILED-LOADER-v1
from importlib.machinery import SourcelessFileLoader as _CutroomLoader
from pathlib import Path as _CutroomPath
exec(_CutroomLoader(__name__, str(_CutroomPath(__file__).with_suffix('.pyc'))).get_code(__name__), globals())
"""
TS_LOADER = """// CUTROOM-COMPILED-LOADER-v1
require('module').prototype._compile.call(module, require('fs').readFileSync(require('path').join(__dirname, %s), 'utf8'), __filename);
"""
RUNTIME_SCRIPTS = {
    "ad_pipeline_runner.py", "ad_tts_runner.py", "codex_toml.py", "cutroom_mcp.py",
    "gen-fancy-text.py", "refined_layers.py", "jianying_media_preflight.py", "jianying_probe.py",
    "make-phone-frame.py", "media_catalog.py", "plan_brief_runner.py", "probe-cutroom-mcp.py",
    "register_open_commercial_asset_pack.py", "transcribe.py", "upgrade_cutroom.py", "product_update.py", "public_updates.py",
}
USER_OPERATION_GUIDES = {"docs/operations/cutroom-maintenance.md", "docs/operations/model-profiles-new-api.md", "docs/operations/cutroom-agent-mcp-clients.md", "docs/implementation/runtime-prompts.md"}
RUNTIME_CONTRACTS = (
    "docs/ux/final-2026-09-24/readiness/prompts/P09.txt",
    "docs/ux/final-2026-09-24/readiness/prompts/P11.txt",
    "docs/ux/final-2026-09-24/readiness/contracts/prompt-registry.json",
    "docs/ux/final-2026-09-24/readiness/contracts/prompt-hashes.json",
)
BLOCKED_PARTS = {".git", "tests", "__pycache__", "node_modules", ".venv", "out", "runtime", "assets-local"}


def _windows_text_bytes(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def copy_runtime_file(source: Path, target: Path) -> None:
    """JSON whitespace uses stable Windows endings; other assets stay raw."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".json":
        target.write_bytes(_windows_text_bytes(source.read_bytes()))
    else:
        shutil.copy2(source, target)


def compile_python_file(source: Path, target: Path, relative: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Canonical Windows source bytes keep PEP 552 source hashes reproducible
    # across LF Git archives and CRLF checkouts, without modifying the input.
    target.write_bytes(_windows_text_bytes(source.read_bytes()))
    py_compile.compile(str(target), cfile=str(target.with_suffix(".pyc")), dfile=relative,
                       doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    target.write_text(PY_LOADER, encoding="utf-8", newline="\n")


def compile_python_files(entries) -> None:
    """Compile one ordered batch in a fresh interpreter with a fixed hash seed."""
    if not entries:
        return
    program = """
import json, runpy, sys
from pathlib import Path
compile_file = runpy.run_path(sys.argv[1])['compile_python_file']
for source, target, relative in json.load(sys.stdin):
    compile_file(Path(source), Path(target), relative)
"""
    environment = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    subprocess.run([sys.executable, "-B", "-c", program, str(Path(__file__).resolve())],
                   input=json.dumps([[str(source), str(target), relative] for source, target, relative in entries]),
                   text=True, encoding="utf-8", env=environment, check=True, timeout=180, capture_output=True)


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Build helpers must execute the source just verified, never a stale .pyc.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def _allowed(relative):
    parts = relative.parts
    if any(part in BLOCKED_PARTS for part in parts) or relative.name in {".env", "AGENTS.md", "CLAUDE.md", "capture_browser_page.mjs"}:
        return False
    if relative.suffix.lower() in {".map", ".log", ".sqlite", ".db", ".pyc", ".pyo"}:
        return False
    if parts[0] == "docs":
        return (len(parts) > 1 and parts[1] == "user-guide") or relative.as_posix() in USER_OPERATION_GUIDES
    if parts[0] == "scripts" and relative.suffix == ".py":
        return relative.name in RUNTIME_SCRIPTS
    if relative.suffix == ".md" and len(parts) > 1:
        return parts[0] in {"release", "assets", "asset-packs"} or "license" in relative.name.lower() or "notice" in relative.name.lower()
    return True


def _transform_javascript(root, stage, node, typescript, esbuild):
    entries=[]
    for path in stage.rglob("*"):
        if path.is_file() and path.suffix in {".ts", ".js", ".mjs", ".cjs"}:
            entries.append(str(path))
    program = r"""
const fs=require('fs'),path=require('path'),ts=require(process.argv[1]);
const esbuild=require(process.argv[2]);
const files=JSON.parse(fs.readFileSync(0,'utf8'));
for(const file of files){
 const ext=path.extname(file),isTs=ext==='.ts';
 const result=ts.transpileModule(fs.readFileSync(file,'utf8'),{fileName:file,reportDiagnostics:true,
 compilerOptions:{target:ts.ScriptTarget.ES2020,module:isTs?ts.ModuleKind.CommonJS:ts.ModuleKind.Preserve,
 esModuleInterop:true,allowJs:true,removeComments:true,sourceMap:false}});
 const errors=(result.diagnostics||[]).filter(d=>d.category===ts.DiagnosticCategory.Error);
 if(errors.length)throw Error(file+': '+errors.map(d=>ts.flattenDiagnosticMessageText(d.messageText,' ')).join('; '));
 const compact=esbuild.transformSync(result.outputText,{loader:'js',minify:true,target:'es2020',legalComments:'none',charset:'utf8',sourcemap:false}).code;
 fs.writeFileSync(isTs?file.slice(0,-3)+'.js':file,compact,'utf8');
}
"""
    subprocess.run([str(node), "-e", program, str(typescript), str(esbuild)], input=json.dumps(entries),
                   text=True, encoding="utf-8", check=True, timeout=120, capture_output=True)
    for name in entries:
        path=Path(name)
        if path.suffix == ".ts":
            path.write_text(TS_LOADER % json.dumps(path.with_suffix('.js').name), encoding="utf-8", newline="\n")


def resolve_build_tools(root: Path, version: str, typescript: Path | None = None, esbuild: Path | None = None) -> tuple[Path, Path]:
    """Use only the build versions declared by this source checkout."""
    root = root.resolve()
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != version:
        raise ValueError("VERSION_MISMATCH: release version must match package.json")
    typescript = (typescript or root / "node_modules/typescript/lib/typescript.js").resolve()
    esbuild = (esbuild or root / "node_modules/esbuild").resolve()
    for name, folder, entry in (("typescript", typescript.parent.parent, typescript), ("esbuild", esbuild, esbuild / "lib/main.js")):
        try:
            installed = json.loads((folder / "package.json").read_text(encoding="utf-8"))["version"]
            if not entry.is_file():
                raise FileNotFoundError(entry)
        except (OSError, ValueError, KeyError) as error:
            raise ValueError(f"BUILD_TOOL_MISSING: run npm ci in the source checkout ({name})") from error
        if package.get("devDependencies", {}).get(name) != installed:
            raise ValueError(f"BUILD_TOOL_VERSION: {name} must use the exact declared version")
    return typescript, esbuild


# These files affect selection, transformation, and manifest generation even when
# they are excluded from the public payload itself.
SOURCE_CONTROLS = ("scripts/build_public_release.py", "scripts/build_release.py",
                   "scripts/product_update.py", "release/release-files.json")
SOURCE_TEXT_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".cjs", ".json", ".md",
                        ".txt", ".ps1", ".bat", ".cmd", ".html", ".css", ".sql",
                        ".svg", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".csv"}


def _source_selection(contract, available):
    """Map shipped names to source names; shared by verification and packaging."""
    available = set(available)
    selected = {Path(name).as_posix(): Path(name).as_posix()
                for name in contract["files"] if _allowed(Path(name))}
    for directory in contract["directories"]:
        prefix = Path(directory).as_posix().rstrip("/") + "/"
        for name in available:
            if name.startswith(prefix) and _allowed(Path(name)):
                selected[name] = name
    if "release/public/README.md" in available:
        selected["README.md"] = "release/public/README.md"
    for name in (*RUNTIME_CONTRACTS, *sorted(USER_OPERATION_GUIDES)):
        if name in available:
            selected[name] = name
    return selected


def _local_source_names(root, contract):
    names = set()
    for directory in contract["directories"]:
        names.update(path.relative_to(root).as_posix()
                     for path in (root / directory).rglob("*") if path.is_file())
    for name in (*contract["files"], "release/public/README.md",
                 *RUNTIME_CONTRACTS, *USER_OPERATION_GUIDES):
        if (root / name).is_file():
            names.add(Path(name).as_posix())
    return names


def verify_release_source(root: Path, commit: str | None = None, *,
                          source_repository: Path | None = None) -> str:
    """Verify release inputs against Git objects and return the full commit ID.

    No Git status/clean-filter decision is used: Python reads plaintext, including
    on transparent-encryption hosts. Only known UTF-8 text allows LF/CRLF changes.
    The local Git repository and the running Python/Git/toolchain are trusted.
    """
    root = root.resolve()
    checkout = (root / ".git").exists()
    if source_repository is None and not checkout:
        raise ValueError("SOURCE_REPOSITORY: an archive requires --source-repository")
    if not checkout and not commit:
        raise ValueError("SOURCE_COMMIT: an archive requires --commit")
    repository = (source_repository or root).resolve()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}

    def git(*args):
        try:
            return subprocess.run(["git", "--no-replace-objects", "-C", str(repository), *args],
                                  env=environment, capture_output=True, check=True, timeout=60).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValueError("SOURCE_COMMIT: cannot read the declared repository/commit") from error

    if commit is not None and not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        raise ValueError("SOURCE_COMMIT: --commit must be a Git commit ID")
    sha = git("rev-parse", "--verify", "--end-of-options", (commit or "HEAD") + "^{commit}").decode().strip()
    tree = {}
    for record in git("ls-tree", "-rz", "--full-tree", sha).split(b"\0"):
        if record:
            metadata, name = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            tree[name.decode("utf-8")] = (mode, kind, oid)

    def blob(name):
        record = tree.get(name)
        if record is None or record[0] not in {"100644", "100755"} or record[1] != "blob":
            raise ValueError(f"SOURCE_DRIFT: missing/non-regular committed input: {name}")
        return record[2]

    def matches(name, data, oid):
        algorithm = "sha1" if len(oid) == 40 else "sha256"
        def object_hash(value):
            return hashlib.new(algorithm, b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()
        if object_hash(data) == oid:
            return True
        if Path(name).suffix.lower() not in SOURCE_TEXT_SUFFIXES or b"\0" in data:
            return False
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        normalized = data.replace(b"\r\n", b"\n")
        if object_hash(normalized) == oid or object_hash(_windows_text_bytes(data)) == oid:
            return True
        original = git("cat-file", "blob", oid)
        return b"\0" not in original and original.replace(b"\r\n", b"\n") == normalized

    def verify_file(name, path=None):
        oid = blob(name)
        path = path or root / name
        # Reject redirections even when they happen to contain matching bytes.
        for part in (path, *path.parents):
            if part == root.parent:
                break
            if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError(f"SOURCE_DRIFT: linked input: {name}")
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ValueError(f"SOURCE_DRIFT: unreadable input: {name}") from error
        if not matches(name, data, oid):
            raise ValueError(f"SOURCE_DRIFT: content differs from {sha}: {name}")
        return data

    # Verify all executable helpers and the contract before importing/parsing them.
    for name in SOURCE_CONTROLS:
        verify_file(name)
    verify_file("scripts/build_public_release.py", Path(__file__).resolve())
    contract = json.loads((root / "release/release-files.json").read_text(encoding="utf-8"))
    builder = _module("cutroom_verified_release_builder", root / "scripts/build_release.py")
    builder.validate_contract(contract)
    expected = _source_selection(contract, tree)
    actual = _source_selection(contract, _local_source_names(root, contract))
    if expected != actual:
        changed = sorted(name for name in expected.keys() | actual.keys()
                         if expected.get(name) != actual.get(name))
        raise ValueError("SOURCE_INPUTS: selected files differ from commit: " + ", ".join(changed[:10]))
    for name in sorted(set(expected.values()) - set(SOURCE_CONTROLS)):
        verify_file(name)
    return sha


def build_public_release(root: Path, output: Path, version: str, *, node: Path, typescript: Path | None = None, esbuild: Path | None = None, commit: str | None = None):
    """Low-level fixture API. Published builds must use main() source verification."""
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("The public runtime targets CPython 3.11; build with Python 3.11")
    root, output = root.resolve(), output.resolve()
    typescript, esbuild = resolve_build_tools(root, version, typescript, esbuild)
    builder = _module("cutroom_private_release_builder", root / "scripts/build_release.py")
    updater = _module("cutroom_private_product_update", root / "scripts/product_update.py")
    contract=json.loads((root / builder.CONTRACT_PATH).read_text(encoding="utf-8"))
    builder.validate_contract(contract)
    selected = {name: root / source for name, source in
                _source_selection(contract, _local_source_names(root, contract)).items()}
    # This digest describes the original dependency/schema contract, before compilation.
    compatibility=updater.compatibility_digest({name:path.read_bytes() for name,path in selected.items()})
    package=f"CUTROOM-v{version}"
    output.mkdir(parents=True,exist_ok=True)
    archive=output/f"{package}-windows-x64.zip"
    with tempfile.TemporaryDirectory(prefix="cutroom-public-") as temporary:
        stage=Path(temporary)/package
        stage.mkdir()
        python_entries=[]
        for name,source in sorted(selected.items()):
            if source.is_symlink(): raise ValueError(f"release symlink is forbidden: {name}")
            target=stage/name
            if source.suffix == ".py": python_entries.append((source,target,name))
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                copy_runtime_file(source,target)
        compile_python_files(python_entries)
        _transform_javascript(root,stage,node,typescript,esbuild)
        records=[]
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            if path.suffix == ".py" and path.read_text(encoding="utf-8") != PY_LOADER:
                raise ValueError(f"private Python source leaked: {path.name}")
            if path.suffix == ".ts" and path.read_text(encoding="utf-8") != TS_LOADER % json.dumps(path.with_suffix('.js').name):
                raise ValueError(f"private TypeScript source leaked: {path.name}")
            records.append({"path":path.relative_to(stage).as_posix(),"bytes":path.stat().st_size,"sha256":builder._hash_file(path)})
        manifest={"product":"CUTROOM","version":version,"commit":commit or builder._git_commit(root),
                  "platform":"windows-x64","distribution":"compiled-loaders-v1", "distribution_format":"compiled-v1", "python":"3.11",
                  "update_compatibility":compatibility,"files":records}
        (stage/'release-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        builder._write_deterministic_zip(stage,archive,package)
    checksum=archive.with_suffix(archive.suffix+'.sha256')
    builder._write_text_atomic(checksum,f"{builder._hash_file(archive)}  {archive.name}\n")
    return archive


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--commit',help='Commit ID to verify; defaults to checkout HEAD')
    parser.add_argument('--source-repository',type=Path,help='Trusted local Git repository; required for an exported source archive')
    parser.add_argument('--node',type=Path,default=Path(shutil.which('node') or 'node'))
    parser.add_argument('--typescript',type=Path,help='Defaults to project node_modules/typescript/lib/typescript.js')
    parser.add_argument('--esbuild',type=Path,help='Defaults to project node_modules/esbuild')
    args=parser.parse_args()
    commit = verify_release_source(args.root, args.commit, source_repository=args.source_repository)
    print(build_public_release(args.root,args.output,args.version,node=args.node,typescript=args.typescript,esbuild=args.esbuild,commit=commit))

if __name__ == '__main__': main()
