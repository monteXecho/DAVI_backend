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
                print(f"Could not remove {file_path}: {e}")
    else:
        os.makedirs(output_dir, exist_ok=True)


def highlight_documents(documents, output_dir: str, user_documents: list):
    """
    Highlight documents returned from RAG query.
    
    Args:
        documents: List of document dicts with content and meta from RAG
        output_dir: Directory to save highlighted PDFs
        user_documents: List of user documents with path info (from get_user_with_documents)
    """
    # Create a map of file_name -> path for quick lookup
    file_path_map = {doc.get("file_name"): doc.get("path", "") for doc in user_documents}
    
    for doc in documents:
        snippet = doc["content"]
        meta = doc["meta"]
        file_id = meta.get("file_id")
        actual_file_name = meta.get("file_path") or meta.get("file_name")
        
        if not file_id:
            logger.warning("Missing file_id in document meta.")
            continue
        
        # Try to get the actual file path from the document path map
        # The file_name in meta might be just the filename or the full file_path
        file_name_from_meta = meta.get("file_name", "")
        abs_input_path = None
        
        # Strategy 1: Check if original_file_path exists and is valid
        original_path = meta.get("original_file_path", "")
        if original_path and os.path.exists(original_path):
            abs_input_path = original_path
            logger.info(f"Using original_file_path: {abs_input_path}")
        else:
            # Strategy 2: Use file_path from meta if it exists and is valid
            file_path_from_meta = meta.get("file_path", "")
            if file_path_from_meta and os.path.exists(file_path_from_meta):
                abs_input_path = file_path_from_meta
                logger.info(f"Using file_path from meta: {abs_input_path}")
            else:
                # Strategy 3: Look up in user_documents by file_name
                # Try different variations of the file name
                possible_names = [
                    actual_file_name,
                    file_name_from_meta,
                    os.path.basename(actual_file_name) if actual_file_name else None,
                    os.path.basename(file_path_from_meta) if file_path_from_meta else None
                ]
                
                for name in possible_names:
                    if name and name in file_path_map:
                        candidate_path = file_path_map[name]
                        if candidate_path and os.path.exists(candidate_path):
                            abs_input_path = candidate_path
                            logger.info(f"Found path from user_documents map: {abs_input_path}")
                            break
                
                # Strategy 4: Try constructing path from original_file_path if it has a pattern
                if not abs_input_path and original_path:
                    # If original_path looks like it's from /var/opt, try /app/uploads equivalent
                    if '/var/opt/DAVI_backend/uploads/documents' in original_path:
                        relative = original_path.replace('/var/opt/DAVI_backend/uploads/documents', '').lstrip('/')
                        candidate = os.path.join('/app/uploads/documents', relative)
                        if os.path.exists(candidate):
                            abs_input_path = candidate
                            logger.info(f"Constructed path from /var/opt pattern: {abs_input_path}")
                    # If it starts with /app/uploads/documents, try /var/opt equivalent
                    elif original_path.startswith('/app/uploads/documents'):
                        relative = original_path.replace('/app/uploads/documents', '').lstrip('/')
                        candidate = os.path.join('/var/opt/DAVI_backend/uploads/documents', relative)
                        if os.path.exists(candidate):
                            abs_input_path = candidate
                            logger.info(f"Constructed path from /app/uploads pattern: {abs_input_path}")
        
        if not abs_input_path:
            logger.error(f"File not found for highlighting. file_id: {file_id}, file_name: {actual_file_name}, original_path: {original_path}")
            continue
        
        if not os.path.exists(abs_input_path):
            logger.error(f"File path does not exist: {abs_input_path}")
            continue
        
        # Use the actual file name (not full path) for output
        output_file_name = os.path.basename(abs_input_path)
        output_path = os.path.join(output_dir, output_file_name)
        
        logger.info(f"Highlighting: {abs_input_path} -> {output_path}")
        
        try:
            find_and_highlight(abs_input_path, snippet, meta.get("page_number", 1), output_path)
            meta["highlighted_path"] = output_path
            # Update file_path to just the filename for the frontend
            meta["file_path"] = output_file_name
            logger.info(f"Successfully highlighted: {output_file_name}")
        except Exception as e:
            logger.error(f"Failed to process {abs_input_path}: {e}")

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
        try:
            rag_result = await rag_query(
                pass_ids=user_data["pass_ids"],
                question=request.question,
                file_names=file_names,
                company_id=company_id
            )
        except RuntimeError as rag_error:
            # Provide user-friendly error message for RAG service issues
            error_detail = str(rag_error)
            if "404" in error_detail or "not found" in error_detail.lower():
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "RAG query service is not available. "
                        "The query pipeline is not configured. "
                        "Please contact your administrator to configure the RAG service pipeline."
                    )
                )
            raise HTTPException(
                status_code=503,
                detail=f"RAG query service error: {error_detail}"
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
                    "original_file_path": meta.get("original_file_path", ""),
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
        await run_in_threadpool(highlight_documents, normalized_docs, output_dir, user_documents)

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
