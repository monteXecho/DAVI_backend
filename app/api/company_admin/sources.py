"""
Sources (Bronnen) domain router for company admin API.

Handles all source-related endpoints:
- GET /sources - List all sources (URLs and HTML files)
- POST /sources/url - Add URL source (extract HTML and index)
- POST /sources/html - Upload HTML file and index
- PUT /sources/{source_id} - Update source
- DELETE /sources/{source_id} - Delete source
- POST /sources/sync - Sync all sources
"""

import logging
import os
import asyncio
import httpx
import aiofiles
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from fastapi.responses import FileResponse
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.api.company_admin.shared import (
    get_admin_or_user_company_id,
    check_teamlid_permission
)
from app.api.rag import rag_index_files
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)
router = APIRouter()

# Log router initialization
logger.info("Sources router initialized")

UPLOAD_ROOT = "/app/uploads"
SOURCES_DIR = os.path.join(UPLOAD_ROOT, "sources")

# Ensure sources directory exists
os.makedirs(SOURCES_DIR, exist_ok=True)


@router.get("/sources/health")
async def health_check():
    """Health check endpoint to verify the router is working."""
    return {"status": "ok", "message": "Sources router is working"}


async def extract_html_from_url(url: str) -> str:
    """
    Extract HTML content from a URL with retry logic and improved error handling.
    
    Args:
        url: URL to extract HTML from
        
    Returns:
        HTML content as string
        
    Raises:
        HTTPException: If URL extraction fails
    """
    # Add proper headers to avoid 403 Forbidden errors and make requests look more like a real browser
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0"
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
                # Check if response is already decoded
                if response.headers.get("content-encoding"):
                    # Response might be compressed, but httpx should handle it
                    # If we get binary data, try to decode it
                    try:
                        return response.text
                    except UnicodeDecodeError:
                        # If text decoding fails, the response might be binary
                        # Try to decode as UTF-8
                        return response.content.decode('utf-8', errors='ignore')
                else:
                    return response.text
                
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


