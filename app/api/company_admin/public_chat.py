"""
Public Chat domain router for company admin API.

Handles all public chat-related endpoints:
- GET /public-chats - List all public chats
- POST /public-chats - Create a new public chat
- GET /public-chats/{chat_id} - Get a specific public chat
- PUT /public-chats/{chat_id} - Update a public chat
- DELETE /public-chats/{chat_id} - Delete a public chat
- POST /public-chats/{chat_id}/sources/url - Add URL source to public chat
- POST /public-chats/{chat_id}/sources/html - Add HTML source to public chat
- POST /public-chats/{chat_id}/sources/file - Add file source to public chat
"""

import logging
import os
import httpx
import aiofiles
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from bson import ObjectId
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.api.company_admin.shared import (
    get_admin_or_user_company_id,
    check_teamlid_permission
)
from app.api.rag import rag_index_files
from app.models.company_admin_schema import (
    PublicChatCreate,
    PublicChatUpdate,
    PublicChatOut
)
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)
router = APIRouter()

UPLOAD_ROOT = "/app/uploads"
PUBLIC_CHAT_DIR = os.path.join(UPLOAD_ROOT, "public_chats")
SOURCES_DIR = os.path.join(UPLOAD_ROOT, "sources")

# Ensure directories exist
os.makedirs(PUBLIC_CHAT_DIR, exist_ok=True)
os.makedirs(SOURCES_DIR, exist_ok=True)


async def extract_html_from_url(url: str) -> str:
    """Extract HTML content from a URL."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch URL {url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error fetching URL {url}: {e}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


def get_public_chat_index_id(company_id: str, admin_id: str, chat_name: str) -> str:
    """Generate index_id for public chat RAG indexing."""
    return f"publicchat/{company_id}/{admin_id}/{chat_name}"


@router.get("/public-chats")
async def list_public_chats(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """List all public chats for the company admin."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        chats = await db.public_chats.find({
            "company_id": company_id,
            "admin_id": admin_id
        }).sort("created_at", -1).to_list(length=None)
        
        result = []
        for chat in chats:
            result.append({
                "id": str(chat["_id"]),
                "chat_name": chat.get("chat_name", ""),
                "password": chat.get("password"),  # Include password for admin
                "is_private": chat.get("is_private", False),
                "created_at": chat.get("created_at", datetime.utcnow()).isoformat(),
                "updated_at": chat.get("updated_at", datetime.utcnow()).isoformat(),
            })
        
        return {"success": True, "chats": result}
    except Exception as e:
        logger.exception("Failed to list public chats")
        raise HTTPException(status_code=500, detail=f"Failed to list public chats: {str(e)}")


