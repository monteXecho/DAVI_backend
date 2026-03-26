import os
import shutil
import logging
import asyncio

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.concurrency import run_in_threadpool

from app.core.highlight_snippet_in_pdf import find_and_highlight
from app.models.schema import QuestionRequest, AnswerResponse, DocumentResponse, ErrorResponse
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.api.rag import rag_query
from app.services.multi_index_answer_merge import RagIndexSegment, select_segments_for_merge

logger = logging.getLogger("uvicorn")

ask_router = APIRouter(prefix="/ask", tags=["Ask"])


async def _record_unanswered_no_docs_and_raise(
    db,
    *,
    company_id,
    user_data: dict,
    question: str,
):
    """Persist analytics row, then return the standard 404 for no indexed documents."""
    try:
        from app.services.document_chat_unanswered import record_unanswered_no_documents

        await record_unanswered_no_documents(
            db,
            company_id=company_id,
            user_data=user_data,
            question_text=question,
        )
    except Exception as e:
        logger.warning("record_unanswered_no_documents failed (non-fatal): %s", e)
    raise HTTPException(
        status_code=404,
        detail="No documents found in any available index. Please ensure documents are indexed.",
    )


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
        
        # Group documents by index_id since private and role-based documents use different indexes
        # Private documents: documentchat-{company_id}-{user_id}--{filename}
        # Role-based documents: documentchat-{company_id}-{admin_id}--{filename}
        import re
        pass_ids_list = user_data["pass_ids"] if isinstance(user_data["pass_ids"], list) else user_data["pass_ids"].split(",")
        
        # Group pass_ids and documents by index_id
        index_groups = {}  # {index_id: {"pass_ids": [...], "file_names": [...], "documents": [...]}}
        
        for i, pass_id in enumerate(pass_ids_list):
            pass_id = pass_id.strip()
            if not pass_id:
                continue
                
            # Extract index_id from format: documentchat-{company_id}-{user_id_or_admin_id}--{filename}
            match = re.match(rf"(documentchat-{re.escape(company_id)}-[a-f0-9-]+)--", pass_id)
            if match:
                index_id = match.group(1)
            else:
                # Fallback: try to extract from other formats or use default
                logger.warning(f"Could not extract index_id from pass_id: {pass_id}")
                # Try to determine from document
                if i < len(user_documents):
                    doc = user_documents[i]
                    upload_type = doc.get("upload_type", "document")
                    if upload_type == "document":
                        user_id = user_data.get("user_id")
                        index_id = f"documentchat-{company_id}-{user_id}" if user_id else f"documentchat-{company_id}"
                    else:
                        admin_id = user_data.get("added_by_admin_id")
                        if not admin_id:
                            admin_user = await db.company_admins.find_one({"email": email})
                            if admin_user:
                                admin_id = admin_user.get("user_id")
                        index_id = f"documentchat-{company_id}-{admin_id}" if admin_id else f"documentchat-{company_id}"
                else:
                    # Last resort: use user_id
                    user_id = user_data.get("user_id")
                    index_id = f"documentchat-{company_id}-{user_id}" if user_id else f"documentchat-{company_id}"
            
            if index_id not in index_groups:
                index_groups[index_id] = {"pass_ids": [], "file_names": [], "documents": []}
            
            index_groups[index_id]["pass_ids"].append(pass_id)
            if i < len(user_documents):
                doc = user_documents[i]
                index_groups[index_id]["file_names"].append(doc.get("file_name", ""))
                index_groups[index_id]["documents"].append(doc)

        # ------------------------------------------------------------------
        # Call RAG API for each index group and merge results
        # ------------------------------------------------------------------
        rag_segment_results: list[RagIndexSegment] = []

        for index_id, group in index_groups.items():
            if not group["pass_ids"]:
                continue
                
            pass_ids_str = ",".join(group["pass_ids"])
            file_names = group["file_names"]
            
            logger.info(f"Querying index {index_id} with {len(group['pass_ids'])} documents")
            
            try:
                # Add retry logic for index not found errors
                max_retries = 3
                retry_delay = 1  # seconds
                rag_result = None
                
                for attempt in range(max_retries):
                    try:
                        rag_result = await rag_query(
                            pass_ids=pass_ids_str,
                            question=request.question,
                            file_names=file_names,
                            company_id=company_id,
                            index_id=index_id
                        )
                        break  # Success, exit retry loop
                    except RuntimeError as e:
                        error_str = str(e)
                        if "404" in error_str or "not found" in error_str.lower() or "index_not_found" in error_str.lower():
                            if attempt < max_retries - 1:
                                logger.warning(f"Index {index_id} not found (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                                await asyncio.sleep(retry_delay)
                                retry_delay *= 2  # Exponential backoff
                                continue
                            else:
                                logger.error(f"Index {index_id} not found after {max_retries} attempts")
                                # Continue to next index group instead of failing completely
                                continue
                        else:
                            raise  # Re-raise if it's a different error
                
                if not rag_result:
                    logger.warning(f"No result from index {index_id}, skipping")
                    continue
                    
            except RuntimeError as rag_error:
                # Log error but continue with other index groups
                error_detail = str(rag_error)
                logger.error(f"RAG query failed for index {index_id}: {error_detail}")
                if "404" not in error_detail and "not found" not in error_detail.lower():
                    # Only raise if it's not a "not found" error (we'll handle those by continuing)
                    raise HTTPException(
                        status_code=503,
                        detail=f"RAG query service error: {error_detail}"
                    )
                continue  # Skip this index group and try others
            
            # Parse RAG result for this index
            answer_data = (
                rag_result.get("result", [{}])[1]
                if isinstance(rag_result.get("result"), list)
                else rag_result.get("result", {})
            )
            
            answer_text = answer_data.get("data", "")
            raw_docs = answer_data.get("documents", []) or rag_result.get("documents", [])
            
            logger.info(f"Index {index_id}: Answer length={len(answer_text) if answer_text else 0}, Documents={len(raw_docs)}")
            rag_segment_results.append(
                RagIndexSegment(index_id=index_id, answer_text=answer_text or "", raw_docs=raw_docs)
            )

        # Drop redundant per-index answers (e.g. "no information" when another index
        # cites evidence), then rebuild combined docs + offsets so [n] stays valid.
        selected_segments = select_segments_for_merge(rag_segment_results)

        all_raw_docs = []
        all_answers_with_offsets = []  # (answer_text, doc_offset, len(raw_docs))
        doc_offset = 0
        for seg in selected_segments:
            if seg.answer_text:
                all_answers_with_offsets.append((seg.answer_text, doc_offset, len(seg.raw_docs)))
            all_raw_docs.extend(seg.raw_docs)
            doc_offset += len(seg.raw_docs)

        # Merge answers and adjust citation numbers
        # Citations in each answer are relative to that query's document list (1-based)
        # We need to adjust them to be relative to the combined document list
        import re
        merged_answer_parts = []
        
        for answer_text, offset, doc_count in all_answers_with_offsets:
            # Adjust citation numbers: [1] becomes [offset+1], [2] becomes [offset+2], etc.
            # Citations are 1-based, so [1] = first document (index 0)
            # If offset=10 (10 docs from previous queries), then [1] from this query should become [11]
            def adjust_citation(match):
                citation_num = int(match.group(1))
                adjusted_num = citation_num + offset
                return f"[{adjusted_num}]"
            
            adjusted_answer = re.sub(r'\[(\d+)\]', adjust_citation, answer_text)
            merged_answer_parts.append(adjusted_answer)
            logger.info(f"Adjusted answer from offset {offset}: citations adjusted, answer length={len(adjusted_answer)}")
        
        if merged_answer_parts:
            answer_text = " ".join(merged_answer_parts) if len(merged_answer_parts) > 1 else merged_answer_parts[0]
            logger.info(f"Merged answer from {len(merged_answer_parts)} sources, total length={len(answer_text)}")
        else:
            answer_text = "No answer generated"
        
        # Use all raw_docs from all index groups
        raw_docs = all_raw_docs
        
        # If no documents were found from any index, return a helpful message
        if not raw_docs and not all_answers_with_offsets:
            await _record_unanswered_no_docs_and_raise(
                db,
                company_id=company_id,
                user_data=user_data,
                question=request.question,
            )

        # ------------------------------------------------------------------
        # Parse citations from merged answer and filter documents
        # ------------------------------------------------------------------
        # Parse citations from answer text (e.g., [1], [2])
        citation_matches = re.findall(r'\[(\d+)\]', answer_text)
        cited_indices = [int(match) - 1 for match in citation_matches]  # Convert to 0-based indices
        unique_cited_indices = sorted(set(cited_indices))  # Remove duplicates
        
        logger.info(f"Merged answer has {len(citation_matches)} citations: {citation_matches}, which map to document indices: {unique_cited_indices}")
        logger.info(f"Total documents available: {len(raw_docs)}")
        
        # Filter documents to only include cited ones
        if cited_indices:
            filtered_docs = []
            seen_file_ids = set()  # Track by file_id to avoid duplicates
            for idx in cited_indices:
                if 0 <= idx < len(raw_docs):
                    doc = raw_docs[idx]
                    file_id = doc.get("meta", {}).get("file_id", "")
                    file_name = doc.get("meta", {}).get("file_name", "unknown")
                    # Only add if we haven't seen this document yet (avoid duplicates)
                    if file_id not in seen_file_ids:
                        filtered_docs.append(doc)
                        seen_file_ids.add(file_id)
                        logger.info(f"Added cited document at index {idx}: file_id={file_id}, file_name={file_name}")
                    else:
                        logger.info(f"Skipped duplicate document at index {idx}: file_id={file_id}, file_name={file_name} (already added)")
                else:
                    logger.warning(f"Citation index {idx} out of range for {len(raw_docs)} documents (max index: {len(raw_docs)-1})")
            raw_docs = filtered_docs
            logger.info(f"Filtered to {len(raw_docs)} unique cited documents from {len(unique_cited_indices)} unique citations")
        else:
            # No citations found, don't show any documents
            raw_docs = []
            logger.info("No citations found in answer, showing no documents")
        
        # If no documents were found from any index, return a helpful message
        if not raw_docs and not all_answers_with_offsets:
            await _record_unanswered_no_docs_and_raise(
                db,
                company_id=company_id,
                user_data=user_data,
                question=request.question,
            )
        
        # If we have an answer but no cited documents, log a warning
        if answer_text and not raw_docs:
            logger.warning(f"Answer generated but no cited documents found. Citations in answer: {citation_matches}")
            # Don't show any documents if none are cited - this ensures we only show cited documents

        normalized_docs = []
        for doc in raw_docs:
            meta = doc.get("meta", {})
            file_id = meta.get("file_id", "")  # This contains user_id--filename.pdf
            file_path = meta.get("file_path", "")
            
            # Extract filename from file_id (format: documentchat-{company_id}-{user_id}--{filename})
            file_name = meta.get("file_name", "")
            if not file_name and file_id:
                # Extract filename from file_id after the last --
                if "--" in file_id:
                    file_name = file_id.split("--")[-1]
                else:
                    file_name = os.path.basename(file_id)
            
            normalized_doc = {
                "content": doc.get("content", ""),
                "meta": {
                    "file_id": file_id,
                    "file_path": file_path,  # for backward compatibility
                    "original_file_path": meta.get("original_file_path", ""),
                    "page_number": meta.get("page_number", 1),
                    "file_name": file_name,
                    "score": meta.get("score", None),
                    "source_url": meta.get("source_url"),  # From RAG metadata
                    "source_title": meta.get("source_title"),  # From RAG metadata
                }
            }
            normalized_docs.append(normalized_doc)
            logger.info(f"Normalized document: file_id={file_id}, file_name={file_name}, original_file_path={normalized_doc['meta'].get('original_file_path', 'N/A')}")

        # ------------------------------------------------------------------
        # Highlighting process
        # ------------------------------------------------------------------
        output_dir = os.path.join("output", "highlighted")
        await run_in_threadpool(prepare_highlighted_dir, output_dir)
        await run_in_threadpool(highlight_documents, normalized_docs, output_dir, user_documents)

        # ------------------------------------------------------------------
        # Build response
        # ------------------------------------------------------------------
        logger.info(f"RAG Answer: {answer_text}")
        logger.info(f"Docs processed: {len(normalized_docs)} documents from {len(index_groups)} index groups")
        
        # Log each document being returned
        for i, doc in enumerate(normalized_docs):
            logger.info(f"Returning document {i+1}: file_id={doc['meta'].get('file_id', 'N/A')}, file_name={doc['meta'].get('file_name', 'N/A')}")

        # ------------------------------------------------------------------
        # Per-document usage (cited in generated answer) for admin dashboard
        # ------------------------------------------------------------------
        try:
            from app.services.document_usage import record_document_answer_usage

            await record_document_answer_usage(
                db,
                company_id,
                normalized_docs,
                asker_user_id=user_data.get("user_id"),
                asker_email=email,
                question_text=request.question,
            )
        except Exception as e:
            logger.warning("record_document_answer_usage failed (non-fatal): %s", e)

        try:
            from app.services.document_chat_questions import record_document_chat_question

            await record_document_chat_question(
                db,
                company_id=company_id,
                user_data=user_data,
                question_text=request.question,
                answer_text=answer_text,
                has_cited_sources=len(normalized_docs) > 0,
            )
        except Exception as e:
            logger.warning("record_document_chat_question failed (non-fatal): %s", e)

        # No sources returned to the client: index had no hits (404 above) OR model
        # answered without [n] citations (e.g. "not in the documents") — still "no
        # related documents" for the dashboard.
        if not normalized_docs:
            try:
                from app.services.document_chat_unanswered import (
                    REASON_NO_CITATIONS_IN_ANSWER,
                    record_unanswered_no_documents,
                )

                await record_unanswered_no_documents(
                    db,
                    company_id=company_id,
                    user_data=user_data,
                    question_text=request.question,
                    reason=REASON_NO_CITATIONS_IN_ANSWER,
                )
            except Exception as e:
                logger.warning(
                    "record_unanswered_no_documents (no citations path) failed (non-fatal): %s",
                    e,
                )

        return AnswerResponse(
            answer=answer_text,  # Use parsed answer_text
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
