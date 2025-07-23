import os
import shutil
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from app.core.pipeline import run_search
from app.models.schema import QuestionRequest, AnswerResponse, DocumentResponse, ErrorResponse
from app.core.highlight_snippet_in_pdf import find_and_highlight

ask_router = APIRouter(prefix="", tags=["Ask"])

@ask_router.post("/ask", response_model=AnswerResponse, responses={500: {"model": ErrorResponse}})
async def ask_question(request: QuestionRequest):
    try:
        result = await run_in_threadpool(run_search, request.question, request.model)

        highlighted_dir = os.path.join("output", "highlighted")
        if os.path.exists(highlighted_dir):
            shutil.rmtree(highlighted_dir)
        os.makedirs(highlighted_dir, exist_ok=True)

        async def highlight_all():
            for doc in result["documents"]:
                snippet = doc["content"]
                meta = doc["meta"]
                output_pdf = os.path.join("output", "highlighted", meta["file_path"])
                find_and_highlight(meta["file_path"], snippet, meta["page_number"], output_pdf)

        await run_in_threadpool(lambda: asyncio.run(highlight_all()))

        return AnswerResponse(
            answer=result["answer"],
            model_used=result["model_used"],
            documents=[DocumentResponse(**doc) for doc in result["documents"]]
        )
    except ValueError as ve:
        raise HTTPException(status_code=500, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")
