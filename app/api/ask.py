import os
import shutil
import logging

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.concurrency import run_in_threadpool

from app.core.highlight_snippet_in_pdf import find_and_highlight
from app.models.schema import QuestionRequest, AnswerResponse, DocumentResponse, ErrorResponse
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.api.rag import rag_query

logger = logging.getLogger("uvicorn")

ask_router = APIRouter(prefix="/ask", tags=["Ask"])


# --------------------------------------------------------------------------
# Utility functions
# --------------------------------------------------------------------------

def prepare_highlighted_dir(output_dir: str):
    """Clean up old highlighted files but keep the directory intact."""
    if os.path.exists(output_dir):
        for filename in os.listdir(output_dir):
            file_path = os.path.join(output_dir, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"⚠️ Could not remove {file_path}: {e}")
    else:
        os.makedirs(output_dir, exist_ok=True)


def highlight_documents(documents, output_dir: str):
    """
    Highlight matched snippets in user documents.
    File naming convention: {user_id}--{file_name}.pdf
    """
    base_upload_dir = "/var/opt/DAVI_backend/uploads/documents"

    for doc in documents:
        snippet = doc["content"]
        meta = doc["meta"]
        file_id = meta.get("file_id") 

        if not file_id:
            logger.warning("⚠️ Missing file_id in document meta.")
            continue

        # Extract user_id and file_name
        if "--" in file_id:
            user_id, actual_file_name = file_id.split("--", 1)
        else:
            logger.warning(f"⚠️ Unexpected file_id format: {meta}")
            continue

        abs_input_path = os.path.join(base_upload_dir, user_id, actual_file_name)
        output_path = os.path.join(output_dir, actual_file_name)

        if not os.path.exists(abs_input_path):
            logger.error(f"❌ File not found for highlighting: {abs_input_path}")
            continue

        try:
            find_and_highlight(abs_input_path, snippet, meta.get("page_number", 1), output_path)
            meta["highlighted_path"] = output_path  # add for frontend
            logger.info(f"✅ Highlighted file created: {output_path}")
        except Exception as e:
            logger.error(f"❌ Failed to highlight {abs_input_path}: {e}")


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------

@ask_router.post(
    "/run",
    response_model=AnswerResponse,
    responses={500: {"model": ErrorResponse}},
    status_code=status.HTTP_200_OK,
)
async def ask_question(
    request: QuestionRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    logger.info(f"Incoming raw request: {request}")

    try:
        # ------------------------------------------------------------------
        # Validate user
        # ------------------------------------------------------------------
        email = current_user.get("email")
        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")

        logger.info(f"Received question from {email}")

        # ------------------------------------------------------------------
        # Get user and documents
        # ------------------------------------------------------------------
        company_repo = CompanyRepository(db)
        user_data = await company_repo.get_user_with_documents(email)
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found in DB")

        company_id = user_data.get("company_id")
        user_documents = user_data["documents"]

        # ------------------------------------------------------------------
        # Call RAG API
        # ------------------------------------------------------------------
        payload = {"query": request.question, "document_list": user_documents}
        logger.info(f"Payload to RAG: {payload}")

        file_names = [doc["file_name"] for doc in user_documents]
        rag_result = await rag_query(
            user_id=user_data["user_id"],
            question=request.question,
            file_names=file_names,
            company_id=company_id
        )

        # ------------------------------------------------------------------
        # Parse RAG result
        # ------------------------------------------------------------------
        answer_data = (
            rag_result.get("result", [{}])[1]
            if isinstance(rag_result.get("result"), list)
            else rag_result.get("result", {})
        )

        raw_docs = answer_data.get("documents", []) or rag_result.get("documents", [])

        normalized_docs = []
        for doc in raw_docs:
            meta = doc.get("meta", {})
            file_id = meta.get("file_id", "")  # This contains user_id--filename.pdf
            file_path = meta.get("file_path", "")
            normalized_docs.append({
                "content": doc.get("content", ""),
                "meta": {
                    "file_id": file_id,
                    "file_path": file_path,  # for backward compatibility
                    "page_number": meta.get("page_number", 1),
                    "file_name": meta.get("file_name", os.path.basename(file_id)),
                    "score": meta.get("score", None),
                }
            })

        # ------------------------------------------------------------------
        # Highlighting process
        # ------------------------------------------------------------------
        output_dir = os.path.join("output", "highlighted")
        await run_in_threadpool(prepare_highlighted_dir, output_dir)
        await run_in_threadpool(highlight_documents, normalized_docs, output_dir)

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        logger.info(f"RAG Answer: {answer_data.get('data')}")
        logger.info(f"Docs processed: {normalized_docs}")

        return AnswerResponse(
            answer=answer_data.get("data", "No answer generated"),
            documents=[
                DocumentResponse(
                    content=doc["content"],
                    meta=doc["meta"]
                )
                for doc in normalized_docs
            ],
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled exception during /ask")
        raise HTTPException(status_code=500, detail="Internal server error")
