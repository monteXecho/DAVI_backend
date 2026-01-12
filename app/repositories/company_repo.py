import logging
import uuid
import copy
import os
import re
import io
import glob
import aiofiles
import pandas as pd
import shutil
from fastapi import HTTPException, UploadFile
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional
from pymongo.errors import DuplicateKeyError
from collections import defaultdict
from typing import List

# Storage provider for Nextcloud integration
try:
    from app.storage.providers import get_storage_provider, StorageError
except ImportError:
    # Storage provider not available
    get_storage_provider = None
    StorageError = Exception

logger = logging.getLogger(__name__)

BASE_DOC_URL = "https://your-backend.com/documents/download"

# Base path for all uploads
UPLOAD_ROOT = "/app/uploads/documents"

# Ensure root folder exists at startup
os.makedirs(UPLOAD_ROOT, exist_ok=True)

DEFAULT_MODULES = {
    "Documenten chat": {"desc": "AI-zoek & Q&A over geuploade documenten.", "enabled": False},
    "GGD Checks": {"desc": "Automatische GGD-controles, inclusief BKR-bewaking, afwijkingslogica en rapportage.", "enabled": False}
}

def serialize_modules(modules: dict) -> list:
    return [{"name": k, "desc": v["desc"], "enabled": v["enabled"]} for k, v in modules.items()]

def serialize_documents(docs_cursor, user_id: str):
    return [
        {
            "file_name": d["file_name"],
            "file_url": f"{BASE_DOC_URL}/{user_id}/{d['file_name']}",
        }
        for d in docs_cursor
    ]

class CompanyRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.companies = db.companies
        self.admins = db.company_admins
        self.users = db.company_users
        self.roles = db.company_roles
        self.documents = db.documents
        self.folders = db.company_folders
        self.guest_access = db.company_guest_access

    async def get_admins_by_company(self, company_id: str):
        return await self.admins.find({"company_id": company_id}).to_list(None)

    async def get_users_by_company(self, company_id: str):
        return await self.users.find({"company_id": company_id}).to_list(None)

    async def get_users_by_company_admin(self, admin_id: str):
        return await self.users.find({"added_by_admin_id": admin_id}, {"_id": 0}).to_list(None)

    async def get_admin_by_id(self, company_id: str, user_id: str):
        return await self.admins.find_one({"company_id": company_id, "user_id": user_id})
    
    async def get_role_by_name(self, company_id: str, admin_id: str, role_name: str):
        return await self.roles.find_one({"company_id": company_id, "added_by_admin_id": admin_id, "name": role_name})


    # ---------------- Companies ---------------- #
    async def create_company(self, name: str) -> dict:
        now = datetime.utcnow()
        company_id = str(uuid.uuid4())
        doc = {
            "company_id": company_id,
            "name": name,
            "created_at": now,
            "updated_at": now,
        }
        await self.companies.insert_one(doc)
        return {
            "id": company_id,
            "name": name,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    async def get_all_companies(self):
        companies_cursor = self.companies.find().sort("name", 1)
        companies = []

        async for company in companies_cursor:
            company_id = company["company_id"]

            admins_cursor = self.admins.find({"company_id": company_id})
            admins = []
            async for admin in admins_cursor:
                docs_cursor = self.documents.find({"user_id": admin["user_id"]})
                documents = [
                    {
                        "file_name": d["file_name"],
                        "file_url": f"{BASE_DOC_URL}/{d['user_id']}/{d['file_name']}",
                    }
                    async for d in docs_cursor
                ]
                admins.append({
                    "id": admin["user_id"],
                    "user_id": admin["user_id"],
                    "name": admin["name"],
                    "email": admin["email"],
                    "modules": serialize_modules(admin["modules"]),
                    "documents": documents,
                })

            users_cursor = self.users.find({"company_id": company_id})
            users = []
            async for user in users_cursor:
                docs_cursor = self.documents.find({"user_id": user["user_id"]})
                documents = [
                    {
                        "file_name": d["file_name"],
                        "file_url": f"{BASE_DOC_URL}/{d['user_id']}/{d['file_name']}",
                    }
                    async for d in docs_cursor
                ]
                users.append({
                    "id": user["user_id"],
                    "name": user["name"],
                    "email": user["email"],
                    "documents": documents,
                })

            companies.append({
                "id": company_id,
                "name": company["name"],
                "admins": admins,
                "users": users,
            })

        return {"companies": companies}

    async def delete_company(self, company_id: str) -> bool:
        await self.admins.delete_many({"company_id": company_id})
        await self.users.delete_many({"company_id": company_id})
        await self.documents.delete_many({"company_id": company_id})
        result = await self.companies.delete_one({"company_id": company_id})
        return result.deleted_count > 0


    # ---------------- Admins ---------------- #
    async def add_admin(self, company_id: str, admin_id: str, name: str, email: str, modules: Optional[dict] = None):
        if await self.admins.find_one({"company_id": company_id, "email": email}):
            raise ValueError("Admin with this email already exists")

        admin_modules = copy.deepcopy(DEFAULT_MODULES)
        if modules:
            for k, v in modules.items():
                if k in admin_modules:
                    admin_modules[k]["enabled"] = v.get("enabled", False)

        admin_doc = {
            "company_id": company_id,
            "user_id": str(uuid.uuid4()),
            "name": name,
            "email": email,
            "added_by_admin_id": admin_id,
            "modules": admin_modules,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        await self.admins.insert_one(admin_doc)

        return {
            "user_id": admin_doc["user_id"],
            "company_id": admin_doc["company_id"],
            "name": admin_doc["name"],
            "email": admin_doc["email"],
            "modules": serialize_modules(admin_doc["modules"]),
            "documents": [], 
        }

    async def reassign_admin(self, company_id: str, admin_id: str, name: str, email: str):
        # Check if another admin already uses this email
        existing = await self.admins.find_one({
            "company_id": company_id,
            "email": email,
        })
        if existing:
            raise ValueError("Admin with this email already exists")

        # Update only the fields you want to change
        update_result = await self.admins.update_one(
            {"company_id": company_id, "user_id": admin_id},
            {
                "$set": {
                    "name": name,
                    "email": email,
                    "updated_at": datetime.utcnow(),
                }
            }
        )

        if update_result.matched_count == 0:
            raise ValueError("Admin not found")

        # Return updated fields
        updated_admin = await self.admins.find_one({"company_id": company_id, "user_id": admin_id})

        return {
            "user_id": updated_admin["user_id"],
            "company_id": updated_admin["company_id"],
            "name": updated_admin["name"],
            "email": updated_admin["email"],
            "modules": serialize_modules(updated_admin["modules"]),
            "documents": updated_admin.get("documents", []),
        }


    async def delete_admin(self, company_id: str, user_id: str, admin_id: str = None) -> bool:
        """
        Delete an admin. If admin_id is provided, only deletes if the admin was added by admin_id.
        """
        query = {"company_id": company_id, "user_id": user_id}
        if admin_id:
            # Only delete if added by this admin
            query["added_by_admin_id"] = admin_id
        
        admin = await self.admins.find_one(query)
        if not admin:
            if admin_id:
                raise HTTPException(
                    status_code=403,
                    detail="You can only delete admins that you added."
                )
            return False

        result = await self.admins.delete_one({"company_id": company_id, "user_id": user_id})
        if result.deleted_count > 0:
            await self.documents.delete_many({"user_id": admin["user_id"]})
            # Also delete any guest_access entries where this admin is the guest
            await self.guest_access.delete_many({
                "company_id": company_id,
                "guest_user_id": user_id
            })
            return True
        return False

    async def assign_modules(self, company_id: str, user_id: str, modules: dict) -> Optional[dict]:
        admin = await self.admins.find_one({"company_id": company_id, "user_id": user_id})
        if not admin:
            return None

        for k, v in modules.items():
            if k in admin["modules"]:
                admin["modules"][k]["enabled"] = v.get("enabled", False)

        await self.admins.update_one(
            {"company_id": company_id, "user_id": user_id},
            {"$set": {"modules": admin["modules"], "updated_at": datetime.utcnow()}},
        )

        # return clean version
        return {
            "id": admin["user_id"],
            "user_id": admin["user_id"],
            "company_id": admin["company_id"],
            "name": admin["name"],
            "email": admin["email"],
            "modules": serialize_modules(admin["modules"]),
        }

    async def find_admin_by_email(self, email: str):
        admin = await self.admins.find_one({"email": email})
        if not admin:
            return None
        return {"email": admin["email"], "role": "company_admin"}

    async def find_user_by_email(self, email: str):
        user = await self.users.find_one({"email": email})
        if not user:
            return None
        return {"email": user["email"], "role": "company_user"}

    async def get_all_private_documents(self, email: str, document_type: str):
        user_rec = await self.users.find_one({"email": email})
        admin_rec = await self.admins.find_one({"email": email})

        user = user_rec or admin_rec

        if not user:
            return {"documents": []}

        user_id = user["user_id"]
        if not user_id:
            return {"documents": []}

        docs_cursor = self.documents.find({
            "user_id": user_id,
            "upload_type": document_type
        })

        docs = await docs_cursor.to_list(length=None)

        if not docs:
            return {"documents": []}

        result = {"documents": []}

        for doc in docs:
            result["documents"].append({
                "file_name": doc.get("file_name"),
                "upload_type": doc.get("upload_type")
            })

        return result


    async def get_admin_documents(self, company_id: str, admin_id: str):
        try:
            roles_cursor = self.roles.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            roles = await roles_cursor.to_list(None)
            
            if not roles:
                return {}
            
            folders_cursor = self.folders.find({
                "company_id": company_id,
                "admin_id": admin_id
            })
            all_folders = await folders_cursor.to_list(None)
            folder_names = [folder["name"] for folder in all_folders]
            
            if not folder_names:
                return {}
            
            docs_cursor = self.documents.find({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$in": folder_names}
            })
            documents = await docs_cursor.to_list(None)
            
            users_cursor = self.users.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            users = await users_cursor.to_list(None)
            
            role_folders_map = defaultdict(set)
            for role in roles:
                role_name = role["name"]
                for folder_name in role.get("folders", []):
                    role_folders_map[role_name].add(folder_name)
            
            folder_docs_map = defaultdict(list)
            for doc in documents:
                folder_name = doc.get("upload_type")
                if folder_name in folder_names:
                    folder_docs_map[folder_name].append({
                        "file_name": doc.get("file_name"),
                        "path": doc.get("path", ""),
                        "uploaded_at": doc.get("uploaded_at") or doc.get("created_at"),
                        "_id": str(doc.get("_id", ""))
                    })
            
            role_users_map = defaultdict(list)
            for user in users:
                for role_name in user.get("assigned_roles", []):
                    user_info = {
                        "id": user.get("user_id", ""),
                        "name": user.get("name", ""),
                        "email": user.get("email", ""),
                        "user_id": user.get("user_id", "")
                    }
                    # Check if user already exists (by user_id) to avoid duplicates
                    existing_user = next((u for u in role_users_map[role_name] if u.get("user_id") == user_info["user_id"]), None)
                    if not existing_user:
                        role_users_map[role_name].append(user_info)
            
            # Create a map of folder_name -> all roles that have this folder
            folder_roles_map = defaultdict(set)
            for role in roles:
                role_name = role["name"]
                for folder_name in role.get("folders", []):
                    folder_roles_map[folder_name].add(role_name)
            
            result = {}
            for role in roles:
                role_name = role["name"]
                role_data = {"folders": []}
                
                for folder_name in role_folders_map[role_name]:
                    folder_docs = folder_docs_map.get(folder_name, [])
                    
                    # For each document, get users from ALL roles that have this folder
                    for doc in folder_docs:
                        all_users_for_folder = []
                        seen_user_ids = set()
                        
                        # Get users from all roles that have this folder
                        for role_with_folder in folder_roles_map.get(folder_name, []):
                            users_in_role = role_users_map.get(role_with_folder, [])
                            for user in users_in_role:
                                user_id = user.get("user_id") or user.get("id")
                                if user_id and user_id not in seen_user_ids:
                                    seen_user_ids.add(user_id)
                                    all_users_for_folder.append(user)
                        
                        doc["assigned_to"] = all_users_for_folder
                    
                    folder_entry = {
                        "name": folder_name,
                        "documents": folder_docs
                    }
                    role_data["folders"].append(folder_entry)
                
                if role_data["folders"]:
                    result[role_name] = role_data
            
            # Also include documents for folders that are not assigned to any role
            # These folders exist but aren't in any role's folder list
            folders_in_roles_set = set()
            for role in roles:
                for folder_name in role.get("folders", []):
                    folders_in_roles_set.add(folder_name)
            
            # Find folders with documents that aren't assigned to any role
            unassigned_folders_docs = {}
            for folder_name, folder_docs in folder_docs_map.items():
                if folder_name not in folders_in_roles_set and folder_docs:
                    # Create a special entry for unassigned folders
                    if "Geen rol toegewezen" not in result:
                        result["Geen rol toegewezen"] = {"folders": []}
                    
                    # Add users (empty array since no roles assigned)
                    for doc in folder_docs:
                        doc["assigned_to"] = []
                    
                    folder_entry = {
                        "name": folder_name,
                        "documents": folder_docs
                    }
                    result["Geen rol toegewezen"]["folders"].append(folder_entry)
            
            return result
            
        except Exception as e:
            print(f"Error in get_admin_documents: {str(e)}")
            return {}


    async def delete_private_documents(
        self,
        email: str,
        documents_to_delete: List[dict]
    ) -> int:

        user_rec = await self.users.find_one({"email": email})
        admin_rec = await self.admins.find_one({"email": email})

        user = user_rec or admin_rec

        user_id = user["user_id"]

        deleted_count = 0

        for doc_info in documents_to_delete:
            file_name = doc_info.get("file_name")

            if not file_name:
                continue 

            try:
                query = {
                    "user_id": user_id,
                    "file_name": file_name,
                    "upload_type": "document"
                }

                document = await self.documents.find_one(query)
                if not document:
                    continue

                file_path = document.get("path")

                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted physical file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete physical file {file_path}: {str(e)}")

                delete_result = await self.documents.delete_one({"_id": document["_id"]})
                if delete_result.deleted_count > 0:
                    deleted_count += 1

            except Exception as e:
                logger.error(f"Error deleting document {file_name}: {str(e)}")
                continue

        return deleted_count

    async def delete_documents(
        self,
        company_id: str,
        admin_id: str,
        documents_to_delete: List[dict]
    ) -> int:

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

                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted physical file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete physical file {file_path}: {str(e)}")

                delete_result = await self.documents.delete_one({"_id": document["_id"]})
                if delete_result.deleted_count > 0:
                    deleted_count += 1

                    await self.folders.update_one(
                        {"company_id": company_id, 'admin_id': admin_id, "name": folder_name},
                        {"$inc": {"document_count": -1}}
                    )

            except Exception as e:
                logger.error(f"Error deleting document {file_name} for folder {folder_name}: {str(e)}")
                continue

        return deleted_count

    async def get_folders(
        self,
        company_id: str,
        admin_id: str
    ) -> dict:
        """
        Get folders for a company/admin.
        
        Returns both folder names (for backward compatibility) and full folder objects
        with metadata (origin, indexed, sync_enabled, storage_path).
        """
        # Get existing folders for this company
        existing_folders = await self.folders.find(
            {"company_id": company_id, "admin_id": admin_id}
        ).to_list(length=None)

        # Extract folder names (for backward compatibility)
        folder_names = [folder.get("name", "") for folder in existing_folders]
        
        # Return full folder objects with metadata
        folder_objects = [
            {
                "name": folder.get("name", ""),
                "origin": folder.get("origin", "davi"),  # "davi" or "imported"
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
            "folders": folder_names,  # Backward compatibility
            "folders_metadata": folder_objects,  # Full metadata
            "total": len(folder_names)
        }

    async def add_folders(
        self,
        company_id: str,
        admin_id: str,
        folder_names: List[str],
        storage_provider=None  # Optional storage provider for Nextcloud sync
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
                # Normalize folder name to match upload logic (strip "/ " to be consistent)
                normalized_name = folder_name.strip("/ ")
                
                # Build storage path: company_id/admin_id/folder_name
                storage_path = f"{company_id}/{admin_id}/{normalized_name}"
                
                # Create folder in Nextcloud if storage provider is available
                storage_created = False
                if storage_provider:
                    try:
                        storage_created = await storage_provider.create_folder(storage_path)
                        logger.info(f"Created Nextcloud folder: {storage_path}")
                    except Exception as e:
                        logger.error(f"Failed to create Nextcloud folder {storage_path}: {e}")
                        # Continue with MongoDB creation even if Nextcloud fails
                        # This allows DAVI to function even if Nextcloud is temporarily unavailable
                
                folder_doc = {
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": normalized_name,
                    "document_count": 0,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                    "status": "active",
                    # Storage metadata (Scenario A1 support)
                    "storage_provider": "nextcloud" if storage_provider else None,
                    "storage_path": storage_path if storage_provider else None,
                    "origin": "davi",  # Created in DAVI
                    "indexed": False,  # Will be indexed when documents are added
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
        admin_id: str
    ) -> dict:

        deleted_folders = []
        total_documents_deleted = 0

        # Loop through matched role/folder pairs
        for role_name, folder_name in zip(role_names, folder_names):

            # Handle unassigned folders (empty role_name)
            if not role_name or role_name.strip() == "":
                # Delete all documents for this folder (upload_type matches folder_name)
                delete_docs_result = await self.documents.delete_many({
                    "company_id": company_id,
                    "user_id": admin_id,
                    "upload_type": folder_name
                })
                
                total_documents_deleted += delete_docs_result.deleted_count
                
                # Remove folder from filesystem - check all possible paths
                # Documents might be in different role folders
                pattern = os.path.join(
                    UPLOAD_ROOT,
                    "roleBased",
                    company_id,
                    admin_id,
                    "*",
                    folder_name
                )
                for path in glob.glob(pattern):
                    if os.path.exists(path):
                        shutil.rmtree(path, ignore_errors=True)
                
                # Delete the folder record itself
                await self.folders.delete_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name
                })
                
                deleted_folders.append({
                    "role_name": None,
                    "folder_name": folder_name
                })
                continue

            # Handle folders assigned to roles
            # Find the role that contains this folder
            role = await self.roles.find_one({
                "company_id": company_id,
                "added_by_admin_id": admin_id,
                "name": role_name,
                "folders": folder_name   # folder exists in the role's array
            })

            if not role:
                # Role not found or folder not in role - try to delete documents and folder anyway
                # This handles cases where the folder exists but role assignment is inconsistent
                logger.warning(f"Role '{role_name}' with folder '{folder_name}' not found, attempting cleanup anyway")
                
                # Try to delete documents for this folder
                delete_docs_result = await self.documents.delete_many({
                    "company_id": company_id,
                    "user_id": admin_id,
                    "upload_type": folder_name
                })
                
                if delete_docs_result.deleted_count > 0:
                    total_documents_deleted += delete_docs_result.deleted_count
                    logger.info(f"Deleted {delete_docs_result.deleted_count} documents for folder '{folder_name}' despite role mismatch")
                
                # Try to remove folder from filesystem
                pattern = os.path.join(
                    UPLOAD_ROOT,
                    "roleBased",
                    company_id,
                    admin_id,
                    "*",
                    folder_name
                )
                for path in glob.glob(pattern):
                    if os.path.exists(path):
                        shutil.rmtree(path, ignore_errors=True)
                
                base_path = os.path.join(
                    UPLOAD_ROOT,
                    "roleBased",
                    company_id,
                    admin_id,
                    folder_name
                )
                if os.path.exists(base_path):
                    shutil.rmtree(base_path, ignore_errors=True)
                
                # Try to remove folder from any roles that might have it
                await self.roles.update_many(
                    {
                        "company_id": company_id,
                        "added_by_admin_id": admin_id,
                        "folders": folder_name
                    },
                    {"$pull": {"folders": folder_name}}
                )
                
                # Try to delete folder record
                folder_delete_result = await self.folders.delete_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name
                })
                
                if folder_delete_result.deleted_count > 0 or delete_docs_result.deleted_count > 0:
                    deleted_folders.append({
                        "role_name": role_name,
                        "folder_name": folder_name
                    })
                
                continue

            # Delete all documents belonging to this role/folder
            delete_docs_result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": folder_name,
                "path": {"$regex": f"/{folder_name}/"}
            })

            total_documents_deleted += delete_docs_result.deleted_count

            # Remove folder from filesystem - check all possible paths
            # The folder might exist in multiple locations
            pattern = os.path.join(
                UPLOAD_ROOT,
                "roleBased",
                company_id,
                admin_id,
                "*",
                folder_name
            )
            for path in glob.glob(pattern):
                if os.path.exists(path):
                    shutil.rmtree(path, ignore_errors=True)
            
            # Also check the direct path (without role name in path)
            base_path = os.path.join(
                UPLOAD_ROOT,
                "roleBased",
                company_id,
                admin_id,
                folder_name
            )
            if os.path.exists(base_path):
                shutil.rmtree(base_path, ignore_errors=True)

            # Remove folder name from ALL roles that have this folder
            # (in case the folder is assigned to multiple roles)
            await self.roles.update_many(
                {
                    "company_id": company_id,
                    "added_by_admin_id": admin_id,
                    "folders": folder_name
                },
                {"$pull": {"folders": folder_name}}
            )

            # Delete the folder record itself from the folders collection
            await self.folders.delete_one({
                "company_id": company_id,
                "admin_id": admin_id,
                "name": folder_name
            })

            deleted_folders.append({
                "role_name": role_name,
                "folder_name": folder_name
            })

        # Only raise 404 if we didn't delete anything (no documents, no folders)
        if not deleted_folders and total_documents_deleted == 0:
            raise HTTPException(status_code=404, detail="No matching folders found")
        
        # If we deleted documents but no folders were recorded, that's still a partial success
        if not deleted_folders and total_documents_deleted > 0:
            logger.warning(f"Deleted {total_documents_deleted} documents but no folders were recorded in deleted_folders")

        return {
            "status": "deleted",
            "deleted_folders": deleted_folders,
            "total_documents_deleted": total_documents_deleted
        }


    async def add_users_from_email_file(
        self,
        company_id: str,
        admin_id: str,
        file_content: bytes,
        file_extension: str,
        selected_role: str = None  # Add selected_role parameter
    ) -> dict:
        """
        Add multiple users from CSV/Excel file containing email addresses.
        Handles emails in column headers, data cells, or anywhere in the file.
        """
        try:
            emails = []
            
            print(f"DEBUG: Processing file with extension: {file_extension}")
            print(f"DEBUG: File size: {len(file_content)} bytes")
            print(f"DEBUG: Selected role: {selected_role}")

            # Get roles based on the selected role logic
            roles_to_assign = await self._get_roles_to_assign(company_id, admin_id, selected_role)
            print(f"DEBUG: Roles to assign: {roles_to_assign}")

            # Read the file
            try:
                if file_extension == '.csv':
                    df = pd.read_csv(io.BytesIO(file_content))
                else:
                    df = pd.read_excel(io.BytesIO(file_content))
                
                print(f"DEBUG: DataFrame shape: {df.shape}")
                print(f"DEBUG: DataFrame columns: {df.columns.tolist()}")
                
                # Strategy 1: Check if emails are in COLUMN HEADERS (your case!)
                column_emails = []
                for col_name in df.columns:
                    col_str = str(col_name).strip()
                    if re.match(r'^[a-zA-Z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$', col_str):
                        column_emails.append(col_str)
                
                if column_emails:
                    print(f"DEBUG: Found {len(column_emails)} emails in column headers: {column_emails}")
                    emails.extend(column_emails)
                
                # Strategy 2: Check data cells (normal case)
                all_cell_emails = []
                for col in df.columns:
                    # Get non-null values from this column
                    col_data = df[col].dropna()
                    if not col_data.empty:
                        for value in col_data:
                            value_str = str(value).strip()
                            if re.match(r'^[a-zA-Z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$', value_str):
                                all_cell_emails.append(value_str)
                
                if all_cell_emails:
                    print(f"DEBUG: Found {len(all_cell_emails)} emails in data cells: {all_cell_emails}")
                    emails.extend(all_cell_emails)
                
                # Strategy 3: If no emails found in structured data, try text extraction
                if not emails:
                    print("DEBUG: No emails found in structured data, trying text extraction...")
                    # Convert entire DataFrame to string and extract emails
                    df_text = df.to_string()
                    text_emails = self._extract_emails_from_text(df_text)
                    if text_emails:
                        print(f"DEBUG: Found {len(text_emails)} emails via text extraction: {text_emails}")
                        emails.extend(text_emails)
                
            except Exception as file_error:
                print(f"DEBUG: File read failed: {file_error}")
                # Fallback to raw text parsing
                text_content = file_content.decode('utf-8', errors='ignore')
                emails = self._extract_emails_from_text(text_content)

            print(f"DEBUG: Total found emails: {len(emails)} - {emails}")

            if not emails:
                raise ValueError("No valid email addresses found in the file.")

            # Process the emails
            results = {
                "successful": [],
                "failed": [],
                "duplicates": []
            }

            # Track successful user creations for role assignment counting
            successful_users = []

            for email in emails:
                try:
                    # Check for existing user
                    existing_user = await self.users.find_one({
                        "company_id": company_id,
                        "email": email
                    })

                    if existing_user:
                        results["duplicates"].append({
                            "email": email,
                            "user_id": existing_user.get("user_id")
                        })
                        continue

                    # Use email prefix as name
                    name = email.split('@')[0]

                    # Create user document with role assignment
                    user_doc = {
                        "user_id": str(uuid.uuid4()),
                        "company_id": company_id,
                        "added_by_admin_id": admin_id,
                        "email": email,
                        "name": name,
                        "company_role": "company_user",
                        "assigned_roles": roles_to_assign,  # Assign the determined roles
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }

                    await self.users.insert_one(user_doc)

                    # Add to successful users for role counting
                    successful_users.append(user_doc)

                    results["successful"].append({
                        "email": email,
                        "name": name,
                        "user_id": user_doc["user_id"],
                        "assigned_roles": roles_to_assign  # Include assigned roles in response
                    })

                except Exception as e:
                    results["failed"].append({
                        "email": email,
                        "error": str(e)
                    })

            # Update assigned_user_count for roles if there are successful users and roles to assign
            if successful_users and roles_to_assign:
                await self._update_role_user_counts(company_id, admin_id, roles_to_assign, len(successful_users))

            return results

        except Exception as e:
            logger.error(f"Error processing user upload file: {str(e)}")
            raise

    async def _get_roles_to_assign(self, company_id: str, admin_id: str, selected_role: str) -> list:
        """
        Determine which roles to assign based on the selected role logic:
        - "Alle rollen": Assign all roles created by this admin
        - "Zonder rol" or empty: Assign no roles (empty list)
        - Specific role: Validate and assign that specific role if created by this admin
        """
        
        # Case 1: No role or "Zonder rol" - assign empty roles
        if not selected_role or selected_role == "Zonder rol":
            print(f"DEBUG: No roles to assign for selected_role: {selected_role}")
            return []
        
        # Case 2: "Alle rollen" - get all roles created by this admin
        if selected_role == "Alle rollen":
            try:
                # Get all roles created by this admin for this company
                all_admin_roles = await self.roles.find({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id
                }).to_list(length=None)
                
                role_names = [role.get("name") for role in all_admin_roles if role.get("name")]
                print(f"DEBUG: Found {len(role_names)} roles for admin {admin_id}: {role_names}")
                return role_names
                
            except Exception as e:
                print(f"DEBUG: Error fetching all admin roles: {e}")
                return []
        
        # Case 3: Specific role - validate and assign if exists
        try:
            # Check if the specific role exists and was created by this admin
            existing_role = await self.roles.find_one({
                "company_id": company_id,
                "added_by_admin_id": admin_id,
                "name": selected_role
            })
            
            if existing_role:
                print(f"DEBUG: Valid role found: {selected_role}")
                return [selected_role]
            else:
                print(f"DEBUG: Role '{selected_role}' not found or not created by this admin")
                # You might want to handle this case differently - either raise error or proceed without role
                return []
                
        except Exception as e:
            print(f"DEBUG: Error validating specific role '{selected_role}': {e}")
            return []

    async def _update_role_user_counts(self, company_id: str, admin_id: str, roles: list, user_count: int):
        """
        Update the assigned_user_count for roles when users are assigned to them.
        More granular approach that handles missing roles gracefully.
        """
        try:
            print(f"DEBUG: Updating role user counts for {len(roles)} roles with {user_count} users")
            
            updated_roles = []
            failed_roles = []
            
            for role_name in roles:
                try:
                    # First check if the role exists
                    existing_role = await self.roles.find_one({
                        "company_id": company_id,
                        "added_by_admin_id": admin_id,
                        "name": role_name
                    })
                    
                    if not existing_role:
                        print(f"DEBUG: Role '{role_name}' not found, skipping count update")
                        failed_roles.append(role_name)
                        continue
                    
                    # Increment the assigned_user_count for the role
                    result = await self.roles.update_one(
                        {
                            "company_id": company_id,
                            "added_by_admin_id": admin_id,
                            "name": role_name
                        },
                        {
                            "$inc": {"assigned_user_count": user_count},
                            "$set": {"updated_at": datetime.utcnow()}
                        }
                    )
                    
                    if result.modified_count > 0:
                        print(f"DEBUG: Successfully updated assigned_user_count for role '{role_name}' by {user_count}")
                        updated_roles.append(role_name)
                    else:
                        print(f"DEBUG: Failed to update role '{role_name}'")
                        failed_roles.append(role_name)
                        
                except Exception as role_error:
                    print(f"DEBUG: Error updating role '{role_name}': {role_error}")
                    failed_roles.append(role_name)
            
            print(f"DEBUG: Role update summary - Updated: {len(updated_roles)}, Failed: {len(failed_roles)}")
            if failed_roles:
                print(f"DEBUG: Failed to update these roles: {failed_roles}")
                
        except Exception as e:
            print(f"DEBUG: Error in _update_role_user_counts: {e}")

    def _extract_emails_from_text(self, text_content):
        """Extract emails from plain text content"""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, text_content)
        
        # Remove duplicates
        seen = set()
        unique_emails = []
        for email in emails:
            if email not in seen:
                seen.add(email)
                unique_emails.append(email)
        
        return unique_emails


    # ---------------- Users ---------------- #
    async def add_user(self, company_id: str, name: str, email: str):
        if await self.users.find_one({"company_id": company_id, "email": email}):
            raise ValueError("User with this email already exists")

        user_doc = {
            "user_id": str(uuid.uuid4()),
            "company_id": company_id,
            "name": name,
            "email": email,
            "created_at": datetime.utcnow(),
        }
        await self.users.insert_one(user_doc)

        return {
            "id": user_doc["user_id"],
            "company_id": user_doc["company_id"],
            "name": user_doc["name"],
            "email": user_doc["email"],
            "documents": [],
        }

    async def delete_users(self, company_id: str, user_ids: list[str], admin_id: str = None) -> int:
        """
        Delete users and decrease role user counts.
        Only deletes users added by the specified admin_id.
        """
        try:
            # 1) First, get all users to be deleted to retrieve their assigned roles
            # Only get users that were added by this admin
            users_to_delete = await self.users.find({
                "company_id": company_id,
                "user_id": {"$in": user_ids},
                "added_by_admin_id": admin_id  # Only delete users added by this admin
            }).to_list(length=None)
            
            print(f"DEBUG: Found {len(users_to_delete)} users to delete")

            if not users_to_delete:
                return 0

            # 2) Delete users from MongoDB
            user_ids_to_delete = [u["user_id"] for u in users_to_delete]
            result = await self.users.delete_many({
                "company_id": company_id,
                "user_id": {"$in": user_ids_to_delete}
            })

            deleted_count = result.deleted_count
            
            if deleted_count > 0:
                # 3) Delete user documents
                await self.documents.delete_many({"user_id": {"$in": user_ids_to_delete}})
                
                # 4) Update role user counts if admin_id is provided
                if admin_id and users_to_delete:
                    await self._decrease_role_user_counts(company_id, admin_id, users_to_delete)

            return deleted_count

        except Exception as e:
            logger.error(f"Error in delete_users: {str(e)}")
            raise


    async def _decrease_role_user_counts(self, company_id: str, admin_id: str, deleted_users: list):
        """
        Decrease the assigned_user_count for roles when users with those roles are deleted.
        """
        try:
            # Count how many users had each role
            role_decrement_counts = {}
            
            for user in deleted_users:
                assigned_roles = user.get("assigned_roles", [])
                for role_name in assigned_roles:
                    if role_name in role_decrement_counts:
                        role_decrement_counts[role_name] += 1
                    else:
                        role_decrement_counts[role_name] = 1
            
            print(f"DEBUG: Role decrement counts: {role_decrement_counts}")
            
            # Update each role's count using $inc with negative value
            updated_roles = []
            failed_roles = []
            
            for role_name, decrement_count in role_decrement_counts.items():
                try:
                    # Use $inc with negative value to decrement
                    result = await self.roles.update_one(
                        {
                            "company_id": company_id,
                            "added_by_admin_id": admin_id,
                            "name": role_name
                        },
                        {
                            "$inc": {"assigned_user_count": -decrement_count},
                            "$set": {"updated_at": datetime.utcnow()}
                        }
                    )
                    
                    if result.modified_count > 0:
                        print(f"DEBUG: Successfully decreased assigned_user_count for role '{role_name}' by {decrement_count}")
                        updated_roles.append(role_name)
                    else:
                        print(f"DEBUG: Role '{role_name}' not found or count not updated")
                        failed_roles.append(role_name)
                        
                except Exception as role_error:
                    print(f"DEBUG: Error updating role '{role_name}': {role_error}")
                    failed_roles.append(role_name)
            
            print(f"DEBUG: Role decrement summary - Updated: {len(updated_roles)}, Failed: {len(failed_roles)}")
            if failed_roles:
                print(f"DEBUG: Failed to update these roles: {failed_roles}")
                
        except Exception as e:
            print(f"DEBUG: Error in _decrease_role_user_counts: {e}")
            # Don't raise the error to avoid failing the entire delete operation

    async def delete_users_by_admin(self, company_id: str, admin_id: str, kc_admin=None) -> int:
        """
        Delete all users added by a specific admin.
        Returns the number of users deleted.
        """
        try:
            # Get all users added by this admin
            admin_users = await self.users.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            }).to_list(length=None)
            
            user_emails = [user.get("email") for user in admin_users if user.get("email")]
            user_ids = [user.get("user_id") for user in admin_users if user.get("user_id")]
            
            print(f"DEBUG: Found {len(admin_users)} users added by admin {admin_id}")
            
            if not admin_users:
                return 0

            # Delete users from Keycloak if kc_admin is provided
            keycloak_deleted = 0
            if kc_admin:
                for user in admin_users:
                    email = user.get("email")
                    if email:
                        try:
                            kc_users = kc_admin.get_users(query={"email": email})
                            if kc_users:
                                keycloak_user_id = kc_users[0]["id"]
                                kc_admin.delete_user(keycloak_user_id)
                                keycloak_deleted += 1
                                print(f"DEBUG: Deleted Keycloak user: {email}")
                        except Exception as e:
                            print(f"DEBUG: Failed to delete Keycloak user {email}: {e}")

            # Delete user documents
            if user_ids:
                await self.documents.delete_many({"user_id": {"$in": user_ids}})

            # Delete users from MongoDB
            result = await self.users.delete_many({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} users added by admin {admin_id}")
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting users for admin {admin_id}: {str(e)}")
            print(f"DEBUG: Error in delete_users_by_admin: {e}")
            return 0

    async def get_all_users_created_by_admin_id(
        self,
        company_id: str,
        admin_id: str
    ) -> List[dict]:
        """
        Get all users and admins that can be managed by this admin:
        1. Users added by this admin (added_by_admin_id = admin_id)
        2. Admins added by this admin (added_by_admin_id = admin_id)
        3. Admins who have teamlid role assigned by this admin (from guest_access where created_by = admin_id)
        """
        try:
            users = []
            seen_user_ids = set()  # Track to avoid duplicates
            
            # 1. Get regular users added by this admin
            users_cursor = self.users.find({
                "company_id": company_id, 
                "added_by_admin_id": admin_id
            })
            
            async for usr in users_cursor:
                user_id = usr.get("user_id")
                if user_id in seen_user_ids:
                    continue
                seen_user_ids.add(user_id)
                
                user_name = usr.get("name")
                user_email = usr.get("email")
                users.append({
                    "id": user_id,
                    "name": user_name if user_name is not None else "",
                    "email": user_email if user_email is not None else "",
                    "roles": usr.get("assigned_roles", []),
                    "type": "user",
                    "added_by_admin_id": usr.get("added_by_admin_id"),
                    "created_at": usr.get("created_at"),
                    "updated_at": usr.get("updated_at")
                })
            
            # 2. Get admins added by this admin
            admins_cursor = self.admins.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            async for admin in admins_cursor:
                admin_user_id = admin.get("user_id")
                if admin_user_id in seen_user_ids:
                    continue
                seen_user_ids.add(admin_user_id)
                
                admin_name = admin.get("name")
                admin_email = admin.get("email")
                users.append({
                    "id": admin_user_id,
                    "name": admin_name if admin_name is not None else "",
                    "email": admin_email if admin_email is not None else "",
                    "roles": ["Beheerder"],  
                    "type": "admin",
                    "added_by_admin_id": admin.get("added_by_admin_id"),
                    "is_teamlid": admin.get("is_teamlid", False),
                    "created_at": admin.get("created_at"),
                    "updated_at": admin.get("updated_at")
                })
            
            # 3. Get admins who have teamlid role assigned by this admin
            # Check guest_access collection where created_by = admin_id
            teamlid_assignments = self.guest_access.find({
                "company_id": company_id,
                "created_by": admin_id,
                "is_active": True
            })
            
            async for assignment in teamlid_assignments:
                teamlid_user_id = assignment.get("guest_user_id")
                if teamlid_user_id in seen_user_ids:
                    # Already added, but mark as teamlid if not already marked
                    for user in users:
                        if user.get("id") == teamlid_user_id:
                            user["is_teamlid"] = True
                            user["teamlid_assigned_by"] = admin_id
                            break
                    continue
                
                # Find the admin record for this teamlid
                teamlid_admin = await self.admins.find_one({
                    "company_id": company_id,
                    "user_id": teamlid_user_id
                })
                
                if teamlid_admin:
                    seen_user_ids.add(teamlid_user_id)
                    admin_name = teamlid_admin.get("name")
                    admin_email = teamlid_admin.get("email")
                    users.append({
                        "id": teamlid_user_id,
                        "name": admin_name if admin_name is not None else "",
                        "email": admin_email if admin_email is not None else "",
                        "roles": ["Beheerder"],
                        "type": "admin",
                        "added_by_admin_id": teamlid_admin.get("added_by_admin_id"),
                        "is_teamlid": True,
                        "teamlid_assigned_by": admin_id,  # This admin assigned the teamlid role
                        "created_at": teamlid_admin.get("created_at"),
                        "updated_at": teamlid_admin.get("updated_at")
                    })
            
            def get_sort_key(user):
                type_weight = 0 if user.get("type") == "admin" else 1
                
                name = user.get("name")
                email = user.get("email")
                
                sort_name = ""
                if name:
                    sort_name = name.lower()
                elif email:
                    sort_name = email.lower()
                
                return (type_weight, sort_name)
            
            users.sort(key=get_sort_key)
            
            return users
            
        except Exception as e:
            print(f"Error in get_all_users_created_by_admin_id: {str(e)}")
            return []

    async def remove_teamlid_role(
        self,
        company_id: str,
        admin_id: str,  # The admin who wants to remove the teamlid role
        target_admin_id: str  # The admin whose teamlid role to remove
    ) -> bool:
        """
        Remove teamlid role from an admin.
        Only the admin who assigned the teamlid role can remove it.
        """
        # Check if this admin assigned the teamlid role to the target admin
        guest_entry = await self.guest_access.find_one({
            "company_id": company_id,
            "owner_admin_id": admin_id,  # The workspace owner
            "guest_user_id": target_admin_id,  # The teamlid
            "created_by": admin_id,  # Must be created by this admin
            "is_active": True
        })
        
        if not guest_entry:
            raise HTTPException(
                status_code=403,
                detail="You can only remove teamlid roles that you assigned."
            )
        
        # Deactivate the guest_access entry (soft delete)
        result = await self.guest_access.update_one(
            {
                "company_id": company_id,
                "owner_admin_id": admin_id,
                "guest_user_id": target_admin_id,
            },
            {
                "$set": {
                    "is_active": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        # Also update the admin document to remove teamlid status if this was their only teamlid assignment
        # Check if there are any other active teamlid assignments for this admin
        other_assignments = await self.guest_access.count_documents({
            "company_id": company_id,
            "guest_user_id": target_admin_id,
            "is_active": True
        })
        
        if other_assignments == 0:
            # No other teamlid assignments, remove teamlid status from admin document
            await self.admins.update_one(
                {
                    "company_id": company_id,
                    "user_id": target_admin_id
                },
                {
                    "$set": {
                        "is_teamlid": False,
                        "updated_at": datetime.utcnow()
                    },
                    "$unset": {
                        "teamlid_permissions": "",
                        "assigned_teamlid_by_id": "",
                        "assigned_teamlid_by_name": "",
                        "assigned_teamlid_at": ""
                    }
                }
            )
        
        return result.modified_count > 0

    # ---------------- Company Roles ---------------- #
    async def add_or_update_role(self, company_id: str, admin_id: str, role_name: str, folders: list[str], modules: list, action: str) -> dict:
        """Add or update a role based on the action parameter."""
        
        folders = [f.strip("/") for f in folders if f.strip()]

        existing_role = await self.roles.find_one({
            "company_id": company_id,
            "added_by_admin_id": admin_id,
            "name": role_name
        })

        if action == "create" and existing_role:
            return {
                "status": "error",
                "error_type": "duplicate_role",
                "message": f"Role '{role_name}' already exists",
                "company_id": company_id,
                "role_name": role_name
            }

        modules_dict = None
        if modules is not None:
            if isinstance(modules, list):
                modules_dict = {}
                for module in modules:
                    if isinstance(module, dict):
                        module_name = module.get('name')
                        if module_name:
                            module_data = {k: v for k, v in module.items() if k != 'name'}
                            modules_dict[module_name] = module_data
                    elif hasattr(module, 'dict'):  
                        module_dict = module.dict()
                        module_name = module_dict.get('name')
                        if module_name:
                            module_data = {k: v for k, v in module_dict.items() if k != 'name'}
                            modules_dict[module_name] = module_data
            else:
                modules_dict = modules

        if existing_role:
            update_data = {
                "folders": folders,
                "updated_at": datetime.utcnow()
            }
            
            if modules_dict is not None:
                update_data["modules"] = modules_dict
            
            await self.roles.update_one(
                {"_id": existing_role["_id"]},
                {"$set": update_data}
            )
            status = "role_updated"
            updated_folders = folders
            final_modules = modules_dict if modules_dict is not None else existing_role.get("modules", {})
        else:
            role_data = {
                "company_id": company_id,
                "name": role_name,
                "added_by_admin_id": admin_id,
                "folders": folders,
                "assigned_user_count": 0,
                "document_count": 0,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            
            if modules_dict is not None:
                role_data["modules"] = modules_dict
            
            await self.roles.insert_one(role_data)
            updated_folders = folders
            status = "role_created"
            final_modules = modules_dict if modules_dict is not None else {}

        base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id)
        for folder in updated_folders:
            full_path = os.path.join(base_path, folder)
            os.makedirs(full_path, exist_ok=True)

        return {
            "status": status,
            "company_id": company_id,
            "role_name": role_name,
            "folders": updated_folders,
            "modules": final_modules
        }

    async def list_roles(self, company_id: str, admin_id: str) -> list[dict]:
        """List all roles for a given company."""
        cursor = self.roles.find({"company_id": company_id, "added_by_admin_id": admin_id})
        roles = await cursor.to_list(length=None)
        
        result = []
        
        for r in roles:
            role_folders = r.get("folders", [])
            
            if role_folders:
                folder_cursor = self.folders.find({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": {"$in": role_folders}
                })
                
                folders_data = await folder_cursor.to_list(length=None)
                
                total_document_count = sum(
                    folder.get("document_count", 0) 
                    for folder in folders_data
                )
            else:
                total_document_count = 0
            
            result.append({
                "name": r.get("name"),
                "folders": role_folders,
                "user_count": r.get("assigned_user_count", 0),
                "document_count": total_document_count,
                "modules": r.get("modules", [])
            })
        
        return result

    async def delete_roles(self, company_id: str, role_names: List[str], admin_id: str) -> dict:
        """Delete one or multiple roles by name, remove them from users' assigned_roles, and delete related documents/folders."""

        deleted_roles = []
        total_users_updated = 0
        total_documents_deleted = 0

        for role_name in role_names:
            # --- Verify role exists ---
            role = await self.roles.find_one({"company_id": company_id, "added_by_admin_id": admin_id, "name": role_name})
            if not role:
                # Skip if role doesn't exist, but continue with others
                continue

            # --- Delete all documents uploaded by admin for this role ---
            delete_docs_result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": role_name
            })

            # --- Remove related folders ---
            base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name)
            if os.path.exists(base_path):
                for root, dirs, files in os.walk(base_path, topdown=False):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                        except Exception:
                            pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(root, d))
                        except Exception:
                            pass
                try:
                    os.rmdir(base_path)
                except Exception:
                    pass

            # --- Remove this role from all users' assigned_roles ---
            update_result = await self.users.update_many(
                {"company_id": company_id, "assigned_roles": role_name},
                {"$pull": {"assigned_roles": role_name}}
            )

            # --- Delete the role itself ---
            await self.roles.delete_one({"_id": role["_id"]})

            # --- Track results for this role ---
            deleted_roles.append(role_name)
            total_users_updated += update_result.modified_count
            total_documents_deleted += delete_docs_result.deleted_count

        if not deleted_roles:
            raise HTTPException(status_code=404, detail="No valid roles found to delete")

        # --- Return cleanup result ---
        return {
            "status": "deleted",
            "deleted_roles": deleted_roles,
            "total_users_updated": total_users_updated,
            "total_documents_deleted": total_documents_deleted
        }

    async def assign_role_to_user(self, company_id: str, user_id: str, role_name: str) -> dict:
        """
        Assign a role to a company user (by user_id).
        Adds the role to 'assigned_roles' array (no duplicates)
        and increments the role's assigned_user_count.
        """

        # --- Verify role exists ---
        role = await self.roles.find_one({"company_id": company_id, "name": role_name})
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_name}' not found")

        # --- Verify company user exists ---
        user = await self.users.find_one({"user_id": user_id, "company_id": company_id})
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found in this company")

        # --- Prepare assigned roles ---
        assigned_roles = user.get("assigned_roles", [])
        if role_name not in assigned_roles:
            assigned_roles.append(role_name)

            # Update user with the new role list
            await self.users.update_one(
                {"user_id": user_id, "company_id": company_id},
                {
                    "$set": {
                        "assigned_roles": assigned_roles,
                        "updated_at": datetime.utcnow()
                    }
                }
            )

            # Increment the count for the role
            await self.roles.update_one(
                {"_id": role["_id"]},
                {"$inc": {"assigned_user_count": 1}}
            )

            status = "role_assigned"
        else:
            status = "role_already_assigned"

        return {
            "status": status,
            "company_id": company_id,
            "user_id": user_id,
            "assigned_roles": assigned_roles
        }

    async def upload_document_for_folder(
        self,
        company_id: str,
        admin_id: str,
        folder_name: str,
        file: UploadFile
    ) -> dict:

        file_name = (file.filename or "").strip()
        
        # CRITICAL: Extract ONLY the file name, removing any folder path
        # When uploading from a folder, file_name might contain the folder path (e.g., "roro/file.pdf")
        # We need to extract just the file name (e.g., "file.pdf")
        if "/" in file_name or "\\" in file_name:
            # File name contains a path, extract just the filename
            file_name = os.path.basename(file_name)
        
        # Remove any remaining path separators (shouldn't be any, but safety first)
        file_name = file_name.replace("/", "").replace("\\", "").strip()
        
        # CRITICAL: The folder_name should already be normalized from API endpoint
        # But we need to be absolutely sure it's just a folder name, not a path
        import re
        
        # Strip and get only the last component if it contains path separators
        safe_folder = folder_name.strip()
        
        # Split by any path separator and take the LAST non-empty part
        parts = re.split(r'[/\\]+', safe_folder)
        non_empty_parts = [p.strip() for p in parts if p.strip()]
        
        if non_empty_parts:
            # Take only the last part
            safe_folder = non_empty_parts[-1]
        else:
            safe_folder = safe_folder.strip()
        
        # Remove any remaining path separators (shouldn't be any, but safety first)
        safe_folder = safe_folder.replace("/", "").replace("\\", "").strip()

        if not file_name:
            raise HTTPException(status_code=400, detail="Missing file name")

        if not safe_folder:
            raise HTTPException(status_code=400, detail="Invalid folder name")
        
        # CRITICAL CHECK: Verify folder name doesn't contain itself (like "folder/folder")
        # This would indicate a duplication issue
        if "/" in safe_folder or "\\" in safe_folder:
            logger.error(f"ERROR: Folder name '{safe_folder}' still contains path separators after normalization!")
            # Force extract just the last component
            safe_folder = os.path.basename(safe_folder).replace("/", "").replace("\\", "").strip()
            if not safe_folder:
                raise HTTPException(status_code=400, detail="Invalid folder name: could not extract valid name")
        
        # Log the folder name to debug any duplication issues
        logger.info(f"Repository: received folder_name='{folder_name}', extracted safe_folder='{safe_folder}'")

        # Find the existing folder in database (should already exist from add_folders)
        # Try to find folder by the normalized name first
        folder_exists = await self.folders.find_one({
            "company_id": company_id,
            "admin_id": admin_id,
            "name": safe_folder
        })

        # If not found, try case-insensitive search (folders might have been created with different casing)
        if not folder_exists:
            all_folders = await self.folders.find({
                "company_id": company_id,
                "admin_id": admin_id
            }).to_list(length=None)
            
            # Find folder by case-insensitive match
            for folder in all_folders:
                if folder.get("name", "").lower() == safe_folder.lower():
                    folder_exists = folder
                    break

        if not folder_exists:
            # Folder doesn't exist, create it as fallback
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
                # Folder was created by another request, fetch it
                logger.info(f"Folder '{safe_folder}' was created by another request")
                folder_exists = await self.folders.find_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": safe_folder
                })
        
        # CRITICAL: Use the EXACT folder name from database - this is the source of truth
        # This ensures we use the same name that was stored, preventing any path duplication
        if folder_exists:
            actual_folder_name = folder_exists["name"]
        else:
            actual_folder_name = safe_folder
            logger.warning(f"Using safe_folder as fallback: '{actual_folder_name}'")
        
        # CRITICAL: Use the EXACT folder name from database - no more normalization needed
        # The folder name from database is the source of truth and should be used as-is
        # This prevents any duplication issues from multiple normalizations
        # This is the key difference: manual upload uses folder from dropdown (already in DB),
        # folder upload creates folder first, then we use that exact name from DB
        final_folder_name = actual_folder_name.strip()
        
        # Only remove path separators if they somehow exist (shouldn't happen, but safety first)
        if "/" in final_folder_name or "\\" in final_folder_name:
            logger.warning(f"WARNING: Folder name from database contains path separators: '{final_folder_name}'")
            final_folder_name = final_folder_name.replace("/", "").replace("\\", "").strip()
        
        if not final_folder_name:
            raise HTTPException(status_code=400, detail="Invalid folder name: empty after extraction from database")
        
        logger.info(f"Using EXACT folder name from database: '{final_folder_name}' (original: '{folder_name}', safe_folder: '{safe_folder}', actual_folder_name: '{actual_folder_name}')")
        
        path_list = []
        
        # Add base path components (these are guaranteed safe)
        if UPLOAD_ROOT:
            path_list.append(UPLOAD_ROOT)
        path_list.append("roleBased")
        path_list.append(company_id)
        path_list.append(admin_id)
        
        # CRITICAL: Verify final_folder_name is NOT already in the path components
        if final_folder_name in path_list:
            logger.error(f"ERROR: Folder name '{final_folder_name}' already in path components: {path_list}")
            # Remove it and rebuild
            path_list = [p for p in path_list if p != final_folder_name]
            # Re-add base components to be safe
            path_list = [UPLOAD_ROOT, "roleBased", company_id, admin_id]
            logger.error(f"  Rebuilt path_list: {path_list}")
        
        # CRITICAL: Check if folder name is already in path_list before adding
        if final_folder_name in path_list:
            logger.error(f"CRITICAL: Folder name '{final_folder_name}' already in path_list before adding!")
            logger.error(f"  path_list: {path_list}")
            # Remove all occurrences
            path_list = [p for p in path_list if p != final_folder_name]
            logger.error(f"  path_list after removal: {path_list}")
        
        # Add folder name ONCE (this is the ONLY place we add it)
        path_list.append(final_folder_name)
        
        # CRITICAL: Verify path_list doesn't have duplicates AFTER adding
        if path_list.count(final_folder_name) != 1:
            logger.error(f"CRITICAL: path_list has folder name {path_list.count(final_folder_name)} times after adding!")
            logger.error(f"  path_list: {path_list}")
            # Remove all occurrences and re-add once
            path_list = [p for p in path_list if p != final_folder_name]
            path_list = [UPLOAD_ROOT, "roleBased", company_id, admin_id, final_folder_name]
            logger.error(f"  Fixed path_list: {path_list}")
        
        # Build the path by joining the list manually
        # CRITICAL: Don't use os.path.normpath as it might cause issues with folder names
        base_path = os.sep.join(path_list)
        
        # Clean up any double separators manually
        while f"{os.sep}{os.sep}" in base_path:
            base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
        
        # VERIFY: Check that folder name appears exactly ONCE
        path_parts = [p for p in base_path.split(os.sep) if p]
        folder_count = path_parts.count(final_folder_name)
        
        if folder_count != 1:
            logger.error(f"CRITICAL: Folder name '{final_folder_name}' appears {folder_count} times!")
            logger.error(f"  Path list: {path_list}")
            logger.error(f"  Path parts: {path_parts}")
            logger.error(f"  base_path: '{base_path}'")
            
            # Force correction: find admin_id and rebuild manually
            admin_idx = None
            for i, part in enumerate(path_parts):
                if part == admin_id:
                    admin_idx = i
                    break
            
            if admin_idx is not None:
                # Rebuild: up to admin_id, then folder name ONCE
                corrected = path_parts[:admin_idx + 1] + [final_folder_name]
                base_path = os.sep.join(corrected)
                # Clean double separators
                while f"{os.sep}{os.sep}" in base_path:
                    base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
                
                # Final check
                verify = [p for p in base_path.split(os.sep) if p]
                if verify.count(final_folder_name) != 1:
                    # Absolute last resort: manual string construction
                    base_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}"
                    # Clean double separators
                    while f"{os.sep}{os.sep}" in base_path:
                        base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
            else:
                # Manual build
                base_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}"
                # Clean double separators
                while f"{os.sep}{os.sep}" in base_path:
                    base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
        
        # Final verification before using the path
        final_check_parts = [p for p in base_path.split(os.sep) if p]
        final_folder_count = final_check_parts.count(final_folder_name)
        
        if final_folder_count != 1:
            logger.error(f"CRITICAL: After all corrections, folder name still appears {final_folder_count} times!")
            logger.error(f"  base_path: '{base_path}'")
            logger.error(f"  final_folder_name: '{final_folder_name}'")
            logger.error(f"  final_check_parts: {final_check_parts}")
            
            # Force rebuild: find admin_id and rebuild manually
            admin_idx = None
            for i, part in enumerate(final_check_parts):
                if part == admin_id:
                    admin_idx = i
                    break
            
            if admin_idx is not None:
                # Rebuild manually: everything up to admin_id, then folder name ONCE
                corrected_parts = final_check_parts[:admin_idx + 1] + [final_folder_name]
                base_path = os.sep.join(corrected_parts)
                # Clean double separators
                while f"{os.sep}{os.sep}" in base_path:
                    base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
                logger.error(f"  FORCE REBUILT base_path: '{base_path}'")
                
                # Verify the rebuild worked
                verify_parts = [p for p in base_path.split(os.sep) if p]
                if verify_parts.count(final_folder_name) != 1:
                    # Absolute last resort: manual string construction
                    base_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}"
                    while f"{os.sep}{os.sep}" in base_path:
                        base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
            else:
                # Manual build
                base_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}"
                while f"{os.sep}{os.sep}" in base_path:
                    base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
        
        # Final check before creating directory
        verify_final = [p for p in base_path.split(os.sep) if p]
        if verify_final.count(final_folder_name) != 1:
            logger.error(f"CRITICAL ERROR: base_path verification failed! Folder name appears {verify_final.count(final_folder_name)} times")
            logger.error(f"  base_path: '{base_path}'")
            logger.error(f"  verify_final: {verify_final}")
            raise HTTPException(status_code=500, detail=f"Path construction error: duplicate folder name in base path")
        
        # CRITICAL: One more verification before creating directory
        # Split and check one more time to be absolutely sure
        pre_makedirs_parts = [p for p in base_path.split(os.sep) if p]
        pre_makedirs_count = pre_makedirs_parts.count(final_folder_name)
        
        if pre_makedirs_count != 1:
            logger.error(f"CRITICAL: base_path STILL has duplication before makedirs! Count: {pre_makedirs_count}")
            logger.error(f"  base_path: '{base_path}'")
            logger.error(f"  pre_makedirs_parts: {pre_makedirs_parts}")
            
            # Force rebuild one more time
            admin_idx = None
            for i, part in enumerate(pre_makedirs_parts):
                if part == admin_id:
                    admin_idx = i
                    break
            
            if admin_idx is not None:
                base_path = os.sep.join(pre_makedirs_parts[:admin_idx + 1] + [final_folder_name])
                while f"{os.sep}{os.sep}" in base_path:
                    base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
            else:
                base_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}"
                while f"{os.sep}{os.sep}" in base_path:
                    base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
        
        # Final verification of base_path before using it
        # CRITICAL: Split base_path and verify it has folder name exactly once
        # Note: We preserve the absolute path nature by checking if base_path starts with /
        is_absolute = base_path.startswith(os.sep)
        base_path_parts_final = [p for p in base_path.split(os.sep) if p]
        base_path_folder_count = base_path_parts_final.count(final_folder_name)
        
        if base_path_folder_count != 1:
            logger.error(f"CRITICAL: base_path has wrong folder count before file_path construction: {base_path_folder_count}")
            logger.error(f"  base_path: '{base_path}'")
            logger.error(f"  base_path_parts_final: {base_path_parts_final}")
            # Force rebuild base_path from scratch
            # CRITICAL: Build absolute path properly - UPLOAD_ROOT is already absolute
            base_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}"
            while f"{os.sep}{os.sep}" in base_path:
                base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
            # Re-verify
            base_path_parts_final = [p for p in base_path.split(os.sep) if p]
            if base_path_parts_final.count(final_folder_name) != 1:
                raise HTTPException(status_code=500, detail=f"Path construction error: duplicate folder name in base path")
        
        os.makedirs(base_path, exist_ok=True)

        # Build file path: base_path already contains folder name, just add file_name
        # CRITICAL: Ensure file_name doesn't contain any path (should already be extracted above)
        # Double-check: if file_name still contains path separators, extract just the basename
        clean_file_name = file_name
        if "/" in clean_file_name or "\\" in clean_file_name:
            clean_file_name = os.path.basename(clean_file_name)
            clean_file_name = clean_file_name.replace("/", "").replace("\\", "").strip()
        
        # Build from verified base_path_parts to ensure no duplication
        # CRITICAL: Ensure absolute path - if base_path starts with /, file_path should too
        # When we split base_path and filter empty strings, we lose the leading /
        # So we need to restore it for file_path
        file_path_parts_clean = base_path_parts_final + [clean_file_name]
        file_path = os.sep.join(file_path_parts_clean)
        # Ensure absolute path if base_path is absolute
        if is_absolute and not file_path.startswith(os.sep):
            file_path = os.sep + file_path
        
        logger.info(f"file_path_parts_clean before join: {file_path_parts_clean}")
        logger.info(f"  final_folder_name count in file_path_parts_clean: {file_path_parts_clean.count(final_folder_name)}")
        
        # Clean up any double separators
        while f"{os.sep}{os.sep}" in file_path:
            file_path = file_path.replace(f"{os.sep}{os.sep}", os.sep)
        
        # Final verification - check file path
        file_path_parts = [p for p in file_path.split(os.sep) if p]
        folder_name_count_in_file_path = file_path_parts.count(final_folder_name)
        
        if folder_name_count_in_file_path != 1:
            logger.error(f"CRITICAL ERROR: File path has incorrect folder name count: {folder_name_count_in_file_path}")
            logger.error(f"  file_path: '{file_path}'")
            logger.error(f"  file_path_parts: {file_path_parts}")
            logger.error(f"  base_path: '{base_path}'")
            logger.error(f"  base_path_parts_final: {base_path_parts_final}")
            
            # CRITICAL: Rebuild from verified base_path_parts, NOT from corrupted file_path_parts
            # The base_path_parts_final is verified to have folder name exactly once
            file_path_parts_clean = base_path_parts_final + [clean_file_name]
            file_path = os.sep.join(file_path_parts_clean)
            # CRITICAL: Ensure absolute path if base_path is absolute
            # When we split base_path and filter empty strings, we lose the leading /
            # So we need to restore it for file_path
            if is_absolute and not file_path.startswith(os.sep):
                file_path = os.sep + file_path
            
            # Clean double separators
            while f"{os.sep}{os.sep}" in file_path:
                file_path = file_path.replace(f"{os.sep}{os.sep}", os.sep)
            
            
            # Final verification
            verify_parts = [p for p in file_path.split(os.sep) if p]
            verify_count = verify_parts.count(final_folder_name)
            
            if verify_count != 1:
                logger.error(f"CRITICAL: Rebuild from base_path_parts still has wrong count: {verify_count}")
                logger.error(f"  verify_parts: {verify_parts}")
                logger.error(f"  base_path_parts_final: {base_path_parts_final}")
                logger.error(f"  file_name: '{file_name}'")
                
                # Absolute last resort: build completely from scratch using known good values
                file_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}{os.sep}{clean_file_name}"
                while f"{os.sep}{os.sep}" in file_path:
                    file_path = file_path.replace(f"{os.sep}{os.sep}", os.sep)
                base_path = f"{UPLOAD_ROOT}{os.sep}roleBased{os.sep}{company_id}{os.sep}{admin_id}{os.sep}{final_folder_name}"
                while f"{os.sep}{os.sep}" in base_path:
                    base_path = base_path.replace(f"{os.sep}{os.sep}", os.sep)
                os.makedirs(base_path, exist_ok=True)
                
                # Final verification
                final_verify = [p for p in file_path.split(os.sep) if p]
                if final_verify.count(final_folder_name) != 1:
                    logger.error(f"CRITICAL: File path still incorrect after absolute rebuild!")
                    logger.error(f"  final_verify: {final_verify}")
                    raise HTTPException(status_code=500, detail=f"Path construction error: duplicate folder name in file path")

        # Check for existing document (check by file_name and upload_type)
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

        # Get storage provider and folder's storage_path for Nextcloud upload
        storage_provider = None
        storage_path = None
        if get_storage_provider:
            try:
                storage_provider = get_storage_provider()
                # Get folder's storage_path from database
                if folder_exists and folder_exists.get("storage_path"):
                    folder_storage_path = folder_exists["storage_path"]
                    # Build storage path for the file: folder_storage_path/file_name
                    storage_path = f"{folder_storage_path}/{file_name}"
                    logger.info(f"Uploading to Nextcloud: {storage_path}")
                elif storage_provider:
                    # Folder doesn't have storage_path (e.g., created before Nextcloud integration)
                    # Create one based on company_id/admin_id/folder_name and update folder record
                    folder_storage_path = f"{company_id}/{admin_id}/{final_folder_name}"
                    storage_path = f"{folder_storage_path}/{file_name}"
                    logger.info(f"Folder has no storage_path, using default: {storage_path}")
                    
                    # Update folder record with storage_path for future uploads
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
                        logger.info(f"Updated folder '{final_folder_name}' with storage_path: {folder_storage_path}")
            except Exception as e:
                logger.warning(f"Storage provider not available: {e}, falling back to local storage")
                storage_provider = None

        # Read file content once
        await file.seek(0)  # Reset file pointer
        content = await file.read()
        
        # Upload to Nextcloud if storage provider is available
        if storage_provider and storage_path:
            try:
                # Ensure folder exists in Nextcloud
                folder_storage_path = storage_path.rsplit("/", 1)[0]
                if not await storage_provider.folder_exists(folder_storage_path):
                    await storage_provider.create_folder(folder_storage_path)
                    logger.info(f"Created Nextcloud folder: {folder_storage_path}")
                
                # Upload file to Nextcloud
                # upload_file can handle bytes directly
                await storage_provider.upload_file(storage_path, content, content_length=len(content) if isinstance(content, bytes) else None)
                logger.info(f"Successfully uploaded to Nextcloud: {storage_path}")
            except StorageError as e:
                logger.error(f"Failed to upload to Nextcloud: {e}, falling back to local storage")
                storage_path = None  # Don't save storage_path if upload failed
            except Exception as e:
                logger.error(f"Unexpected error uploading to Nextcloud: {e}, falling back to local storage")
                storage_path = None

        # Save to local disk for backward compatibility and RAG indexing
        # (RAG service may need local file access)
        try:
            os.makedirs(base_path, exist_ok=True)
            async with aiofiles.open(file_path, "wb") as f:
                await f.write(content)
        except Exception as e:
            logger.error(f"Failed to save file locally: {e}")
            # If Nextcloud upload succeeded, we can continue; otherwise fail
            if not storage_path:
                raise HTTPException(status_code=500, detail=f"File save failed: {str(e)}")

        # Use DocumentRepository to add document with storage_path
        from app.repositories.document_repo import DocumentRepository
        document_repo = DocumentRepository(self.db)
        
        doc_record = await document_repo.add_document(
            company_id=company_id,
            user_id=admin_id,
            file_name=file_name,
            upload_type=safe_folder,
            path=file_path,  # Keep local path for backward compatibility
            storage_path=storage_path,  # Nextcloud path
            source="manual_upload"
        )

        if not doc_record:
            # Document already exists (race condition)
            raise HTTPException(
                status_code=409,
                detail=f"Het document '{file_name}' bestaat al in de map '{safe_folder}'."
            )

        # Update folder document count - check if folder exists first
        folder_update_result = await self.folders.update_one(
            {"company_id": company_id, "admin_id": admin_id, "name": safe_folder},
            {"$inc": {"document_count": 1}}
        )
        
        # If folder doesn't exist, log a warning but don't fail the upload
        if folder_update_result.matched_count == 0:
            logger.warning(
                f"Folder '{safe_folder}' not found when updating document count. "
                f"Company: {company_id}, Admin: {admin_id}. "
                f"Document was uploaded successfully but folder count was not updated."
        )

        return {
            "success": True,
            "status": "uploaded",
            "folder": safe_folder,
            "file_name": file_name,
            "path": file_path,  # Local path for backward compatibility
            "storage_path": storage_path,  # Nextcloud path if uploaded
        }

    async def sync_documents_from_nextcloud(
        self,
        company_id: str,
        admin_id: str,
        folder_id: Optional[str] = None
    ) -> dict:
        """
        Sync documents from Nextcloud to DAVI for imported folders.
        
        Only syncs folders with origin="imported" (folders imported from Nextcloud).
        When a user uploads a document to an imported folder in Nextcloud, this method
        syncs those documents to DAVI.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            folder_id: Optional specific folder ID to sync (if None, syncs all imported folders)
            
        Returns:
            Dictionary with sync results
        """
        if not get_storage_provider:
            raise HTTPException(
                status_code=400,
                detail="Storage provider not available"
            )
        
        try:
            storage_provider = get_storage_provider()
        except StorageError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Nextcloud storage not configured: {e}"
            )
        
        # Find imported folders
        folder_query = {
            "company_id": company_id,
            "admin_id": admin_id,
            "origin": "imported",  # Only sync imported folders
            "storage_path": {"$exists": True, "$ne": None}  # Must have storage_path
        }
        
        if folder_id:
            folder_query["_id"] = folder_id
        
        imported_folders = await self.folders.find(folder_query).to_list(length=None)
        
        if not imported_folders:
            return {
                "success": True,
                "synced_folders": 0,
                "new_documents": 0,
                "skipped_documents": 0,
                "errors": [],
                "message": "No imported folders found to sync"
            }
        
        synced_folders = 0
        new_documents = 0
        skipped_documents = 0
        errors = []
        
        from app.repositories.document_repo import DocumentRepository
        document_repo = DocumentRepository(self.db)
        
        for folder in imported_folders:
            folder_name = folder.get("name")
            storage_path = folder.get("storage_path")
            
            if not storage_path:
                errors.append(f"Folder '{folder_name}' has no storage_path")
                continue
            
            try:
                # List files in Nextcloud folder
                nextcloud_files = await storage_provider.list_files(storage_path, recursive=False)
                
                if not nextcloud_files:
                    logger.info(f"No files found in Nextcloud folder: {storage_path}")
                    continue
                
                # Get existing documents for this folder
                existing_docs = await self.documents.find({
                    "company_id": company_id,
                    "user_id": admin_id,
                    "upload_type": folder_name
                }).to_list(length=None)
                
                existing_file_names = {doc.get("file_name") for doc in existing_docs if doc.get("file_name")}
                
                # Process each file from Nextcloud
                for file_info in nextcloud_files:
                    file_name = file_info["name"]
                    file_storage_path = file_info["path"]
                    
                    # Skip if document already exists
                    if file_name in existing_file_names:
                        skipped_documents += 1
                        continue
                    
                    try:
                        # Download file from Nextcloud for local storage (RAG indexing needs local files)
                        file_content = await storage_provider.download_file(file_storage_path)
                        file_bytes = file_content.read() if hasattr(file_content, 'read') else file_content
                        
                        # Save file locally for RAG indexing
                        # Use same path structure as upload_document_for_folder
                        local_base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, folder_name)
                        os.makedirs(local_base_path, exist_ok=True)
                        local_file_path = os.path.join(local_base_path, file_name)
                        
                        async with aiofiles.open(local_file_path, "wb") as f:
                            await f.write(file_bytes)
                        
                        # Create document record
                        doc_record = await document_repo.add_document(
                            company_id=company_id,
                            user_id=admin_id,
                            file_name=file_name,
                            upload_type=folder_name,
                            path=local_file_path,  # Local path for RAG
                            storage_path=file_storage_path,  # Nextcloud path
                            source="nextcloud_sync"  # Mark as synced from Nextcloud
                        )
                        
                        if doc_record:
                            new_documents += 1
                            logger.info(f"Synced document from Nextcloud: {file_name} in folder {folder_name}")
                        else:
                            # Document already exists (race condition)
                            skipped_documents += 1
                            
                    except Exception as e:
                        error_msg = f"Failed to sync file '{file_name}' from folder '{folder_name}': {str(e)}"
                        logger.error(error_msg)
                        errors.append(error_msg)
                
                synced_folders += 1
                
                # Update folder document count
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
            "skipped_documents": skipped_documents,
            "errors": errors,
            "message": f"Synced {synced_folders} folder(s), added {new_documents} new document(s)"
        }

    async def delete_roles_by_admin(self, company_id: str, admin_id: str) -> int:
        """
        Delete all roles created by a specific admin.
        Returns the number of roles deleted.
        """
        try:
            # Get all roles created by this admin
            admin_roles = await self.roles.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            }).to_list(length=None)
            
            role_names = [role.get("name") for role in admin_roles if role.get("name")]
            print(f"DEBUG: Deleting {len(admin_roles)} roles created by admin {admin_id}: {role_names}")
            
            # Simply delete the roles - no need to update users since they're being deleted too
            result = await self.roles.delete_many({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} roles created by admin {admin_id}")
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting roles for admin {admin_id}: {str(e)}")
            print(f"DEBUG: Error in delete_roles_by_admin: {e}")
            return 0

    async def delete_role_documents_by_admin(self, company_id: str, admin_id: str) -> int:
        """
        Delete all role documents uploaded by a specific admin.
        This includes both database records and actual files from the filesystem.
        Returns the number of documents deleted.
        """
        try:
            # Get all role documents uploaded by this admin
            role_documents = await self.documents.find({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$exists": True}  # This indicates role-based documents
            }).to_list(length=None)
            
            print(f"DEBUG: Found {len(role_documents)} role documents uploaded by admin {admin_id}")
            
            if not role_documents:
                return 0

            # Delete actual files from filesystem
            files_deleted = 0
            for doc in role_documents:
                file_path = doc.get("path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        files_deleted += 1
                        print(f"DEBUG: Deleted file: {file_path}")
                        
                        # Also delete the directory if it's empty
                        directory = os.path.dirname(file_path)
                        if os.path.exists(directory) and not os.listdir(directory):
                            os.rmdir(directory)
                            print(f"DEBUG: Deleted empty directory: {directory}")
                            
                    except Exception as e:
                        print(f"DEBUG: Failed to delete file {file_path}: {e}")
            
            # Update document counts for roles
            role_doc_counts = {}
            for doc in role_documents:
                role_name = doc.get("upload_type")
                if role_name:
                    if role_name in role_doc_counts:
                        role_doc_counts[role_name] += 1
                    else:
                        role_doc_counts[role_name] = 1
            
            # Decrement document_count for each role
            for role_name, doc_count in role_doc_counts.items():
                try:
                    await self.roles.update_one(
                        {"company_id": company_id, "name": role_name},
                        {"$inc": {"document_count": -doc_count}}
                    )
                    print(f"DEBUG: Decreased document_count for role '{role_name}' by {doc_count}")
                except Exception as e:
                    print(f"DEBUG: Failed to update document_count for role '{role_name}': {e}")

            # Delete database records
            result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$exists": True}
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} role document records from database")
            print(f"DEBUG: Successfully deleted {files_deleted} actual files from filesystem")
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting role documents for admin {admin_id}: {str(e)}")
            print(f"DEBUG: Error in delete_role_documents_by_admin: {e}")
            return 0

    async def delete_company_role_documents(self, company_id: str) -> int:
        """
        Delete all role-based documents for a company.
        Simple delete - no need to update role counts since roles are being deleted.
        Returns the number of documents deleted.
        """
        try:
            # Get all role documents for this company
            role_documents = await self.documents.find({
                "company_id": company_id,
                "upload_type": {"$exists": True}  # This indicates role-based documents
            }).to_list(length=None)
            
            print(f"DEBUG: Found {len(role_documents)} role documents for company {company_id}")
            
            if not role_documents:
                return 0

            # Delete actual files from filesystem
            files_deleted = 0
            for doc in role_documents:
                file_path = doc.get("path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        files_deleted += 1
                        print(f"DEBUG: Deleted file: {file_path}")
                    except Exception as e:
                        print(f"DEBUG: Failed to delete file {file_path}: {e}")

            # Delete database records
            result = await self.documents.delete_many({
                "company_id": company_id,
                "upload_type": {"$exists": True}
            })
            
            print(f"DEBUG: Successfully deleted {result.deleted_count} role document records from database")
            print(f"DEBUG: Successfully deleted {files_deleted} actual role document files from filesystem")
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting role documents for company {company_id}: {str(e)}")
            print(f"DEBUG: Error in delete_company_role_documents: {e}")
            return 0

    # ---------------- Shared ---------------- #
    async def get_user_with_documents(self, email: str):
        user = await self.admins.find_one({"email": email})
        user_type = "admin"
        if not user:
            user = await self.users.find_one({"email": email})
            user_type = "company_user"

        if not user:
            return None

        user_id = user["user_id"]
        company_id = user["company_id"]

        # Get private documents (upload_type="document")
        private_docs_query = {
            "user_id": user_id,
            "company_id": company_id,
            "upload_type": "document"
        }
        private_docs_cursor = self.documents.find(private_docs_query)
        private_docs = [d async for d in private_docs_cursor]

        role_based_docs = []
        if user_type == "admin":
            # For company admins: get all documents in folders created by this admin
            # These have upload_type = folder_name (not "document")
            folder_docs_query = {
                "user_id": user_id,
                "company_id": company_id,
                "upload_type": {"$ne": "document"}
            }
            folder_docs_cursor = self.documents.find(folder_docs_query)
            role_based_docs = [d async for d in folder_docs_cursor]
        else:
            # For company users: get documents from folders assigned via roles
            assigned_roles = user.get("assigned_roles", [])
            added_by_admin_id = user.get("added_by_admin_id")

            if assigned_roles and added_by_admin_id:
                # Find roles with matching name, company_id, and added_by_admin_id
                roles_query = {
                    "company_id": company_id,
                    "added_by_admin_id": added_by_admin_id,
                    "name": {"$in": assigned_roles}
                }
                roles_cursor = self.roles.find(roles_query)
                roles = [r async for r in roles_cursor]
                
                # Collect all folder names from these roles
                folder_names = set()
                for role in roles:
                    folders = role.get("folders", [])
                    folder_names.update(folders)
                
                # Find documents with upload_type in folder names and user_id = added_by_admin_id
                if folder_names:
                    role_based_docs_query = {
                        "user_id": added_by_admin_id,
                        "company_id": company_id,
                        "upload_type": {"$in": list(folder_names)}
                    }
                    role_based_docs_cursor = self.documents.find(role_based_docs_query)
                    role_based_docs = [d async for d in role_based_docs_cursor]

        all_docs = private_docs + role_based_docs
        formatted_docs = [
            {
                "file_name": doc["file_name"],
                "upload_type": doc.get("upload_type", "document"),
                "path": doc.get("path", ""),
            }
            for doc in all_docs
        ]

        pass_ids = []
        for doc in formatted_docs:
            fn = doc["file_name"]
            upload_type = doc.get("upload_type", "document")

            if upload_type == "document":
                # Private documents: format is "user_id--filename"
                pid = f"{user_id}--{fn}"
            else:
                # Role-based/folder documents: format must match indexing format
                # When indexing, file_id is "company_id-admin_id" (see company_admin.py line 1678)
                # So pass_id should be "company_id-admin_id--filename"
                if user_type == "admin":
                    # For admin's own folder documents, admin_id is user_id
                    pid = f"{company_id}-{user_id}--{fn}"
                else:
                    # For company user's role-based documents, use added_by_admin_id
                    added_by_admin_id = user.get("added_by_admin_id")
                    if added_by_admin_id:
                        pid = f"{company_id}-{added_by_admin_id}--{fn}"
                    else:
                        # Fallback (shouldn't happen for role-based docs)
                        pid = f"{company_id}-{user_id}--{fn}"
            pass_ids.append(pid)

        return {
            "user_id": user_id,
            "company_id": company_id,
            "user_type": user_type,
            "documents": formatted_docs,
            "pass_ids": pass_ids,
        }

    async def get_all_user_documents(self, email: str):
        """
        Get all documents for a user (both private and role-based).
        
        For company admins:
        - Returns private documents (upload_type="document") 
        - Returns all documents in folders created by this admin (upload_type=folder_name)
        
        For company users:
        - Returns private documents (upload_type="document")
        - Returns documents from folders assigned via roles:
          1. Get user's assigned_roles and added_by_admin_id
          2. Find roles with matching name, company_id, and added_by_admin_id
          3. Get folders from those roles
          4. Find documents with upload_type in folder names and user_id = added_by_admin_id
        """
        # Check if user is admin or company user
        admin_user = await self.admins.find_one({"email": email})
        company_user = await self.users.find_one({"email": email})
        
        if not admin_user and not company_user:
            return {
                "documents": [],
                "user_type": "unknown"
            }
        
        formatted_documents = []
        
        if admin_user:
            # Company admin: get private documents + all documents in folders created by this admin
            admin_id = admin_user["user_id"]
            company_id = admin_user["company_id"]
            
            # Get private documents
            private_docs_query = {
                "user_id": admin_id,
                "company_id": company_id,
                "upload_type": "document"
            }
            private_docs_cursor = self.documents.find(private_docs_query)
            private_docs = [d async for d in private_docs_cursor]
            
            # Get all role-based documents (documents in folders created by this admin)
            # These have upload_type = folder_name (not "document")
            role_based_docs_query = {
                "user_id": admin_id,
                "company_id": company_id,
                "upload_type": {"$ne": "document"}
            }
            role_based_docs_cursor = self.documents.find(role_based_docs_query)
            role_based_docs = [d async for d in role_based_docs_cursor]
            
            # Combine and format
            all_docs = private_docs + role_based_docs
            for doc in all_docs:
                upload_type = doc.get("upload_type", "document")
                formatted_documents.append({
                    "file_name": doc.get("file_name", ""),
                    "upload_type": upload_type,
                    "path": doc.get("path", ""),
                    "is_private": upload_type == "document"
                })
            
            user_type = "admin"
            
        else:
            # Company user: get private documents + documents from assigned roles
            user_id = company_user["user_id"]
            company_id = company_user["company_id"]
            assigned_roles = company_user.get("assigned_roles", [])
            added_by_admin_id = company_user.get("added_by_admin_id")
            
            # Get private documents
            private_docs_query = {
                "user_id": user_id,
                "company_id": company_id,
                "upload_type": "document"
            }
            private_docs_cursor = self.documents.find(private_docs_query)
            private_docs = [d async for d in private_docs_cursor]
            
            # Get role-based documents
            role_based_docs = []
            if assigned_roles and added_by_admin_id:
                # Find roles with matching name, company_id, and added_by_admin_id
                roles_query = {
                    "company_id": company_id,
                    "added_by_admin_id": added_by_admin_id,
                    "name": {"$in": assigned_roles}
                }
                roles_cursor = self.roles.find(roles_query)
                roles = [r async for r in roles_cursor]
                
                # Collect all folder names from these roles
                folder_names = set()
                for role in roles:
                    folders = role.get("folders", [])
                    folder_names.update(folders)
                
                # Find documents with upload_type in folder names and user_id = added_by_admin_id
                if folder_names:
                    role_based_docs_query = {
                        "user_id": added_by_admin_id,
                        "company_id": company_id,
                        "upload_type": {"$in": list(folder_names)}
                    }
                    role_based_docs_cursor = self.documents.find(role_based_docs_query)
                    role_based_docs = [d async for d in role_based_docs_cursor]
            
            # Combine and format
            all_docs = private_docs + role_based_docs
            for doc in all_docs:
                upload_type = doc.get("upload_type", "document")
                formatted_documents.append({
                    "file_name": doc.get("file_name", ""),
                    "upload_type": upload_type,
                    "path": doc.get("path", ""),
                    "is_private": upload_type == "document"
                })
            
            user_type = "company_user"

        return {
            "documents": formatted_documents,
            "user_type": user_type
        }

    async def get_admin_with_documents_by_id(
        self,
        company_id: str,
        admin_user_id: str,
    ):
        """
        Returns the same structure as get_user_with_documents, but for a known admin user_id.
        """
        admin = await self.admins.find_one(
            {"company_id": company_id, "user_id": admin_user_id}
        )
        if not admin:
            return None

        email = admin["email"]
        return await self.get_user_with_documents(email)


    async def add_user_by_admin(self, company_id: str, added_by_admin_id: str, email: str, company_role: str, assigned_role: str):
        if await self.users.find_one({"company_id": company_id, "added_by_admin_id": added_by_admin_id, "email": email}):
            raise ValueError("User with this email already exists in this company")

        # Handle empty assigned_role
        assigned_roles_list = [assigned_role] if assigned_role and assigned_role.strip() else []

        user_doc = {
            "user_id": str(uuid.uuid4()),
            "company_id": company_id,
            "added_by_admin_id": added_by_admin_id,  
            "email": email, 
            "company_role": company_role,
            "assigned_roles": assigned_roles_list,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "name": None,  
        }
        await self.users.insert_one(user_doc)

        # Update role user count if a role was assigned
        if assigned_roles_list:
            await self._update_role_user_counts(company_id, added_by_admin_id, assigned_roles_list, 1)

        return {
            "user_id": user_doc["user_id"],
            "company_id": company_id,
            "email": email,
            "company_role": company_role,
            "added_by_admin_id": added_by_admin_id,
            "assigned_roles": user_doc["assigned_roles"],
            "name": None,
            "documents": [],
        }

    async def assign_teamlid_permissions(self, company_id: str, admin_id: str, email: str, permissions: dict) -> bool:
        """
        Assign teamlid permissions to a user for a specific admin's workspace.
        Supports multiple teamlid roles - each assignment creates a separate guest_access entry.
        """
        current_admin = await self.admins.find_one({
            "company_id": company_id,
            "user_id": admin_id
        })  

        current_admin_name = current_admin.get("name", "Een beheerder")

        target_in_admins = await self.admins.find_one({
            "company_id": company_id,
            "email": email
        })

        target_in_users = await self.users.find_one({
            "company_id": company_id,
            "email": email
        })

        target_user = target_in_admins or target_in_users
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        target_user_id = target_user["user_id"]
        
        # Determine which collection to update based on whether user is admin or regular user
        if target_in_admins:
            target_collection = self.admins
        else:
            target_collection = self.users

        # Update user document to mark as teamlid (for backward compatibility)
        # Note: This stores the LAST assignment, but we use guest_access for actual permissions
        update_result = await target_collection.update_one(
            {"company_id": company_id, "email": email},
            {
                "$set": {
                    "is_teamlid": True,
                    "teamlid_permissions": permissions,  # Keep for backward compatibility
                    "assigned_teamlid_by_id": admin_id,  # Keep for backward compatibility
                    "assigned_teamlid_by_name": current_admin_name,
                    "assigned_teamlid_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
            },
        )

        if update_result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to update user")

        # Create/update guest_access entry for this specific teamlid assignment
        # This allows multiple teamlid roles from different admins
        # Each assignment creates a separate entry, so multiple admins can assign teamlid roles
        
        # Helper to convert permission value to boolean (handles string "True"/"False")
        def to_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)
        
        role_folder = permissions.get("role_folder_modify_permission", False)
        user_modify = permissions.get("user_create_modify_permission", False)
        document_modify = permissions.get("document_modify_permission", False)
        
        # Map teamlid permissions to guest_access format
        # Note: Each call creates/updates a separate entry for this admin_id + target_user_id pair
        await self.upsert_guest_access(
            company_id=company_id,
            owner_admin_id=admin_id,  # The admin whose workspace this teamlid can access
            guest_user_id=target_user_id,
            can_role_write=to_bool(role_folder),
            can_user_write=to_bool(user_modify),  # Note: teamlid uses user_create_modify_permission
            can_document_write=to_bool(document_modify),
            can_folder_write=to_bool(role_folder),
            created_by=admin_id,
        )

        return True

    async def update_user(
        self, 
        company_id: str, 
        user_id: str, 
        user_type: str, 
        name: str, 
        email: str, 
        assigned_roles: list[str]
    ) -> bool:
        """Update user or admin information."""
        now = datetime.utcnow()

        if user_type == "admin":
            admin_doc = await self.admins.find_one({
                "company_id": company_id, 
                "user_id": user_id
            })
            
            if not admin_doc:
                raise HTTPException(status_code=404, detail="Admin not found")

            # Check if admin has Teamlid in assigned_roles and it's being removed
            # Note: For admins, Teamlid is not typically in assigned_roles, but we check is_teamlid flag
            # If the admin is a teamlid and we're updating them, we need to check if Teamlid should be removed
            # Since admins don't have assigned_roles in the same way, we check if is_teamlid should be cleared
            # This would typically be handled through the delete_role_from_user endpoint, but we handle it here too
            # for consistency when updating through WijzigenTab
            
            # If Teamlid is in the assigned_roles list and admin is a teamlid, remove teamlid status
            teamlid_should_be_removed = "Teamlid" not in assigned_roles and admin_doc.get("is_teamlid", False)
            
            update_op = {
                "$set": {
                    "name": name,
                    "email": email,
                    "updated_at": now,
                }
            }
            
            if teamlid_should_be_removed:
                update_op["$set"]["is_teamlid"] = False
                update_op["$unset"] = {
                    "teamlid_permissions": "",
                    "assigned_teamlid_by_id": "",
                    "assigned_teamlid_by_name": "",
                    "assigned_teamlid_at": ""
                }
                
                # Remove all guest_access entries for this admin
                await self.guest_access.delete_many({
                    "company_id": company_id,
                    "guest_user_id": user_id
                })

            await self.admins.update_one(
                {"company_id": company_id, "user_id": user_id},
                update_op,
            )
            
            return True

        else:
            user_doc = await self.users.find_one({
                "company_id": company_id, 
                "user_id": user_id
            })
            
            if not user_doc:
                raise HTTPException(status_code=404, detail="User not found")

            old_roles = set(user_doc.get("assigned_roles", []))
            new_roles = set(assigned_roles)

            added_roles = new_roles - old_roles
            removed_roles = old_roles - new_roles

            # Check if Teamlid role is being removed
            # Teamlid might not be in assigned_roles in DB, but is shown in UI based on is_teamlid flag
            # So we check: if user has is_teamlid=True AND Teamlid is not in new_roles, then it's being removed
            user_is_teamlid = user_doc.get("is_teamlid", False)
            teamlid_in_new_roles = "Teamlid" in new_roles
            teamlid_removed = user_is_teamlid and not teamlid_in_new_roles
            
            # Prepare update operation
            update_op = {
                "$set": {
                    "name": name,
                    "email": email,
                    "assigned_roles": list(new_roles),
                    "updated_at": now,
                }
            }
            
            # If Teamlid is being removed, clean up all teamlid-related data
            if teamlid_removed:
                update_op["$set"]["is_teamlid"] = False
                update_op["$unset"] = {
                    "teamlid_permissions": "",
                    "assigned_teamlid_by_id": "",
                    "assigned_teamlid_by_name": "",
                    "assigned_teamlid_at": ""
                }
                
                # Remove all guest_access entries for this user
                await self.guest_access.delete_many({
                    "company_id": company_id,
                    "guest_user_id": user_id
                })

            await self.users.update_one(
                {"company_id": company_id, "user_id": user_id},
                update_op,
            )

            if added_roles:
                # Don't update role counts for Teamlid (it's not a real role)
                real_added_roles = [r for r in added_roles if r != "Teamlid"]
                if real_added_roles:
                    await self.roles.update_many(
                        {"company_id": company_id, "name": {"$in": real_added_roles}},
                        {"$inc": {"assigned_user_count": 1}},
                    )

            if removed_roles:
                # Don't update role counts for Teamlid (it's not a real role)
                real_removed_roles = [r for r in removed_roles if r != "Teamlid"]
                if real_removed_roles:
                    await self.roles.update_many(
                        {"company_id": company_id, "name": {"$in": real_removed_roles}},
                        {"$inc": {"assigned_user_count": -1}},
                    )

            return True

    # ---------------- Guest Access ---------------- #
    async def upsert_guest_access(
        self,
        company_id: str,
        owner_admin_id: str,
        guest_user_id: str,
        can_role_write: bool,
        can_user_write: bool,
        can_document_write: bool,
        can_folder_write: bool,
        created_by: str,
    ) -> dict:
        """
        Create or update guest access for a given (owner_admin, guest_user) pair.
        """
        now = datetime.utcnow()
        doc = {
            "company_id": company_id,
            "owner_admin_id": owner_admin_id,
            "guest_user_id": guest_user_id,
            "can_role_write": can_role_write,
            "can_user_write": can_user_write,
            "can_document_write": can_document_write,
            "can_folder_write": can_folder_write,
            "is_active": True,
            "updated_at": now,
            "created_by": created_by,
        }

        await self.guest_access.update_one(
            {
                "company_id": company_id,
                "owner_admin_id": owner_admin_id,
                "guest_user_id": guest_user_id,
            },
            {
                "$set": doc,
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return doc

    async def list_guest_workspaces_for_user(
        self,
        company_id: str,
        guest_user_id: str,
    ) -> list[dict]:
        """
        Return all active guest-access entries for given user in a company.
        Automatically migrates old field names to new ones.
        """
        cursor = self.guest_access.find(
            {
                "company_id": company_id,
                "guest_user_id": guest_user_id,
                "is_active": True,
            }
        )
        
        entries = []
        async for doc in cursor:
            # Migrate each entry if needed
            needs_migration = False
            update_fields = {}
            
            if "can_user_read" in doc and "can_user_write" not in doc:
                update_fields["can_user_write"] = doc["can_user_read"]
                needs_migration = True
            
            if "can_document_read" in doc and "can_document_write" not in doc:
                update_fields["can_document_write"] = doc["can_document_read"]
                needs_migration = True
            
            if needs_migration:
                unset_fields = {}
                if "can_user_read" in doc:
                    unset_fields["can_user_read"] = ""
                if "can_document_read" in doc:
                    unset_fields["can_document_read"] = ""
                
                update_doc = {
                    "$set": {
                        **update_fields,
                        "updated_at": datetime.utcnow(),
                    }
                }
                if unset_fields:
                    update_doc["$unset"] = unset_fields
                
                await self.guest_access.update_one(
                    {"_id": doc["_id"]},
                    update_doc
                )
                
                # Update the doc dict to reflect changes
                doc.update(update_fields)
                if unset_fields:
                    for field in unset_fields.keys():
                        doc.pop(field, None)
            
            entries.append(doc)
        
        return entries

    async def get_guest_access(
        self,
        company_id: str,
        guest_user_id: str,
        owner_admin_id: str,
    ) -> Optional[dict]:
        entry = await self.guest_access.find_one(
            {
                "company_id": company_id,
                "guest_user_id": guest_user_id,
                "owner_admin_id": owner_admin_id,
                "is_active": True,
            }
        )
        
        if not entry:
            return None
        
        # Automatic migration: Convert old field names to new ones if needed
        needs_migration = False
        update_fields = {}
        
        # Check if old field names exist and new ones don't
        if "can_user_read" in entry and "can_user_write" not in entry:
            # Migrate: can_user_read -> can_user_write (same value, just rename)
            update_fields["can_user_write"] = entry["can_user_read"]
            needs_migration = True
        
        if "can_document_read" in entry and "can_document_write" not in entry:
            # Migrate: can_document_read -> can_document_write (same value, just rename)
            update_fields["can_document_write"] = entry["can_document_read"]
            needs_migration = True
        
        # If migration is needed, update the document
        if needs_migration:
            # Remove old fields and add new ones
            unset_fields = {}
            if "can_user_read" in entry:
                unset_fields["can_user_read"] = ""
            if "can_document_read" in entry:
                unset_fields["can_document_read"] = ""
            
            update_doc = {
                "$set": {
                    **update_fields,
                    "updated_at": datetime.utcnow(),
                }
            }
            if unset_fields:
                update_doc["$unset"] = unset_fields
            
            await self.guest_access.update_one(
                {"_id": entry["_id"]},
                update_doc
            )
            
            # Update the entry dict to reflect changes
            entry.update(update_fields)
            if unset_fields:
                for field in unset_fields.keys():
                    entry.pop(field, None)
        
        return entry

    async def disable_guest_access(
        self,
        company_id: str,
        owner_admin_id: str,
        guest_user_id: str,
    ) -> int:
        res = await self.guest_access.update_one(
            {
                "company_id": company_id,
                "owner_admin_id": owner_admin_id,
                "guest_user_id": guest_user_id,
            },
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
        )
        return res.modified_count

    # ---------------- Debug / Inspection ---------------- #
    async def get_all_collections_data(self) -> dict:
        """Return all documents from companies, admins, users, and documents collections."""
        companies = await self.companies.find().to_list(None)
        admins = await self.admins.find().to_list(None)
        users = await self.users.find().to_list(None)
        documents = await self.documents.find().to_list(None)
        roles = await self.roles.find().to_list(None)
        folders = await self.folders.find().to_list(None)
        guests = await self.guest_access.find().to_list(None)

        def serialize(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, dict):
                return {k: serialize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [serialize(v) for v in obj]
            return str(obj)

        return {
            "companies": serialize(companies),
            "company_admins": serialize(admins),
            "company_users": serialize(users),
            "documents": serialize(documents),
            "roles": serialize(roles),
            "folders": serialize(folders),
            "guest_access": serialize(guests)
        }

    async def clear_all_data(self) -> dict:
        await self.companies.delete_many({})
        await self.admins.delete_many({})
        await self.users.delete_many({})
        await self.documents.delete_many({})    
        await self.roles.delete_many({})
        await self.folders.delete_many({})
        await self.guest_access.delete_many({})
        return {"status": "All collections cleared"}
