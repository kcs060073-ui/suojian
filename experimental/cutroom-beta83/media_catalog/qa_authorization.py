"""Separate QA authorization evidence from immutable rendered media inputs."""
from .material_authorization import get_material_authorization, shot_authorization
from .provenance import canonical_digest

AUTHORIZATION_FIELDS = frozenset({
    "shot_authorization", "material_authorization_attestation",
})


def refresh_qa_authorization(db, task_id: str, render_snapshot: dict) -> dict:
    """Refresh only QA facts; callers must still verify every render dependency."""
    dependency = render_snapshot.get("dependency_snapshot") or {}
    if dependency.get("task_id") != task_id or not AUTHORIZATION_FIELDS <= dependency.keys():
        return render_snapshot
    fields = {
        "shot_authorization": shot_authorization(db, task_id),
        "material_authorization_attestation": get_material_authorization(db, task_id),
    }
    updated = {**dependency, **fields}
    if updated == dependency:
        return render_snapshot
    return {
        **render_snapshot,
        "dependency_snapshot": updated,
        "qa_authorization": {"fields": fields, "dependency_digest": canonical_digest(updated)},
    }


def reviewed_qa_dependency(render_dependency, authorization):
    """Project exactly the QA-reviewed authorization without changing the mix record."""
    if authorization is None:
        return render_dependency
    if (not isinstance(authorization, dict) or not isinstance(render_dependency, dict)
            or not isinstance(authorization.get("fields"), dict)
            or set(authorization["fields"]) != AUTHORIZATION_FIELDS):
        raise ValueError("INVALID_QA_AUTHORIZATION")
    effective = {**render_dependency, **authorization["fields"]}
    if canonical_digest(effective) != authorization.get("dependency_digest"):
        raise ValueError("INVALID_QA_AUTHORIZATION_DIGEST")
    return effective
