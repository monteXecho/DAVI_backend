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
import httpx
import aiofiles
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
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
    Extract HTML content from a URL.
    
    Args:
        url: URL to extract HTML from
        
    Returns:
        HTML content as string
        
    Raises:
        HTTPException: If URL extraction fails
    """
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


@router.get("/sources")
async def list_sources(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """List all sources (URLs and HTML files) for the company."""
    try:
        await check_teamlid_permission(admin_context, db, "roles_folders")
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
        await check_teamlid_permission(admin_context, db, "roles_folders")
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
        
        # Index to RAG with webchat index_id
        webchat_index_id = f"{company_id}_webchat"
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=webchat_index_id
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
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
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
        
        # Index to RAG with webchat index_id
        webchat_index_id = f"{company_id}_webchat"
        try:
            await rag_index_files(
                user_id=admin_id,
                file_paths=[file_path],
                company_id=company_id,
                is_role_based=False,
                index_id=webchat_index_id
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
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
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
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
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


@router.post("/sources/sync")
async def sync_sources(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Sync all active URL sources by re-extracting HTML and re-indexing.
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    
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
        
        webchat_index_id = f"{company_id}_webchat"
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
                
                # Re-index to RAG
                if file_path and os.path.exists(file_path):
                    await rag_index_files(
                        user_id=admin_id,
                        file_paths=[file_path],
                        company_id=company_id,
                        is_role_based=False,
                        index_id=webchat_index_id
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


