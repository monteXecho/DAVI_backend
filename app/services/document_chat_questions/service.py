"""
Record each Document Chat (/ask/run) question for company-admin analytics.

Visibility: company admins see questions from users they created (`added_by_admin_id`)
plus their own questions when they use Document Chat as admin.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.document_chat_questions.constants import (
    DOCUMENT_CHAT_QUESTIONS_COLLECTION,
    MAX_ANSWER_PREVIEW_LENGTH,
    MAX_QUESTION_TEXT_LENGTH,
)
from app.services.document_chat_questions.repository import insert_document_chat_question
from app.services.document_usage.ids import normalize_company_id_str

logger = logging.getLogger(__name__)


def _resolve_added_by_admin_id(user_data: Dict[str, Any]) -> Optional[str]:
    """Workspace owner for dashboard scoping: admin → self; company user → added_by_admin_id."""
    if user_data.get("user_type") == "admin":
        return user_data.get("user_id")
    return user_data.get("added_by_admin_id")


def _folder_context_from_documents(documents: List[Dict[str, Any]]) -> List[str]:
    names: set = set()
    for d in documents or []:
        ut = (d.get("upload_type") or "").strip()
        if ut and ut != "document":
            names.add(ut)
    return sorted(names)


def _trim(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


async def record_document_chat_question(
    db,
    *,
    company_id: Any,
    user_data: Dict[str, Any],
    question_text: str,
    answer_text: Optional[str],
    has_cited_sources: bool = False,
) -> None:
    """
    Persist one row per successful Document Chat turn (non-blocking callers should try/except).

    Skips when we cannot attribute a workspace admin (missing added_by_admin_id for users).
    """
    added_by = _resolve_added_by_admin_id(user_data)
    if not added_by:
        logger.debug("document_chat_question skipped — no added_by_admin_id")
        return

    cid = normalize_company_id_str(company_id)
    if not cid:
        logger.warning("document_chat_question skipped — bad company_id")
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

    preview = None
    ac = None
    if answer_text:
        preview = _trim(answer_text, MAX_ANSWER_PREVIEW_LENGTH)
        ac = len(answer_text)

    doc = {
        "company_id": cid,
        "added_by_admin_id": added_by,
        "asker_user_id": asker_user_id,
        "asker_email": asker_email,
        "asker_name": asker_name,
        "asker_is_company_admin": is_admin,
        "question_text": q,
        "answer_preview": preview,
        "answer_char_count": ac,
        "has_cited_sources": bool(has_cited_sources),
        "assigned_roles_snapshot": roles,
        "folder_context": folder_ctx,
        "at": datetime.utcnow(),
        "source": "document_chat",
    }

    await insert_document_chat_question(db, doc)
