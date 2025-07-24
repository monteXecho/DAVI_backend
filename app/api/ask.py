import os
import shutil
import logging

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.concurrency import run_in_threadpool

from app.core.pipeline import run_search
from app.core.highlight_snippet_in_pdf import find_and_highlight
from app.models.schema import QuestionRequest, AnswerResponse, DocumentResponse, ErrorResponse
from app.deps.auth import get_current_user

logger = logging.getLogger(__name__)

ask_router = APIRouter(prefix="", tags=["Ask"])

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
    current_user: dict = Depends(get_current_user)  # 🔐 Auth handled here
):
    try:
        logger.info(f"Received question from user: {current_user.get('preferred_username', 'unknown')}")

        result = await run_in_threadpool(run_search, request.question, request.model)

        output_dir = os.path.join("output", "highlighted")
        await run_in_threadpool(prepare_highlighted_dir, output_dir)
        await run_in_threadpool(highlight_documents, result["documents"], output_dir)

        return AnswerResponse(
            answer=result.get("answer", ""),
            model_used=result.get("model_used", ""),
            documents=[DocumentResponse(**doc) for doc in result.get("documents", [])]
        )

    except ValueError as ve:
        logger.warning(f"Validation error: {ve}")
        raise HTTPException(status_code=500, detail=str(ve))

    except Exception as e:
        logger.exception("Unhandled exception during /ask")
        raise HTTPException(status_code=500, detail="Internal server error")
