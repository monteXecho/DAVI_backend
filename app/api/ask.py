import os
import shutil
import logging
import httpx

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.concurrency import run_in_threadpool

from app.core.highlight_snippet_in_pdf import find_and_highlight
from app.models.schema import QuestionRequest, AnswerResponse, DocumentResponse, ErrorResponse
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository

logger = logging.getLogger(__name__)

ask_router = APIRouter(prefix="", tags=["Ask"])

RAG_API_URL = os.getenv("RAG_API_URL", "http://rag-service:8001/query")  


def prepare_highlighted_dir(output_dir: str):
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)


def highlight_documents(documents, output_dir: str):
    for doc in documents:
        snippet = doc["content"]
        meta = doc["meta"]
        output_path = os.path.join(output_dir, meta["file_path"])
        find_and_highlight(meta["file_path"], snippet, meta["page_number"], output_path)


@ask_router.post(
    "/ask",
    response_model=AnswerResponse,
    responses={500: {"model": ErrorResponse}},
    status_code=status.HTTP_200_OK,
)
async def ask_question(
    request: QuestionRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        email = current_user.get("email")
        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")

        logger.info(f"Received question from {email}")

        # Get documents for the current user (admin or normal user)
        company_repo = CompanyRepository(db)
        user_data = await company_repo.get_user_with_documents(email)
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found in DB")

        user_documents = user_data["documents"]

        # Send payload to external RAG API
        payload = {"query": request.question, "document_list": user_documents}
        logger.info(f"Payload to RAG: {payload}")


        async with httpx.AsyncClient(timeout=60) as client:
            rag_response = await client.post(RAG_API_URL, json=payload)
            rag_response.raise_for_status()
            rag_result = rag_response.json()

        # Highlight flow
        output_dir = os.path.join("output", "highlighted")
        await run_in_threadpool(prepare_highlighted_dir, output_dir)
        await run_in_threadpool(highlight_documents, rag_result.get("documents", []), output_dir)

        return AnswerResponse(
            answer=rag_result.get("answer", ""),
            model_used=rag_result.get("model_used", request.model),
            documents=[DocumentResponse(**doc) for doc in rag_result.get("documents", [])],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled exception during /ask")
        raise HTTPException(status_code=500, detail="Internal server error")
