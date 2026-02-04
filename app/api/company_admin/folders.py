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

logger = logging.getLogger(__name__)
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
            
            if user_email and access_token:
                storage_provider = get_storage_provider(
                    username=user_email,
                    access_token=access_token,
                    url=NEXTCLOUD_URL,
                    root_path=NEXTCLOUD_ROOT_PATH
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
    db=Depends(get_db)
):
    """Get all folders for the admin."""
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
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
    
    storage_provider = None
    if user_email and access_token:
        try:
            storage_provider = get_storage_provider(
                username=user_email,
                access_token=access_token,
                url=NEXTCLOUD_URL,
                root_path=NEXTCLOUD_ROOT_PATH
            )
        except (StorageError, Exception) as e:
            logger.debug(f"Storage provider not available for folder deletion sync: {e}")

    try:
        result = await repo.delete_folders(
            company_id=company_id,
            folder_names=payload.folder_names,
            role_names=payload.role_names,
            admin_id=admin_id,
            storage_provider=storage_provider
        )

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
                root_path=NEXTCLOUD_ROOT_PATH
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
            "admin_id": admin_id
        }).to_list(length=None)
        
        existing_storage_paths = {
            folder.get("storage_path", "").lower()
            for folder in existing_folders
            if folder.get("storage_path")
        }
        
        # Build response with import status
        folders_list = []
        normalized_company_id = company_id.lower()
        
        for folder_info in nextcloud_folders:
            folder_path = folder_info.get("path", "")
            normalized_path = folder_path.lower().rstrip("/")
            
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
            
            is_imported = normalized_path in existing_storage_paths
            
            folders_list.append({
                "path": folder_path,
                "name": folder_info.get("name", ""),
                "is_imported": is_imported,
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
                root_path=NEXTCLOUD_ROOT_PATH
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
                
                # Extract folder name from path
                folder_name = folder_path.split("/")[-1] if "/" in folder_path else folder_path
                
                # Check if folder already exists in DAVI
                existing = await repo.folders.find_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name,
                    "storage_path": folder_path
                })
                
                if existing:
                    skipped_folders.append(folder_name)
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
                    "storage_path": folder_path,
                    "origin": "imported",
                    "indexed": False,
                    "sync_enabled": True
                }
                
                await repo.folders.insert_one(folder_doc)
                imported_folders.append(folder_name)
                
            except Exception as e:
                error_msg = f"Failed to import folder '{folder_path}': {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        return {
            "success": True,
            "imported_folders": imported_folders,
            "skipped_folders": skipped_folders,
            "errors": errors,
            "total_imported": len(imported_folders),
            "total_skipped": len(skipped_folders),
            "total_errors": len(errors)
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
        
        try:
            storage_provider = get_storage_provider(
                username=user_email,
                access_token=access_token,
                url=NEXTCLOUD_URL,
                root_path=NEXTCLOUD_ROOT_PATH
            )
        except StorageError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Nextcloud not configured: {str(e)}"
            )
        
        result = await repo.sync_documents_from_nextcloud(
            company_id=company_id,
            admin_id=admin_id,
            folder_id=folder_id,
            storage_provider=storage_provider
        )
        
        # Trigger RAG indexing for synced files
        if result.get("synced_file_paths"):
            from app.api.rag import rag_index_files
            try:
                await rag_index_files(result["synced_file_paths"], company_id, admin_id)
            except Exception as e:
                logger.warning(f"RAG indexing failed for synced files: {e}")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to sync documents from Nextcloud")
        raise HTTPException(status_code=500, detail=f"Failed to sync documents: {str(e)}")

