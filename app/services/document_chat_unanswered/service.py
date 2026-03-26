"""
Record Document Chat questions where the user gets no document-backed answer.

- no_documents_in_index: HTTP 404 — nothing retrieved from any index.
- no_citations_in_answer: HTTP 200 — RAG may return chunks, but the model answer
  has no [n] citations, so we return no sources (nothing to ground in the UI).

Not used for HTTP 503/500 or other service errors.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from app.services.document_chat_questions.constants import MAX_QUESTION_TEXT_LENGTH
from app.services.document_chat_questions.service import (
    _folder_context_from_documents,
    _resolve_added_by_admin_id,
    _trim,
)
from app.services.document_chat_unanswered.repository import insert_document_chat_unanswered
from app.services.document_usage.ids import normalize_company_id_str

logger = logging.getLogger(__name__)

REASON_NO_DOCUMENTS_IN_INDEX = "no_documents_in_index"
REASON_NO_CITATIONS_IN_ANSWER = "no_citations_in_answer"


async def record_unanswered_no_documents(
    db,
    *,
    company_id: Any,
    user_data: Dict[str, Any],
    question_text: str,
    reason: str = REASON_NO_DOCUMENTS_IN_INDEX,
) -> None:
    added_by = _resolve_added_by_admin_id(user_data)
    if not added_by:
        logger.debug("document_chat_unanswered skipped — no added_by_admin_id")
        return

    cid = normalize_company_id_str(company_id)
    if not cid:
        logger.warning("document_chat_unanswered skipped — bad company_id")
        return

    q = _trim(question_text, MAX_QUESTION_TEXT_LENGTH)
    if not q:
        return

    asker_user_id = user_data.get("user_id")
    asker_email = (user_data.get("email") or "").strip()
    asker_name = (user_data.get("name") or "").strip()
    is_admin = user_data.get("user_type") == "admin"
    roles = list(user_data.get("assigned_roles") or [])
    folder_ctx = _folder_context_from_documents(user_data.get("documents") or [])

    doc = {
        "company_id": cid,
        "added_by_admin_id": added_by,
        "asker_user_id": asker_user_id,
        "asker_email": asker_email,
        "asker_name": asker_name,
        "asker_is_company_admin": is_admin,
        "question_text": q,
        "assigned_roles_snapshot": roles,
        "folder_context": folder_ctx,
        "at": datetime.utcnow(),
        "source": "document_chat",
        "reason": reason,
    }

    await insert_document_chat_unanswered(db, doc)
