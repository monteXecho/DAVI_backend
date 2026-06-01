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
import os
import re
from datetime import datetime
from typing import List, Optional

from bson import ObjectId
from fastapi import HTTPException
import pandas as pd

from app.repositories.base_repo import BaseRepository
from app.repositories.constants import BASE_DOC_URL, serialize_documents, serialize_modules

logger = logging.getLogger(__name__)


def _to_bool(value):  # noqa: N802
    """Convert permission value to bool (handles string/boolean from API)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _assigner_has_module(module_name: str, assigner_modules: dict, company_modules: dict) -> bool:
    if not company_modules.get(module_name, {}).get("enabled", False):
        return False
    return assigner_modules.get(module_name, {}).get("enabled", False) is True


def _clamp_teamlid_permissions(
    permissions: dict,
    assigner_modules: dict,
    company_modules: dict,
) -> dict:
    """Teamlid flags may only cover modules the assigning admin has (company + assigner scope)."""
    out = dict(permissions or {})
    # Documenten chat covers Rollen-mappen + Documenten only — not Gebruikers (matches company admin UI).
    if not _assigner_has_module("Documenten chat", assigner_modules, company_modules):
        out["role_folder_modify_permission"] = False
        out["document_modify_permission"] = False
    if not _assigner_has_module("PublicChat", assigner_modules, company_modules):
        out["publicchat_modify_permission"] = False
    if not _assigner_has_module("WebChat", assigner_modules, company_modules):
        out["webchat_modify_permission"] = False
    return out


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
            is_teamlid = usr.get("is_teamlid", False)
            user_entry = {
                "id": user_id,
                "user_id": user_id,
                "name": user_name if user_name is not None else "",
                "email": user_email if user_email is not None else "",
                "company_role": usr.get("company_role", "company_user"),
                "roles": usr.get("assigned_roles", []),
                "type": "user",
                "added_by_admin_id": usr.get("added_by_admin_id"),
                "is_teamlid": is_teamlid,
                "created_at": usr.get("created_at"),
                "updated_at": usr.get("updated_at"),
                "documents": serialize_documents(await self.documents.find({"user_id": user_id}).to_list(None), user_id)
            }
            if is_teamlid and usr.get("teamlid_permissions"):
                user_entry["teamlid_permissions"] = usr.get("teamlid_permissions")
            if is_teamlid and usr.get("teamlid_public_chat_ids") is not None:
                user_entry["teamlid_public_chat_ids"] = usr.get("teamlid_public_chat_ids")
            users.append(user_entry)
        
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
            is_teamlid = adm.get("is_teamlid", False)
            admin_entry = {
                "id": admin_user_id,
                "user_id": admin_user_id,
                "name": admin_name if admin_name is not None else "",
                "email": admin_email if admin_email is not None else "",
                "company_role": "company_admin",
                "roles": ["Beheerder"],
                "type": "admin",
                "added_by_admin_id": adm.get("added_by_admin_id"),
                "is_teamlid": is_teamlid,
                "created_at": adm.get("created_at"),
                "updated_at": adm.get("updated_at"),
                "documents": serialize_documents(await self.documents.find({"user_id": admin_user_id}).to_list(None), admin_user_id),
                "modules": serialize_modules(adm.get("modules", {})),
            }
            if is_teamlid and adm.get("teamlid_permissions"):
                admin_entry["teamlid_permissions"] = adm.get("teamlid_permissions")
            if is_teamlid and adm.get("teamlid_public_chat_ids") is not None:
                admin_entry["teamlid_public_chat_ids"] = adm.get("teamlid_public_chat_ids")
            users.append(admin_entry)
        
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
                        if "assigned_public_chat_ids" in assignment:
                            user["teamlid_public_chat_ids"] = assignment.get(
                                "assigned_public_chat_ids"
                            )
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
                admin_entry = {
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
                    "documents": serialize_documents(await self.documents.find({"user_id": teamlid_user_id}).to_list(None), teamlid_user_id),
                    "modules": serialize_modules(teamlid_admin.get("modules", {})),
                }
                if teamlid_admin.get("teamlid_permissions"):
                    admin_entry["teamlid_permissions"] = teamlid_admin.get("teamlid_permissions")
                if teamlid_admin.get("teamlid_public_chat_ids") is not None:
                    admin_entry["teamlid_public_chat_ids"] = teamlid_admin.get(
                        "teamlid_public_chat_ids"
                    )
                if "assigned_public_chat_ids" in assignment:
                    admin_entry["teamlid_public_chat_ids"] = assignment.get("assigned_public_chat_ids")
                users.append(admin_entry)

        # Prefer guest_access copy of public-chat assignments (canonical for workspace switcher users)
        teamlid_uid_list = [u["id"] for u in users if u.get("is_teamlid")]
        if teamlid_uid_list:
            chat_from_guest: dict = {}
            async for gdoc in self.guest_access.find(
                {
                    "company_id": company_id,
                    "owner_admin_id": admin_id,
                    "guest_user_id": {"$in": teamlid_uid_list},
                    "is_active": True,
                },
                projection={"guest_user_id": 1, "assigned_public_chat_ids": 1},
            ):
                if "assigned_public_chat_ids" not in gdoc:
                    continue
                chat_from_guest[gdoc["guest_user_id"]] = gdoc["assigned_public_chat_ids"]
            for entry in users:
                if not entry.get("is_teamlid"):
                    continue
                gid = entry.get("id")
                if gid in chat_from_guest:
                    entry["teamlid_public_chat_ids"] = chat_from_guest[gid]

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
    
    async def get_user_with_documents(self, email: str, company_id=None) -> Optional[dict]:
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
            company_id: When the same email has multiple memberships, pass the resolver's
                        ``company_id`` (BSON/string as stored together with that admin/user row).

        Returns:
            Dictionary with user details and documents, or None if not found
        """
        if company_id is not None:
            user = await self.admins.find_one({"email": email, "company_id": company_id})
            user_type = "admin"
            if not user:
                user = await self.users.find_one({"email": email, "company_id": company_id})
                user_type = "company_user"
        else:
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

        # Generate pass_ids matching the indexing format
        # Private documents: documentchat-{company_id}-{user_id}--{filename} (use user_id, not admin_id)
        # Role-based documents: documentchat-{company_id}-{admin_id}--{filename} (use admin_id)
        pass_ids = []
        for doc in formatted_docs:
            fn = doc["file_name"]
            upload_type = doc.get("upload_type", "document")

            if upload_type == "document":
                # Private documents: use user_id for personal document separation
                pid = f"documentchat-{company_id}-{user_id}--{fn}"
            else:
                # Role-based documents: use admin_id for admin-level separation
                admin_id = None
                if user_type == "admin":
                    admin_id = user_id  # Admin's own user_id
                else:
                    admin_id = user.get("added_by_admin_id")  # Company user's admin
                
                if admin_id:
                    pid = f"documentchat-{company_id}-{admin_id}--{fn}"
                else:
                    # Fallback: if no admin_id, use old format for backward compatibility
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
                
                from app.repositories.role_constants import COMPANY_USER_ROLE_MODULE_NAMES

                # Aggregate modules from all assigned roles (only modules company users can use in-app)
                for role in roles:
                    role_modules = role.get("modules", {})
                    if isinstance(role_modules, dict):
                        for module_name, module_config in role_modules.items():
                            if module_name not in COMPANY_USER_ROLE_MODULE_NAMES:
                                continue
                            if module_config.get("enabled", False):
                                modules_obj[module_name] = module_config
        
        # Get is_teamlid flag and teamlid_permissions (for frontend permission helpers when guest_permissions not in response)
        is_teamlid = user.get("is_teamlid", False)
        teamlid_permissions = user.get("teamlid_permissions") if is_teamlid else None
        
        # Get company-level modules for role assignment (what modules are enabled at company level)
        from app.repositories.modules_repo import ModulesRepository
        from app.repositories.constants import serialize_modules
        modules_repo = ModulesRepository(self.db)
        company_modules = await modules_repo.get_company_modules(company_id)
        
        result = {
            "user_id": user_id,
            "company_id": company_id,
            "user_type": user_type,
            # Workspace owner for analytics (Document Chat questions, etc.)
            "added_by_admin_id": user_id
            if user_type == "admin"
            else user.get("added_by_admin_id"),
            "documents": formatted_docs,
            "pass_ids": pass_ids,
            "modules": modules_obj,  # Return as object, not array (user's aggregated modules)
            "company_modules": serialize_modules(company_modules),  # Company-level modules for role assignment
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "is_teamlid": is_teamlid,  # Required for workspace switcher
            "assigned_roles": user.get("assigned_roles", []),  # Include assigned roles
        }
        if teamlid_permissions is not None:
            result["teamlid_permissions"] = teamlid_permissions
        return result
    
    async def get_all_user_documents(self, email: str) -> dict:
        """
        Get all documents for a user.
        
        For company users, this includes:
        - Private documents (upload_type="document") uploaded by the user
        - Documents from folders assigned via their assigned roles
        
        Args:
            email: User email
            
        Returns:
            Dictionary with user details and all documents
        """
        # Check if user is an admin first
        user = await self.admins.find_one({"email": email})
        user_type = "admin"
        if not user:
            user = await self.users.find_one({"email": email})
            user_type = "company_user"
        
        if not user:
            return {"documents": []}
        
        user_id = user.get("user_id")
        company_id = user.get("company_id")
        
        # Get private documents (upload_type="document")
        private_docs_query = {
            "user_id": user_id,
            "company_id": company_id,
            "upload_type": "document"
        }
        private_docs_cursor = self.documents.find(private_docs_query)
        private_docs = []
        async for doc in private_docs_cursor:
            private_docs.append({
                "file_name": doc.get("file_name", ""),
                "path": doc.get("path", ""),
                "upload_type": doc.get("upload_type", "document"),
                "storage_path": doc.get("storage_path"),
            })
        
        role_based_docs = []
        if user_type == "admin":
            # For company admins: get all documents in folders created by this admin
            folder_docs_query = {
                "user_id": user_id,
                "company_id": company_id,
                "upload_type": {"$ne": "document"}
            }
            folder_docs_cursor = self.documents.find(folder_docs_query)
            async for doc in folder_docs_cursor:
                role_based_docs.append({
                    "file_name": doc.get("file_name", ""),
                    "path": doc.get("path", ""),
                    "upload_type": doc.get("upload_type", ""),
                    "storage_path": doc.get("storage_path"),
                })
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
                    async for doc in role_based_docs_cursor:
                        role_based_docs.append({
                            "file_name": doc.get("file_name", ""),
                            "path": doc.get("path", ""),
                            "upload_type": doc.get("upload_type", ""),
                            "storage_path": doc.get("storage_path"),
                        })
        
        # Combine private and role-based documents
        all_documents = private_docs + role_based_docs
        
        return {
            "user_id": user_id,
            "company_id": company_id,
            "documents": all_documents,
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
            # When Teamlid is removed from assigned_roles, clear teamlid state
            if "Teamlid" not in assigned_roles:
                update_data["is_teamlid"] = False
        if is_teamlid is not None:
            update_data["is_teamlid"] = is_teamlid
        if teamlid_permissions is not None:
            update_data["teamlid_permissions"] = teamlid_permissions
        
        # When removing Teamlid from assigned_roles, delete guest_access and unset teamlid fields
        unset_data = {}
        if assigned_roles is not None and "Teamlid" not in assigned_roles:
            await self.guest_access.delete_many({
                "company_id": company_id,
                "guest_user_id": user_id
            })
            unset_data = {
                "teamlid_permissions": "",
                "assigned_teamlid_by_id": "",
                "assigned_teamlid_by_name": "",
                "assigned_teamlid_at": ""
            }

        update_op = {"$set": update_data}
        if unset_data:
            update_op["$unset"] = unset_data
        result = await self.users.update_one(
            {"company_id": company_id, "user_id": user_id},
            update_op
        )
        
        if result.modified_count == 0:
            return None
        
        updated_user = await self.users.find_one({"company_id": company_id, "user_id": user_id})
        
        # Convert ObjectId to string for JSON serialization
        if updated_user and "_id" in updated_user:
            updated_user["_id"] = str(updated_user["_id"])
        
        return updated_user
    
    async def _normalize_assigned_public_chat_ids(
        self,
        company_id: str,
        owner_admin_id: str,
        chat_ids: List[str],
    ) -> List[str]:
        """Ensure every id exists under this owner's ``public_chats``. De-duplicates, preserves order."""
        seen = set()
        out: List[str] = []
        for raw in chat_ids or []:
            cid = str(raw).strip()
            if not cid or cid in seen:
                continue
            if not ObjectId.is_valid(cid):
                raise ValueError(f"Ongeldige public chat id: {raw}")
            doc = await self.db.public_chats.find_one({
                "_id": ObjectId(cid),
                "company_id": company_id,
                "admin_id": owner_admin_id,
            })
            if not doc:
                raise ValueError(f"Public chat niet gevonden: {cid}")
            seen.add(cid)
            out.append(cid)
        return out

    async def assign_teamlid_permissions(
        self,
        company_id: str,
        admin_id: str,
        email: str,
        permissions: dict,
        assigned_public_chat_ids: Optional[List[str]] = None,
    ) -> bool:
        """
        Assign teamlid permissions to a user or admin.

        ``assigned_public_chat_ids`` when provided updates which public chats a teamlid may see under
        this workspace owner. When omitted, existing assignments are unchanged. When PublicChat
        permission is false, assignments are cleared.
        """
        from app.repositories.modules_repo import ModulesRepository

        modules_repo = ModulesRepository(self.db)
        company_modules = await modules_repo.get_company_modules(company_id)
        assigner = await self.admins.find_one({"company_id": company_id, "user_id": admin_id})
        assigner_modules = (assigner or {}).get("modules") or {}
        permissions = _clamp_teamlid_permissions(permissions or {}, assigner_modules, company_modules)

        can_publicchat = _to_bool(permissions.get("publicchat_modify_permission", False))
        validated_chats: Optional[List[str]] = None
        if assigned_public_chat_ids is not None:
            validated_chats = await self._normalize_assigned_public_chat_ids(
                company_id, admin_id, assigned_public_chat_ids
            )

        async def persist_guest_updates(guest_user_id: str) -> None:
            can_role = _to_bool(permissions.get("role_folder_modify_permission", False))
            can_user = _to_bool(permissions.get("user_create_modify_permission", False))
            can_doc = _to_bool(permissions.get("document_modify_permission", False))
            can_webchat = _to_bool(permissions.get("webchat_modify_permission", False))
            set_payload = {
                "can_role_write": can_role,
                "can_user_write": can_user,
                "can_document_write": can_doc,
                "can_folder_write": can_role,
                "can_publicchat_write": can_publicchat,
                "can_webchat_write": can_webchat,
                "is_active": True,
                "updated_at": datetime.utcnow(),
                "created_by": admin_id,
            }
            unset_payload = {}
            if not can_publicchat:
                unset_payload["assigned_public_chat_ids"] = ""
            elif validated_chats is not None:
                set_payload["assigned_public_chat_ids"] = validated_chats

            upd_doc = {"$set": set_payload, "$setOnInsert": {"created_at": datetime.utcnow()}}
            if unset_payload:
                upd_doc["$unset"] = unset_payload

            await self.guest_access.update_one(
                {
                    "company_id": company_id,
                    "owner_admin_id": admin_id,
                    "guest_user_id": guest_user_id,
                },
                upd_doc,
                upsert=True,
            )

        user = await self.users.find_one({"company_id": company_id, "email": email})
        if user:
            update_data = {
                "is_teamlid": True,
                "assigned_teamlid_by_id": admin_id,
                "teamlid_permissions": permissions,
                "updated_at": datetime.utcnow(),
            }
            if can_publicchat and validated_chats is not None:
                update_data["teamlid_public_chat_ids"] = validated_chats

            mongo_user_upd = {
                "$set": update_data,
                "$addToSet": {"assigned_roles": "Teamlid"},
            }
            if not can_publicchat:
                mongo_user_upd["$unset"] = {"teamlid_public_chat_ids": ""}

            result = await self.users.update_one(
                {"company_id": company_id, "email": email},
                mongo_user_upd,
            )
            if result.matched_count > 0:
                await persist_guest_updates(user["user_id"])
            return bool(result.matched_count)

        admin = await self.admins.find_one({"company_id": company_id, "email": email})
        if admin:
            update_data = {
                "is_teamlid": True,
                "assigned_teamlid_by_id": admin_id,
                "teamlid_permissions": permissions,
                "updated_at": datetime.utcnow(),
            }
            if can_publicchat and validated_chats is not None:
                update_data["teamlid_public_chat_ids"] = validated_chats

            mongo_admin_upd: dict = {"$set": update_data}
            if not can_publicchat:
                mongo_admin_upd["$unset"] = {"teamlid_public_chat_ids": ""}

            result = await self.admins.update_one(
                {"company_id": company_id, "email": email},
                mongo_admin_upd,
            )
            if result.matched_count > 0:
                await persist_guest_updates(admin["user_id"])
            return bool(result.matched_count)

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
                    "teamlid_permissions": "",
                    "teamlid_public_chat_ids": "",
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
                    "teamlid_permissions": "",
                    "teamlid_public_chat_ids": "",
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
                        os.remove(file_path)
                        logger.info(f"Deleted physical file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete physical file {file_path}: {str(e)}")

                delete_result = await self.documents.delete_one({"_id": document["_id"]})
                if delete_result.deleted_count > 0:
                    deleted_count += 1
                    cid = user.get("company_id")
                    uid_str = str(user_id)
                    if user_rec:
                        added_by = user_rec.get("added_by_admin_id") or uid_str
                    else:
                        added_by = (admin_rec or {}).get("added_by_admin_id") or uid_str
                    if cid and added_by:
                        try:
                            from app.services.user_activity_log import (
                                record_user_activity_event,
                            )

                            await record_user_activity_event(
                                self.db,
                                company_id=cid,
                                added_by_admin_id=str(added_by),
                                user_id=uid_str,
                                kind="delete_private",
                                what=f'Privédocument verwijderd "{file_name}"',
                            )
                        except Exception:
                            logger.exception(
                                "record_user_activity_event after private delete"
                            )

            except Exception as e:
                logger.error(f"Error deleting document {file_name}: {str(e)}")
                continue

        return deleted_count

