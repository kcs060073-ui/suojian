"""Check current media permissions again at external-consumer boundaries."""
import json
from pathlib import Path


def require_source_access(db, task_id, asset_id, start_us=None, end_us=None):
    from .task_sources import scoped_asset_location
    from .rough_media import bound_digital_source_location
    from .visual_batch_preview import _privacy_pending
    from .workbench_worker import ProviderRequestNotSent
    location = (scoped_asset_location(db, task_id, asset_id)
                or bound_digital_source_location(db, task_id, asset_id))
    if location is None:
        raise ProviderRequestNotSent("MODEL_MEDIA_SCOPE_CHANGED: 源素材不在当前任务授权范围")
    asset = db.connection.execute("SELECT media_kind FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None:
        raise ProviderRequestNotSent("MODEL_MEDIA_SOURCE_MISSING: 源素材不存在")
    # Unknown source mapping must not authorize a privacy-sensitive part.
    if asset["media_kind"] == "image" or start_us is None or end_us is None:
        start_us = end_us = 0
    if _privacy_pending(db, asset_id, start_us, end_us):
        raise ProviderRequestNotSent("MODEL_MEDIA_PRIVACY_REVIEW: 源素材隐私待处理，不能发送模型")
    return location


def require_shot_access(db, task_id, context):
    for shot in context["shots"]:
        start, end = shot.get("sourceStart"), shot.get("sourceEnd")
        require_source_access(db, task_id, shot["assetId"],
                              None if start is None else round(start * 1000000),
                              None if end is None else round(end * 1000000))


def require_candidate_access(db, task_id, candidate_id):
    """Use the rendered candidate's frozen sources, never the current cut alone.

    If an old snapshot has no source provenance, remote review cannot prove its
    source permission. This does not affect local playback or manual review.
    """
    from .workbench_worker import ProviderRequestNotSent
    row = db.connection.execute("""SELECT r.params_json FROM candidate_versions cv
        JOIN render_snapshots r ON r.snapshot_id=cv.mix_snapshot_id
        WHERE cv.task_id=? AND cv.candidate_id=? AND r.task_id=? AND r.stage='mix'""",
        (task_id, candidate_id, task_id)).fetchone()
    if row is None:
        raise ProviderRequestNotSent("MODEL_MEDIA_PROVENANCE_REQUIRED: 成片缺少可核验的来源快照")
    try:
        snapshot = json.loads(row["params_json"])["dependency_snapshot"]
        cards = snapshot.get("scene_cards") or []
        assets = snapshot.get("assets") or []
        selected = (snapshot.get("selection_state") or {}).get("task_assets") or []
        # assets/task_assets are the dependency inventory, including unused alternatives.
        # Only rendered cards and the always-mixed presenter track are actual sources.
        ids = {c["asset_id"] for c in cards if c.get("asset_id")}
        ids.update(a["asset_id"] for a in [*assets, *selected]
                   if a.get("asset_id") and a.get("role") == "digital_human")
    except (TypeError, KeyError, ValueError) as error:
        raise ProviderRequestNotSent("MODEL_MEDIA_PROVENANCE_REQUIRED: 成片来源快照不可用") from error
    if not ids:
        raise ProviderRequestNotSent("MODEL_MEDIA_PROVENANCE_REQUIRED: 成片缺少可核验的来源素材")
    # Derived video/audio contains mixed layers. Missing exact source mapping is
    # deliberately checked against the whole asset, not guessed from output time.
    for asset_id in sorted(ids):
        require_source_access(db, task_id, asset_id)
    for packaging in (snapshot.get("packaging"), snapshot.get("packaging_compiled")):
        if isinstance(packaging, dict):
            require_packaging_access(db, task_id, packaging)


def require_packaging_access(db, task_id, packaging):
    """Inspect used layers and match copied media to catalog identities by content.

    Registered catalog flags follow byte-identical personal packaging copies.
    Built-in/personal resources with no catalog identity remain authorized by the
    frozen rendered packaging selection; they do not require a footage scope.
    """
    from .fingerprint import quick_fingerprint, full_sha256
    from .workbench_worker import ProviderRequestNotSent
    coverage = packaging.get("coverage")
    rendered_slots = None if coverage is None else {
        item["slot"] for item in coverage
        if item.get("status") == "rendered" and not item.get("instanceId")}
    used = [item for item in packaging.get("items", [])
            if not item.get("is_backup") and
            (rendered_slots is None or item.get("slot") in rendered_slots)]
    used.extend(packaging.get("instances", []))
    for item in used:
        if not item.get("file"):
            continue
        path = Path(item["file"])
        signature = item.get("fileSignature") or item.get("file_signature") or {}
        manifest = item.get("manifest") or {}
        expected = signature.get("sha256") or manifest.get("sha256") or manifest.get("source_sha256")
        try:
            if not expected or full_sha256(path) != expected:
                raise ProviderRequestNotSent("MODEL_MEDIA_PACKAGING_CHANGED: 包装媒体与冻结来源指纹不一致")
            fingerprint = quick_fingerprint(path)
            size = path.stat().st_size
        except OSError as error:
            raise ProviderRequestNotSent("MODEL_MEDIA_PACKAGING_UNAVAILABLE: 实际包装媒体不可核验") from error
        # A copy retains the catalog asset identity even after its path changes.
        matches = db.connection.execute(
            "SELECT asset_id FROM assets WHERE size_bytes=? AND quick_fingerprint=?",
            (size, fingerprint)).fetchall()
        for match in matches:
            require_source_access(db, task_id, match["asset_id"])
