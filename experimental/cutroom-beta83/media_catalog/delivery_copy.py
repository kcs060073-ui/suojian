"""Candidate-bound release copy drafts, independent of video/render revisions."""

from __future__ import annotations

import json
import uuid


class DeliveryCopyConflict(RuntimeError):
    """A newer manual draft exists for this candidate."""


def _public(row) -> dict | None:
    if row is None:
        return None
    value = dict(row)
    value["tags"] = json.loads(value.pop("tags_json"))
    return value


def get_delivery_copy_draft(db, task_id: str, candidate_id: str) -> dict | None:
    row = db.connection.execute(
        """SELECT * FROM delivery_copy_drafts
           WHERE task_id=? AND candidate_id=?
           ORDER BY revision DESC LIMIT 1""",
        (task_id, candidate_id),
    ).fetchone()
    return _public(row)


def _insert_delivery_copy_draft(
    connection, task_id: str, candidate_id: str, *, expected_revision: int,
    title: str, description: str, tags: list[str],
    source_kind: str = "manual", source_ref: str | None = None,
    model_version: str | None = None,
) -> dict:
    """Append one copy revision inside the caller's transaction."""
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError("DELIVERY_DRAFT_REVISION_INVALID: 草稿版本无效")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("DELIVERY_TITLE_REQUIRED: 请填写发布标题")
    if not isinstance(description, str) or not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("DELIVERY_COPY_INVALID: 发布文字格式无效")
    if source_kind not in {"manual", "accepted_suggestion"} or (
        source_kind == "manual" and (source_ref is not None or model_version is not None)
    ) or (source_kind == "accepted_suggestion" and (not source_ref or not model_version)):
        raise ValueError("DELIVERY_SOURCE_INVALID: 发布文字来源无效")
    clean_tags = list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))
    row = connection.execute(
        """SELECT * FROM delivery_copy_drafts
           WHERE task_id=? AND candidate_id=?
           ORDER BY revision DESC LIMIT 1""",
        (task_id, candidate_id),
    ).fetchone()
    current_revision = 0 if row is None else row["revision"]
    if current_revision != expected_revision:
        raise DeliveryCopyConflict("DELIVERY_DRAFT_CHANGED: 发布文字已被更新，请刷新后核对")
    if row is not None and (row["title"], row["description"], json.loads(row["tags_json"])) == (
        title.strip(), description.strip(), clean_tags
    ) and (source_kind == "manual" or (
        row["source_kind"], row["source_ref"], row["model_version"]
    ) == (source_kind, source_ref, model_version)):
        # Saving identical text must retain the original provenance and revision.
        return _public(row)
    draft_id = f"delivery-copy-{uuid.uuid4().hex}"
    connection.execute(
        """INSERT INTO delivery_copy_drafts(
               draft_id,task_id,candidate_id,revision,title,description,tags_json,
               source_kind,source_ref,model_version
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (draft_id, task_id, candidate_id, current_revision + 1,
         title.strip(), description.strip(), json.dumps(clean_tags, ensure_ascii=False),
         source_kind, source_ref, model_version),
    )
    saved = connection.execute(
        "SELECT * FROM delivery_copy_drafts WHERE draft_id=?", (draft_id,),
    ).fetchone()
    return _public(saved)


def save_delivery_copy_draft(
    db, task_id: str, candidate_id: str, *, expected_revision: int,
    title: str, description: str, tags: list[str],
) -> dict:
    with db.transaction(immediate=True) as connection:
        return _insert_delivery_copy_draft(
            connection, task_id, candidate_id, expected_revision=expected_revision,
            title=title, description=description, tags=tags,
        )
