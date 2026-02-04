"""
Folder Repository - Domain-specific repository for folder management operations.

This module handles all folder-related operations including:
- Creating and deleting folders
- Folder metadata management
- Nextcloud folder synchronization
- Document uploads to folders
"""

import logging
import os
import re
import shutil
from datetime import datetime
from typing import List, Optional
from fastapi import HTTPException, UploadFile
from pymongo.errors import DuplicateKeyError
import aiofiles

from app.repositories.base_repo import BaseRepository
from app.repositories.constants import UPLOAD_ROOT

logger = logging.getLogger(__name__)


class FolderRepository(BaseRepository):
    """Repository for folder management operations."""
    
    async def get_folders(
        self,
        company_id: str,
        admin_id: str
    ) -> dict:
        """
        Get folders for a company/admin.
        
        Returns both folder names (for backward compatibility) and full folder objects
        with metadata (origin, indexed, sync_enabled, storage_path).
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            
        Returns:
            Dictionary with folders list and metadata
        """
        existing_folders = await self.folders.find(
            {"company_id": company_id, "admin_id": admin_id}
        ).to_list(length=None)

        folder_names = [folder.get("name", "") for folder in existing_folders]
        
        folder_objects = [
            {
                "name": folder.get("name", ""),
                "origin": folder.get("origin", "davi"),
                "indexed": folder.get("indexed", False),
                "sync_enabled": folder.get("sync_enabled", False),
                "storage_provider": folder.get("storage_provider"),
                "storage_path": folder.get("storage_path"),
                "document_count": folder.get("document_count", 0),
            }
            for folder in existing_folders
        ]

        return {
            "success": True,
            "folders": folder_names,
            "folders_metadata": folder_objects,
            "total": len(folder_names)
        }
    
    async def add_folders(
        self,
        company_id: str,
        admin_id: str,
        folder_names: List[str],
        storage_provider=None
    ) -> dict:
        """
        Create folders in DAVI and optionally sync to Nextcloud.
        
        Scenario A1: DAVI → Nextcloud
        - Creates folder records in MongoDB
        - Creates corresponding folders in Nextcloud
        - Stores canonical storage paths
        
        Args:
            company_id: Company identifier
            admin_id: Admin user identifier
            folder_names: List of folder names to create
            storage_provider: Optional StorageProvider instance for Nextcloud sync
            
        Returns:
            Dictionary with creation results
        """
        existing_folders = await self.folders.find(
            {"company_id": company_id, "admin_id": admin_id}
        ).to_list(length=None)
        
        existing_names = {folder["name"].lower() for folder in existing_folders}
        
        duplicated = []
        new_folders = []
        
        for folder_name in folder_names:
            normalized_name = folder_name.lower()
            if normalized_name in existing_names:
                duplicated.append(folder_name)
            else:
                new_folders.append(folder_name)
        
        if new_folders:
            folder_documents = []
            for folder_name in new_folders:
                normalized_name = folder_name.strip("/ ")
                
                storage_path = f"{company_id}/{admin_id}/{normalized_name}"
                
                storage_created = False
                if storage_provider:
                    try:
                        storage_created = await storage_provider.create_folder(storage_path)
                        logger.info(f"Created Nextcloud folder: {storage_path}")
                    except Exception as e:
                        logger.error(f"Failed to create Nextcloud folder {storage_path}: {e}")
                
                folder_doc = {
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": normalized_name,
                    "document_count": 0,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                    "status": "active",
                    "storage_provider": "nextcloud" if storage_provider else None,
                    "storage_path": storage_path if storage_provider else None,
                    "origin": "davi",
                    "indexed": False,
                    "sync_enabled": True if storage_provider else False
                }
                folder_documents.append(folder_doc)
            
            if folder_documents:
                await self.folders.insert_many(folder_documents)
        
        return {
            "success": True,
            "message": "Folders processed successfully",
            "added_folders": new_folders,
            "duplicated_folders": duplicated,
            "total_added": len(new_folders),
            "total_duplicates": len(duplicated)
        }
    
    async def delete_folders(
        self,
        company_id: str,
        folder_names: List[str],
        role_names: List[str],
        admin_id: str,
        storage_provider=None
    ) -> dict:
        """
        Delete folders and associated documents.
        
        Args:
            company_id: Company identifier
            folder_names: List of folder names to delete
            role_names: List of role names (matched with folder_names)
            admin_id: Admin identifier
            storage_provider: Optional StorageProvider for Nextcloud deletion
            
        Returns:
            Dictionary with deletion results
        """
        deleted_folders = []
        total_documents_deleted = 0

        for role_name, folder_name in zip(role_names, folder_names):
            if not role_name or role_name.strip() == "":
                delete_docs_result = await self.documents.delete_many({
                    "company_id": company_id,
                    "user_id": admin_id,
                    "upload_type": folder_name
                })
                
                total_documents_deleted += delete_docs_result.deleted_count
                
                pattern = os.path.join(
                    UPLOAD_ROOT,
                    "roleBased",
                    company_id,
                    admin_id,
                    folder_name,
                    "*"
                )
                
                import glob
                files = glob.glob(pattern)
                for file_path in files:
                    try:
                        if os.path.isfile(file_path):
                            os.remove(file_path)
                    except Exception as e:
                        logger.warning(f"Failed to delete file {file_path}: {e}")
                
                folder_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, folder_name)
                if os.path.exists(folder_path):
                    try:
                        shutil.rmtree(folder_path)
                    except Exception as e:
                        logger.warning(f"Failed to delete folder {folder_path}: {e}")
                
                if storage_provider:
                    folder = await self.folders.find_one({
                        "company_id": company_id,
                        "admin_id": admin_id,
                        "name": folder_name
                    })
                    
                    if folder and folder.get("storage_path"):
                        try:
                            await storage_provider.delete_folder(folder["storage_path"])
                            logger.info(f"Deleted Nextcloud folder: {folder['storage_path']}")
                        except Exception as e:
                            logger.warning(f"Failed to delete Nextcloud folder: {e}")
                
                await self.folders.delete_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name
                })
                
                deleted_folders.append(folder_name)
            else:
                delete_docs_result = await self.documents.delete_many({
                    "company_id": company_id,
                    "user_id": admin_id,
                    "upload_type": role_name
                })
                
                total_documents_deleted += delete_docs_result.deleted_count
                
                base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name)
                if os.path.exists(base_path):
                    try:
                        shutil.rmtree(base_path)
                    except Exception as e:
                        logger.warning(f"Failed to delete folder {base_path}: {e}")
                
                await self.roles.update_many(
                    {"company_id": company_id, "added_by_admin_id": admin_id, "folders": role_name},
                    {"$pull": {"folders": role_name}}
                )
                
                await self.folders.delete_many({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": {"$in": [f for f in folder_names if f]}
                })
                
                deleted_folders.append(role_name)

        return {
            "status": "deleted",
            "deleted_folders": deleted_folders,
            "total_documents_deleted": total_documents_deleted
        }
    
    async def upload_document_for_folder(
        self,
        company_id: str,
        admin_id: str,
        folder_name: str,
        file: UploadFile,
        storage_provider=None
    ) -> dict:
        """
        Upload a document to a specific folder.
        
        This method handles:
        - File path construction with validation
        - Nextcloud upload (if storage_provider provided)
        - Local file storage for RAG indexing
        - Document metadata creation
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            folder_name: Folder name (will be normalized)
            file: UploadFile object
            storage_provider: Optional StorageProvider for Nextcloud upload
            
        Returns:
            Dictionary with upload results including file path
            
        Raises:
            HTTPException: If folder not found or file already exists
        """
        file_name = (file.filename or "").strip()
        
        if "/" in file_name or "\\" in file_name:
            file_name = os.path.basename(file_name)
        
        file_name = file_name.replace("/", "").replace("\\", "").strip()
        
        safe_folder = folder_name.strip()
        parts = re.split(r'[/\\]+', safe_folder)
        non_empty_parts = [p.strip() for p in parts if p.strip()]
        
        if non_empty_parts:
            safe_folder = non_empty_parts[-1]
        else:
            safe_folder = safe_folder.strip()
        
        safe_folder = safe_folder.replace("/", "").replace("\\", "").strip()

        if not file_name:
            raise HTTPException(status_code=400, detail="Missing file name")

        if not safe_folder:
            raise HTTPException(status_code=400, detail="Invalid folder name")
        
        folder_exists = await self.folders.find_one({
            "company_id": company_id,
            "admin_id": admin_id,
            "name": safe_folder
        })

        if not folder_exists:
            all_folders = await self.folders.find({
                "company_id": company_id,
                "admin_id": admin_id
            }).to_list(length=None)
            
            for folder in all_folders:
                if folder.get("name", "").lower() == safe_folder.lower():
                    folder_exists = folder
                    break

        if not folder_exists:
            logger.warning(f"Folder '{safe_folder}' not found in database, creating it as fallback")
            folder_doc = {
                "company_id": company_id,
                "admin_id": admin_id,
                "name": safe_folder,
                "document_count": 0,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "status": "active"
            }
            try:
                await self.folders.insert_one(folder_doc)
                folder_exists = folder_doc
            except DuplicateKeyError:
                folder_exists = await self.folders.find_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": safe_folder
                })
        
        if folder_exists:
            actual_folder_name = folder_exists["name"]
        else:
            actual_folder_name = safe_folder
            logger.warning(f"Using safe_folder as fallback: '{actual_folder_name}'")
        
        final_folder_name = actual_folder_name.strip()
        
        if "/" in final_folder_name or "\\" in final_folder_name:
            logger.warning(f"WARNING: Folder name from database contains path separators: '{final_folder_name}'")
            final_folder_name = final_folder_name.replace("/", "").replace("\\", "").strip()
        
        if not final_folder_name:
            raise HTTPException(status_code=400, detail="Invalid folder name: empty after extraction from database")
        
        base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, final_folder_name)
        os.makedirs(base_path, exist_ok=True)

        clean_file_name = file_name
        if "/" in clean_file_name or "\\" in clean_file_name:
            clean_file_name = os.path.basename(clean_file_name)
            clean_file_name = clean_file_name.replace("/", "").replace("\\", "").strip()
        
        file_path = os.path.join(base_path, clean_file_name)

        existing_doc = await self.documents.find_one({
            "company_id": company_id,
            "user_id": admin_id,
            "upload_type": safe_folder,
            "file_name": file_name
        })

        if existing_doc:
            raise HTTPException(
                status_code=409,
                detail=f"Het document '{file_name}' bestaat al in de map '{safe_folder}'."
            )

        storage_path = None
        if storage_provider:
            if folder_exists and folder_exists.get("storage_path"):
                folder_storage_path = folder_exists["storage_path"]
                storage_path = f"{folder_storage_path}/{file_name}"
                logger.info(f"Uploading to Nextcloud: {storage_path}")
            else:
                folder_storage_path = f"{company_id}/{admin_id}/{final_folder_name}"
                storage_path = f"{folder_storage_path}/{file_name}"
                logger.info(f"Folder has no storage_path, using default: {storage_path}")
                
                if folder_exists:
                    await self.folders.update_one(
                        {"_id": folder_exists["_id"]},
                        {
                            "$set": {
                                "storage_path": folder_storage_path,
                                "storage_provider": "nextcloud",
                                "sync_enabled": True
                            }
                        }
                    )

        await file.seek(0)
        content = await file.read()
        
        if storage_provider and storage_path:
            try:
                from app.storage.providers import StorageError
                folder_storage_path = storage_path.rsplit("/", 1)[0]
                if not await storage_provider.folder_exists(folder_storage_path):
                    await storage_provider.create_folder(folder_storage_path)
                    logger.info(f"Created Nextcloud folder: {folder_storage_path}")
                
                await storage_provider.upload_file(
                    storage_path,
                    content,
                    content_length=len(content) if isinstance(content, bytes) else None
                )
                logger.info(f"Successfully uploaded to Nextcloud: {storage_path}")
            except StorageError as e:
                logger.error(f"Failed to upload to Nextcloud: {e}, falling back to local storage")
                storage_path = None
            except Exception as e:
                logger.error(f"Unexpected error uploading to Nextcloud: {e}, falling back to local storage")
                storage_path = None

        try:
            os.makedirs(base_path, exist_ok=True)
            async with aiofiles.open(file_path, "wb") as f:
                await f.write(content)
        except Exception as e:
            logger.error(f"Failed to save file locally: {e}")
            if not storage_path:
                raise HTTPException(status_code=500, detail=f"File save failed: {str(e)}")

        from app.repositories.document_repo import DocumentRepository
        document_repo = DocumentRepository(self.db)
        
        doc_record = await document_repo.add_document(
            company_id=company_id,
            user_id=admin_id,
            file_name=file_name,
            upload_type=safe_folder,
            path=file_path,
            storage_path=storage_path,
            source="manual_upload"
        )

        if not doc_record:
            raise HTTPException(
                status_code=409,
                detail=f"Het document '{file_name}' bestaat al in de map '{safe_folder}'."
            )

        await self.folders.update_one(
            {"company_id": company_id, "admin_id": admin_id, "name": safe_folder},
            {"$inc": {"document_count": 1}}
        )

        return {
            "folder": safe_folder,
            "file_name": file_name,
            "path": file_path,
            "storage_path": storage_path,
        }
    
    async def delete_documents(
        self,
        company_id: str,
        admin_id: str,
        documents_to_delete: List[dict],
        storage_provider=None
    ) -> int:
        """
        Delete documents from folders.
        
        This method handles:
        - Deleting documents from MongoDB
        - Deleting files from local filesystem
        - Deleting files from Nextcloud (if storage_provider provided)
        - Updating folder document counts
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            documents_to_delete: List of document info dictionaries with:
                - fileName: File name
                - folderName: Folder name (upload_type)
                - path: Optional file path
            storage_provider: Optional StorageProvider for Nextcloud deletion
            
        Returns:
            Number of documents deleted
        """
        import os
        from app.storage.providers import StorageError
        
        deleted_count = 0
        
        for doc_info in documents_to_delete:
            file_name = doc_info.get("fileName")
            folder_name = doc_info.get("folderName")
            path = doc_info.get("path")
            
            if not file_name or not folder_name:
                continue
            
            try:
                query = {
                    "company_id": company_id,
                    "user_id": admin_id,
                    "file_name": file_name,
                    "upload_type": folder_name
                }
                
                if path:
                    query["path"] = path
                
                document = await self.documents.find_one(query)
                if not document:
                    continue
                
                file_path = document.get("path")
                storage_path = document.get("storage_path")
                
                # Delete from local filesystem
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted physical file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete physical file {file_path}: {str(e)}")
                
                # Delete from Nextcloud if storage_path exists
                if storage_path and storage_provider:
                    try:
                        deleted_from_nextcloud = await storage_provider.delete_file(storage_path)
                        if deleted_from_nextcloud:
                            logger.info(f"Deleted file from Nextcloud: {storage_path}")
                        else:
                            logger.warning(f"File not found in Nextcloud (may have been already deleted): {storage_path}")
                    except StorageError as e:
                        logger.warning(f"Failed to delete file from Nextcloud {storage_path}: {str(e)}")
                    except Exception as e:
                        logger.warning(f"Error deleting file from Nextcloud {storage_path}: {str(e)}")
                
                delete_result = await self.documents.delete_one({"_id": document["_id"]})
                if delete_result.deleted_count > 0:
                    deleted_count += 1
                    
                    # Update folder document count
                    await self.folders.update_one(
                        {"company_id": company_id, 'admin_id': admin_id, "name": folder_name},
                        {"$inc": {"document_count": -1}}
                    )
                    
            except Exception as e:
                logger.error(f"Error deleting document {file_name} for folder {folder_name}: {str(e)}")
                continue
        
        return deleted_count

