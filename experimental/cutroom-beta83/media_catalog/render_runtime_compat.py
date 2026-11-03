"""Exact reviewed release identities; never exempt a file by name or location.

The versioned record retains full-module semantic digests and legacy combined
loader/payload identities. Only debug filenames/line positions and the pyc
header are omitted from semantic comparison. Runtime instructions, constants,
imports, defaults, names, flags, closures and exception handling remain bound.
No historical bytecode is loaded or executed to apply this compatibility map.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import marshal
from pathlib import Path
import sys
from types import CodeType

RECORD_PATH = Path(__file__).with_name("render-runtime-compat-v1.json")


def _semantic_value(value):
    if isinstance(value, CodeType):
        return {"code": {
            key: _semantic_value(getattr(value, key)) for key in (
                "co_argcount", "co_posonlyargcount", "co_kwonlyargcount",
                "co_nlocals", "co_stacksize", "co_flags", "co_code",
                "co_consts", "co_names", "co_varnames", "co_name",
                "co_qualname", "co_freevars", "co_cellvars", "co_exceptiontable",
            )
        }}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, tuple):
        return {"tuple": [_semantic_value(item) for item in value]}
    if isinstance(value, frozenset):
        items = [_semantic_value(item) for item in value]
        return {"frozenset": sorted(items, key=lambda item: json.dumps(item, sort_keys=True))}
    if value is Ellipsis:
        return {"ellipsis": True}
    if isinstance(value, complex):
        return {"complex": [value.real.hex(), value.imag.hex()]}
    if isinstance(value, float):
        return {"float": value.hex()}
    if value is None or isinstance(value, (str, int, bool)):
        return {type(value).__name__: value}
    raise ValueError("Unsupported Python code constant")


def python_semantic_digest(code: CodeType) -> str:
    """Hash all executable structure, independently of build/debug locations."""
    if not isinstance(code, CodeType):
        raise ValueError("A module code object is required")
    canonical = json.dumps(_semantic_value(code), sort_keys=True, ensure_ascii=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"CUTROOM-python-semantics-v1\0" + canonical).hexdigest()


def reviewed_runtime_identity(loader: bytes, payload: bytes) -> dict | None:
    """Return a reviewed historical identity, or None for any unknown version.

    This is render identity, not package-integrity evidence. Installation and
    update manifests must still hash the actual distributed bytes.
    """
    if sys.version_info[:2] != (3, 11) or payload[:4] != importlib.util.MAGIC_NUMBER:
        return None
    try:
        records = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
        if (not isinstance(records, dict) or records.get("schema") != 1
                or records.get("python") != "3.11"):
            return None
        loader_digest = hashlib.sha256(loader).hexdigest()
        eligible = [record for record in records["modules"]
                    if record["loader_sha256"] == loader_digest]
        if not eligible:
            return None
        if len(payload) < 16 or int.from_bytes(payload[4:8], "little") & ~3:
            return None
        stream = io.BytesIO(payload[16:])
        code = marshal.load(stream)
        if stream.read(1):
            return None
        semantic = python_semantic_digest(code)
        for record in eligible:
            if semantic in record["semantic_sha256"]:
                legacy = record["legacy_identity"]
                return {"sha256": legacy["sha256"], "size_bytes": legacy["size_bytes"]}
    except (OSError, ValueError, TypeError, KeyError, EOFError):
        # Corrupt/missing registry, invalid bytecode and unknown code fail closed.
        return None
    return None


def reviewed_data_identity(data: bytes) -> dict | None:
    """Map only complete, audited configuration bytes, never a filename.

    This is a render identity; release integrity still hashes the actual bytes.
    No parsing/canonicalization can hide additional or changed config fields.
    """
    try:
        records = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
        if not isinstance(records, dict) or records.get("schema") != 1:
            return None
        digest = hashlib.sha256(data).hexdigest()
        for record in records.get("data_files", []):
            if digest in record["content_sha256"]:
                legacy = record["legacy_identity"]
                return {"sha256": legacy["sha256"], "size_bytes": legacy["size_bytes"]}
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None
