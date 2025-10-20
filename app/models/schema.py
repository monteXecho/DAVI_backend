from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class QuestionRequest(BaseModel):
    question: str = Field(..., min_length=3)

class DocumentResponse(BaseModel):
    content: str
    meta: Dict[str, Any]
    score: Optional[float] = None

class AnswerResponse(BaseModel):
    answer: str
    documents: List[DocumentResponse]

class ErrorResponse(BaseModel):
    detail: str

