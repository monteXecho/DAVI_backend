"""
Public Chat API - No authentication required.

Handles public access to chat functionality:
- GET /public-chat/{company_admin}/{chat_name} - Get chat info (with password check)
- POST /public-chat/{company_admin}/{chat_name}/query - Query the chat
"""

import logging
import os
import shutil
import asyncio
from typing import Optional
from urllib.parse import unquote
from fastapi import APIRouter, HTTPException, Depends, Body
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from bson import ObjectId
from app.deps.db import get_db
from app.api.rag import rag_query
from app.core.highlight_snippet_in_pdf import find_and_highlight
from motor.motor_asyncio import AsyncIOMotorDatabase
import hashlib

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public-chat", tags=["Public Chat"])

security = HTTPBasic()


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
                logger.warning(f"Could not remove {file_path}: {e}")
    else:
        os.makedirs(output_dir, exist_ok=True)


def highlight_public_chat_documents(documents, output_dir: str, sources: list):
    """
    Highlight documents returned from RAG query for public chat.
    
    Args:
        documents: List of document dicts with content and meta from RAG
        output_dir: Directory to save highlighted PDFs
        sources: List of public chat sources with file_path info
    """
    # Create a map of file_name -> file_path for quick lookup
    source_map = {s.get("file_name"): s.get("file_path", "") for s in sources}
    
    for doc in documents:
        snippet = doc["content"]
        meta = doc["meta"]
        file_id = meta.get("file_id", "")
        file_name = meta.get("file_name", "")
        
        # Extract filename from file_id if it follows the pattern: {index_id}--{filename}
        file_id_filename = None
        if file_id and "--" in file_id:
            file_id_filename = file_id.split("--", 1)[1]
        
        # Find the source file path
        abs_input_path = None
        
        # Try to match by file_name or file_id_filename
        for source_file_name, source_file_path in source_map.items():
            if source_file_name == file_name or (file_id_filename and source_file_name == file_id_filename):
                if source_file_path and os.path.exists(source_file_path):
                    abs_input_path = source_file_path
                    logger.info(f"Found source file path: {abs_input_path}")
                    break
        
        # Fallback: try original_file_path or file_path from meta
        if not abs_input_path:
            original_path = meta.get("original_file_path", "")
            file_path_from_meta = meta.get("file_path", "")
            
            if original_path and os.path.exists(original_path):
                abs_input_path = original_path
                logger.info(f"Using original_file_path: {abs_input_path}")
            elif file_path_from_meta and os.path.exists(file_path_from_meta):
                abs_input_path = file_path_from_meta
                logger.info(f"Using file_path from meta: {abs_input_path}")
        
        if not abs_input_path:
            logger.warning(f"File not found for highlighting. file_id: {file_id}, file_name: {file_name}")
            continue
        
        if not os.path.exists(abs_input_path):
            logger.warning(f"File path does not exist: {abs_input_path}")
            continue
        
        # Only highlight PDF files
        if not abs_input_path.lower().endswith('.pdf'):
            logger.info(f"Skipping non-PDF file: {abs_input_path}")
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


