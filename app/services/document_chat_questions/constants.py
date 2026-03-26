"""MongoDB collection for Document Chat question analytics (admin dashboard)."""

DOCUMENT_CHAT_QUESTIONS_COLLECTION = "document_chat_questions"

# Cap stored question length (full text search / UI); trim with ellipsis in service if needed
MAX_QUESTION_TEXT_LENGTH = 8000
MAX_ANSWER_PREVIEW_LENGTH = 400
