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
import re
import shutil
import asyncio
import hashlib
import httpx
import aiofiles
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from bson import ObjectId
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.repositories.limits_repo import LimitsRepository
from app.api.company_admin.shared import (
    get_admin_or_user_company_id,
    check_teamlid_permission,
    resolve_restricted_public_chat_ids,
    require_public_chat_access_for_teamlid,
    assert_workspace_owner_for_public_chat_mutation,
)
from app.api.rag import rag_index_files, rag_remove_indexed_files, build_rag_file_id
from app.services.rag_lifecycle import migrate_publicchat_index_on_rename, purge_publicchat_index
from app.utils.html_clean_for_rag import clean_html_for_rag_indexing
from app.services.multi_index_answer_merge import answer_matches_documents_no_information_disclaimer
from app.models.company_admin_schema import (
    PublicChatCreate,
    PublicChatUpdate,
    PublicChatOut
)
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)
# Docker/uvicorn often attach handlers only to "uvicorn"; use this for ops-visible INFO lines.
_uvicorn_log = logging.getLogger("uvicorn")
router = APIRouter()

UPLOAD_ROOT = "/app/uploads"
PUBLIC_CHAT_DIR = os.path.join(UPLOAD_ROOT, "public_chats")
SOURCES_DIR = os.path.join(UPLOAD_ROOT, "sources")


