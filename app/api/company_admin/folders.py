"""
Folders domain router for company admin API.

Handles all folder-related endpoints:
- POST /folders - Create folders
- GET /folders - List folders
- POST /folders/delete - Delete folders
- GET /folders/import/list - List importable folders from Nextcloud
- POST /folders/import - Import folders from Nextcloud
- POST /folders/sync - Sync documents from Nextcloud
"""

import logging
import os
import sys
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_admin_schema import AddFoldersPayload, ImportFoldersPayload
from app.models.company_user_schema import DeleteFolderPayload
from app.api.company_admin.shared import (
    get_admin_or_user_company_id,
    check_teamlid_permission
)

# Use uvicorn logger to ensure logs are visible (same as HTTP request logs)
logger = logging.getLogger("uvicorn")
router = APIRouter()


@router.post("/folders")
async def add_folders(
    payload: AddFoldersPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Create folders in DAVI and sync to Nextcloud.
    
    Scenario A1: DAVI → Nextcloud
    - Creates folder records in MongoDB
    - Creates corresponding folders in Nextcloud
    - Stores canonical storage paths for future operations
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        # Get storage provider for Nextcloud sync using Keycloak SSO
        storage_provider = None
        try:
            from app.storage.providers import get_storage_provider, StorageError
            from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH
            
            user_email = admin_context.get("admin_email") or admin_context.get("user_email")
            access_token = admin_context.get("_access_token")
            nextcloud_user_id = admin_context.get("_nextcloud_user_id")
            
            if user_email and access_token:
                storage_provider = get_storage_provider(
                    username=user_email,
                    access_token=access_token,
                    url=NEXTCLOUD_URL,
                    root_path=NEXTCLOUD_ROOT_PATH,
                    user_id_from_token=nextcloud_user_id
                )
            else:
                logger.warning(f"Keycloak token not available for {user_email}, creating folders in DAVI only")
        except StorageError as e:
            logger.warning(f"Storage provider not available, creating folders in DAVI only: {e}")
        except Exception as e:
            logger.warning(f"Storage provider error, creating folders in DAVI only: {e}")

        result = await repo.add_folders(
            company_id=company_id,
            admin_id=admin_id,
            folder_names=payload.folder_names,
            storage_provider=storage_provider
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "added_folders": result["added_folders"],
            "duplicated_folders": result["duplicated_folders"],
            "total_added": result["total_added"],
            "total_duplicates": result["total_duplicates"]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to add folders")
        raise HTTPException(status_code=500, detail=f"Failed to add folders: {str(e)}")


@router.get("/folders")
async def get_folders(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db),
    auto_sync: bool = Query(True, description="Automatically sync documents from Nextcloud")
):
    """
    Get all folders for the admin.
    
    Optionally syncs documents from Nextcloud automatically when folders are accessed.
    This ensures that files uploaded directly to Nextcloud are visible in DAVI.
    """
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        # Auto-sync documents from Nextcloud if enabled (default: True)
        if auto_sync:
            try:
                from app.storage.providers import get_storage_provider, StorageError
                from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH
                
                user_email = admin_context.get("admin_email") or admin_context.get("user_email")
                access_token = admin_context.get("_access_token")
                nextcloud_user_id = admin_context.get("_nextcloud_user_id")
                
                if user_email and access_token:
                    storage_provider = get_storage_provider(
                        username=user_email,
                        access_token=access_token,
                        url=NEXTCLOUD_URL,
                        root_path=NEXTCLOUD_ROOT_PATH,
                        user_id_from_token=nextcloud_user_id
                    )
                    # Trigger sync in background (don't wait for it)
                    try:
                        await repo.sync_documents_from_nextcloud(
                            company_id=company_id,
                            admin_id=admin_id,
                            storage_provider=storage_provider
                        )
                    except Exception as sync_error:
                        # Log but don't fail the request
                        logger.warning(f"Auto-sync failed (non-critical): {sync_error}")
            except (StorageError, Exception) as e:
                # Storage provider not available - not critical, just log
                logger.debug(f"Auto-sync skipped (storage provider not available): {e}")

        result = await repo.get_folders(
            company_id=company_id,
            admin_id=admin_id,
        )

        return {
            "success": result["success"],
            "folders": result["folders"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get folders")
        raise HTTPException(status_code=500, detail=f"Failed to get folders: {str(e)}")


@router.post("/folders/delete")
async def delete_folder(
    payload: DeleteFolderPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete folders and their documents."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Get storage provider using Keycloak SSO
    from app.storage.providers import get_storage_provider, StorageError
    from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH
    
    user_email = admin_context.get("admin_email") or admin_context.get("user_email")
    access_token = admin_context.get("_access_token")
    nextcloud_user_id = admin_context.get("_nextcloud_user_id")
    
    storage_provider = None
    if user_email and access_token:
        try:
            storage_provider = get_storage_provider(
                username=user_email,
                access_token=access_token,
                url=NEXTCLOUD_URL,
                root_path=NEXTCLOUD_ROOT_PATH,
                user_id_from_token=nextcloud_user_id
            )
            logger.info(
                f"✅ Storage provider created for folder deletion: "
                f"user_email={user_email}, nextcloud_user_id={nextcloud_user_id}"
            )
        except (StorageError, Exception) as e:
            logger.warning(
                f"⚠️  Storage provider not available for folder deletion: {e}. "
                f"Folders will be deleted from DAVI but not from Nextcloud."
            )
    else:
        logger.warning(
            f"⚠️  Cannot create storage provider: "
            f"user_email={'present' if user_email else 'missing'}, "
            f"access_token={'present' if access_token else 'missing'}. "
            f"Folders will be deleted from DAVI but not from Nextcloud."
        )

    try:
        log_msg = (
            f"🗑️  DELETE /folders/delete called: "
            f"folder_names={payload.folder_names}, "
            f"role_names={payload.role_names}, "
            f"company_id={company_id}, admin_id={admin_id}, "
            f"storage_provider={'available' if storage_provider else 'not available'}"
        )
        logger.info(log_msg)
        print(log_msg, file=sys.stderr, flush=True)  # Force to stderr so it appears in docker logs
        
        result = await repo.delete_folders(
            company_id=company_id,
            folder_names=payload.folder_names,
            role_names=payload.role_names,
            admin_id=admin_id,
            storage_provider=storage_provider
        )

        log_msg = (
            f"✅ DELETE /folders/delete completed: "
            f"deleted_folders={result.get('deleted_folders', [])}, "
            f"total_documents_deleted={result.get('total_documents_deleted', 0)}"
        )
        logger.info(log_msg)
        print(log_msg, file=sys.stderr, flush=True)

        return {
            "success": True,
            "status": result["status"],
            "deleted_folders": result["deleted_folders"],
            "total_documents_deleted": result["total_documents_deleted"]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete folders")
        raise HTTPException(status_code=500, detail=f"Failed to delete folders: {str(e)}")


@router.get("/folders/import/list")
async def list_importable_folders(
    import_root: Optional[str] = Query(None, description="Root path in Nextcloud to list from"),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    List folders available for import from Nextcloud.
    
    Scenario B2: SharePoint → Nextcloud → DAVI (partial import)
    - Lists folders in Nextcloud that can be imported into DAVI
    - Shows which folders are already imported
    - Supports recursive listing for tree view
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Get storage provider using Keycloak SSO
        from app.storage.providers import get_storage_provider, StorageError
        from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH
        
        user_email = admin_context.get("admin_email") or admin_context.get("user_email")
        access_token = admin_context.get("_access_token")
        nextcloud_user_id = admin_context.get("_nextcloud_user_id")  # sub or preferred_username from token
        
        if not user_email or not access_token:
            return {
                "success": True,
                "folders": [],
                "import_root": import_root or "",
                "message": "Keycloak access token not available. Cannot connect to Nextcloud.",
                "configured": False
            }
        
        try:
            storage_provider = get_storage_provider(
                username=user_email,
                access_token=access_token,
                url=NEXTCLOUD_URL,
                root_path=NEXTCLOUD_ROOT_PATH,
                user_id_from_token=nextcloud_user_id  # Use sub or preferred_username for WebDAV path
            )
        except StorageError as e:
            logger.warning(f"Nextcloud not configured: {e}")
            return {
                "success": True,
                "folders": [],
                "import_root": import_root or "",
                "message": "Nextcloud storage is not configured. Please configure NEXTCLOUD_URL to enable folder import.",
                "configured": False
            }
        
        # Determine import root path
        if import_root is None:
            import_root = ""
        
        # List folders recursively from Nextcloud
        nextcloud_folders = await storage_provider.list_folders(import_root, recursive=True)
        
        # Get existing DAVI folders to mark which are already imported
        existing_folders = await repo.folders.find({
            "company_id": company_id,
            "admin_id": admin_id,
            "storage_path": {"$exists": True, "$ne": None}
        }).to_list(length=None)
        
        # Normalize storage paths for comparison (lowercase, strip trailing slashes)
        existing_storage_paths = set()
        for folder in existing_folders:
            storage_path = folder.get("storage_path", "")
            if storage_path:
                # Normalize: lowercase, strip trailing slashes, strip leading/trailing whitespace
                normalized = storage_path.lower().strip().rstrip("/")
                existing_storage_paths.add(normalized)
                logger.debug(f"Existing folder storage_path: '{storage_path}' -> normalized: '{normalized}'")
        
        # Build response with import status
        folders_list = []
        normalized_company_id = company_id.lower()
        
        for folder_info in nextcloud_folders:
            folder_path = folder_info.get("path", "")
            # Normalize path for comparison: lowercase, strip whitespace, strip trailing slashes
            normalized_path = folder_path.lower().strip().rstrip("/")
            
            # Skip structural folders
            path_parts = [p for p in normalized_path.split("/") if p]
            if not path_parts:
                continue
            if len(path_parts) == 1 and path_parts[0] == normalized_company_id:
                continue
            if len(path_parts) == 2 and path_parts[0] == normalized_company_id and path_parts[1] == normalized_company_id:
                continue
            if len(path_parts) == 2 and path_parts[0] == normalized_company_id:
                continue
            
            # Check if this folder is already imported by comparing normalized paths
            is_imported = normalized_path in existing_storage_paths
            if is_imported:
                logger.debug(f"Folder '{folder_path}' is already imported (normalized: '{normalized_path}')")
            else:
                logger.debug(f"Folder '{folder_path}' is not imported yet (normalized: '{normalized_path}', existing paths: {list(existing_storage_paths)[:5]}...)")
            
            folders_list.append({
                "path": folder_path,
                "name": folder_info.get("name", ""),
                "imported": is_imported,  # Frontend expects "imported", not "is_imported"
                "is_imported": is_imported,  # Keep for backward compatibility
                "selectable": True
            })
        
        return {
            "success": True,
            "folders": folders_list,
            "import_root": import_root or "",
            "configured": True
        }
    except Exception as e:
        logger.exception("Failed to list importable folders")
        raise HTTPException(status_code=500, detail=f"Failed to list folders: {str(e)}")


@router.post("/folders/import")
async def import_folders(
    payload: ImportFoldersPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Import selected folders from Nextcloud into DAVI.
    
    Scenario B2: SharePoint → Nextcloud → DAVI (partial import)
    - Creates DAVI folder records for selected Nextcloud folders
    - Links folders to their Nextcloud storage paths
    - Marks folders as imported (origin="imported")
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Get storage provider using Keycloak SSO
        from app.storage.providers import get_storage_provider, StorageError
        from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH
        from datetime import datetime
        
        user_email = admin_context.get("admin_email") or admin_context.get("user_email")
        access_token = admin_context.get("_access_token")
        nextcloud_user_id = admin_context.get("_nextcloud_user_id")
        
        if not user_email or not access_token:
            raise HTTPException(
                status_code=400,
                detail="Keycloak access token not available. Cannot connect to Nextcloud."
            )
        
        try:
            storage_provider = get_storage_provider(
                username=user_email,
                access_token=access_token,
                url=NEXTCLOUD_URL,
                root_path=NEXTCLOUD_ROOT_PATH,
                user_id_from_token=nextcloud_user_id
            )
        except StorageError as e:
            logger.warning(f"Nextcloud not configured: {e}")
            raise HTTPException(
                status_code=400,
                detail="Nextcloud storage is not configured. Please configure NEXTCLOUD_URL to enable folder import."
            )
        
        imported_folders = []
        skipped_folders = []
        errors = []
        
        normalized_company_id = company_id.lower()
        
        for folder_path in payload.folder_paths:
            try:
                normalized_path = folder_path.lower().rstrip("/")
                path_parts = [p for p in normalized_path.split("/") if p]
                
                # Prevent importing structural folders
                if not path_parts:
                    errors.append(f"Cannot import empty folder path: {folder_path}")
                    continue
                if len(path_parts) == 1 and path_parts[0] == normalized_company_id:
                    errors.append(f"Cannot import structural folder: {folder_path}")
                    continue
                if len(path_parts) == 2 and path_parts[0] == normalized_company_id and path_parts[1] == normalized_company_id:
                    errors.append(f"Cannot import structural folder: {folder_path}")
                    continue
                if len(path_parts) == 2 and path_parts[0] == normalized_company_id:
                    errors.append(f"Cannot import structural folder: {folder_path}")
                    continue
                
                # Verify folder exists in Nextcloud
                if not await storage_provider.folder_exists(folder_path):
                    errors.append(f"Folder not found in Nextcloud: {folder_path}")
                    continue
                
                # Normalize folder path (strip whitespace, trailing slashes)
                normalized_folder_path = folder_path.strip().rstrip("/")
                
                # Extract folder name from path
                folder_name = normalized_folder_path.split("/")[-1] if "/" in normalized_folder_path else normalized_folder_path
                
                # Check if folder already exists in DAVI (check by storage_path for exact match)
                existing = await repo.folders.find_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "storage_path": normalized_folder_path
                })
                
                if existing:
                    skipped_folders.append(folder_name)
                    logger.debug(f"Folder '{folder_name}' with path '{normalized_folder_path}' already exists, skipping")
                    continue
                
                # Create folder record in DAVI
                folder_doc = {
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name,
                    "document_count": 0,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                    "status": "active",
                    "storage_provider": "nextcloud",
                    "storage_path": normalized_folder_path,  # Store normalized path for consistent comparison
                    "origin": "imported",
                    "indexed": False,
                    "sync_enabled": True
                }
                
                logger.info(f"Importing folder '{folder_name}' with storage_path: '{normalized_folder_path}'")
                
                await repo.folders.insert_one(folder_doc)
                imported_folders.append(folder_name)
                
            except Exception as e:
                error_msg = f"Failed to import folder '{folder_path}': {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        # After importing folders, automatically sync documents from those folders
        # This ensures files are imported immediately after folder import
        synced_documents = 0
        sync_errors = []
        if imported_folders and storage_provider:
            try:
                logger.info(f"Auto-syncing documents from {len(imported_folders)} newly imported folder(s)")
                sync_result = await repo.sync_documents_from_nextcloud(
                    company_id=company_id,
                    admin_id=admin_id,
                    storage_provider=storage_provider
                )
                synced_documents = sync_result.get("new_documents", 0)
                if sync_result.get("errors"):
                    sync_errors = sync_result.get("errors", [])
                logger.info(f"Auto-sync completed: {synced_documents} new document(s) imported")
            except Exception as sync_error:
                logger.warning(f"Auto-sync after import failed (non-critical): {sync_error}")
                sync_errors.append(f"Auto-sync failed: {str(sync_error)}")
        
        return {
            "success": True,
            "imported_folders": imported_folders,
            "imported": imported_folders,  # Frontend expects "imported" array
            "skipped_folders": skipped_folders,
            "skipped": skipped_folders,  # Frontend expects "skipped" array
            "errors": errors,
            "total_imported": len(imported_folders),
            "total_skipped": len(skipped_folders),
            "total_errors": len(errors),
            "synced_documents": synced_documents,  # Number of documents auto-synced
            "sync_errors": sync_errors  # Any errors during auto-sync
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to import folders")
        raise HTTPException(status_code=500, detail=f"Failed to import folders: {str(e)}")


@router.post("/folders/sync")
async def sync_documents_from_nextcloud(
    folder_id: Optional[str] = None,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Sync documents from Nextcloud to DAVI.
    
    This endpoint synchronizes documents from Nextcloud folders to DAVI,
    detecting new files, deleted files, and deleted folders.
    """
    await check_teamlid_permission(admin_context, db, "documents")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Get storage provider using Keycloak SSO
        from app.storage.providers import get_storage_provider, StorageError
        from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH
        
        user_email = admin_context.get("admin_email") or admin_context.get("user_email")
        access_token = admin_context.get("_access_token")
        
        if not user_email or not access_token:
            raise HTTPException(
                status_code=400,
                detail="Keycloak access token not available. Cannot connect to Nextcloud."
            )
        
        nextcloud_user_id = admin_context.get("_nextcloud_user_id")
        
        try:
            storage_provider = get_storage_provider(
                username=user_email,
                access_token=access_token,
                url=NEXTCLOUD_URL,
                root_path=NEXTCLOUD_ROOT_PATH,
                user_id_from_token=nextcloud_user_id
            )
        except StorageError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Nextcloud not configured: {str(e)}"
            )
        
        logger.info(f"Starting sync for company_id={company_id}, admin_id={admin_id}, folder_id={folder_id or 'all'}")
        
        result = await repo.sync_documents_from_nextcloud(
            company_id=company_id,
            admin_id=admin_id,
            folder_id=folder_id,
            storage_provider=storage_provider
        )
        
        logger.info(
            f"Sync result: {result.get('synced_folders', 0)} folder(s), "
            f"{result.get('new_documents', 0)} new document(s), "
            f"{len(result.get('errors', []))} error(s)"
        )
        
        # Trigger RAG indexing for synced files
        if result.get("synced_file_paths"):
            from app.api.rag import rag_index_files
            try:
                # rag_index_files expects (user_id, file_paths, company_id, is_role_based)
                # Synced documents from Nextcloud are role-based, so is_role_based=True
                await rag_index_files(admin_id, result["synced_file_paths"], company_id, is_role_based=True)
            except Exception as e:
                logger.warning(f"RAG indexing failed for synced files: {e}")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to sync documents from Nextcloud")
        raise HTTPException(status_code=500, detail=f"Failed to sync documents: {str(e)}")


# Alias endpoint for frontend compatibility
@router.post("/folders/sync-nextcloud")
async def sync_documents_from_nextcloud_alias(
    folder_id: Optional[str] = None,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Alias for /folders/sync endpoint.
    Frontend calls this endpoint, but it's the same as /folders/sync.
    """
    return await sync_documents_from_nextcloud(folder_id, admin_context, db)

