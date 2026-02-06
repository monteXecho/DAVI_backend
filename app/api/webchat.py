"""
WebChat API endpoint.

Handles queries for WebChat module using webchat-specific index_id.
"""

import logging
from fastapi import APIRouter, HTTPException, Depends
from app.models.schema import QuestionRequest, AnswerResponse, DocumentResponse
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.api.rag import rag_query

logger = logging.getLogger("uvicorn")

webchat_router = APIRouter(prefix="/webchat", tags=["WebChat"])


@webchat_router.post("/ask", response_model=AnswerResponse)
async def ask_webchat_question(
    request: QuestionRequest,
    current_user=Depends(get_current_user),
    db=Depends(get_db)
):
    """
    Ask a question using WebChat sources (HTML files indexed with webchat index_id).
    """
    logger.info(f"WebChat question from {current_user.get('email')}")

    try:
        email = current_user.get("email")
        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")

        # Get user data
        company_repo = CompanyRepository(db)
        user_data = await company_repo.get_user_with_documents(email)
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found in DB")

        company_id = user_data.get("company_id")
        
        # Get all webchat sources for this company
        sources = await db.webchat_sources.find({
            "company_id": company_id,
            "status": "active"
        }).to_list(length=None)

        if not sources:
            return AnswerResponse(
                answer="Geen bronnen beschikbaar. Vraag uw beheerder om bronnen toe te voegen.",
                documents=[]
            )

        # Build file_ids from sources
        # Format: {admin_id}--{filename} for webchat sources
        file_ids = []
        file_names = []
        for source in sources:
            file_name = source.get("file_name", "")
            admin_id = source.get("admin_id", "")
            if file_name and admin_id:
                file_id = f"{admin_id}--{file_name}"
                file_ids.append(file_id)
                file_names.append(file_name)

        if not file_ids:
            return AnswerResponse(
                answer="Geen geïndexeerde bronnen beschikbaar.",
                documents=[]
            )

        # Query RAG with webchat index_id
        webchat_index_id = f"{company_id}_webchat"
        try:
            rag_result = await rag_query(
                pass_ids=",".join(file_ids),
                question=request.question,
                file_names=file_names,
                company_id=company_id,
                index_id=webchat_index_id
            )
        except RuntimeError as rag_error:
            error_detail = str(rag_error)
            if "404" in error_detail or "not found" in error_detail.lower():
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "WebChat service is not available. "
                        "Please contact your administrator to configure the WebChat sources."
                    )
                )
            raise HTTPException(
                status_code=503,
                detail=f"WebChat query service error: {error_detail}"
            )

        # Parse RAG result
        answer_data = (
            rag_result.get("result", [{}])[1]
            if isinstance(rag_result.get("result"), list)
            else rag_result.get("result", {})
        )

        raw_docs = answer_data.get("documents", []) or rag_result.get("documents", [])

        # Build source documents list
        normalized_docs = []
        for doc in raw_docs:
            meta = doc.get("meta", {})
            file_id = meta.get("file_id", "")
            file_name = meta.get("file_name", "")
            
            # Find source info
            source_info = None
            for source in sources:
                if source.get("file_name") == file_name:
                    source_info = source
                    break
            
            normalized_docs.append({
                "content": doc.get("content", ""),
                "meta": {
                    "file_id": file_id,
                    "file_name": file_name,
                    "url": source_info.get("url") if source_info else None,
                    "page_number": meta.get("page_number", 1),
                    "score": meta.get("score", None),
                }
            })

        logger.info(f"WebChat Answer: {answer_data.get('data')}")
        logger.info(f"Docs processed: {len(normalized_docs)}")

        return AnswerResponse(
            answer=answer_data.get("data", "No answer generated"),
            documents=[
                DocumentResponse(
                    content=doc["content"],
                    meta=doc["meta"]
                )
                for doc in normalized_docs
            ]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("WebChat query failed")
        raise HTTPException(status_code=500, detail=f"WebChat query failed: {str(e)}")