def hash_password(password: str) -> str:
    """Simple password hashing (in production, use bcrypt or similar)."""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash."""
    return hash_password(password) == hashed


class PublicChatQueryRequest(BaseModel):
    question: str
    password: Optional[str] = None

class PublicChatPasswordVerify(BaseModel):
    password: str


@router.get("/{company_admin}/{chat_name}")
async def get_public_chat_info(
    company_admin: str,
    chat_name: str,
    db=Depends(get_db)
):
    """Get public chat information. Returns chat details if accessible."""
    try:
        # Find admin by email or user_id (company_admin can be either)
        admin = await db.company_admins.find_one({
            "$or": [
                {"email": company_admin},
                {"user_id": company_admin}
            ]
        })
        if not admin:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        admin_id = str(admin.get("user_id", ""))
        company_id = admin.get("company_id", "")
        
        # Find chat
        chat = await db.public_chats.find_one({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_name": chat_name
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        # Return chat info (without password)
        return {
            "success": True,
            "chat": {
                "chat_name": chat.get("chat_name", ""),
                "is_private": chat.get("is_private", False),
                "requires_password": bool(chat.get("password")),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get public chat info")
        raise HTTPException(status_code=500, detail=f"Failed to get chat info: {str(e)}")


@router.post("/{company_admin}/{chat_name}/verify-password")
async def verify_public_chat_password(
    company_admin: str,
    chat_name: str,
    request: PublicChatPasswordVerify = Body(...),
    db=Depends(get_db)
):
    """Verify password for a private public chat without querying RAG."""
    try:
        # Find admin by email or user_id (company_admin can be either)
        admin = await db.company_admins.find_one({
            "$or": [
                {"email": company_admin},
                {"user_id": company_admin}
            ]
        })
        if not admin:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        admin_id = str(admin.get("user_id", ""))
        company_id = admin.get("company_id", "")
        
        # Find chat
        chat = await db.public_chats.find_one({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_name": chat_name
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        # Check if chat is private
        if not chat.get("is_private", False):
            return {
                "success": True,
                "verified": True,
                "message": "Chat is not private, no password required"
            }
        
        # Verify password
        stored_password_hash = chat.get("password_hash")
        stored_password_plain = chat.get("password")
        
        if not stored_password_hash and not stored_password_plain:
            raise HTTPException(status_code=403, detail="Chat requires password")
        
        if not request.password:
            raise HTTPException(status_code=401, detail="Password required")
        
        # If password_hash exists, use it (new format - preferred)
        if stored_password_hash:
            if not verify_password(request.password, stored_password_hash):
                raise HTTPException(status_code=401, detail="Invalid password")
        # If only plain password exists (old format), hash it and compare
        elif stored_password_plain:
            input_hash = hash_password(request.password)
            stored_hash = hash_password(stored_password_plain)
            if input_hash != stored_hash:
                raise HTTPException(status_code=401, detail="Invalid password")
            # Auto-migrate: update to new format for future requests
            try:
                await db.public_chats.update_one(
                    {"_id": chat["_id"]},
                    {"$set": {"password_hash": stored_hash}}
                )
                logger.info(f"Auto-migrated password hash for chat {chat.get('chat_name')}")
            except Exception as e:
                logger.warning(f"Failed to migrate password hash for chat {chat['_id']}: {e}")
        else:
            raise HTTPException(status_code=401, detail="Invalid password")
        
        return {
            "success": True,
            "verified": True,
            "message": "Password verified successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to verify password")
        raise HTTPException(status_code=500, detail=f"Failed to verify password: {str(e)}")


@router.post("/{company_admin}/{chat_name}/query")
async def query_public_chat(
    company_admin: str,
    chat_name: str,
    request: PublicChatQueryRequest = Body(...),
    db=Depends(get_db)
):
    """Query a public chat. Requires password if chat is private."""
    try:
        # Find admin by email or user_id (company_admin can be either)
        admin = await db.company_admins.find_one({
            "$or": [
                {"email": company_admin},
                {"user_id": company_admin}
            ]
        })
        if not admin:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        admin_id = str(admin.get("user_id", ""))
        company_id = admin.get("company_id", "")
        
        # Find chat
        chat = await db.public_chats.find_one({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_name": chat_name
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        # Note: Password verification is handled by /verify-password endpoint
        # Once verified, users can query without sending password again
        
        # Get all sources for this chat
        sources = await db.public_chat_sources.find({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": str(chat["_id"]),
            "status": "active"
        }).to_list(length=None)
        
        if not sources:
            raise HTTPException(
                status_code=400,
                detail="No sources available for this chat"
            )
        
        # Build file_ids for RAG query
        # Use hyphens instead of slashes to comply with Elasticsearch index name restrictions
        index_id = f"publicchat-{company_id}-{admin_id}-{chat_name}"
        file_ids = []
        file_names = []
        
        for source in sources:
            file_name = source.get("file_name", "")
            if file_name:
                # Format: publicchat-{company_id}-{admin_id}-{chat_name}--{filename}
                file_id = f"{index_id}--{file_name}"
                file_ids.append(file_id)
                file_names.append(file_name)
        
        if not file_ids:
            raise HTTPException(
                status_code=400,
                detail="No valid sources found for querying"
            )
        
        # Query RAG with retry logic for index not found errors
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
                    index_id=index_id
                )
                break  # Success, exit retry loop
            except RuntimeError as e:
                error_str = str(e)
                if "404" in error_str or "not found" in error_str.lower() or "index_not_found" in error_str.lower():
                    if attempt < max_retries - 1:
                        logger.warning(f"PublicChat index {index_id} not found (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                        import asyncio
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        continue
                    else:
                        logger.error(f"PublicChat index {index_id} not found after {max_retries} attempts")
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "Public chat service is not available. "
                                "The index is not ready. Please try again in a moment."
                            )
                        )
                else:
                    raise  # Re-raise if it's a different error
        
        if not rag_result:
            raise HTTPException(
                status_code=503,
                detail="Public chat query service is not available."
            )
        
        try:
            
            # Parse RAG result
            answer_data = (
                rag_result.get("result", [{}])[1]
                if isinstance(rag_result.get("result"), list)
                else rag_result.get("result", {})
            )
            
            answer_text = answer_data.get("data", "No answer generated")
            
            # Get documents from RAG result (same format as ask endpoint)
            raw_docs = answer_data.get("documents", []) or rag_result.get("documents", [])
            
            # Keep full raw_docs - frontend filterDocumentsByCitations maps [3] -> documents[2]
            # Normalize documents to match Document Chat format
            normalized_docs = []
            used_file_ids = set()  # Track used file_ids for better matching
            for doc in raw_docs:
                meta = doc.get("meta", {})
                file_id = meta.get("file_id", "")
                file_path = meta.get("file_path", "")
                
                # Find corresponding source to get type and url
                # Try matching by file_id first (most reliable), then by file_name
                file_name = meta.get("file_name", "")
                source_info = None
                
                # Extract filename from file_id if it follows the pattern: {index_id}--{filename}
                file_id_filename = None
                if file_id and "--" in file_id:
                    file_id_filename = file_id.split("--", 1)[1]
                
                # Try to match source by file_id filename or file_name
                for s in sources:
                    source_file_name = s.get("file_name", "")
                    # Match by exact file_name or by filename extracted from file_id
                    if source_file_name == file_name or (file_id_filename and source_file_name == file_id_filename):
                        source_info = s
                        break
                
                if not source_info:
                    source_info = {}
                
                used_file_ids.add(file_id)
                
                # Prefer source_url and source_title from RAG metadata, fallback to database
                source_url_val = meta.get("source_url") or source_info.get("url", "")
                source_title_val = meta.get("source_title") or source_info.get("title", "")
                # Use full URL as title when no title (same as WebChat)
                if source_url_val and not source_title_val:
                    source_title_val = source_url_val

                original_path = meta.get("original_file_path") or (source_info.get("file_path") if source_info else "")

                normalized_docs.append({
                    "content": doc.get("content", ""),
                    "meta": {
                        "file_id": file_id,
                        "file_path": source_info.get("file_path", "") if source_info else file_path,
                        "original_file_path": original_path,
                        "page_number": meta.get("page_number", 1),
                        "file_name": file_name,
                        "type": source_info.get("type", ""),
                        "url": source_url_val,
                        "score": meta.get("score", None),
                        "source_url": source_url_val,
                        "source_title": source_title_val or source_info.get("file_name", ""),
                    }
                })
            
            # Filter sources to only include those that were actually used in the RAG response
            # Match by file_name or by extracting filename from file_id
            filtered_sources = []
            for s in sources:
                source_file_name = s.get("file_name", "")
                # Check if this source matches any used file_id
                matches = False
                for file_id in used_file_ids:
                    if "--" in file_id:
                        file_id_filename = file_id.split("--", 1)[1]
                        if source_file_name == file_id_filename:
                            matches = True
                            break
                    # Also check direct file_name match
                    if source_file_name == file_id:
                        matches = True
                        break
                
                # Also check if file_name matches any normalized doc file_name
                if not matches:
                    for doc in normalized_docs:
                        if doc["meta"].get("file_name") == source_file_name:
                            matches = True
                            break
                
                if matches:
                    filtered_sources.append({
                        "type": s.get("type", ""),
                        "file_name": source_file_name,
                        "url": s.get("url")
                    })
            
            # Highlight documents (similar to DocumentChat)
            output_dir = os.path.join("output", "highlighted")
            await run_in_threadpool(prepare_highlighted_dir, output_dir)
            await run_in_threadpool(highlight_public_chat_documents, normalized_docs, output_dir, sources)
            
            return {
                "success": True,
                "answer": answer_text,  # Use parsed answer_text
                "documents": normalized_docs,
                "sources": filtered_sources
            }
        except Exception as rag_error:
            logger.error(f"RAG query failed: {rag_error}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to generate answer: {str(rag_error)}"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to query public chat")
        raise HTTPException(status_code=500, detail=f"Failed to query chat: {str(e)}")


@router.get("/{company_admin}/{chat_name}/download/{filename:path}")
async def download_public_chat_file(
    company_admin: str,
    chat_name: str,
    filename: str,
    db=Depends(get_db)
):
    """Download a file from a public chat. No authentication required."""
    try:
        # Find admin by email or user_id (company_admin can be either)
        admin = await db.company_admins.find_one({
            "$or": [
                {"email": company_admin},
                {"user_id": company_admin}
            ]
        })
        if not admin:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        admin_id = str(admin.get("user_id", ""))
        company_id = admin.get("company_id", "")
        
        # Find chat
        chat = await db.public_chats.find_one({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_name": chat_name
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        
        chat_id = str(chat["_id"])
        
        # URL-decode the filename (it comes encoded in the URL)
        decoded_filename = unquote(filename)
        
        # Find source to verify file belongs to this chat
        # Try both encoded and decoded filename to handle different cases
        source = await db.public_chat_sources.find_one({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "$or": [
                {"file_name": filename},
                {"file_name": decoded_filename}
            ],
            "status": "active"
        })
        
        if not source:
            raise HTTPException(status_code=404, detail=f"File not found: {filename} (decoded: {decoded_filename})")
        
        file_path = source.get("file_path")
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found on server")
        
        # Determine media type based on file extension
        file_ext = os.path.splitext(filename)[1].lower()
        media_type_map = {
            '.pdf': 'application/pdf',
            '.html': 'text/html',
            '.htm': 'text/html',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        }
        media_type = media_type_map.get(file_ext, 'application/octet-stream')
        
        # Return the file with headers to open in browser instead of downloading
        return FileResponse(
            file_path,
            filename=filename,
            media_type=media_type,
            headers={
                'Content-Disposition': f'inline; filename="{filename}"'
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to download public chat file")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")

