"""Canonical gate and selection identities for trusted render provenance."""

import hashlib
import json
from pathlib import Path
import uuid


def canonical_digest(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def selection_state(db, task_id: str) -> dict:
    assets = []
    rows = db.connection.execute(
        """
        SELECT ta.asset_id, ta.role, a.media_kind
        FROM task_assets ta JOIN assets a USING(asset_id)
        WHERE ta.task_id=? ORDER BY ta.asset_id
        """, (task_id,),
    ).fetchall()
    for row in rows:
        locations = db.connection.execute(
            """
            SELECT al.location_id, al.library_id, al.relative_path, l.mount_path, l.kind
            FROM asset_locations al JOIN libraries l USING(library_id)
            WHERE al.asset_id=? AND al.state='available' AND l.online=1
            ORDER BY CASE l.kind WHEN 'local' THEN 0 ELSE 1 END,
                     al.library_id, al.relative_path, al.location_id
            """, (row["asset_id"],),
        ).fetchall()
        if not locations:
            raise RuntimeError(f"ASSET_LOCATION_UNAVAILABLE: {row['asset_id']}")
        chosen = None
        for location in locations:
            relative = Path(location["relative_path"])
            root = Path(location["mount_path"]).resolve()
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"ASSET_PATH_INVALID: {row['asset_id']}")
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise RuntimeError(f"ASSET_PATH_INVALID: {row['asset_id']}") from error
            if path.is_file():
                chosen = {
                    "location_id": location["location_id"],
                    "library_id": location["library_id"],
                    "relative_path": location["relative_path"],
                    "path": str(path),
                    "sha256": content_sha256(path),
                }
                break
        if chosen is None:
            raise RuntimeError(f"ASSET_FILE_MISSING: {row['asset_id']}")
        assets.append({"asset_id": row["asset_id"], "role": row["role"],
                       "media_kind": row["media_kind"], "location": chosen})
    cards = []
    for row in db.connection.execute(
        "SELECT * FROM scene_cards WHERE task_id=? ORDER BY idx, card_id", (task_id,)
    ).fetchall():
        card = dict(row)
        card.pop("created_at", None)
        card.pop("updated_at", None)
        if card.get("segment_id"):
            from .segment_marks import resolve_source_segment
            try:
                card["segment"] = resolve_source_segment(db.connection, card["asset_id"], card["segment_id"])
            except (KeyError, ValueError):
                # Broken historical references remain readable, but cannot validate a current lock.
                card["segment"] = None
        cards.append(card)
    return {"task_assets": assets, "scene_cards": cards}


def selection_digest(db, task_id: str) -> tuple[str, dict]:
    state = selection_state(db, task_id)
    return canonical_digest(state), state



def selection_matches_picture_lock(
    db, task_id: str, locked_digest: str | None, *, current_state: dict | None = None
) -> bool:
    """Allow only the exact, default script identity backfill of migration 041."""
    if not locked_digest:
        return False
    state = current_state if current_state is not None else selection_state(db, task_id)
    if canonical_digest(state) == locked_digest:
        return True
    gate = latest_gate(db, task_id, "picture_lock")
    if gate is None or gate["gate_digest"] != locked_digest:
        return False
    try:
        locked_state = json.loads(gate["details_json"])
    except (TypeError, ValueError):
        return False
    if not isinstance(locked_state, dict) or canonical_digest(locked_state) != locked_digest:
        return False
    if locked_state.keys() != {"task_assets", "scene_cards"}:
        return False
    old_cards = locked_state["scene_cards"]
    cards = state.get("scene_cards")
    if (locked_state["task_assets"] != state.get("task_assets")
            or not isinstance(old_cards, list) or not isinstance(cards, list)
            or len(old_cards) != len(cards)):
        return False
    projected = []
    for old, card in zip(old_cards, cards, strict=True):
        if not isinstance(old, dict) or not isinstance(card, dict):
            return False
        if "script_segment_id" in old or "shot_revision" in old:
            return False
        expected_id = str(card.get("card_id")) + ":script"
        if card.get("script_segment_id") != expected_id or card.get("shot_revision") != 1:
            return False
        segment = db.connection.execute(
            "SELECT task_id,script_revision,idx,text,output_start_us,output_end_us "
            "FROM script_segments WHERE segment_id=?", (expected_id,)
        ).fetchone()
        if segment is None or segment["task_id"] != task_id or segment["script_revision"] != "legacy":
            return False
        if (segment["idx"] != card.get("idx") or segment["text"] != card.get("narrative_text")
                or segment["output_start_us"] != round(card["narrative_start"] * 1_000_000)
                or segment["output_end_us"] != round(card["narrative_end"] * 1_000_000)):
            return False
        projected_card = dict(card)
        projected_card.pop("script_segment_id")
        projected_card.pop("shot_revision")
        projected.append(projected_card)
    return canonical_digest({"task_assets": state["task_assets"], "scene_cards": projected}) == locked_digest


def record_gate(conn, task_id: str, gate_type: str, digest: str, details: dict) -> str:
    event_id = f"gate-{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO task_gate_events(event_id,task_id,gate_type,gate_digest,details_json) VALUES(?,?,?,?,?)",
        (event_id, task_id, gate_type, digest,
         json.dumps(details, ensure_ascii=False, sort_keys=True)),
    )
    return event_id


def latest_gate(db, task_id: str, gate_type: str):
    return db.connection.execute(
        "SELECT * FROM task_gate_events WHERE task_id=? AND gate_type=? ORDER BY rowid DESC LIMIT 1",
        (task_id, gate_type),
    ).fetchone()