@router.post("/public-chats")
async def create_public_chat(
    payload: PublicChatCreate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Create a new public chat."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    # Validate: if password is set, is_private must be True
    if payload.password and not payload.is_private:
        raise HTTPException(
            status_code=400,
            detail="Password can only be set for private chats (is_private must be True)"
        )
    
    # Check if chat name already exists
    existing = await db.public_chats.find_one({
        "company_id": company_id,
        "admin_id": admin_id,
        "chat_name": payload.chat_name
    })
    
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Public chat with name '{payload.chat_name}' already exists"
        )
    
    try:
        # Store password in plain text for admin viewing, hash for verification
        password_plain = None
        password_hash = None
        if payload.password and payload.is_private:
            password_plain = payload.password  # Store plain text for admin viewing
            import hashlib
            password_hash = hashlib.sha256(payload.password.encode()).hexdigest()  # Hash for verification
        
        chat_doc = {
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_name": payload.chat_name,
            "password": password_plain,  # Store plain text for admin viewing
            "password_hash": password_hash,  # Store hash for verification
            "is_private": payload.is_private,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        result = await db.public_chats.insert_one(chat_doc)
        chat_id = str(result.inserted_id)
        
        logger.info(f"Created public chat: {chat_id} for company_id={company_id}, admin_id={admin_id}")
        
        return {
            "success": True,
            "chat_id": chat_id,
            "message": "Public chat created successfully"
        }
    except Exception as e:
        logger.exception("Failed to create public chat")
        raise HTTPException(status_code=500, detail=f"Failed to create public chat: {str(e)}")


@router.get("/public-chats/{chat_id}")
async def get_public_chat(
    chat_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Get a specific public chat."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        return {
            "success": True,
            "chat": {
                "id": str(chat["_id"]),
                "chat_name": chat.get("chat_name", ""),
                "password": chat.get("password"),
                "is_private": chat.get("is_private", False),
                "created_at": chat.get("created_at", datetime.utcnow()).isoformat(),
                "updated_at": chat.get("updated_at", datetime.utcnow()).isoformat(),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get public chat")
        raise HTTPException(status_code=500, detail=f"Failed to get public chat: {str(e)}")


@router.put("/public-chats/{chat_id}")
async def update_public_chat(
    chat_id: str,
    payload: PublicChatUpdate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Update a public chat."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        update_data = {"updated_at": datetime.utcnow()}
        
        if payload.chat_name is not None:
            # Check if new name conflicts with existing chat
            if payload.chat_name != chat.get("chat_name"):
                existing = await db.public_chats.find_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "chat_name": payload.chat_name,
                    "_id": {"$ne": ObjectId(chat_id)}
                })
                if existing:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Public chat with name '{payload.chat_name}' already exists"
                    )
            update_data["chat_name"] = payload.chat_name
        
        # Handle is_private update
        if payload.is_private is not None:
            update_data["is_private"] = payload.is_private
            # If setting to not private, remove password
            if not payload.is_private:
                update_data["password"] = None
                update_data["password_hash"] = None
        
        # Handle password update (can be set, changed, or removed)
        if payload.password is not None:
            # Determine if chat should be private
            will_be_private = payload.is_private if payload.is_private is not None else chat.get("is_private", False)
            
            if not will_be_private and payload.password and payload.password.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Password can only be set for private chats"
                )
            
            # If password is empty string or None, remove it
            password_trimmed = payload.password.strip() if payload.password else ""
            if not password_trimmed:
                update_data["password"] = None
                update_data["password_hash"] = None
            else:
                # Store both plain text and hash - ensure both are always set together
                import hashlib
                update_data["password"] = password_trimmed  # Store plain text for admin viewing
                update_data["password_hash"] = hashlib.sha256(password_trimmed.encode()).hexdigest()  # Store hash for verification
                # Ensure is_private is True when password is set
                if will_be_private:
                    update_data["is_private"] = True
        
        # Ensure password_hash exists if password exists and chat is private (migration/safety check)
        final_is_private = update_data.get("is_private", chat.get("is_private", False))
        final_password = update_data.get("password", chat.get("password"))
        if final_is_private and final_password and not update_data.get("password_hash"):
            # If password exists but password_hash doesn't, create it
            import hashlib
            update_data["password_hash"] = hashlib.sha256(final_password.encode()).hexdigest()
            logger.info(f"Auto-created password_hash for chat {chat_id} during update")
        
        await db.public_chats.update_one(
            {"_id": ObjectId(chat_id)},
            {"$set": update_data}
        )
        
        logger.info(f"Updated public chat: {chat_id}")
        
        return {
            "success": True,
            "message": "Public chat updated successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update public chat")
        raise HTTPException(status_code=500, detail=f"Failed to update public chat: {str(e)}")