@router.get("/sources")
async def list_sources(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """List all sources (URLs and HTML files) for the company."""
    try:
        await check_teamlid_permission(admin_context, db, "webchat", require_write=False)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Permission check failed: {e}")
        raise HTTPException(status_code=403, detail=f"Permission denied: {str(e)}")
    
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    logger.info(f"Listing sources for company_id={company_id}, admin_id={admin_id}")
    
    try:
        # Initialize collection if it doesn't exist (MongoDB will create it automatically)
        sources = await db.webchat_sources.find({
            "company_id": company_id,
            "admin_id": admin_id
        }).sort("created_at", -1).to_list(length=None)
        
        logger.info(f"Found {len(sources)} sources")
        
        # Convert ObjectId to string
        for source in sources:
            if "_id" in source:
                source["id"] = str(source["_id"])
                del source["_id"]
        
        # Calculate last sync time (most recent last_updated from active URL sources)
        last_sync = None
        active_url_sources = [s for s in sources if s.get("type") == "url" and s.get("status") == "active"]
        if active_url_sources:
            last_updated_times = []
            for s in active_url_sources:
                last_upd = s.get("last_updated")
                if last_upd:
                    # Handle both datetime objects and ISO strings
                    if isinstance(last_upd, datetime):
                        last_updated_times.append(last_upd)
                    elif isinstance(last_upd, str):
                        try:
                            last_updated_times.append(datetime.fromisoformat(last_upd.replace('Z', '+00:00')))
                        except:
                            pass
            if last_updated_times:
                last_sync = max(last_updated_times)
        
        # Calculate next sync time (2:10 AM next day)
        next_sync = None
        now = datetime.utcnow()
        next_sync_dt = now.replace(hour=2, minute=10, second=0, microsecond=0)
        if next_sync_dt < now:
            next_sync_dt += timedelta(days=1)
        next_sync = next_sync_dt
        
        return {
            "success": True,
            "sources": sources,
            "last_sync": last_sync.isoformat() if last_sync else None,
            "next_sync": next_sync.isoformat() if next_sync else None
        }
    except Exception as e:
        logger.exception("Failed to list sources")
        raise HTTPException(status_code=500, detail=f"Failed to list sources: {str(e)}")


@router.post("/sources/url")
async def add_url_source(
    url: str = Form(...),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Add a URL source. Extracts HTML from the URL and indexes it to RAG.
    """
    try:
        await check_teamlid_permission(admin_context, db, "webchat")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Permission check failed: {e}")
        raise HTTPException(status_code=403, detail=f"Permission denied: {str(e)}")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    # Validate URL
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    
    try:
        # Extract HTML from URL
        logger.info(f"Extracting HTML from URL: {url}")
        html_content = await extract_html_from_url(url)
        
        # Save HTML to file
        url_safe = url.replace("https://", "").replace("http://", "").replace("/", "_").replace("?", "_").replace("=", "_")
        file_name = f"{url_safe}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.html"
        file_path = os.path.join(SOURCES_DIR, company_id, admin_id, file_name)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(html_content)
        
        # Create source record
        source_doc = {
            "company_id": company_id,
            "admin_id": admin_id,
            "url": url,
            "type": "url",
            "file_path": file_path,
            "file_name": file_name,
            "status": "active",
            "fetch_status": "OK",
            "last_updated": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        result = await db.webchat_sources.insert_one(source_doc)
        source_id = str(result.inserted_id)
        
        # Index to RAG: use same 2-field metadata as PublicChat indexing.
        # Extra keys (e.g. 4 fields) can trigger RAG indexer errors
        # ("dictionary update sequence element #0 has length 4; 2 is required").
        webchat_index_id = f"webchat-{company_id}-{admin_id}"
        display_title = source_doc.get("title") or url
        file_metadata = [{"url": url, "title": display_title}]
        
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=webchat_index_id,
                file_metadata=file_metadata
            )
            logger.info(f"Successfully indexed URL source: {url}")
        except Exception as e:
            logger.error(f"Failed to index URL source to RAG: {e}")
            # Update source status to indicate indexing failure
            await db.webchat_sources.update_one(
                {"_id": result.inserted_id},
                {"$set": {"fetch_status": "FOUT", "updated_at": datetime.utcnow()}}
            )
            raise HTTPException(status_code=500, detail=f"Failed to index source: {str(e)}")
        
        return {
            "success": True,
            "source_id": source_id,
            "message": "URL source added and indexed successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add URL source")
        raise HTTPException(status_code=500, detail=f"Failed to add URL source: {str(e)}")


@router.post("/sources/html")
async def upload_html_source(
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Upload an HTML file and index it to RAG.
    """
    await check_teamlid_permission(admin_context, db, "webchat")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    # Validate file type
    if not file.filename.endswith((".html", ".htm")):
        raise HTTPException(status_code=400, detail="Only HTML files are allowed")
    
    try:
        # Save file
        file_name = file.filename
        save_dir = os.path.join(SOURCES_DIR, company_id, admin_id)
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
            "url": None,
            "type": "html",
            "file_path": file_path,
            "file_name": file_name,
            "status": "active",
            "fetch_status": "OK",
            "last_updated": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        result = await db.webchat_sources.insert_one(source_doc)
        source_id = str(result.inserted_id)
        
        webchat_index_id = f"webchat-{company_id}-{admin_id}"
        html_title = source_doc.get("title") or file_name
        # Empty url string (not null) — same shape as PublicChat file indexing
        file_metadata = [{"url": "", "title": html_title}]
        
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=webchat_index_id,
                file_metadata=file_metadata
            )
            logger.info(f"Successfully indexed HTML source: {file_name}")
        except Exception as e:
            logger.error(f"Failed to index HTML source to RAG: {e}")
            # Update source status to indicate indexing failure
            await db.webchat_sources.update_one(
                {"_id": result.inserted_id},
                {"$set": {"fetch_status": "FOUT", "updated_at": datetime.utcnow()}}
            )
            raise HTTPException(status_code=500, detail=f"Failed to index source: {str(e)}")
        
        return {
            "success": True,
            "source_id": source_id,
            "message": "HTML source uploaded and indexed successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to upload HTML source")
        raise HTTPException(status_code=500, detail=f"Failed to upload HTML source: {str(e)}")


@router.put("/sources/{source_id}")
async def update_source(
    source_id: str,
    status: Optional[str] = Form(None),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Update a source (e.g., change status)."""
    await check_teamlid_permission(admin_context, db, "webchat")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        from bson import ObjectId
        
        update_data = {"updated_at": datetime.utcnow()}
        if status:
            update_data["status"] = status
        
        result = await db.webchat_sources.update_one(
            {
                "_id": ObjectId(source_id),
                "company_id": company_id,
                "admin_id": admin_id
            },
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Source not found")
        
        return {
            "success": True,
            "message": "Source updated successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to update source")
        raise HTTPException(status_code=500, detail=f"Failed to update source: {str(e)}")


@router.delete("/sources/{source_id}")
async def delete_source(
    source_id: str,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete a source."""
    await check_teamlid_permission(admin_context, db, "webchat")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        from bson import ObjectId
        
        # Get source before deleting
        source = await db.webchat_sources.find_one({
            "_id": ObjectId(source_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")
        
        # Delete file if it exists
        file_path = source.get("file_path")
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Failed to delete file {file_path}: {e}")
        
        # Delete source record
        result = await db.webchat_sources.delete_one({
            "_id": ObjectId(source_id),
            "company_id": company_id,
            "admin_id": admin_id
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Source not found")
        
        return {
            "success": True,
            "message": "Source deleted successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete source")
        raise HTTPException(status_code=500, detail=f"Failed to delete source: {str(e)}")


@router.get("/sources/download")
async def download_source_file(
    file_path: str = Query(..., description="Path to the source file"),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Download/view a source file (HTML or URL-extracted HTML).
    Verifies the user has access to the source before serving it.
    No roles_folders check: company users can view sources from their admin's workspace
    (same pattern as documents/download for read-only access).
    """
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Verify the source belongs to this company/admin
        # Check webchat_sources first
        source = await db.webchat_sources.find_one({
            "file_path": file_path,
            "company_id": company_id,
            "admin_id": admin_id
        })
        # If not found, check public_chat_sources (HTML/files for public chats)
        if not source and "public_chat" in file_path:
            source = await db.public_chat_sources.find_one({
                "file_path": file_path,
                "company_id": company_id,
                "admin_id": admin_id
            })
        
        if not source:
            raise HTTPException(status_code=403, detail="You don't have access to this source")
        
        # Check if file exists
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found")
        
        # Return file with appropriate content type
        file_name = source.get("file_name", os.path.basename(file_path))
        return FileResponse(
            path=file_path,
            filename=file_name,
            media_type="text/html"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to download source file")
        raise HTTPException(status_code=500, detail=f"Failed to download source file: {str(e)}")


@router.post("/sources/sync")
async def sync_sources(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Sync all active URL sources by re-extracting HTML and re-indexing.
    """
    await check_teamlid_permission(admin_context, db, "webchat")
    
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Get all active URL sources
        url_sources = await db.webchat_sources.find({
            "company_id": company_id,
            "admin_id": admin_id,
            "type": "url",
            "status": "active"
        }).to_list(length=None)
        
        synced_count = 0
        failed_count = 0
        
        for source in url_sources:
            try:
                url = source.get("url")
                if not url:
                    continue
                
                # Re-extract HTML
                html_content = await extract_html_from_url(url)
                
                # Update file
                file_path = source.get("file_path")
                if file_path:
                    async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                        await f.write(html_content)
                
                # Re-index into the WebChat index (must match file_id prefix used in /webchat/ask)
                if file_path and os.path.exists(file_path):
                    webchat_index_id = f"webchat-{company_id}-{admin_id}"
                    display_title = source.get("title") or url
                    await rag_index_files(
                        user_id=admin_id,
                        file_paths=[file_path],
                        company_id=company_id,
                        is_role_based=False,
                        index_id=webchat_index_id,
                        file_metadata=[{"url": url, "title": display_title}],
                    )
                
                # Update source record
                await db.webchat_sources.update_one(
                    {"_id": source["_id"]},
                    {
                        "$set": {
                            "last_updated": datetime.utcnow(),
                            "fetch_status": "OK",
                            "updated_at": datetime.utcnow()
                        }
                    }
                )
                synced_count += 1
                
            except Exception as e:
                logger.error(f"Failed to sync source {source.get('url')}: {e}")
                # Update source status
                await db.webchat_sources.update_one(
                    {"_id": source["_id"]},
                    {
                        "$set": {
                            "fetch_status": "FOUT",
                            "updated_at": datetime.utcnow()
                        }
                    }
                )
                failed_count += 1
        
        # Get the most recent last_updated time after sync
        sync_time = datetime.utcnow()
        
        # Calculate next sync time (2:10 AM next day)
        next_sync_dt = sync_time.replace(hour=2, minute=10, second=0, microsecond=0)
        if next_sync_dt < sync_time:
            next_sync_dt += timedelta(days=1)
        
        return {
            "success": True,
            "synced_count": synced_count,
            "failed_count": failed_count,
            "message": f"Synced {synced_count} source(s), {failed_count} failed",
            "last_sync": sync_time.isoformat(),
            "next_sync": next_sync_dt.isoformat()
        }
        
    except Exception as e:
        logger.exception("Failed to sync sources")
        raise HTTPException(status_code=500, detail=f"Failed to sync sources: {str(e)}")
