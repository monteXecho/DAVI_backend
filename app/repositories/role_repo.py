"""
Role Repository - Domain-specific repository for role management operations.

This module handles all role-related operations including:
- Creating and updating roles
- Role-folder associations
- Role-user assignments
- Role counting and statistics
"""

import logging
import os
import shutil
from datetime import datetime
from typing import List, Optional
from fastapi import HTTPException

from app.repositories.base_repo import BaseRepository
from app.repositories.constants import UPLOAD_ROOT

logger = logging.getLogger(__name__)


class RoleRepository(BaseRepository):
    """Repository for role management operations."""
    
    async def add_or_update_role(
        self,
        company_id: str,
        admin_id: str,
        role_name: str,
        folders: List[str],
        modules: Optional[List] = None,
        action: str = "create",
        company_modules: Optional[dict] = None
    ) -> dict:
        """
        Add or update a role based on the action parameter.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            role_name: Role name
            folders: List of folder names associated with the role
            modules: Optional list of module configurations
            action: "create" or "update"
            company_modules: Company-level module configuration (optional)
            
        Returns:
            Dictionary with role creation/update status
        """
        folders = [f.strip("/") for f in folders if f.strip()]

        existing_role = await self.roles.find_one({
            "company_id": company_id,
            "added_by_admin_id": admin_id,
            "name": role_name
        })

        # Check roles limit only when creating a new role
        if action == "create" and not existing_role:
            from app.repositories.limits_repo import LimitsRepository
            limits_repo = LimitsRepository(self.db)
            allowed, error_msg = await limits_repo.check_roles_limit(company_id, admin_id)
            if not allowed:
                return {
                    "status": "error",
                    "error_type": "limit_exceeded",
                    "message": error_msg,
                    "company_id": company_id,
                    "role_name": role_name
                }

        if action == "create" and existing_role:
            return {
                "status": "error",
                "error_type": "duplicate_role",
                "message": f"Role '{role_name}' already exists",
                "company_id": company_id,
                "role_name": role_name
            }

        # Get company modules to filter role modules
        if company_modules is None:
            from app.repositories.modules_repo import ModulesRepository
            modules_repo = ModulesRepository(self.db)
            company_modules = await modules_repo.get_company_modules(company_id)
        
        modules_dict = None
        if modules is not None:
            if isinstance(modules, list):
                modules_dict = {}
                for module in modules:
                    if isinstance(module, dict):
                        module_name = module.get('name')
                        if module_name:
                            company_has_module = company_modules.get(module_name, {}).get("enabled", False)
                            if company_has_module:
                                module_data = {k: v for k, v in module.items() if k != 'name'}
                                modules_dict[module_name] = module_data
                    elif hasattr(module, 'dict'):
                        module_dict = module.dict()
                        module_name = module_dict.get('name')
                        if module_name:
                            company_has_module = company_modules.get(module_name, {}).get("enabled", False)
                            if company_has_module:
                                module_data = {k: v for k, v in module_dict.items() if k != 'name'}
                                modules_dict[module_name] = module_data
            else:
                modules_dict = {}
                for module_name, module_config in modules.items():
                    company_has_module = company_modules.get(module_name, {}).get("enabled", False)
                    if company_has_module:
                        modules_dict[module_name] = module_config

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

        # Create folder structure on filesystem
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
    
    async def list_roles(self, company_id: str, admin_id: str) -> List[dict]:
        """
        List all roles for a given company and admin.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            
        Returns:
            List of role dictionaries with statistics
        """
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
    
    async def delete_roles(
        self,
        company_id: str,
        role_names: List[str],
        admin_id: str
    ) -> dict:
        """
        Delete one or multiple roles by name, remove them from users' assigned_roles,
        and delete related documents/folders.
        
        Args:
            company_id: Company identifier
            role_names: List of role names to delete
            admin_id: Admin identifier
            
        Returns:
            Dictionary with deletion results
        """
        deleted_roles = []
        total_users_updated = 0
        total_documents_deleted = 0

        for role_name in role_names:
            role = await self.roles.find_one({
                "company_id": company_id,
                "added_by_admin_id": admin_id,
                "name": role_name
            })
            if not role:
                continue

            # Delete all documents uploaded by admin for this role
            delete_docs_result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": role_name
            })

            # Remove related folders from filesystem
            base_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id, admin_id, role_name)
            if os.path.exists(base_path):
                import shutil
                try:
                    shutil.rmtree(base_path)
                except Exception as e:
                    logger.warning(f"Failed to delete folder {base_path}: {e}")

            # Remove this role from all users' assigned_roles
            update_result = await self.users.update_many(
                {"company_id": company_id, "assigned_roles": role_name},
                {"$pull": {"assigned_roles": role_name}}
            )

            # Delete the role itself
            await self.roles.delete_one({"_id": role["_id"]})

            deleted_roles.append(role_name)
            total_users_updated += update_result.modified_count
            total_documents_deleted += delete_docs_result.deleted_count

        if not deleted_roles:
            raise HTTPException(status_code=404, detail="No valid roles found to delete")

        return {
            "status": "deleted",
            "deleted_roles": deleted_roles,
            "total_users_updated": total_users_updated,
            "total_documents_deleted": total_documents_deleted
        }
    
    async def assign_role_to_user(
        self,
        company_id: str,
        user_id: str,
        role_name: str
    ) -> dict:
        """
        Assign a role to a company user (by user_id).
        Adds the role to 'assigned_roles' array (no duplicates)
        and increments the role's assigned_user_count.
        
        Args:
            company_id: Company identifier
            user_id: User identifier
            role_name: Role name to assign
            
        Returns:
            Dictionary with assignment status
        """
        role = await self.roles.find_one({"company_id": company_id, "name": role_name})
        if not role:
            raise HTTPException(status_code=404, detail=f"Role '{role_name}' not found")

        user = await self.users.find_one({"user_id": user_id, "company_id": company_id})
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found in this company")

        assigned_roles = user.get("assigned_roles", [])
        if role_name not in assigned_roles:
            assigned_roles.append(role_name)

            await self.users.update_one(
                {"user_id": user_id, "company_id": company_id},
                {
                    "$set": {
                        "assigned_roles": assigned_roles,
                        "updated_at": datetime.utcnow()
                    }
                }
            )

            await self.roles.update_one(
                {"_id": role["_id"]},
                {"$inc": {"assigned_user_count": 1}, "$set": {"updated_at": datetime.utcnow()}}
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
    
    async def get_role_by_name(
        self,
        company_id: str,
        admin_id: str,
        role_name: str
    ) -> Optional[dict]:
        """
        Get a role by name for a specific company and admin.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            role_name: Role name
            
        Returns:
            Role dictionary or None if not found
        """
        return await self.roles.find_one({
            "company_id": company_id,
            "added_by_admin_id": admin_id,
            "name": role_name
        })
    
    async def delete_roles_by_admin(self, company_id: str, admin_id: str) -> int:
        """
        Delete all roles created by a specific admin.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            
        Returns:
            Number of roles deleted
        """
        try:
            result = await self.roles.delete_many({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting roles for admin {admin_id}: {str(e)}")
            return 0
    
    async def update_role_user_counts(
        self,
        company_id: str,
        admin_id: str,
        roles: List[str],
        user_count: int
    ):
        """
        Update the assigned_user_count for roles when users are assigned to them.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            roles: List of role names
            user_count: Number of users to add to count (can be negative)
        """
        try:
            for role_name in roles:
                existing_role = await self.roles.find_one({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id,
                    "name": role_name
                })
                
                if not existing_role:
                    logger.debug(f"Role '{role_name}' not found, skipping count update")
                    continue
                
                await self.roles.update_one(
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
                logger.debug(f"Updated assigned_user_count for role '{role_name}' by {user_count}")
                    
        except Exception as e:
            logger.error(f"Error in update_role_user_counts: {e}")
    
    async def get_roles_to_assign(
        self,
        company_id: str,
        admin_id: str,
        selected_role: str
    ) -> List[str]:
        """
        Determine which roles to assign based on the selected role logic:
        - "Alle rollen": Assign all roles created by this admin
        - "Zonder rol" or empty: Assign no roles (empty list)
        - Specific role: Validate and assign that specific role if created by this admin
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            selected_role: Selected role string ("Alle rollen", "Zonder rol", or specific role name)
            
        Returns:
            List of role names to assign
        """
        # Case 1: No role or "Zonder rol" - assign empty roles
        if not selected_role or selected_role == "Zonder rol":
            return []
        
        # Case 2: "Alle rollen" - get all roles created by this admin
        if selected_role == "Alle rollen":
            try:
                all_admin_roles = await self.roles.find({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id
                }).to_list(length=None)
                
                role_names = [role.get("name") for role in all_admin_roles if role.get("name")]
                return role_names
                
            except Exception as e:
                logger.error(f"Error fetching all admin roles: {e}")
                return []
        
        # Case 3: Specific role - validate and assign if exists
        try:
            existing_role = await self.roles.find_one({
                "company_id": company_id,
                "added_by_admin_id": admin_id,
                "name": selected_role
            })
            
            if existing_role:
                return [selected_role]
            else:
                logger.warning(f"Role '{selected_role}' not found or not created by this admin")
                return []
                
        except Exception as e:
            logger.error(f"Error validating specific role '{selected_role}': {e}")
            return []
    
    async def delete_role_documents_by_admin(self, company_id: str, admin_id: str) -> int:
        """
        Delete all role documents uploaded by a specific admin.
        This includes both database records and actual files from the filesystem.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            
        Returns:
            Number of documents deleted
        """
        try:
            role_documents = await self.documents.find({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$exists": True}
            }).to_list(length=None)
            
            if not role_documents:
                return 0

            files_deleted = 0
            for doc in role_documents:
                file_path = doc.get("path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        files_deleted += 1
                        
                        directory = os.path.dirname(file_path)
                        if os.path.exists(directory) and not os.listdir(directory):
                            os.rmdir(directory)
                    except Exception as e:
                        logger.warning(f"Failed to delete file {file_path}: {e}")
            
            role_doc_counts = {}
            for doc in role_documents:
                role_name = doc.get("upload_type")
                if role_name:
                    role_doc_counts[role_name] = role_doc_counts.get(role_name, 0) + 1
            
            for role_name, doc_count in role_doc_counts.items():
                try:
                    await self.roles.update_one(
                        {"company_id": company_id, "name": role_name},
                        {"$inc": {"document_count": -doc_count}}
                    )
                except Exception as e:
                    logger.warning(f"Failed to update document_count for role '{role_name}': {e}")

            result = await self.documents.delete_many({
                "company_id": company_id,
                "user_id": admin_id,
                "upload_type": {"$exists": True}
            })
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting role documents for admin {admin_id}: {str(e)}")
            return 0
    
    async def delete_company_role_documents(self, company_id: str) -> int:
        """
        Delete all role documents for a company.
        
        Args:
            company_id: Company identifier
            
        Returns:
            Number of documents deleted
        """
        try:
            role_documents = await self.documents.find({
                "company_id": company_id,
                "upload_type": {"$exists": True, "$ne": "document"}
            }).to_list(length=None)
            
            if not role_documents:
                return 0

            files_deleted = 0
            for doc in role_documents:
                file_path = doc.get("path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        files_deleted += 1
                    except Exception as e:
                        logger.warning(f"Failed to delete file {file_path}: {e}")

            result = await self.documents.delete_many({
                "company_id": company_id,
                "upload_type": {"$exists": True, "$ne": "document"}
            })
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting company role documents: {str(e)}")
            return 0