async def _get_public_chat_sync_schedule(db, company_id: str, admin_id: str):
    """Get public chat URL sync schedule for company+admin. Initializes if not set."""
    try:
        from app.core.config import PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES
    except ImportError:
        PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES = 60
    coll = db.public_chat_url_sync_schedules
    doc = await coll.find_one({"company_id": company_id, "admin_id": admin_id})
    interval = doc.get("interval_minutes", PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES) if doc else PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES
    next_at = doc.get("next_sync_at") if doc else None
    if next_at is None:
        next_at = datetime.now(timezone.utc) + timedelta(minutes=interval)
        await coll.update_one(
            {"company_id": company_id, "admin_id": admin_id},
            {"$set": {"next_sync_at": next_at, "interval_minutes": interval, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    elif next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=timezone.utc)
    return {"next_sync_at": next_at, "interval_minutes": interval}


async def _update_public_chat_sync_schedule(db, company_id: str, admin_id: str):
    """Set next_sync_at = now + interval after a sync."""
    try:
        from app.core.config import PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES
    except ImportError:
        PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES = 60
    coll = db.public_chat_url_sync_schedules
    doc = await coll.find_one({"company_id": company_id, "admin_id": admin_id})
    interval = doc.get("interval_minutes", PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES) if doc else PUBLIC_CHAT_URL_SYNC_INTERVAL_MINUTES
    next_at = datetime.now(timezone.utc) + timedelta(minutes=interval)
    await coll.update_one(
        {"company_id": company_id, "admin_id": admin_id},
        {"$set": {"next_sync_at": next_at, "interval_minutes": interval, "last_sync_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


# Ensure directories exist
os.makedirs(PUBLIC_CHAT_DIR, exist_ok=True)
os.makedirs(SOURCES_DIR, exist_ok=True)


async def extract_html_from_url(url: str) -> str:
    """Extract HTML content from a URL with retry logic and improved error handling."""
    # Add proper headers to avoid 403 Forbidden errors and make requests look more like a real browser
    # Try without compression first - some sites are more lenient without Accept-Encoding
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
        # Removed Accept-Encoding to avoid compression issues and some bot detection
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "DNT": "1"  # Do Not Track
    }
    
    max_retries = 3
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            # Increase timeout for slow websites
            timeout = httpx.Timeout(60.0, connect=10.0)  # 60s total, 10s connect
            
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
                verify=True  # SSL verification
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                
                # Handle content encoding - httpx should auto-decompress, but ensure we get text
                try:
                    return response.text
                except UnicodeDecodeError:
                    # If text decoding fails, the response might be binary
                    # Try to decode as UTF-8
                    return response.content.decode('utf-8', errors='ignore')
                
        except httpx.TimeoutException as e:
            if attempt < max_retries - 1:
                logger.warning(f"Timeout fetching URL {url} (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff
                continue
            else:
                logger.error(f"Timeout fetching URL {url} after {max_retries} attempts: {e}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Timeout when fetching URL. The website took too long to respond. Please try again or check if the URL is accessible."
                )
        except httpx.ConnectError as e:
            if attempt < max_retries - 1:
                logger.warning(f"Connection error fetching URL {url} (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5
                continue
            else:
                logger.error(f"Connection error fetching URL {url} after {max_retries} attempts: {e}")
                raise HTTPException(
                    status_code=400,
                    detail=f"Connection error when fetching URL. The server disconnected without sending a response. Please check if the URL is correct and accessible."
                )
        except httpx.HTTPStatusError as e:
            # Try to get response text, but handle cases where it might be compressed or binary
            try:
                response_preview = e.response.text[:200] if e.response.text else "No response body"
            except (UnicodeDecodeError, AttributeError):
                response_preview = f"Binary/compressed response ({len(e.response.content)} bytes)"
            
            logger.error(f"Failed to fetch URL {url}: HTTP {e.response.status_code} - {response_preview}")
            
            if e.response.status_code == 403:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Access denied (403 Forbidden) when fetching URL. The website may be blocking automated requests. Please try accessing the URL manually in a browser first, or contact the website administrator."
                )
            elif e.response.status_code == 404:
                raise HTTPException(
                    status_code=400,
                    detail=f"URL not found (404). Please check if the URL is correct and accessible."
                )
            raise HTTPException(status_code=400, detail=f"Failed to fetch URL: HTTP {e.response.status_code}")
        except httpx.HTTPError as e:
            if attempt < max_retries - 1:
                logger.warning(f"HTTP error fetching URL {url} (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5
                continue
            else:
                logger.error(f"Failed to fetch URL {url}: {e}")
                raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error fetching URL {url}: {e}")
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    
    # Should never reach here, but just in case
    raise HTTPException(status_code=500, detail="Failed to fetch URL after all retry attempts")


def _file_name_for_public_chat_url(url: str) -> str:
    """
    Stable filename unique per URL for storage and RAG ``file_id`` (basename of the saved file).

    Includes host, path and query-derived segments so distinct pages on one host map to distinct files.
    """
    from urllib.parse import urlparse

    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        u = f"https://{u}"

    parsed = urlparse(u)
    host = (parsed.netloc or "source").replace("www.", "")
    path = (parsed.path or "").strip("/").replace("/", "_")
    qs = parsed.query.replace("&", "_").replace("=", "_").replace("?", "_")
    parts = [p for p in (host, path, qs) if p]
    base = "_".join(parts) if parts else host
    url_safe = base
    max_len = 220
    if len(url_safe) > max_len:
        digest = hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
        url_safe = f"{digest}_{url_safe[: max_len - len(digest) - 1]}"
    # Windows-safe-ish; Mongo paths are POSIX in containers
    for c in ':\\*?"<>|\n\r':
        url_safe = url_safe.replace(c, "_")
    return f"{url_safe}.html"


_OPENSEARCH_INDEX_NAME_CHARS = "\\/*?\"<>|, "
_PUBLIC_CHAT_INDEX_CHAT_TRANS = str.maketrans(_OPENSEARCH_INDEX_NAME_CHARS, "-" * len(_OPENSEARCH_INDEX_NAME_CHARS))


def _sanitize_chat_name_for_index_id(chat_name: str) -> str:
    """OpenSearch rejects index names containing spaces, commas, quotes, slashes, etc."""
    raw = (chat_name or "").strip().lower()
    if not raw:
        return "chat"
    cleaned = raw.translate(_PUBLIC_CHAT_INDEX_CHAT_TRANS)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "chat"


def get_public_chat_index_id(company_id: str, admin_id: str, chat_name: str) -> str:
    """Generate index_id for public chat RAG indexing.

    OpenSearch/Elasticsearch: index names must be lowercase and must not contain
    ``\", /, *, ?, <, >, |, comma, space`` (and related). Display names like
    ``test public`` or ``Wetboek`` are normalized for the index segment only.
    Format: publicchat-{company_id}-{admin_id}-{sanitized_chat_name}
    """
    safe_chat = _sanitize_chat_name_for_index_id(chat_name)
    return f"publicchat-{company_id}-{admin_id}-{safe_chat}".lower()


async def _reindex_public_chat_sources_after_chat_rename(
    db: AsyncIOMotorDatabase,
    *,
    company_id: str,
    admin_id: str,
    chat_id: str,
    new_chat_name: str,
) -> None:
    """
    After ``chat_name`` changes, public query uses a new OpenSearch ``index_id``.
    Re-index every disk-backed source under the new id so Q&A keeps working.
    """
    index_id_new = get_public_chat_index_id(company_id, admin_id, new_chat_name)
    sources = await db.public_chat_sources.find(
        {
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "status": "active",
        }
    ).to_list(length=None)

    for source in sources or []:
        fp = source.get("file_path")
        if not fp or not os.path.exists(fp):
            _uvicorn_log.warning(
                "Skip re-index after chat rename — missing file: chat_id=%s source=%s path=%s",
                chat_id,
                source.get("_id"),
                fp,
            )
            continue

        disk_bn = os.path.basename(fp) or (source.get("file_name") or "source").strip() or "source"

        url = source.get("url")
        title = (
            source.get("title")
            or source.get("file_name")
            or (url if url else disk_bn)
        )
        file_metadata = [{"url": url, "title": title}]

        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[fp],
                company_id=company_id,
                is_role_based=False,
                index_id=index_id_new,
                file_metadata=file_metadata,
            )
            await db.public_chat_sources.update_one(
                {"_id": source["_id"]},
                {
                    "$set": {
                        "chat_name": new_chat_name,
                        "rag_logical_file_name": disk_bn,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            logger.info(
                "Re-indexed source %s for public chat rename → %s",
                source.get("_id"),
                new_chat_name,
            )
        except Exception as e:
            logger.error(
                "Failed re-index after rename for source %s: %s",
                source.get("_id"),
                e,
            )


@router.get("/public-chats")
async def list_public_chats(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """List all public chats for the company admin."""
    await check_teamlid_permission(admin_context, db, "publicchat", require_write=False)
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        chat_filter = {"company_id": company_id, "admin_id": admin_id}
        scope = await resolve_restricted_public_chat_ids(db, admin_context)
        if scope is not None:
            if not scope:
                return {"success": True, "chats": []}
            oids = [ObjectId(cid) for cid in scope if ObjectId.is_valid(cid)]
            if not oids:
                return {"success": True, "chats": []}
            chat_filter["_id"] = {"$in": oids}

        chats = await db.public_chats.find(chat_filter).sort("created_at", -1).to_list(length=None)

        limits_repo = LimitsRepository(db)
        max_public_chats = await limits_repo.get_effective_public_chats_limit(company_id)
        
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
        
        return {
            "success": True,
            "chats": result,
            "max_public_chats": max_public_chats,
            "can_create_more": len(result) < max_public_chats,
        }
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
    await check_teamlid_permission(admin_context, db, "publicchat")
    assert_workspace_owner_for_public_chat_mutation(admin_context)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    limits_repo = LimitsRepository(db)
    allowed, limit_msg = await limits_repo.check_public_chats_limit(company_id, admin_id)
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=limit_msg,
            headers={"X-Public-Chat-Limit": "reached"},
        )
    
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
    await check_teamlid_permission(admin_context, db, "publicchat", require_write=False)
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)

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


@router.get("/public-chats/{chat_id}/query-history")
async def get_public_chat_query_history(
    chat_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """
    List stored public-chat questions for this chat, grouped into with_answer / without_answer.
    Read access is enough (same module visibility as viewing chats).
    """
    await check_teamlid_permission(admin_context, db, "publicchat", require_write=False)
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id,
        })
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")

        cursor = (
            db.public_chat_query_history.find(
                {"company_id": company_id, "admin_id": admin_id, "chat_id": chat_id}
            )
            .sort("created_at", -1)
            .limit(500)
        )
        docs = await cursor.to_list(length=500)

        def serialize(doc):
            dt = doc.get("created_at")
            return {
                "id": str(doc["_id"]),
                "question": doc.get("question", ""),
                "answer": doc.get("answer"),
                "has_answer": doc.get("has_answer", False),
                "error_detail": doc.get("error_detail"),
                "linked_source_count": doc.get("linked_source_count"),
                "created_at": dt.isoformat() if hasattr(dt, "isoformat") else None,
            }

        with_answer = []
        without_answer = []
        for doc in docs:
            item = serialize(doc)
            lc = doc.get("linked_source_count")
            downgrade_disclaimer = (
                bool(doc.get("has_answer"))
                and answer_matches_documents_no_information_disclaimer(doc.get("answer"))
            )
            downgrade_no_linked = bool(doc.get("has_answer")) and lc is not None and int(lc) == 0
            if downgrade_disclaimer or downgrade_no_linked:
                item["has_answer"] = False
                if not item.get("error_detail"):
                    item["error_detail"] = (
                        "Geen relevante informatie in de aangeleverde documenten"
                        if downgrade_disclaimer
                        else "Geen gekoppelde bron voor dit antwoord"
                    )
                without_answer.append(item)
            elif doc.get("has_answer"):
                with_answer.append(item)
            else:
                without_answer.append(item)

        return {
            "success": True,
            "chat_id": chat_id,
            "chat_name": chat.get("chat_name", ""),
            "counts": {"with_answer": len(with_answer), "without_answer": len(without_answer)},
            "with_answer": with_answer,
            "without_answer": without_answer,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to load public chat query history")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load query history: {str(e)}",
        )


@router.put("/public-chats/{chat_id}")
async def update_public_chat(
    chat_id: str,
    payload: PublicChatUpdate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Update a public chat."""
    await check_teamlid_permission(admin_context, db, "publicchat")
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)

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

        old_chat_name = chat.get("chat_name") or ""
        
        update_data = {"updated_at": datetime.utcnow()}
        renamed_for_rag = False
        
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
                renamed_for_rag = True
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

        if renamed_for_rag:
            await migrate_publicchat_index_on_rename(
                db,
                company_id=company_id,
                admin_id=admin_id,
                chat_id=chat_id,
                old_chat_name=old_chat_name,
                new_chat_name=update_data["chat_name"],
            )
            await db.public_chat_sources.update_many(
                {
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "chat_id": chat_id,
                },
                {
                    "$set": {
                        "chat_name": update_data["chat_name"],
                        "updated_at": datetime.utcnow(),
                    }
                },
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
    await check_teamlid_permission(admin_context, db, "publicchat")
    assert_workspace_owner_for_public_chat_mutation(admin_context)

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

        if chat_name:
            try:
                await purge_publicchat_index(company_id, admin_id, chat_name)
            except Exception as e:
                logger.warning(f"RAG purge failed for public chat {chat_id}: {e}")

        sources = await db.public_chat_sources.find({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
        }).to_list(length=None)
        for source in sources:
            file_path = source.get("file_path")
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    logger.warning(f"Failed to delete file {file_path}: {e}")

        chat_sources_dir = os.path.join(SOURCES_DIR, company_id, admin_id, "public_chat", chat_id)
        if os.path.isdir(chat_sources_dir):
            try:
                shutil.rmtree(chat_sources_dir)
            except Exception as e:
                logger.warning(f"Failed to delete public chat sources dir {chat_sources_dir}: {e}")

        await db.public_chat_query_history.delete_many({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
        })
        
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
    await check_teamlid_permission(admin_context, db, "publicchat")
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)

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
        
        # Extract HTML from URL and strip noisy markup so RAG chunks resemble PDF-ish text density
        html_content = await extract_html_from_url(url)
        html_content = clean_html_for_rag_indexing(html_content, page_url=url, plain_text_only=True)
        
        # Save HTML file
        save_dir = os.path.join(SOURCES_DIR, company_id, admin_id, "public_chat", chat_id)
        os.makedirs(save_dir, exist_ok=True)
        
        # Unique per full URL (same host, different paths must not share one file_id)
        file_name = _file_name_for_public_chat_url(url)
        file_path = os.path.join(save_dir, file_name)
        
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(html_content)

        _uvicorn_log.info(
            "Saved cleaned plain HTML for URL source (%s chars): %s -> %s",
            len(html_content),
            url,
            file_path,
        )
        
        # Create source record
        now_utc = datetime.utcnow()
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
            "created_at": now_utc,
            "updated_at": now_utc,
            "last_updated": now_utc,
        }
        
        result = await db.public_chat_sources.insert_one(source_doc)
        source_oid = result.inserted_id
        source_id = str(source_oid)
        
        # Index to RAG with publicchat index and metadata
        # file_id: {index_id}--{basename(file_path)} (see app.api.rag.build_rag_file_ids)
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)
        file_metadata = [{
            "url": url,
            "title": source_doc.get("title", url)  # Use title if available, otherwise use URL
        }]
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=index_id,
                file_metadata=file_metadata,
            )
            await db.public_chat_sources.update_one(
                {"_id": source_oid},
                {
                    "$set": {
                        "rag_logical_file_name": file_name,
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            logger.info(f"Successfully indexed URL source for public chat: {url}")
        except Exception as e:
            logger.error(f"Failed to index URL source to RAG: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to index source: {str(e)}")
        
        return {
            "success": True,
            "source_id": source_id,
            "message": "URL source added and indexed successfully",
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
    await check_teamlid_permission(admin_context, db, "publicchat")
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)
    
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
        source_oid = result.inserted_id
        source_id = str(source_oid)
        
        # Index to RAG with publicchat index and metadata
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)
        file_metadata = [{
            "url": source_doc.get("url"),  # May be None for HTML files
            "title": source_doc.get("title", file_name)  # Use title if available, otherwise use file_name
        }]
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=index_id,
                file_metadata=file_metadata,
            )
            await db.public_chat_sources.update_one(
                {"_id": source_oid},
                {"$set": {"rag_logical_file_name": file_name, "updated_at": datetime.utcnow()}},
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
    await check_teamlid_permission(admin_context, db, "publicchat")
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)
    
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
        source_oid = result.inserted_id
        source_id = str(source_oid)
        
        # Index to RAG with publicchat index
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)
        file_metadata = [{
            "url": None,  # File sources don't have URLs
            "title": source_doc.get("title", file_name)  # Use title if available, otherwise use file_name
        }]
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=index_id,
                file_metadata=file_metadata,
            )
            await db.public_chat_sources.update_one(
                {"_id": source_oid},
                {"$set": {"rag_logical_file_name": file_name, "updated_at": datetime.utcnow()}},
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
    await check_teamlid_permission(admin_context, db, "publicchat", require_write=False)
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)
    
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
            item = {
                "id": str(source["_id"]),
                "type": source.get("type", ""),
                "url": source.get("url"),
                "file_name": source.get("file_name", ""),
                "status": source.get("status", "active"),
                "created_at": source.get("created_at", datetime.utcnow()).isoformat(),
            }
            last_upd = source.get("last_updated")
            if last_upd:
                item["last_updated"] = last_upd.isoformat() if isinstance(last_upd, datetime) else last_upd
            result.append(item)
        
        # last_sync, next_sync from URL sources (for periodic sync display)
        last_sync = None
        url_sources = [s for s in sources if s.get("type") == "url" and s.get("status") == "active"]
        if url_sources:
            times = []
            for s in url_sources:
                t = s.get("last_updated") or s.get("created_at") or s.get("updated_at")
                if t:
                    times.append(t if isinstance(t, datetime) else datetime.fromisoformat(str(t).replace("Z", "+00:00")))
            if times:
                last_sync = max(times)
        
        next_sync = None
        if url_sources:
            schedule = await _get_public_chat_sync_schedule(db, company_id, admin_id)
            next_at = schedule.get("next_sync_at")
            next_sync = next_at.isoformat() if next_at else None
        if next_sync is None:
            now = datetime.utcnow()
            next_sync_dt = now.replace(hour=2, minute=10, second=0, microsecond=0)
            if next_sync_dt < now:
                next_sync_dt += timedelta(days=1)
            next_sync = next_sync_dt.isoformat()
        
        return {
            "success": True,
            "sources": result,
            "last_sync": last_sync.isoformat() if last_sync else None,
            "next_sync": next_sync,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to list chat sources")
        raise HTTPException(status_code=500, detail=f"Failed to list chat sources: {str(e)}")


@router.get("/public-chats/sources/sync-schedule")
async def get_public_chat_sync_schedule(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
):
    """Get Public Chat URL auto-sync schedule (next sync time and interval)."""
    await check_teamlid_permission(admin_context, db, "publicchat", require_write=False)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    schedule = await _get_public_chat_sync_schedule(db, company_id, admin_id)
    next_at = schedule["next_sync_at"]
    return {
        "next_sync_at": next_at.isoformat() if next_at else None,
        "interval_minutes": schedule["interval_minutes"],
    }


@router.post("/public-chats/sources/sync-all")
async def sync_all_chat_url_sources(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Re-fetch URL sources for all public chats of this company, update HTML files, and re-index to RAG."""
    await check_teamlid_permission(admin_context, db, "publicchat")

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        chats = await db.public_chats.find({
            "company_id": company_id,
            "admin_id": admin_id,
        }).to_list(length=None)

        scope_ids = await resolve_restricted_public_chat_ids(db, admin_context)

        total_synced = 0
        total_failed = 0

        for chat in chats:
            chat_id = str(chat["_id"])
            if scope_ids is not None and chat_id not in scope_ids:
                continue
            chat_name = chat.get("chat_name", "")
            index_id = get_public_chat_index_id(company_id, admin_id, chat_name)

            url_sources = await db.public_chat_sources.find({
                "company_id": company_id,
                "admin_id": admin_id,
                "chat_id": chat_id,
                "type": "url",
                "status": "active",
            }).to_list(length=None)

            for source in url_sources:
                try:
                    rag_logical = None
                    url = source.get("url")
                    if not url:
                        continue

                    html_content = await extract_html_from_url(url)
                    html_content = clean_html_for_rag_indexing(html_content, page_url=url, plain_text_only=True)
                    file_path = source.get("file_path")
                    if file_path:
                        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                            await f.write(html_content)
                        _uvicorn_log.info(
                            "Saved cleaned plain HTML for URL sync (%s chars): %s -> %s",
                            len(html_content),
                            url,
                            file_path,
                        )

                    if file_path and os.path.exists(file_path):
                        rag_logical = os.path.basename(file_path)
                        await rag_index_files(
                            user_id=admin_id,
                            file_paths=[file_path],
                            company_id=company_id,
                            is_role_based=False,
                            index_id=index_id,
                            file_metadata=[{"url": url, "title": source.get("title", url)}],
                        )

                    now_utc = datetime.utcnow()
                    await db.public_chat_sources.update_one(
                        {"_id": source["_id"]},
                        {
                            "$set": {
                                "last_updated": now_utc,
                                "updated_at": now_utc,
                                **({"rag_logical_file_name": rag_logical} if rag_logical is not None else {}),
                            }
                        },
                    )
                    total_synced += 1

                except Exception as e:
                    logger.error(f"Failed to sync URL source {source.get('url')}: {e}")
                    await db.public_chat_sources.update_one(
                        {"_id": source["_id"]},
                        {"$set": {"updated_at": datetime.utcnow()}},
                    )
                    total_failed += 1

        sync_time = datetime.utcnow()
        await _update_public_chat_sync_schedule(db, company_id, admin_id)
        schedule = await _get_public_chat_sync_schedule(db, company_id, admin_id)
        next_at = schedule["next_sync_at"]

        return {
            "success": True,
            "synced_count": total_synced,
            "failed_count": total_failed,
            "message": f"Synced {total_synced} source(s), {total_failed} failed",
            "last_sync": sync_time.isoformat(),
            "next_sync": next_at.isoformat() if next_at else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to sync all chat sources")
        raise HTTPException(status_code=500, detail=f"Failed to sync chat sources: {str(e)}")


@router.post("/public-chats/{chat_id}/sources/sync")
async def sync_chat_url_sources(
    chat_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Re-fetch URL sources for this chat, update HTML files, and re-index to RAG."""
    await check_teamlid_permission(admin_context, db, "publicchat")
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        chat = await db.public_chats.find_one({
            "_id": ObjectId(chat_id),
            "company_id": company_id,
            "admin_id": admin_id,
        })
        if not chat:
            raise HTTPException(status_code=404, detail="Public chat not found")

        chat_name = chat.get("chat_name", "")
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)

        url_sources = await db.public_chat_sources.find({
            "company_id": company_id,
            "admin_id": admin_id,
            "chat_id": chat_id,
            "type": "url",
            "status": "active",
        }).to_list(length=None)

        synced_count = 0
        failed_count = 0

        for source in url_sources:
            try:
                rag_logical = None
                url = source.get("url")
                if not url:
                    continue

                html_content = await extract_html_from_url(url)
                html_content = clean_html_for_rag_indexing(html_content, page_url=url, plain_text_only=True)
                file_path = source.get("file_path")
                if file_path:
                    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                        await f.write(html_content)
                    _uvicorn_log.info(
                        "Saved cleaned plain HTML for URL sync (%s chars): %s -> %s",
                        len(html_content),
                        url,
                        file_path,
                    )

                if file_path and os.path.exists(file_path):
                    rag_logical = os.path.basename(file_path)
                    await rag_index_files(
                        user_id=admin_id,
                        file_paths=[file_path],
                        company_id=company_id,
                        is_role_based=False,
                        index_id=index_id,
                        file_metadata=[{"url": url, "title": source.get("title", url)}],
                    )

                now_utc = datetime.utcnow()
                await db.public_chat_sources.update_one(
                    {"_id": source["_id"]},
                    {
                        "$set": {
                            "last_updated": now_utc,
                            "updated_at": now_utc,
                            **({"rag_logical_file_name": rag_logical} if rag_logical is not None else {}),
                        }
                    },
                )
                synced_count += 1

            except Exception as e:
                logger.error(f"Failed to sync URL source {source.get('url')}: {e}")
                await db.public_chat_sources.update_one(
                    {"_id": source["_id"]},
                    {"$set": {"updated_at": datetime.utcnow()}},
                )
                failed_count += 1

        sync_time = datetime.utcnow()
        await _update_public_chat_sync_schedule(db, company_id, admin_id)
        schedule = await _get_public_chat_sync_schedule(db, company_id, admin_id)
        next_at = schedule["next_sync_at"]

        return {
            "success": True,
            "synced_count": synced_count,
            "failed_count": failed_count,
            "message": f"Synced {synced_count} source(s), {failed_count} failed",
            "last_sync": sync_time.isoformat(),
            "next_sync": next_at.isoformat() if next_at else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to sync chat sources")
        raise HTTPException(status_code=500, detail=f"Failed to sync chat sources: {str(e)}")


@router.delete("/public-chats/{chat_id}/sources/{source_id}")
async def delete_chat_source(
    chat_id: str,
    source_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete a source from a public chat."""
    await check_teamlid_permission(admin_context, db, "publicchat")
    await require_public_chat_access_for_teamlid(admin_context, db, chat_id)
    
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

        # Remove indexed chunks before deleting DB record / disk file
        chat_name = chat.get("chat_name")
        index_id = get_public_chat_index_id(company_id, admin_id, chat_name)
        logical = source.get("rag_logical_file_name") or source.get("file_name")
        if logical:
            try:
                await rag_remove_indexed_files(index_id, [build_rag_file_id(index_id, logical)])
            except Exception as e:
                logger.warning(f"RAG remove failed for public chat source {source_id}: {e}")

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