@router.delete("/public-chats/{chat_id}")
async def delete_public_chat(
    chat_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete a public chat and all its sources."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        chat_name = chat.get("chat_name")
        
        # Delete all sources for this chat
        await db.public_chat_sources.delete_many({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id
        })
        
        # Delete chat
        await db.public_chats.delete_one({"_id": ObjectId(chat_id)})
        
        logger.info(f"Deleted public chat: {chat_id} ({chat_name})")
        
        return {
            "success": True,
            "message": "Public chat deleted successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete public chat")
        raise HTTPException(status_code=500, detail=f"Failed to delete public chat: {str(e)}")


@router.post("/public-chats/{chat_id}/sources/url")
async def add_url_source_to_chat(
    chat_id: str,
    url: str = Form(...),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Add a URL source to a public chat and index it."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Verify chat exists
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        chat_name = chat.get("chat_name")
        
        # Extract HTML from URL
        html_content = await extract_html_from_url(url)
        
        # Save HTML file
        save_dir = os.path.join(SOURCES_DIR, company_id, admin_id, "public_chat", chat_id)
        os.makedirs(save_dir, exist_ok=True)
        
        # Generate filename from URL
        from urllib.parse import urlparse
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.replace("www.", "")
        file_name = f"{domain}.html"
        file_path = os.path.join(save_dir, file_name)
        
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(html_content)
        
        # Create source record
        source_doc = {
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "type": "url",
            "url": url,
            "file_path": file_path,
            "file_name": file_name,
            "status": "active",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        result = await db.public_chat_sources.insert_one(source_doc)
        source_id = str(result.inserted_id)
        
        # Index to RAG with publicchat index
        # For publicchat, file_id format should be: {index_id}--{filename}
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)
        try:
            # For publicchat, we use index_id as the prefix for file_id
            # The rag_index_files function will generate {admin_id}--{filename}
            # But we need {index_id}--{filename}, so we'll handle this in the query
            # by constructing file_ids with the index_id prefix
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=index_id
            )
            logger.info(f"Successfully indexed URL source for public chat: {url}")
        except Exception as e:
            logger.error(f"Failed to index URL source to RAG: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to index source: {str(e)}")
        
        return {
            "success": True,
            "source_id": source_id,
            "message": "URL source added and indexed successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add URL source to public chat")
        raise HTTPException(status_code=500, detail=f"Failed to add URL source: {str(e)}")


@router.post("/public-chats/{chat_id}/sources/html")
async def add_html_source_to_chat(
    chat_id: str,
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Add an HTML file source to a public chat and index it."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Verify chat exists
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        chat_name = chat.get("chat_name")
        
        # Validate file type
        if not file.filename.endswith((".html", ".htm")):
            raise HTTPException(status_code=400, detail="Only HTML files are allowed")
        
        # Save file
        file_name = file.filename
        save_dir = os.path.join(SOURCES_DIR, company_id, admin_id, "public_chat", chat_id)
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, file_name)
        
        # Check if file already exists
        if os.path.exists(file_path):
            raise HTTPException(status_code=409, detail=f"File '{file_name}' already exists")
        
        # Read and save file content
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
        
        # Create source record
        source_doc = {
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "type": "html",
            "url": None,
            "file_path": file_path,
            "file_name": file_name,
            "status": "active",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        result = await db.public_chat_sources.insert_one(source_doc)
        source_id = str(result.inserted_id)
        
        # Index to RAG with publicchat index
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=index_id
            )
            logger.info(f"Successfully indexed HTML source for public chat: {file_name}")
        except Exception as e:
            logger.error(f"Failed to index HTML source to RAG: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to index source: {str(e)}")
        
        return {
            "success": True,
            "source_id": source_id,
            "message": "HTML source added and indexed successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add HTML source to public chat")
        raise HTTPException(status_code=500, detail=f"Failed to add HTML source: {str(e)}")


@router.post("/public-chats/{chat_id}/sources/file")
async def add_file_source_to_chat(
    chat_id: str,
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Add a document file source to a public chat and index it."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Verify chat exists
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        chat_name = chat.get("chat_name")
        
        # Save file
        file_name = file.filename
        save_dir = os.path.join(SOURCES_DIR, company_id, admin_id, "public_chat", chat_id, "files")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, file_name)
        
        # Check if file already exists
        if os.path.exists(file_path):
            raise HTTPException(status_code=409, detail=f"File '{file_name}' already exists")
        
        # Read and save file content
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
        
        # Create source record
        source_doc = {
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "type": "file",
            "url": None,
            "file_path": file_path,
            "file_name": file_name,
            "status": "active",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        result = await db.public_chat_sources.insert_one(source_doc)
        source_id = str(result.inserted_id)
        
        # Index to RAG with publicchat index
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=index_id
            )
            logger.info(f"Successfully indexed file source for public chat: {file_name}")
        except Exception as e:
            logger.error(f"Failed to index file source to RAG: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to index source: {str(e)}")
        
        return {
            "success": True,
            "source_id": source_id,
            "message": "File source added and indexed successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add file source to public chat")
        raise HTTPException(status_code=500, detail=f"Failed to add file source: {str(e)}")


@router.get("/public-chats/{chat_id}/sources")
async def list_chat_sources(
    chat_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """List all sources for a public chat."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Verify chat exists
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        sources = await db.public_chat_sources.find({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id
        }).sort("created_at", -1).to_list(length=None)
        
        result = []
        for source in sources:
            result.append({
                "id": str(source["_id"]),
                "type": source.get("type", ""),
                "url": source.get("url"),
                "file_name": source.get("file_name", ""),
                "status": source.get("status", "active"),
                "created_at": source.get("created_at", datetime.utcnow()).isoformat(),
            })
        
        return {"success": True, "sources": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to list chat sources")
        raise HTTPException(status_code=500, detail=f"Failed to list chat sources: {str(e)}")


@router.delete("/public-chats/{chat_id}/sources/{source_id}")
async def delete_chat_source(
    chat_id: str,
    source_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete a source from a public chat."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Verify chat exists
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")
        
        # Get source
        source = await db.public_chat_sources.find_one({
            "_id": ObjectId(source_id),
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id
        })
        
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")
        
        # Delete file if exists
        file_path = source.get("file_path")
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Failed to delete file {file_path}: {e}")
        
        # Delete source record
        await db.public_chat_sources.delete_one({"_id": ObjectId(source_id)})
        
        logger.info(f"Deleted source {source_id} from public chat {chat_id}")
        
        return {
            "success": True,
            "message": "Source deleted successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete chat source")
        raise HTTPException(status_code=500, detail=f"Failed to delete chat source: {str(e)}")

