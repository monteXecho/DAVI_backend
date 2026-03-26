"""Persistence for document usage events."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from app.services.document_usage.constants import USAGE_EVENTS_COLLECTION

logger = logging.getLogger(__name__)


async def ensure_usage_indexes(db) -> None:
    coll = db[USAGE_EVENTS_COLLECTION]
    await coll.create_index([("company_id", 1), ("document_id", 1), ("at", -1)])
    await coll.create_index([("document_id", 1)])
    await coll.create_index([("company_id", 1), ("asker_user_id", 1), ("at", -1)])


async def insert_usage_events(db, events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    try:
        await ensure_usage_indexes(db)
    except Exception as e:
        logger.warning("ensure_usage_indexes: %s", e)
    try:
        await db[USAGE_EVENTS_COLLECTION].insert_many(events)
    except Exception:
        logger.exception("insert_usage_events failed")
