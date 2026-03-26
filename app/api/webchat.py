"""
WebChat API endpoint.

Handles queries for WebChat module using webchat-specific index_id.
"""

import logging
import asyncio
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
        user_id = user_data.get("user_id")
        
        logger.info(f"WebChat query - user_id={user_id}, company_id={company_id}")
        
        # Check if user is a company admin by checking if they exist in company_admins collection
        admin_user = await db.company_admins.find_one({"email": email})
        if admin_user:
            # Company admin: use their own user_id as admin_id
            admin_id_for_sources = user_id
            logger.info(f"User is company admin, using admin_id_for_sources={admin_id_for_sources}")
        else:
            # Company user: must find the admin who added this user
            # First try to get added_by_admin_id from user_data (it might be included)
            user_admin_id = user_data.get("added_by_admin_id")
            logger.info(f"added_by_admin_id from user_data: {user_admin_id}")
            
            # If not in user_data, query the company_users collection directly by user_id
            if not user_admin_id:
                logger.info(f"Querying company_users collection for user_id: {user_id}")
                company_user = await db.company_users.find_one({"user_id": user_id})
                if company_user:
                    user_admin_id = company_user.get("added_by_admin_id")
                    logger.info(f"Retrieved added_by_admin_id from company_users collection: {user_admin_id}")
                    logger.info(f"User document keys: {list(company_user.keys())}")
                else:
                    logger.error(f"Company user not found in company_users collection with user_id: {user_id}")
                    return AnswerResponse(
                        answer="Geen bronnen beschikbaar. Uw account is niet correct gekoppeld aan een beheerder. Neem contact op met uw beheerder.",
                        documents=[]
                    )
            
            logger.info(f"Company user found - added_by_admin_id={user_admin_id}")
            
            if not user_admin_id:
                logger.error(f"Company user {email} has no added_by_admin_id in database. This should not happen.")
                return AnswerResponse(
                    answer="Geen bronnen beschikbaar. Uw account is niet correct gekoppeld aan een beheerder. Neem contact op met uw beheerder.",
                    documents=[]
                )
            
            # Verify that the admin exists
            admin_who_added = await db.company_admins.find_one({
                "user_id": user_admin_id,
                "company_id": company_id
            })
            
            if not admin_who_added:
                logger.error(f"Admin who added user {email} (admin_id={user_admin_id}) not found in company_admins")
                return AnswerResponse(
                    answer="Geen bronnen beschikbaar. De beheerder die u heeft toegevoegd is niet meer actief. Neem contact op met uw beheerder.",
                    documents=[]
                )
            
            admin_id_for_sources = user_admin_id
            logger.info(f"User is company user, using admin_id_for_sources={admin_id_for_sources} (admin who added this user)")
        
        if not admin_id_for_sources:
            logger.warning(f"No admin_id_for_sources found for user {email}")
            return AnswerResponse(
                answer="Geen bronnen beschikbaar. Vraag uw beheerder om bronnen toe te voegen.",
                documents=[]
            )
        
        # Get webchat sources ONLY from the determined admin
        # WebChat sources are admin-specific: users only see sources from their admin
        sources_query = {
            "company_id": company_id,
            "admin_id": admin_id_for_sources,  # Filter by determined admin
            "status": "active"
        }
        logger.info(f"Querying webchat_sources with: {sources_query}")
        
        sources = await db.webchat_sources.find(sources_query).to_list(length=None)
        logger.info(f"Found {len(sources)} active sources for admin_id={admin_id_for_sources}")

        if not sources:
            logger.warning(f"No active sources found for company_id={company_id}, admin_id={admin_id_for_sources}")
            return AnswerResponse(
                answer="Geen bronnen beschikbaar. Vraag uw beheerder om bronnen toe te voegen.",
                documents=[]
            )

        # Build file_ids from sources
        # Format: webchat-{company_id}-{admin_id}--{filename} to match indexing format
        file_ids = []
        file_names = []
        
        # All sources are from the same admin (admin_id_for_sources), so use single index
        webchat_index_id = f"webchat-{company_id}-{admin_id_for_sources}"
        logger.info(f"Using webchat_index_id={webchat_index_id}")
        
        for source in sources:
            file_name = source.get("file_name", "")
            if file_name:
                # Use webchat-{company_id}-{admin_id}--{filename} format to match indexing
                file_id = f"{webchat_index_id}--{file_name}"
                file_ids.append(file_id)
                file_names.append(file_name)
                logger.debug(f"Added source: file_name={file_name}, file_id={file_id}")
            else:
                logger.warning(f"Source missing file_name: {source}")

        logger.info(f"Built {len(file_ids)} file_ids from {len(sources)} sources")
        
        if not file_ids:
            logger.warning(f"No file_ids built from sources. Sources: {[s.get('file_name', 'N/A') for s in sources]}")
            return AnswerResponse(
                answer="Geen geïndexeerde bronnen beschikbaar.",
                documents=[]
            )
        
        # Add retry logic for index not found errors (similar to DocumentChat)
        max_retries = 3
        retry_delay = 1  # seconds
        rag_result = None
        
        for attempt in range(max_retries):
            try:
                rag_result = await rag_query(
                    pass_ids=",".join(file_ids),
                    question=request.question,
                    file_names=file_names,
                    company_id=company_id,
                    index_id=webchat_index_id
                )
                break  # Success, exit retry loop
            except RuntimeError as e:
                error_str = str(e)
                if "404" in error_str or "not found" in error_str.lower() or "index_not_found" in error_str.lower():
                    if attempt < max_retries - 1:
                        logger.warning(f"WebChat index {webchat_index_id} not found (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        continue
                    else:
                        logger.error(f"WebChat index {webchat_index_id} not found after {max_retries} attempts")
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "WebChat service is not available. "
                                "The index is not ready. Please try again in a moment."
                            )
                        )
                if "500" in error_str or "Internal Server Error" in error_str:
                    logger.error(f"RAG query service error: {e}")
                    return AnswerResponse(
                        answer=(
                            "De antwoordservice is tijdelijk niet beschikbaar. "
                            "Probeer het opnieuw. Als het probleem aanhoudt, vraag uw beheerder om bronnen opnieuw toe te voegen (URL opnieuw importeren)."
                        ),
                        documents=[],
                    )
                raise
        
        if not rag_result:
            raise HTTPException(
                status_code=503,
                detail="WebChat query service is not available."
            )

        # Parse RAG result
        answer_data = (
            rag_result.get("result", [{}])[1]
            if isinstance(rag_result.get("result"), list)
            else rag_result.get("result", {})
        )

        answer_text = answer_data.get("data", "No answer generated")
        raw_docs = answer_data.get("documents", []) or rag_result.get("documents", [])

        # Parse citations from answer text (e.g., [1], [2])
        # Keep full raw_docs - frontend filterDocumentsByCitations maps citation [3] -> documents[2]
        # Backend must send documents in original RAG order for that mapping to work

        # Build source documents list
        normalized_docs = []
        for doc in raw_docs:
            meta = doc.get("meta", {})
            file_id = meta.get("file_id", "")
            file_name = meta.get("file_name", "")
            
            # Find source info from database (for backward compatibility)
            # Try matching by file_id filename first, then by file_name
            source_info = None
            file_id_filename = None
            if file_id and "--" in file_id:
                file_id_filename = file_id.split("--", 1)[1]
            
            for source in sources:
                source_file_name = source.get("file_name", "")
                # Match by exact file_name or by filename extracted from file_id
                if source_file_name == file_name or (file_id_filename and source_file_name == file_id_filename):
                    source_info = source
                    break
            
            # Prefer source_url and source_title from RAG metadata, fallback to database
            source_url_val = meta.get("source_url") or (source_info.get("url") if source_info else None)
            source_title_val = meta.get("source_title") or (source_info.get("title") if source_info else None)
            # For URL/HTML sources: use full URL as display title when no title (avoids showing only domain)
            if source_url_val and not source_title_val:
                source_title_val = source_url_val

            normalized_docs.append({
                "content": doc.get("content", ""),
                "meta": {
                    "file_id": file_id,
                    "file_name": file_name,
                    "file_path": source_info.get("file_path") if source_info else None,
                    "url": source_url_val,
                    "page_number": meta.get("page_number", 1),
                    "score": meta.get("score", None),
                    "source_url": source_url_val,
                    "source_title": source_title_val or (source_info.get("file_name", "") if source_info else ""),
                    "type": source_info.get("type") if source_info else None,
                }
            })

        logger.info(f"WebChat Answer: {answer_data.get('data')}")
        logger.info(f"Docs processed: {len(normalized_docs)}")

        return AnswerResponse(
            answer=answer_text,  # Use parsed answer_text
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
