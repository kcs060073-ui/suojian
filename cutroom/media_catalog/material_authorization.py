"""Explicit authorization attestations bound to an immutable picture lock."""

from __future__ import annotations

import json
import uuid


def _locked_assets(db, task_id: str) -> tuple[str, list[str]]:
    lock = db.connection.execute(
        "SELECT selection_digest,changed_since_lock FROM picture_lock WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if lock is None or int(lock["changed_since_lock"] or 0):
        raise RuntimeError(
            "PICTURE_LOCK_MISSING: 必须先锁定当前画面，才能确认镜头素材授权"
        )
    asset_ids = sorted({
        str(row["asset_id"])
        for row in db.connection.execute(
            "SELECT asset_id FROM scene_cards WHERE task_id=? AND asset_id IS NOT NULL",
            (task_id,),
        ).fetchall()
    })
    if not asset_ids:
        raise RuntimeError(
            "MATERIAL_AUTHORIZATION_EMPTY: 当前画面锁没有可确认授权的镜头素材"
        )
    return str(lock["selection_digest"]), asset_ids


def _public(row, asset_ids: list[str]) -> dict:
    return {
        "attestationId": None if row is None else row["attestation_id"],
        "approved": row is not None,
        "pictureLockDigest": None if row is None else row["picture_lock_digest"],
        "assetIds": asset_ids,
        "confirmedBy": None if row is None else row["confirmed_by"],
        "confirmedAt": None if row is None else row["created_at"],
    }


def _self_produced_approved(db, asset_ids: list[str]) -> dict[str, dict]:
    if not asset_ids:
        return {}
    placeholders = ",".join("?" for _ in asset_ids)
    rows = db.connection.execute(
        f"""
        SELECT asset.asset_id,own.confirmed_by,own.confirmed_at
        FROM assets asset
        JOIN self_produced_asset_authorizations own USING(asset_id)
        WHERE asset.asset_id IN ({placeholders})
          AND asset.quick_fingerprint=own.quick_fingerprint
        """,
        tuple(asset_ids),
    ).fetchall()
    return {str(row["asset_id"]): dict(row) for row in rows}


def get_material_authorization(db, task_id: str) -> dict:
    try:
        picture_digest, asset_ids = _locked_assets(db, task_id)
    except RuntimeError:
        return _public(None, [])
    row = db.connection.execute(
        """
        SELECT * FROM material_authorization_attestations
        WHERE task_id=? AND picture_lock_digest=?
        ORDER BY created_at DESC,rowid DESC LIMIT 1
        """,
        (task_id, picture_digest),
    ).fetchone()
    if row is not None:
        try:
            attested_ids = sorted(str(value) for value in json.loads(row["asset_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            attested_ids = []
        if attested_ids == asset_ids:
            return _public(row, asset_ids)
    reusable = _self_produced_approved(db, asset_ids)
    if len(reusable) != len(asset_ids):
        return _public(None, asset_ids)
    result = _public(None, asset_ids)
    result.update(
        approved=True,
        pictureLockDigest=picture_digest,
        confirmedBy="自摄素材已登记",
        confirmedAt=max(item["confirmed_at"] for item in reusable.values()),
    )
    return result


def confirm_material_authorization(db, task_id: str, confirmed_by: str) -> dict:
    actor = str(confirmed_by or "").strip()
    if not actor:
        raise ValueError("MATERIAL_AUTHORIZATION_REVIEWER_REQUIRED: 请填写授权核验人")
    picture_digest, asset_ids = _locked_assets(db, task_id)
    attestation_id = f"material-auth-{uuid.uuid4().hex[:16]}"
    with db.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO material_authorization_attestations(
              attestation_id,task_id,picture_lock_digest,asset_ids_json,confirmed_by
            ) VALUES(?,?,?,?,?)
            """,
            (
                attestation_id,
                task_id,
                picture_digest,
                json.dumps(asset_ids, ensure_ascii=False, separators=(",", ":")),
                actor,
            ),
        )
    return get_material_authorization(db, task_id)


def confirm_self_produced_material(db, task_id: str, confirmed_by: str) -> dict:
    actor = str(confirmed_by or "").strip()
    if not actor:
        raise ValueError("MATERIAL_AUTHORIZATION_REVIEWER_REQUIRED: 请填写自摄素材核验人")
    _picture_digest, asset_ids = _locked_assets(db, task_id)
    placeholders = ",".join("?" for _ in asset_ids)
    rows = db.connection.execute(
        f"SELECT asset_id,quick_fingerprint FROM assets WHERE asset_id IN ({placeholders})",
        tuple(asset_ids),
    ).fetchall()
    if len(rows) != len(asset_ids) or any(not row["quick_fingerprint"] for row in rows):
        raise RuntimeError("MATERIAL_AUTHORIZATION_ASSET_MISSING: 镜头素材身份不完整")
    with db.transaction(immediate=True) as connection:
        for row in rows:
            connection.execute(
                """
                INSERT INTO self_produced_asset_authorizations(
                  asset_id,quick_fingerprint,confirmed_by
                ) VALUES(?,?,?)
                ON CONFLICT(asset_id) DO UPDATE SET
                  quick_fingerprint=excluded.quick_fingerprint,
                  confirmed_by=excluded.confirmed_by,
                  confirmed_at=datetime('now')
                """,
                (row["asset_id"], row["quick_fingerprint"], actor),
            )
    return get_material_authorization(db, task_id)


def shot_authorization(db, task_id: str) -> dict[str, str]:
    attestation = get_material_authorization(db, task_id)
    if attestation["approved"]:
        return {asset_id: "approved" for asset_id in attestation["assetIds"]}
    return {
        asset_id: "approved"
        for asset_id in _self_produced_approved(db, attestation["assetIds"])
    }
