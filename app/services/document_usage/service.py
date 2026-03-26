"""
Record citation-level usage for Document Chat answers.

Called after a successful /ask/run when cited sources are known. Tracks who asked
(company user vs admin) so dashboard aggregates remain correct for admin-owned documents.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.document_usage.constants import EVENT_ANSWER_CITATION
from app.services.document_usage.ids import normalize_company_id_str
from app.services.document_usage.repository import insert_usage_events
from app.services.document_usage.resolver import resolve_document_for_citation

logger = logging.getLogger(__name__)


async def record_answer_citation_events(
    db,
    company_id: str,
    normalized_docs: List[Dict[str, Any]],
    *,
    asker_user_id: Optional[str] = None,
    asker_email: Optional[str] = None,
    question_text: Optional[str] = None,
) -> None:
    """
    Insert one event per unique cited document.

    `normalized_docs` matches the structure built in ask.py (content + meta per source).
    """
    if not normalized_docs:
        return

    now = datetime.utcnow()
    seen: set = set()
    events: List[Dict[str, Any]] = []

    for doc in normalized_docs:
        meta = doc.get("meta") or {}
        file_id = (meta.get("file_id") or "").strip()

        rec = await resolve_document_for_citation(db, company_id, meta)
        if not rec:
            logger.warning(
                "Usage event skipped — unresolved document (file_id=%s, path=%s)",
                file_id[:100] if file_id else "",
                (meta.get("original_file_path") or "")[:120],
            )
            continue

        doc_id = str(rec["_id"])
        if doc_id in seen:
            continue
        seen.add(doc_id)

        cid_store = normalize_company_id_str(company_id)
        if not cid_store:
            logger.warning("Usage event skipped — empty company_id")
            continue

        upload_type = rec.get("upload_type") or ""
        path = rec.get("path") or ""
        folder_label = upload_type if upload_type != "document" else ""
        file_name = rec.get("file_name") or meta.get("file_name") or ""

        qt = (question_text or "").strip()
        events.append(
            {
                "company_id": cid_store,
                "document_id": doc_id,
                "document_owner_id": rec.get("user_id"),
                "file_id": file_id,
                "file_name": file_name,
                "folder_name": folder_label,
                "path": path,
                "event_type": EVENT_ANSWER_CITATION,
                "asker_user_id": asker_user_id,
                "asker_email": asker_email,
                "at": now,
                "action": "Antwoord gegenereerd",
                "question_text": qt,
            }
        )

    await insert_usage_events(db, events)


async def record_document_answer_usage(
    db,
    company_id: str,
    normalized_docs: List[Dict[str, Any]],
    *,
    asker_user_id: Optional[str] = None,
    asker_email: Optional[str] = None,
    question_text: Optional[str] = None,
) -> None:
    await record_answer_citation_events(
        db,
        company_id,
        normalized_docs,
        asker_user_id=asker_user_id,
        asker_email=asker_email,
        question_text=question_text,
    )
