"""Indexes and persistence for 'no answer — no documents in index' events."""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.services.document_chat_unanswered.constants import (
    DOCUMENT_CHAT_UNANSWERED_COLLECTION,
)

logger = logging.getLogger(__name__)


async def ensure_document_chat_unanswered_indexes(db) -> None:
    coll = db[DOCUMENT_CHAT_UNANSWERED_COLLECTION]
    await coll.create_index(
        [("company_id", 1), ("added_by_admin_id", 1), ("at", -1)]
    )
    await coll.create_index([("added_by_admin_id", 1), ("at", -1)])
    await coll.create_index([("company_id", 1), ("at", -1)])


async def insert_document_chat_unanswered(db, doc: Dict[str, Any]) -> None:
    try:
        await ensure_document_chat_unanswered_indexes(db)
    except Exception as e:
        logger.warning("ensure_document_chat_unanswered_indexes: %s", e)
    try:
        await db[DOCUMENT_CHAT_UNANSWERED_COLLECTION].insert_one(doc)
    except Exception:
        logger.exception("insert_document_chat_unanswered failed")
