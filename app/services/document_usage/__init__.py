"""
Document usage analytics (Document Chat citations, dashboard aggregates).

Public API:
  - record_document_answer_usage / record_answer_citation_events
  - USAGE_COLLECTION (alias for Mongo collection name)
  - ensure_usage_indexes
"""

from app.services.document_usage.constants import USAGE_EVENTS_COLLECTION
from app.services.document_usage.repository import ensure_usage_indexes
from app.services.document_usage.service import (
    record_answer_citation_events,
    record_document_answer_usage,
)

# Legacy alias used by dashboard and older imports
USAGE_COLLECTION = USAGE_EVENTS_COLLECTION

__all__ = [
    "USAGE_COLLECTION",
    "USAGE_EVENTS_COLLECTION",
    "ensure_usage_indexes",
    "record_answer_citation_events",
    "record_document_answer_usage",
]
