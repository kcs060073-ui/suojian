"""Bind public-release loaders together with the compiled code they execute."""
import hashlib
from pathlib import Path

PY_MARKER = b"# CUTROOM-COMPILED-LOADER-v1\n"
TS_MARKER = b"// CUTROOM-COMPILED-LOADER-v1\n"

def raw_compiled_signature(path):
    """Return a combined signature only for explicitly marked release loaders.

    Ordinary source and media signatures stay byte-for-byte compatible. Payload
    loss fails closed; payload changes and loader changes both change the digest.
    """
    path = Path(path)
    marker = PY_MARKER if path.suffix == ".py" else TS_MARKER if path.suffix == ".ts" else None
    if marker is None:
        return None
    with path.open("rb") as stream:
        if stream.readline(100).rstrip(b"\r\n") != marker.rstrip(b"\n"):
            return None
        stream.seek(0)
        loader = stream.read()
    payload = path.with_suffix(".pyc" if path.suffix == ".py" else ".js")
    digest = hashlib.sha256(b"CUTROOM-runtime-artifact-v1\0")
    digest.update(len(loader).to_bytes(8, "big"))
    digest.update(loader)
    size = payload.stat().st_size
    digest.update(size.to_bytes(8, "big"))
    with payload.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size_bytes": len(loader) + size,
            "mtime_ns": max(path.stat().st_mtime_ns, payload.stat().st_mtime_ns)}


def compiled_signature(path):
    """Render identity including narrowly reviewed release compatibility.

    Unknown programs retain their exact byte identity. Package-integrity checks
    must use raw bytes or raw_compiled_signature, never this compatibility view.
    """
    result = raw_compiled_signature(path)
    path = Path(path)
    if result is None and path.suffix.lower() == ".json" and path.stat().st_size <= 16384:
        from .render_runtime_compat import reviewed_data_identity
        identity = reviewed_data_identity(path.read_bytes())
        if identity is not None:
            return {**identity, "mtime_ns": path.stat().st_mtime_ns}
    if result is not None and path.suffix == ".py":
        from .render_runtime_compat import reviewed_runtime_identity
        identity = reviewed_runtime_identity(path.read_bytes(), path.with_suffix(".pyc").read_bytes())
        if identity is not None:
            return {**result, **identity}
    return result


def node_script_command(node, project_root, script_relative):
    """Use marked release loaders directly; keep ts-node for source checkouts."""
    root = Path(project_root)
    entry = root / script_relative
    if entry.is_file():
        with entry.open("rb") as stream:
            compiled = stream.readline(100).rstrip(b"\r\n") == TS_MARKER.rstrip(b"\n")
        if compiled:
            if not entry.with_suffix(".js").is_file():
                raise RuntimeError("RENDER_RUNTIME_MISSING: 渲染组件缺失，请修复安装")
            return [str(node), str(entry)]
    return [str(node), str(root / "node_modules/ts-node/dist/bin.js"), str(entry)]


def is_compiled_script(path):
    path = Path(path)
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.readline(100).rstrip(b"\r\n") == TS_MARKER.rstrip(b"\n")
