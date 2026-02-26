"""
Public Chat API - No authentication required.

Handles public access to chat functionality:
- GET /public-chat/{company_admin}/{chat_name} - Get chat info (with password check)
- POST /public-chat/{company_admin}/{chat_name}/query - Query the chat
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Body
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from bson import ObjectId
from app.deps.db import get_db
from app.api.rag import rag_query
from motor.motor_asyncio import AsyncIOMotorDatabase
import hashlib

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public-chat", tags=["Public Chat"])

security = HTTPBasic()


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
        
        # Check password if chat is private
        if chat.get("is_private", False):
            stored_password_hash = chat.get("password_hash")
            stored_password_plain = chat.get("password")
            
            if not stored_password_hash and not stored_password_plain:
                raise HTTPException(status_code=403, detail="Chat requires password")
            
            if not request.password:
                raise HTTPException(status_code=401, detail="Password required")
            
            # If password_hash exists, use it (new format - preferred)
            if stored_password_hash:
                if not verify_password(request.password, stored_password_hash):
                    logger.warning(f"Password verification failed for chat {chat.get('chat_name')} - hash mismatch")
                    raise HTTPException(status_code=401, detail="Invalid password")
            # If only plain password exists (old format), hash it and compare
            elif stored_password_plain:
                input_hash = hash_password(request.password)
                stored_hash = hash_password(stored_password_plain)
                if input_hash != stored_hash:
                    logger.warning(f"Password verification failed for chat {chat.get('chat_name')} - plain text comparison failed")
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
        index_id = f"publicchat/{company_id}/{admin_id}/{chat_name}"
        file_ids = []
        file_names = []
        
        for source in sources:
            file_name = source.get("file_name", "")
            if file_name:
                # Format: publicchat/{company_id}/{admin_id}/{chat_name}--{filename}
                file_id = f"{index_id}--{file_name}"
                file_ids.append(file_id)
                file_names.append(file_name)
        
        if not file_ids:
            raise HTTPException(
                status_code=400,
                detail="No valid sources found for querying"
            )
        
        # Query RAG
        try:
            rag_result = await rag_query(
                pass_ids=",".join(file_ids),
                question=request.question,
                file_names=file_names,
                company_id=company_id,
                index_id=index_id
            )
            
            # Parse RAG result
            answer_data = (
                rag_result.get("result", [{}])[1]
                if isinstance(rag_result.get("result"), list)
                else rag_result.get("result", {})
            )
            
            answer = answer_data.get("data", "No answer generated")
            
            # Get documents from RAG result (same format as ask endpoint)
            raw_docs = answer_data.get("documents", []) or rag_result.get("documents", [])
            
            # Normalize documents to match Document Chat format
            normalized_docs = []
            for doc in raw_docs:
                meta = doc.get("meta", {})
                file_id = meta.get("file_id", "")
                file_path = meta.get("file_path", "")
                
                # Find corresponding source to get type and url
                file_name = meta.get("file_name", "")
                source_info = next((s for s in sources if s.get("file_name") == file_name), {})
                
                normalized_docs.append({
                    "content": doc.get("content", ""),
                    "meta": {
                        "file_id": file_id,
                        "file_path": file_path,
                        "original_file_path": meta.get("original_file_path", ""),
                        "page_number": meta.get("page_number", 1),
                        "file_name": file_name,
                        "type": source_info.get("type", ""),
                        "url": source_info.get("url", ""),
                        "score": meta.get("score", None),
                    }
                })
            
            return {
                "success": True,
                "answer": answer,
                "documents": normalized_docs,
                "sources": [
                    {
                        "type": s.get("type", ""),
                        "file_name": s.get("file_name", ""),
                        "url": s.get("url")
                    }
                    for s in sources
                ]
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

