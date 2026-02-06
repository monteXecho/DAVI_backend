"""
User Repository - Domain-specific repository for user management operations.

This module handles all user-related operations including:
- Creating and deleting users
- User queries and lookups
- User role assignments
- Teamlid permissions
"""

import logging
import uuid
import io
import re
from datetime import datetime
from typing import List, Optional
from fastapi import HTTPException
import pandas as pd

from app.repositories.base_repo import BaseRepository
from app.repositories.constants import BASE_DOC_URL, serialize_documents

logger = logging.getLogger(__name__)


class UserRepository(BaseRepository):
    """Repository for user management operations."""
    
    async def add_user(self, company_id: str, name: str, email: str) -> dict:
        """
        Create a new company user.
        
        Args:
            company_id: Company identifier
            name: User name
            email: User email
            
        Returns:
            Dictionary with user details
            
        Raises:
            ValueError: If user with email already exists
        """
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
    
    async def add_user_by_admin(
        self,
        company_id: str,
        added_by_admin_id: str,
        email: str,
        company_role: str,
        assigned_role: str
    ) -> dict:
        """
        Add a user by an admin with role assignment.
        
        Args:
            company_id: Company identifier
            added_by_admin_id: Admin who is adding the user
            email: User email
            company_role: Company role (e.g., "company_user")
            assigned_role: Assigned role name (can be empty)
            
        Returns:
            Dictionary with user details
            
        Raises:
            ValueError: If user already exists or limit exceeded
        """
        # Check for duplicate user
        if await self.users.find_one({
            "company_id": company_id,
            "added_by_admin_id": added_by_admin_id,
            "email": email
        }):
            raise ValueError("User with this email already exists in this company")
        
        # Check users limit
        from app.repositories.limits_repo import LimitsRepository
        limits_repo = LimitsRepository(self.db)
        allowed, error_msg = await limits_repo.check_users_limit(company_id)
        if not allowed:
            raise ValueError(error_msg)
        
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
            from app.repositories.role_repo import RoleRepository
            role_repo = RoleRepository(self.db)
            await role_repo.update_role_user_counts(company_id, added_by_admin_id, assigned_roles_list, 1)
        
        # Return without MongoDB ObjectId
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
    
    async def delete_users(
        self,
        company_id: str,
        user_ids: List[str],
        admin_id: Optional[str] = None
    ) -> int:
        """
        Delete users and decrease role user counts.
        Only deletes users added by the specified admin_id if provided.
        
        Args:
            company_id: Company identifier
            user_ids: List of user IDs to delete
            admin_id: Optional admin ID to filter by (only delete users added by this admin)
            
        Returns:
            Number of users deleted
        """
        try:
            query = {
                "company_id": company_id,
                "user_id": {"$in": user_ids}
            }
            if admin_id:
                query["added_by_admin_id"] = admin_id
            
            users_to_delete = await self.users.find(query).to_list(length=None)
            
            if not users_to_delete:
                return 0

            user_ids_to_delete = [u["user_id"] for u in users_to_delete]
            result = await self.users.delete_many({
                "company_id": company_id,
                "user_id": {"$in": user_ids_to_delete}
            })

            deleted_count = result.deleted_count
            
            if deleted_count > 0:
                # Delete user documents
                await self.documents.delete_many({"user_id": {"$in": user_ids_to_delete}})
                
                # Update role user counts if admin_id is provided
                if admin_id:
                    await self._decrease_role_user_counts(company_id, admin_id, users_to_delete)

            return deleted_count

        except Exception as e:
            logger.error(f"Error in delete_users: {str(e)}")
            raise
    
    async def delete_users_by_admin(
        self,
        company_id: str,
        admin_id: str,
        kc_admin=None
    ) -> int:
        """
        Delete all users added by a specific admin.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            kc_admin: Optional Keycloak admin client for deleting Keycloak users
            
        Returns:
            Number of users deleted
        """
        try:
            admin_users = await self.users.find({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            }).to_list(length=None)
            
            user_emails = [user.get("email") for user in admin_users if user.get("email")]
            user_ids = [user.get("user_id") for user in admin_users if user.get("user_id")]
            
            if not admin_users:
                return 0

            # Delete users from Keycloak if kc_admin is provided
            if kc_admin:
                for user in admin_users:
                    email = user.get("email")
                    if email:
                        try:
                            kc_users = kc_admin.get_users(query={"email": email})
                            if kc_users:
                                keycloak_user_id = kc_users[0]["id"]
                                kc_admin.delete_user(keycloak_user_id)
                                logger.info(f"Deleted Keycloak user: {email}")
                        except Exception as e:
                            logger.warning(f"Failed to delete Keycloak user {email}: {e}")

            # Delete user documents
            if user_ids:
                await self.documents.delete_many({"user_id": {"$in": user_ids}})

            # Delete users from MongoDB
            result = await self.users.delete_many({
                "company_id": company_id,
                "added_by_admin_id": admin_id
            })
            
            return result.deleted_count
            
        except Exception as e:
            logger.error(f"Error deleting users for admin {admin_id}: {str(e)}")
            return 0
    
    async def get_users_by_company(self, company_id: str) -> List[dict]:
        """Get all users for a company."""
        users = await self.users.find({"company_id": company_id}).to_list(None)
        # Convert ObjectId to string for JSON serialization
        for user in users:
            if "_id" in user:
                user["_id"] = str(user["_id"])
        return users
    
    async def get_users_by_company_admin(self, admin_id: str) -> List[dict]:
        """Get all users added by a specific admin."""
        users = await self.users.find({"added_by_admin_id": admin_id}).to_list(None)
        # Convert ObjectId to string for JSON serialization
        for user in users:
            if "_id" in user:
                user["_id"] = str(user["_id"])
        return users
    
    async def get_all_users_created_by_admin_id(
        self,
        company_id: str,
        admin_id: str
    ) -> List[dict]:
        """
        Get all users and admins that can be managed by this admin:
        1. Users added by this admin
        2. Admins added by this admin
        3. Admins who have teamlid role assigned by this admin (from guest_access)
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            
        Returns:
            List of users and admins
        """
        users = []
        seen_user_ids = set()
        
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
                "user_id": user_id,
                "name": user_name if user_name is not None else "",
                "email": user_email if user_email is not None else "",
                "company_role": usr.get("company_role", "company_user"),
                "roles": usr.get("assigned_roles", []),
                "type": "user",
                "added_by_admin_id": usr.get("added_by_admin_id"),
                "is_teamlid": usr.get("is_teamlid", False),
                "created_at": usr.get("created_at"),
                "updated_at": usr.get("updated_at"),
                "documents": serialize_documents(await self.documents.find({"user_id": user_id}).to_list(None), user_id)
            })
        
        # 2. Get admins added by this admin
        admins_cursor = self.admins.find({
            "company_id": company_id,
            "added_by_admin_id": admin_id
        })
        
        async for adm in admins_cursor:
            admin_user_id = adm.get("user_id")
            if admin_user_id in seen_user_ids:
                continue
            seen_user_ids.add(admin_user_id)
            
            admin_name = adm.get("name")
            admin_email = adm.get("email")
            users.append({
                "id": admin_user_id,
                "user_id": admin_user_id,
                "name": admin_name if admin_name is not None else "",
                "email": admin_email if admin_email is not None else "",
                "company_role": "company_admin",
                "roles": ["Beheerder"],
                "type": "admin",
                "added_by_admin_id": adm.get("added_by_admin_id"),
                "is_teamlid": adm.get("is_teamlid", False),
                "created_at": adm.get("created_at"),
                "updated_at": adm.get("updated_at"),
                "documents": serialize_documents(await self.documents.find({"user_id": admin_user_id}).to_list(None), admin_user_id)
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
                    "user_id": teamlid_user_id,
                    "name": admin_name if admin_name is not None else "",
                    "email": admin_email if admin_email is not None else "",
                    "company_role": "company_admin",
                    "roles": ["Beheerder"],
                    "type": "admin",
                    "added_by_admin_id": teamlid_admin.get("added_by_admin_id"),
                    "is_teamlid": True,
                    "teamlid_assigned_by": admin_id,
                    "created_at": teamlid_admin.get("created_at"),
                    "updated_at": teamlid_admin.get("updated_at"),
                    "documents": serialize_documents(await self.documents.find({"user_id": teamlid_user_id}).to_list(None), teamlid_user_id)
                })
        
        # Sort users: admins first, then by name/email
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
    
    async def find_user_by_email(self, email: str) -> Optional[dict]:
        """Find a user by email."""
        user = await self.users.find_one({"email": email})
        if not user:
            return None
        return {"email": user["email"], "role": "company_user"}
    
    async def get_user_with_documents(self, email: str) -> Optional[dict]:
        """
        Get user with all their documents (works for both admins and users).
        
        For company admins:
        - Returns private documents (upload_type="document")
        - Returns all documents in folders created by this admin (upload_type=folder_name)
        
        For company users:
        - Returns private documents (upload_type="document")
        - Returns documents from folders assigned via roles
        
        Args:
            email: User email
            
        Returns:
            Dictionary with user details and documents, or None if not found
        """
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
                roles_query = {
                    "company_id": company_id,
                    "added_by_admin_id": added_by_admin_id,
                    "name": {"$in": assigned_roles}
                }
                roles_cursor = self.roles.find(roles_query)
                roles = [r async for r in roles_cursor]
                
                folder_names = set()
                for role in roles:
                    folders = role.get("folders", [])
                    folder_names.update(folders)
                
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
                pid = f"{user_id}--{fn}"
            else:
                if user_type == "admin":
                    pid = f"{company_id}-{user_id}--{fn}"
                else:
                    added_by_admin_id = user.get("added_by_admin_id")
                    if added_by_admin_id:
                        pid = f"{company_id}-{added_by_admin_id}--{fn}"
                    else:
                        pid = f"{company_id}-{user_id}--{fn}"
            pass_ids.append(pid)

        # Aggregate modules from user's assigned roles
        # Modules can be assigned directly to the user OR via their assigned roles
        modules_obj = {}
        
        # First, get modules directly assigned to the user
        user_modules = user.get("modules", {})
        if isinstance(user_modules, dict):
            modules_obj.update(user_modules)
        elif isinstance(user_modules, list):
            for module in user_modules:
                if isinstance(module, dict) and "name" in module:
                    modules_obj[module["name"]] = {
                        "enabled": module.get("enabled", False),
                        "desc": module.get("desc", "")
                    }
        
        # Then, aggregate modules from assigned roles (for company users)
        if user_type == "company_user":
            assigned_roles = user.get("assigned_roles", [])
            added_by_admin_id = user.get("added_by_admin_id")
            
            if assigned_roles and added_by_admin_id:
                roles_query = {
                    "company_id": company_id,
                    "added_by_admin_id": added_by_admin_id,
                    "name": {"$in": assigned_roles}
                }
                roles_cursor = self.roles.find(roles_query)
                roles = [r async for r in roles_cursor]
                
                # Aggregate modules from all assigned roles
                for role in roles:
                    role_modules = role.get("modules", {})
                    if isinstance(role_modules, dict):
                        # Merge role modules into user modules (role modules take precedence)
                        for module_name, module_config in role_modules.items():
                            if module_config.get("enabled", False):
                                modules_obj[module_name] = module_config
        
        # Get is_teamlid flag
        is_teamlid = user.get("is_teamlid", False)
        
        return {
            "user_id": user_id,
            "company_id": company_id,
            "user_type": user_type,
            "documents": formatted_docs,
            "pass_ids": pass_ids,
            "modules": modules_obj,  # Return as object, not array
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "is_teamlid": is_teamlid,  # Required for workspace switcher
            "assigned_roles": user.get("assigned_roles", []),  # Include assigned roles
        }
    
    async def get_all_user_documents(self, email: str) -> dict:
        """
        Get all documents for a user.
        
        Args:
            email: User email
            
        Returns:
            Dictionary with user details and all documents
        """
        user = await self.users.find_one({"email": email})
        if not user:
            return {"documents": []}
        
        user_id = user.get("user_id")
        company_id = user.get("company_id")
        
        docs_cursor = self.documents.find({
            "user_id": user_id,
            "company_id": company_id
        })
        
        documents = []
        async for doc in docs_cursor:
            documents.append({
                "file_name": doc.get("file_name", ""),
                "path": doc.get("path", ""),
                "upload_type": doc.get("upload_type", "document"),
                "storage_path": doc.get("storage_path"),
            })
        
        return {
            "user_id": user_id,
            "company_id": company_id,
            "documents": documents,
        }
    
    async def update_user(
        self,
        company_id: str,
        user_id: str,
        name: Optional[str] = None,
        email: Optional[str] = None,
        assigned_roles: Optional[List[str]] = None,
        is_teamlid: Optional[bool] = None,
        teamlid_permissions: Optional[dict] = None
    ) -> Optional[dict]:
        """
        Update user information.
        
        Args:
            company_id: Company identifier
            user_id: User identifier
            name: Optional new name
            email: Optional new email
            assigned_roles: Optional list of assigned roles
            is_teamlid: Optional teamlid flag
            teamlid_permissions: Optional teamlid permissions
            
        Returns:
            Updated user dictionary or None if not found
        """
        update_data = {"updated_at": datetime.utcnow()}
        
        if name is not None:
            update_data["name"] = name
        if email is not None:
            update_data["email"] = email
        if assigned_roles is not None:
            update_data["assigned_roles"] = assigned_roles
        if is_teamlid is not None:
            update_data["is_teamlid"] = is_teamlid
        if teamlid_permissions is not None:
            update_data["teamlid_permissions"] = teamlid_permissions
        
        result = await self.users.update_one(
            {"company_id": company_id, "user_id": user_id},
            {"$set": update_data}
        )
        
        if result.modified_count == 0:
            return None
        
        updated_user = await self.users.find_one({"company_id": company_id, "user_id": user_id})
        
        # Convert ObjectId to string for JSON serialization
        if updated_user and "_id" in updated_user:
            updated_user["_id"] = str(updated_user["_id"])
        
        return updated_user
    
    async def assign_teamlid_permissions(
        self,
        company_id: str,
        admin_id: str,
        email: str,
        permissions: dict
    ) -> bool:
        """
        Assign teamlid permissions to a user or admin.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            email: User/Admin email
            permissions: Dictionary of permissions
            
        Returns:
            True if permissions were assigned, False otherwise
        """
        # Try to find user first
        user = await self.users.find_one({
            "company_id": company_id,
            "email": email
        })
        
        if user:
            # Update user with teamlid permissions and add "Teamlid" to assigned_roles
            update_data = {
                "is_teamlid": True,
                "assigned_teamlid_by_id": admin_id,
                "teamlid_permissions": permissions,
                "updated_at": datetime.utcnow()
            }
            
            result = await self.users.update_one(
                {"company_id": company_id, "email": email},
                {
                    "$set": update_data,
                    "$addToSet": {"assigned_roles": "Teamlid"}  # Add Teamlid to assigned_roles if not present
                }
            )
            
            return result.modified_count > 0
        
        # If not found in users, try admins
        admin = await self.admins.find_one({
            "company_id": company_id,
            "email": email
        })
        
        if admin:
            # Update admin with teamlid permissions
            # Note: Admins don't have assigned_roles array, they have "Beheerder" role by default
            update_data = {
                "is_teamlid": True,
                "assigned_teamlid_by_id": admin_id,
                "teamlid_permissions": permissions,
                "updated_at": datetime.utcnow()
            }
            
            result = await self.admins.update_one(
                {"company_id": company_id, "email": email},
                {"$set": update_data}
            )
            
            return result.modified_count > 0
        
        return False
    
    async def remove_teamlid_role(
        self,
        company_id: str,
        admin_id: str,
        user_id: str
    ) -> bool:
        """
        Remove teamlid role from a user or admin.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            user_id: User/Admin identifier
            
        Returns:
            True if role was removed, False otherwise
        """
        # Try to remove from users first
        result = await self.users.update_one(
            {
                "company_id": company_id,
                "user_id": user_id,
                "assigned_teamlid_by_id": admin_id
            },
            {
                "$set": {
                    "is_teamlid": False,
                    "updated_at": datetime.utcnow()
                },
                "$pull": {"assigned_roles": "Teamlid"},  # Remove Teamlid from assigned_roles
                "$unset": {
                    "assigned_teamlid_by_id": "",
                    "teamlid_permissions": ""
                }
            }
        )
        
        if result.modified_count > 0:
            return True
        
        # If not found in users, try admins
        result = await self.admins.update_one(
            {
                "company_id": company_id,
                "user_id": user_id,
                "assigned_teamlid_by_id": admin_id
            },
            {
                "$set": {
                    "is_teamlid": False,
                    "updated_at": datetime.utcnow()
                },
                "$unset": {
                    "assigned_teamlid_by_id": "",
                    "teamlid_permissions": ""
                }
            }
        )
        
        return result.modified_count > 0
    
    async def _decrease_role_user_counts(
        self,
        company_id: str,
        admin_id: str,
        deleted_users: List[dict]
    ):
        """
        Decrease the assigned_user_count for roles when users with those roles are deleted.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            deleted_users: List of deleted user dictionaries
        """
        try:
            role_decrement_counts = {}
            
            for user in deleted_users:
                assigned_roles = user.get("assigned_roles", [])
                for role_name in assigned_roles:
                    role_decrement_counts[role_name] = role_decrement_counts.get(role_name, 0) + 1
            
            # Update each role's count using $inc with negative value
            for role_name, decrement_count in role_decrement_counts.items():
                try:
                    await self.roles.update_one(
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
                    logger.debug(f"Decreased assigned_user_count for role '{role_name}' by {decrement_count}")
                except Exception as role_error:
                    logger.warning(f"Error updating role '{role_name}': {role_error}")
                    
        except Exception as e:
            logger.error(f"Error in _decrease_role_user_counts: {e}")
    
    async def add_users_from_email_file(
        self,
        company_id: str,
        admin_id: str,
        file_content: bytes,
        file_extension: str,
        selected_role: str = None
    ) -> dict:
        """
        Add multiple users from CSV/Excel file containing email addresses.
        Handles emails in column headers, data cells, or anywhere in the file.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            file_content: File content as bytes
            file_extension: File extension (.csv or .xlsx)
            selected_role: Selected role for assignment ("Alle rollen", "Zonder rol", or specific role)
            
        Returns:
            Dictionary with successful, failed, and duplicate users
        """
        try:
            from app.repositories.role_repo import RoleRepository
            role_repo = RoleRepository(self.db)
            
            emails = []
            roles_to_assign = await role_repo.get_roles_to_assign(company_id, admin_id, selected_role)
            
            try:
                if file_extension == '.csv':
                    df = pd.read_csv(io.BytesIO(file_content))
                else:
                    df = pd.read_excel(io.BytesIO(file_content))
                
                # Strategy 1: Check column headers
                for col_name in df.columns:
                    col_str = str(col_name).strip()
                    if re.match(r'^[a-zA-Z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$', col_str):
                        emails.append(col_str)
                
                # Strategy 2: Check data cells
                for col in df.columns:
                    col_data = df[col].dropna()
                    if not col_data.empty:
                        for value in col_data:
                            value_str = str(value).strip()
                            if re.match(r'^[a-zA-Z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$', value_str):
                                emails.append(value_str)
                
                # Strategy 3: Text extraction
                if not emails:
                    df_text = df.to_string()
                    emails = self._extract_emails_from_text(df_text)
                
            except Exception as file_error:
                text_content = file_content.decode('utf-8', errors='ignore')
                emails = self._extract_emails_from_text(text_content)

            if not emails:
                raise ValueError("No valid email addresses found in the file.")

            results = {
                "successful": [],
                "failed": [],
                "duplicates": []
            }

            successful_users = []

            for email in emails:
                try:
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

                    name = email.split('@')[0]

                    user_doc = {
                        "user_id": str(uuid.uuid4()),
                        "company_id": company_id,
                        "added_by_admin_id": admin_id,
                        "email": email,
                        "name": name,
                        "company_role": "company_user",
                        "assigned_roles": roles_to_assign,
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }

                    await self.users.insert_one(user_doc)
                    successful_users.append(user_doc)

                    results["successful"].append({
                        "email": email,
                        "name": name,
                        "user_id": user_doc["user_id"],
                        "assigned_roles": roles_to_assign
                    })

                except Exception as e:
                    results["failed"].append({
                        "email": email,
                        "error": str(e)
                    })

            # Update role user counts
            if successful_users and roles_to_assign:
                await role_repo.update_role_user_counts(company_id, admin_id, roles_to_assign, len(successful_users))

            return results

        except Exception as e:
            logger.error(f"Error processing user upload file: {str(e)}")
            raise
    
    def _extract_emails_from_text(self, text_content: str) -> List[str]:
        """Extract emails from plain text content."""
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        emails = re.findall(email_pattern, text_content)
        return list(set(emails))  # Remove duplicates
    
    async def get_all_private_documents(self, email: str, document_type: str) -> dict:
        """
        Get all private documents for a user or admin.
        
        Args:
            email: User/Admin email
            document_type: Document type (e.g., "document")
            
        Returns:
            Dictionary with documents list
        """
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
    
    async def delete_private_documents(
        self,
        email: str,
        documents_to_delete: List[dict]
    ) -> int:
        """
        Delete private documents for a user or admin.
        
        Args:
            email: User/Admin email
            documents_to_delete: List of document info dictionaries
            
        Returns:
            Number of documents deleted
        """
        user_rec = await self.users.find_one({"email": email})
        admin_rec = await self.admins.find_one({"email": email})

        user = user_rec or admin_rec

        if not user:
            return 0

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
                        import os
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

