"""Persist one-off user actions for company-admin dashboard (e.g. document deletes)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from app.services.document_usage.ids import normalize_company_id_str

logger = logging.getLogger(__name__)

USER_ACTIVITY_LOG_COLLECTION = "company_user_activity_log"


async def ensure_user_activity_log_indexes(db) -> None:
    coll = db[USER_ACTIVITY_LOG_COLLECTION]
    await coll.create_index(
        [("company_id", 1), ("added_by_admin_id", 1), ("at", -1)]
    )
    await coll.create_index([("user_id", 1), ("at", -1)])


async def record_user_activity_event(
    db,
    *,
    company_id: Any,
    added_by_admin_id: str,
    user_id: str,
    kind: str,
    what: str,
    at: Optional[datetime] = None,
) -> None:
    """
    Non-blocking callers should try/except.

    `added_by_admin_id` is the workspace owner used to filter dashboard rows
    (same as document_chat_questions.added_by_admin_id).

    Always stores a canonical string `company_id` so inserts match dashboard
    queries (never use company_id_field_match here — that is for reads only).
    """
    canon_cid = normalize_company_id_str(company_id)
    if not canon_cid or not user_id:
        return
    doc = {
        "company_id": canon_cid,
        "added_by_admin_id": added_by_admin_id,
        "user_id": user_id,
        "kind": kind,
        "what": (what or "").strip() or kind,
        "at": at or datetime.utcnow(),
    }
    try:
        await ensure_user_activity_log_indexes(db)
    except Exception as e:
        logger.debug("ensure_user_activity_log_indexes: %s", e)
    try:
        await db[USER_ACTIVITY_LOG_COLLECTION].insert_one(doc)
    except Exception:
        logger.exception("record_user_activity_event failed")
