"""
Admin Repository - Domain-specific repository for company admin operations.

This module handles all operations related to company admins including:
- CRUD operations (create, read, update, delete)
- Module assignment
- Admin lookup and queries
"""

import copy
import logging
import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.repositories.base_repo import BaseRepository
from app.repositories.constants import DEFAULT_MODULES, serialize_modules

logger = logging.getLogger(__name__)


class AdminRepository(BaseRepository):
    """Repository for company admin operations."""
    
    async def create_admin(
        self,
        company_id: str,
        admin_id: str,
        name: str,
        email: str,
        modules: Optional[dict] = None,
        company_modules: Optional[dict] = None
    ) -> Dict[str, Any]:
        """
        Create a new company admin.
        
        Args:
            company_id: Company identifier
            admin_id: ID of the admin creating this admin
            name: Admin name
            email: Admin email (must be unique within company)
            modules: Optional module permissions for the admin
            company_modules: Company-level module configuration
            
        Returns:
            Dictionary with created admin details
            
        Raises:
            ValueError: If admin with email already exists
        """
        if await self.admins.find_one({"company_id": company_id, "email": email}):
            raise ValueError("Admin with this email already exists")
        
        # Get company modules if not provided
        if company_modules is None:
            from app.repositories.modules_repo import ModulesRepository
            modules_repo = ModulesRepository(self.db)
            company_modules = await modules_repo.get_company_modules(company_id)
        
        admin_modules = copy.deepcopy(DEFAULT_MODULES)
        if modules:
            for k, v in modules.items():
                if k in admin_modules:
                    # Only enable if company has this module enabled
                    company_has_module = company_modules.get(k, {}).get("enabled", False)
                    if company_has_module:
                        admin_modules[k]["enabled"] = v.get("enabled", False)
                    else:
                        admin_modules[k]["enabled"] = False
        
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
    
    async def get_admin_by_id(self, company_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Get an admin by company and user ID."""
        return await self.admins.find_one({"company_id": company_id, "user_id": user_id})
    
    async def get_admins_by_company(self, company_id: str) -> list:
        """Get all admins for a company."""
        return await self.admins.find({"company_id": company_id}).to_list(None)
    
    async def find_admin_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Find admin by email.
        
        Returns:
            Dictionary with email and role, or None if not found
        """
        admin = await self.admins.find_one({"email": email})
        if not admin:
            return None
        return {"email": admin["email"], "role": "company_admin"}
    
    async def update_admin(
        self,
        company_id: str,
        admin_id: str,
        name: str,
        email: str
    ) -> Dict[str, Any]:
        """
        Update admin information.
        
        Args:
            company_id: Company identifier
            admin_id: Admin user ID
            name: New name
            email: New email (must be unique within company)
            
        Returns:
            Updated admin details
            
        Raises:
            ValueError: If admin not found or email already exists
        """
        # Check if another admin already uses this email
        existing = await self.admins.find_one({
            "company_id": company_id,
            "email": email,
        })
        if existing and existing["user_id"] != admin_id:
            raise ValueError("Admin with this email already exists")
        
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
        
        updated_admin = await self.admins.find_one({"company_id": company_id, "user_id": admin_id})
        
        # Convert ObjectId to string if present
        result = {
            "user_id": updated_admin["user_id"],
            "company_id": updated_admin["company_id"],
            "name": updated_admin["name"],
            "email": updated_admin["email"],
            "modules": serialize_modules(updated_admin["modules"]),
            "documents": updated_admin.get("documents", []),
        }
        
        if "_id" in updated_admin:
            result["_id"] = str(updated_admin["_id"])
        
        return result
    
    async def assign_modules(
        self,
        company_id: str,
        user_id: str,
        modules: dict,
        company_modules: Optional[dict] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Assign module permissions to an admin.
        
        Args:
            company_id: Company identifier
            user_id: Admin user ID
            modules: Module permissions dictionary
            company_modules: Company-level module configuration (optional)
            
        Returns:
            Updated admin details or None if admin not found
        """
        # Get company modules if not provided
        if company_modules is None:
            from app.repositories.modules_repo import ModulesRepository
            modules_repo = ModulesRepository(self.db)
            company_modules = await modules_repo.get_company_modules(company_id)
        
        admin = await self.admins.find_one({"company_id": company_id, "user_id": user_id})
        if not admin:
            return None
        
        # Filter modules to only include those enabled at company level
        for k, v in modules.items():
            if k in admin["modules"]:
                company_has_module = company_modules.get(k, {}).get("enabled", False)
                if company_has_module:
                    admin["modules"][k]["enabled"] = v.get("enabled", False)
                else:
                    admin["modules"][k]["enabled"] = False
        
        await self.admins.update_one(
            {"company_id": company_id, "user_id": user_id},
            {"$set": {"modules": admin["modules"], "updated_at": datetime.utcnow()}},
        )
        
        return {
            "id": admin["user_id"],
            "user_id": admin["user_id"],
            "company_id": admin["company_id"],
            "name": admin["name"],
            "email": admin["email"],
            "modules": serialize_modules(admin["modules"]),
        }
    
    async def delete_admin(
        self,
        company_id: str,
        user_id: str,
        admin_id: Optional[str] = None
    ) -> bool:
        """
        Delete an admin.
        
        Args:
            company_id: Company identifier
            user_id: Admin user ID to delete
            admin_id: Optional admin ID that created this admin (for permission check)
            
        Returns:
            True if admin was deleted, False otherwise
            
        Raises:
            HTTPException: If permission check fails
        """
        query = {"company_id": company_id, "user_id": user_id}
        if admin_id:
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
            # Clean up related data
            await self.documents.delete_many({"user_id": admin["user_id"]})
            await self.guest_access.delete_many({
                "company_id": company_id,
                "guest_user_id": user_id
            })
            return True
        return False
    
    async def get_admin_documents(self, company_id: str, admin_id: str) -> dict:
        """
        Get all documents organized by roles and folders for an admin.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            
        Returns:
            Dictionary with roles as keys, each containing folders and documents
        """
        try:
            from collections import defaultdict
            
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
                    existing_user = next((u for u in role_users_map[role_name] if u.get("user_id") == user_info["user_id"]), None)
                    if not existing_user:
                        role_users_map[role_name].append(user_info)
            
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
                    
                    for doc in folder_docs:
                        all_users_for_folder = []
                        seen_user_ids = set()
                        
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
            
            folders_in_roles_set = set()
            for role in roles:
                for folder_name in role.get("folders", []):
                    folders_in_roles_set.add(folder_name)
            
            for folder_name, folder_docs in folder_docs_map.items():
                if folder_name not in folders_in_roles_set and folder_docs:
                    if "Geen rol toegewezen" not in result:
                        result["Geen rol toegewezen"] = {"folders": []}
                    
                    for doc in folder_docs:
                        doc["assigned_to"] = []
                    
                    folder_entry = {
                        "name": folder_name,
                        "documents": folder_docs
                    }
                    result["Geen rol toegewezen"]["folders"].append(folder_entry)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in get_admin_documents: {str(e)}")
            return {}
    
    async def get_admin_with_documents_by_id(
        self,
        company_id: str,
        admin_user_id: str,
    ) -> Optional[dict]:
        """
        Get admin with documents by admin user ID.
        Uses the same structure as get_user_with_documents.
        
        Args:
            company_id: Company identifier
            admin_user_id: Admin user ID
            
        Returns:
            Dictionary with admin details and documents, or None if not found
        """
        admin = await self.admins.find_one(
            {"company_id": company_id, "user_id": admin_user_id}
        )
        if not admin:
            return None

        email = admin["email"]
        from app.repositories.user_repo import UserRepository
        user_repo = UserRepository(self.db)
        return await user_repo.get_user_with_documents(email)

