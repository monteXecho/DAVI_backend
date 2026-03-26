"""Document Chat questions with no indexed documents (admin dashboard)."""

from app.services.document_chat_unanswered.constants import DOCUMENT_CHAT_UNANSWERED_COLLECTION
from app.services.document_chat_unanswered.repository import ensure_document_chat_unanswered_indexes
from app.services.document_chat_unanswered.service import (
    REASON_NO_CITATIONS_IN_ANSWER,
    REASON_NO_DOCUMENTS_IN_INDEX,
    record_unanswered_no_documents,
)

__all__ = [
    "DOCUMENT_CHAT_UNANSWERED_COLLECTION",
    "ensure_document_chat_unanswered_indexes",
    "record_unanswered_no_documents",
    "REASON_NO_DOCUMENTS_IN_INDEX",
    "REASON_NO_CITATIONS_IN_ANSWER",
]
