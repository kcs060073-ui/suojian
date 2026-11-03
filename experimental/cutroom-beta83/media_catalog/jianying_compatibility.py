"""Truthful target-software gate. No Jianying build has passed our probe yet."""
from .provenance import canonical_digest


def inspect_jianying_compatibility(recipe: dict, target_version: str) -> dict:
    """Return a blocked report until a native adapter and real roundtrip exist.

    Upstream claims and caller-supplied evidence cannot populate this gate.
    A future validated adapter must bind its exact build and evidence in code.
    """
    blockers = [{"code": "JY_TARGET_UNVERIFIED", "message": "该剪映版本尚无实际打开、编辑、重导验收记录"}]
    if recipe.get("recipe_version") != 1:
        blockers.append({"code": "JY_RECIPE_VERSION_UNSUPPORTED", "message": "仅接受RenderRecipe v1"})
    if not isinstance(target_version, str) or not target_version.strip():
        blockers.append({"code": "JY_TARGET_REQUIRED", "message": "需要目标软件完整版本号"})
    return {
        "schema": "cutroom.jianying-compatibility/v1", "status": "blocked",
        "target_version": target_version, "recipe_digest": canonical_digest(recipe),
        "native_project_verified": False, "validated_versions": [],
        "blockers": blockers, "degradations": [], "supported": [],
        "approval_transferred": False,
    }
