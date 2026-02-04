"""
Nextcloud Sync Repository - Domain-specific repository for Nextcloud synchronization.

This module handles all Nextcloud synchronization operations including:
- Document synchronization from Nextcloud to DAVI
- Folder synchronization
- Deletion detection (files and folders)
"""

import logging
import os
import shutil
from datetime import datetime
from typing import Optional
from fastapi import HTTPException
import aiofiles

from app.repositories.base_repo import BaseRepository
from app.repositories.constants import UPLOAD_ROOT

logger = logging.getLogger(__name__)


class NextcloudSyncRepository(BaseRepository):
    """Repository for Nextcloud synchronization operations."""
    
    async def sync_documents_from_nextcloud(
        self,
        company_id: str,
        admin_id: str,
        folder_id: Optional[str] = None,
        storage_provider=None
    ) -> dict:
        """
        Sync documents from Nextcloud to DAVI for folders that exist in Nextcloud.
        
        Syncs folders with origin="imported" (folders imported from Nextcloud) OR
        origin="davi" (folders created in DAVI that were synced to Nextcloud).
        When a user uploads a document to any folder in Nextcloud, this method
        syncs those documents to DAVI.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            folder_id: Optional specific folder ID to sync (if None, syncs all folders with storage_path)
            storage_provider: StorageProvider instance (must be provided, uses Keycloak SSO)
            
        Returns:
            Dictionary with sync results including synced file paths for RAG indexing
        """
        if not storage_provider:
            raise HTTPException(
                status_code=400,
                detail="Storage provider not available"
            )
        
        folder_query = {
            "company_id": company_id,
            "admin_id": admin_id,
            "storage_path": {"$exists": True, "$ne": None}
        }
        
        if folder_id:
            folder_query["_id"] = folder_id
        
        folders_to_sync = await self.folders.find(folder_query).to_list(length=None)
        
        if not folders_to_sync:
            return {
                "success": True,
                "synced_folders": 0,
                "new_documents": 0,
                "skipped_documents": 0,
                "errors": [],
                "message": "No folders with storage_path found to sync"
            }
        
        synced_folders = 0
        new_documents = 0
        deleted_from_davi = 0
        deleted_folders_count = 0
        skipped_documents = 0
        errors = []
        synced_file_paths = []
        
        from app.repositories.document_repo import DocumentRepository
        document_repo = DocumentRepository(self.db)
        
        # Detect folders deleted from Nextcloud (only if syncing all folders)
        if not folder_id:
            try:
                nextcloud_folders_list = await storage_provider.list_folders("", recursive=True)
                nextcloud_folder_paths = {f["path"].lower().rstrip("/") for f in nextcloud_folders_list}
                
                all_folders_query = {
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "storage_path": {"$exists": True, "$ne": None}
                }
                all_folders = await self.folders.find(all_folders_query).to_list(length=None)
                
                for folder in all_folders:
                    storage_path = folder.get("storage_path")
                    if storage_path:
                        normalized_storage_path = storage_path.lower().rstrip("/")
                        
                        if normalized_storage_path not in nextcloud_folder_paths:
                            folder_name = folder.get("name")
                            logger.info(f"Folder '{folder_name}' (path: {storage_path}) was deleted from Nextcloud, removing from DAVI")
                            
                            try:
                                docs_to_delete = await self.documents.find({
                                    "company_id": company_id,
                                    "user_id": admin_id,
                                    "upload_type": folder_name
                                }).to_list(length=None)
                                
                                for doc in docs_to_delete:
                                    file_path = doc.get("path")
                                    if file_path and os.path.exists(file_path):
                                        try:
                                            os.remove(file_path)
                                        except Exception as e:
                                            logger.warning(f"Failed to delete local file {file_path}: {str(e)}")
                                
                                await self.documents.delete_many({
                                    "company_id": company_id,
                                    "user_id": admin_id,
                                    "upload_type": folder_name
                                })
                                
                                local_base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, folder_name)
                                if os.path.exists(local_base_path):
                                    try:
                                        shutil.rmtree(local_base_path, ignore_errors=True)
                                        logger.info(f"Deleted local folder: {local_base_path}")
                                    except Exception as e:
                                        logger.warning(f"Failed to delete local folder {local_base_path}: {str(e)}")
                                
                                await self.roles.update_many(
                                    {
                                        "company_id": company_id,
                                        "added_by_admin_id": admin_id,
                                        "folders": folder_name
                                    },
                                    {"$pull": {"folders": folder_name}}
                                )
                                
                                await self.folders.delete_one({"_id": folder["_id"]})
                                
                                deleted_folders_count += 1
                                logger.info(f"Deleted folder '{folder_name}' from DAVI (removed from Nextcloud)")
                                
                            except Exception as e:
                                error_msg = f"Failed to delete folder '{folder_name}' that was removed from Nextcloud: {str(e)}"
                                logger.error(error_msg)
                                errors.append(error_msg)
            except Exception as e:
                logger.warning(f"Failed to list folders from Nextcloud for deletion detection: {str(e)}")
                errors.append(f"Failed to detect deleted folders: {str(e)}")
        
        # Re-fetch folders_to_sync after deletions
        folders_to_sync = await self.folders.find(folder_query).to_list(length=None)
        
        for folder in folders_to_sync:
            folder_name = folder.get("name")
            storage_path = folder.get("storage_path")
            
            if not storage_path:
                errors.append(f"Folder '{folder_name}' has no storage_path")
                continue
            
            try:
                nextcloud_files = await storage_provider.list_files(storage_path, recursive=False)
                
                existing_docs = await self.documents.find({
                    "company_id": company_id,
                    "user_id": admin_id,
                    "upload_type": folder_name
                }).to_list(length=None)
                
                nextcloud_file_names = {file_info["name"] for file_info in nextcloud_files} if nextcloud_files else set()
                existing_file_names = {doc.get("file_name") for doc in existing_docs if doc.get("file_name")}
                
                # Detect files deleted from Nextcloud
                files_to_delete = existing_file_names - nextcloud_file_names
                folder_deleted_count = 0
                
                for file_name in files_to_delete:
                    doc_to_delete = next((doc for doc in existing_docs if doc.get("file_name") == file_name), None)
                    if not doc_to_delete:
                        continue
                    
                    try:
                        file_path = doc_to_delete.get("path")
                        
                        if file_path and os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                logger.info(f"Deleted local file (removed from Nextcloud): {file_path}")
                            except Exception as e:
                                logger.warning(f"Failed to delete local file {file_path}: {str(e)}")
                        
                        delete_result = await self.documents.delete_one({"_id": doc_to_delete["_id"]})
                        if delete_result.deleted_count > 0:
                            folder_deleted_count += 1
                            deleted_from_davi += 1
                            logger.info(f"Deleted document from DAVI (removed from Nextcloud): {file_name} in folder {folder_name}")
                            
                            await self.folders.update_one(
                                {"company_id": company_id, 'admin_id': admin_id, "name": folder_name},
                                {"$inc": {"document_count": -1}}
                            )
                    except Exception as e:
                        error_msg = f"Failed to delete file '{file_name}' that was removed from Nextcloud: {str(e)}"
                        logger.error(error_msg)
                        errors.append(error_msg)
                
                if not nextcloud_files:
                    logger.info(f"No files found in Nextcloud folder: {storage_path}")
                    synced_folders += 1
                    await self.folders.update_one(
                        {"_id": folder["_id"]},
                        {"$set": {"document_count": 0, "updated_at": datetime.utcnow()}}
                    )
                    continue
                
                # Process each file from Nextcloud (add new files)
                for file_info in nextcloud_files:
                    file_name = file_info["name"]
                    file_storage_path = file_info["path"]
                    
                    if file_name in existing_file_names:
                        skipped_documents += 1
                        continue
                    
                    try:
                        file_content = await storage_provider.download_file(file_storage_path)
                        file_bytes = file_content.read() if hasattr(file_content, 'read') else file_content
                        
                        local_base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, folder_name)
                        os.makedirs(local_base_path, exist_ok=True)
                        local_file_path = os.path.join(local_base_path, file_name)
                        
                        async with aiofiles.open(local_file_path, "wb") as f:
                            await f.write(file_bytes)
                        
                        doc_record = await document_repo.add_document(
                            company_id=company_id,
                            user_id=admin_id,
                            file_name=file_name,
                            upload_type=folder_name,
                            path=local_file_path,
                            storage_path=file_storage_path,
                            source="nextcloud_sync"
                        )
                        
                        if doc_record:
                            new_documents += 1
                            synced_file_paths.append(local_file_path)
                            logger.info(f"Synced document from Nextcloud: {file_name} in folder {folder_name}")
                        else:
                            skipped_documents += 1
                            
                    except Exception as e:
                        error_msg = f"Failed to sync file '{file_name}' from folder '{folder_name}': {str(e)}"
                        logger.error(error_msg)
                        errors.append(error_msg)
                
                synced_folders += 1
                
                await self.folders.update_one(
                    {"_id": folder["_id"]},
                    {"$set": {"document_count": len(nextcloud_files), "updated_at": datetime.utcnow()}}
                )
                
            except Exception as e:
                error_msg = f"Failed to sync folder '{folder_name}': {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)
        
        return {
            "success": True,
            "synced_folders": synced_folders,
            "new_documents": new_documents,
            "deleted_documents": deleted_from_davi,
            "deleted_folders": deleted_folders_count,
            "skipped_documents": skipped_documents,
            "synced_file_paths": synced_file_paths,
            "errors": errors,
            "message": f"Synced {synced_folders} folder(s), added {new_documents} new document(s), deleted {deleted_from_davi} document(s) and {deleted_folders_count} folder(s) removed from Nextcloud"
        }

