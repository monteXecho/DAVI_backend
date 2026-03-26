"""Document Chat question log for company admin dashboard analytics."""

from app.services.document_chat_questions.constants import DOCUMENT_CHAT_QUESTIONS_COLLECTION
from app.services.document_chat_questions.repository import ensure_document_chat_question_indexes
from app.services.document_chat_questions.service import record_document_chat_question

__all__ = [
    "DOCUMENT_CHAT_QUESTIONS_COLLECTION",
    "ensure_document_chat_question_indexes",
    "record_document_chat_question",
]
