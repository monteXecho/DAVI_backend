"""
Main Company Repository - Facade Pattern with Gradual Migration

This module provides a unified interface to all company-related operations
by composing domain-specific repositories. Methods are gradually being migrated
from the legacy implementation to domain repositories.

Architecture:
- Facade pattern: Single entry point for all operations
- Domain repositories: Specialized repositories for each domain
- Gradual migration: Methods are migrated incrementally while maintaining compatibility
"""

import logging
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

# Import domain repositories
from app.repositories.base_repo import BaseRepository
from app.repositories.limits_repo import LimitsRepository
from app.repositories.modules_repo import ModulesRepository
from app.repositories.company_core_repo import CompanyCoreRepository
from app.repositories.user_repo import UserRepository
from app.repositories.admin_repo import AdminRepository
from app.repositories.role_repo import RoleRepository
from app.repositories.folder_repo import FolderRepository
from app.repositories.guest_access_repo import GuestAccessRepository
from app.repositories.nextcloud_sync_repo import NextcloudSyncRepository

logger = logging.getLogger(__name__)


class CompanyRepository(BaseRepository):
    """
    Main repository facade that composes all domain-specific repositories.
    
    This class provides backward compatibility while gradually migrating
    methods to domain-specific repositories. Methods not yet migrated are
    delegated to the legacy implementation.
    
    Architecture:
    - Domain repositories handle specific business domains
    - Facade pattern provides unified interface
    - Legacy repository used as fallback for unmigrated methods
    """
    
    def __init__(self, db: AsyncIOMotorDatabase):
        """Initialize the repository with database connection and compose domain repositories."""
        super().__init__(db)
        
        # Initialize domain repositories
        self._limits_repo = LimitsRepository(db)
        self._modules_repo = ModulesRepository(db)
        self._company_core_repo = CompanyCoreRepository(db)
        self._user_repo = UserRepository(db)
        self._admin_repo = AdminRepository(db)
        self._role_repo = RoleRepository(db)
        self._folder_repo = FolderRepository(db)
        self._guest_access_repo = GuestAccessRepository(db)
        self._nextcloud_sync_repo = NextcloudSyncRepository(db)
    
    # ==================== Resource Limits (Migrated) ====================
    
    async def get_company_limits(self, company_id: str) -> dict:
        """Get resource limits for a company."""
        return await self._limits_repo.get_company_limits(company_id)
    
    async def check_users_limit(self, company_id: str) -> tuple[bool, str]:
        """Check if adding a user would exceed the limit."""
        return await self._limits_repo.check_users_limit(company_id)
    
    async def check_admins_limit(self, company_id: str) -> tuple[bool, str]:
        """Check if adding an admin would exceed the limit."""
        return await self._limits_repo.check_admins_limit(company_id)
    
    async def check_documents_limit(self, company_id: str) -> tuple[bool, str]:
        """Check if uploading a document would exceed the limit."""
        return await self._limits_repo.check_documents_limit(company_id)
    
    async def check_roles_limit(self, company_id: str, admin_id: str) -> tuple[bool, str]:
        """Check if adding a role would exceed the limit."""
        return await self._limits_repo.check_roles_limit(company_id, admin_id)
    
    async def update_company_limits(
        self,
        company_id: str,
        max_users: int = None,
        max_admins: int = None,
        max_documents: int = None,
        max_roles: int = None
    ) -> dict:
        """Update resource limits for a company."""
        return await self._limits_repo.update_company_limits(
            company_id, max_users, max_admins, max_documents, max_roles
        )
    
    # ==================== Modules (Migrated) ====================
    
    async def get_company_modules(self, company_id: str) -> dict:
        """Get module permissions for a company."""
        return await self._modules_repo.get_company_modules(company_id)
    
    async def update_company_modules(self, company_id: str, modules: dict) -> dict:
        """Update module permissions for a company."""
        return await self._modules_repo.update_company_modules(company_id, modules)
    
    # ==================== Companies (Migrated) ====================
    
    async def create_company(self, name: str) -> dict:
        """Create a new company with default settings."""
        return await self._company_core_repo.create_company(name)
    
    async def get_all_companies(self):
        """Get all companies with their admins, users, and statistics."""
        return await self._company_core_repo.get_all_companies()
    
    async def delete_company(self, company_id: str) -> bool:
        """Delete a company and all associated data."""
        return await self._company_core_repo.delete_company(company_id)
    
    # ==================== Delegation to Legacy (Temporary) ====================
    # Methods not yet migrated are delegated to the legacy implementation.
    # These will be gradually migrated to domain repositories.
    
    # ==================== User Operations (Migrated) ====================
    
    async def add_user(self, company_id: str, name: str, email: str):
        """Create a new company user."""
        return await self._user_repo.add_user(company_id, name, email)
    
    async def add_user_by_admin(
        self,
        company_id: str,
        added_by_admin_id: str,
        email: str,
        company_role: str,
        assigned_role: str
    ):
        """Add a user by an admin with role assignment."""
        return await self._user_repo.add_user_by_admin(
            company_id, added_by_admin_id, email, company_role, assigned_role
        )
    
    async def delete_users(
        self,
        company_id: str,
        user_ids: List[str],
        admin_id: str = None
    ) -> int:
        """Delete users and decrease role user counts."""
        return await self._user_repo.delete_users(company_id, user_ids, admin_id)
    
    async def delete_users_by_admin(self, company_id: str, admin_id: str, kc_admin=None) -> int:
        """Delete all users added by a specific admin."""
        return await self._user_repo.delete_users_by_admin(company_id, admin_id, kc_admin)
    
    async def get_users_by_company(self, company_id: str):
        """Get all users for a company."""
        return await self._user_repo.get_users_by_company(company_id)
    
    async def get_users_by_company_admin(self, admin_id: str):
        """Get all users created by a specific admin."""
        return await self._user_repo.get_users_by_company_admin(admin_id)
    
    async def get_all_users_created_by_admin_id(self, company_id: str, admin_id: str):
        """Get all users and admins that can be managed by this admin."""
        return await self._user_repo.get_all_users_created_by_admin_id(company_id, admin_id)
    
    async def find_user_by_email(self, email: str):
        """Find a user by email."""
        return await self._user_repo.find_user_by_email(email)
    
    async def get_user_with_documents(self, email: str):
        """Get user with all their documents."""
        return await self._user_repo.get_user_with_documents(email)
    
    async def get_all_user_documents(self, email: str):
        """Get all documents for a user."""
        return await self._user_repo.get_all_user_documents(email)
    
    async def update_user(
        self,
        company_id: str,
        user_id: str,
        user_type: str = None,
        name: str = None,
        email: str = None,
        assigned_roles: List[str] = None,
        is_teamlid: bool = None,
        teamlid_permissions: dict = None
    ):
        """Update user or admin information."""
        if user_type == "company_admin":
            # Update admin
            return await self._admin_repo.update_admin(company_id, user_id, name, email)
        else:
            # Update user
            return await self._user_repo.update_user(
                company_id, user_id, name, email, assigned_roles, is_teamlid, teamlid_permissions
            )
    
    async def assign_teamlid_permissions(
        self,
        company_id: str,
        admin_id: str,
        email: str,
        permissions: dict
    ) -> bool:
        """Assign teamlid permissions to a user."""
        return await self._user_repo.assign_teamlid_permissions(
            company_id, admin_id, email, permissions
        )
    
    async def remove_teamlid_role(self, company_id: str, admin_id: str, user_id: str) -> bool:
        """Remove teamlid role from a user."""
        return await self._user_repo.remove_teamlid_role(company_id, admin_id, user_id)
    
    # ==================== Admin Operations (Migrated) ====================
    
    async def add_admin(
        self,
        company_id: str,
        admin_id: str,
        name: str,
        email: str,
        modules: dict = None
    ):
        """Create a new company admin."""
        # Get company modules for filtering
        company_modules = await self._modules_repo.get_company_modules(company_id)
        return await self._admin_repo.create_admin(
            company_id, admin_id, name, email, modules, company_modules
        )
    
    async def reassign_admin(self, company_id: str, admin_id: str, name: str, email: str):
        """Update admin information."""
        return await self._admin_repo.update_admin(company_id, admin_id, name, email)
    
    async def delete_admin(self, company_id: str, user_id: str, admin_id: str = None) -> bool:
        """Delete an admin."""
        return await self._admin_repo.delete_admin(company_id, user_id, admin_id)
    
    async def assign_modules(self, company_id: str, user_id: str, modules: dict):
        """Assign module permissions to an admin."""
        company_modules = await self._modules_repo.get_company_modules(company_id)
        return await self._admin_repo.assign_modules(
            company_id, user_id, modules, company_modules
        )
    
    async def find_admin_by_email(self, email: str):
        """Find admin by email."""
        return await self._admin_repo.find_admin_by_email(email)
    
    async def get_admins_by_company(self, company_id: str):
        """Get all admins for a company."""
        return await self._admin_repo.get_admins_by_company(company_id)
    
    async def get_admin_by_id(self, company_id: str, user_id: str):
        """Get an admin by company and user ID."""
        return await self._admin_repo.get_admin_by_id(company_id, user_id)
    
    # ==================== Role Operations (Migrated) ====================
    
    async def add_or_update_role(
        self,
        company_id: str,
        admin_id: str,
        role_name: str,
        folders: List[str],
        modules: Optional[List] = None,
        action: str = "create"
    ):
        """Add or update a role."""
        company_modules = await self._modules_repo.get_company_modules(company_id)
        return await self._role_repo.add_or_update_role(
            company_id, admin_id, role_name, folders, modules, action, company_modules
        )
    
    async def list_roles(self, company_id: str, admin_id: str):
        """List all roles for a company/admin."""
        return await self._role_repo.list_roles(company_id, admin_id)
    
    async def delete_roles(self, company_id: str, role_names: List[str], admin_id: str):
        """Delete roles for a company/admin."""
        return await self._role_repo.delete_roles(company_id, role_names, admin_id)
    
    async def assign_role_to_user(self, company_id: str, user_id: str, role_name: str):
        """Assign a role to a user."""
        return await self._role_repo.assign_role_to_user(company_id, user_id, role_name)
    
    async def get_role_by_name(self, company_id: str, admin_id: str, role_name: str):
        """Get a role by name for a specific company and admin."""
        return await self._role_repo.get_role_by_name(company_id, admin_id, role_name)
    
    async def delete_roles_by_admin(self, company_id: str, admin_id: str) -> int:
        """Delete all roles created by a specific admin."""
        return await self._role_repo.delete_roles_by_admin(company_id, admin_id)
    
    async def get_roles_to_assign(self, company_id: str, admin_id: str, selected_role: str):
        """Get roles to assign based on selection logic."""
        return await self._role_repo.get_roles_to_assign(company_id, admin_id, selected_role)
    
    async def update_role_user_counts(
        self,
        company_id: str,
        admin_id: str,
        roles: List[str],
        user_count: int
    ):
        """Update role user counts."""
        return await self._role_repo.update_role_user_counts(company_id, admin_id, roles, user_count)
    
    # ==================== Folder Operations (Migrated) ====================
    
    async def add_folders(self, company_id: str, admin_id: str, folder_names: List[str], storage_provider=None):
        """Add folders for a company/admin."""
        return await self._folder_repo.add_folders(company_id, admin_id, folder_names, storage_provider)
    
    async def get_folders(self, company_id: str, admin_id: str):
        """Get folders for a company/admin."""
        return await self._folder_repo.get_folders(company_id, admin_id)
    
    async def delete_folders(
        self,
        company_id: str,
        folder_names: List[str],
        role_names: List[str],
        admin_id: str,
        storage_provider=None
    ):
        """Delete folders for a company/admin."""
        return await self._folder_repo.delete_folders(
            company_id, folder_names, role_names, admin_id, storage_provider
        )
    
    async def upload_document_for_folder(
        self,
        company_id: str,
        admin_id: str,
        folder_name: str,
        file,
        storage_provider=None
    ):
        """Upload a document to a folder."""
        return await self._folder_repo.upload_document_for_folder(
            company_id, admin_id, folder_name, file, storage_provider
        )
    
    # ==================== Guest Access Operations (Migrated) ====================
    
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
    ):
        """Create or update guest access."""
        return await self._guest_access_repo.upsert_guest_access(
            company_id, owner_admin_id, guest_user_id,
            can_role_write, can_user_write, can_document_write, can_folder_write,
            created_by
        )
    
    async def list_guest_workspaces_for_user(self, company_id: str, guest_user_id: str):
        """List all guest workspaces for a user."""
        return await self._guest_access_repo.list_guest_workspaces_for_user(company_id, guest_user_id)
    
    async def get_guest_access(
        self,
        company_id: str,
        guest_user_id: str,
        owner_admin_id: str,
    ):
        """Get guest access entry."""
        return await self._guest_access_repo.get_guest_access(company_id, guest_user_id, owner_admin_id)
    
    async def disable_guest_access(
        self,
        company_id: str,
        owner_admin_id: str,
        guest_user_id: str,
    ) -> int:
        """Disable guest access."""
        return await self._guest_access_repo.disable_guest_access(company_id, owner_admin_id, guest_user_id)
    
    # ==================== Nextcloud Sync Operations (Migrated) ====================
    
    async def sync_documents_from_nextcloud(
        self,
        company_id: str,
        admin_id: str,
        folder_id: Optional[str] = None,
        storage_provider=None
    ):
        """Sync documents from Nextcloud to DAVI."""
        return await self._nextcloud_sync_repo.sync_documents_from_nextcloud(
            company_id, admin_id, folder_id, storage_provider
        )
    
    # ==================== Additional Methods (Migrated) ====================
    
    async def add_users_from_email_file(
        self,
        company_id: str,
        admin_id: str,
        file_content: bytes,
        file_extension: str,
        selected_role: str = None
    ):
        """Add multiple users from CSV/Excel file."""
        return await self._user_repo.add_users_from_email_file(
            company_id, admin_id, file_content, file_extension, selected_role
        )
    
    async def get_all_private_documents(self, email: str, document_type: str):
        """Get all private documents for a user/admin."""
        return await self._user_repo.get_all_private_documents(email, document_type)
    
    async def delete_private_documents(self, email: str, documents_to_delete: List[dict]) -> int:
        """Delete private documents for a user/admin."""
        return await self._user_repo.delete_private_documents(email, documents_to_delete)
    
    async def get_admin_documents(self, company_id: str, admin_id: str):
        """Get all documents organized by roles and folders for an admin."""
        return await self._admin_repo.get_admin_documents(company_id, admin_id)
    
    async def get_admin_with_documents_by_id(self, company_id: str, admin_user_id: str):
        """Get admin with documents by admin user ID."""
        return await self._admin_repo.get_admin_with_documents_by_id(company_id, admin_user_id)
    
    async def delete_role_documents_by_admin(self, company_id: str, admin_id: str) -> int:
        """Delete all role documents uploaded by a specific admin."""
        return await self._role_repo.delete_role_documents_by_admin(company_id, admin_id)
    
    async def delete_company_role_documents(self, company_id: str) -> int:
        """Delete all role documents for a company."""
        return await self._role_repo.delete_company_role_documents(company_id)
    
    async def delete_documents(
        self,
        company_id: str,
        admin_id: str,
        documents_to_delete: List[dict],
        storage_provider=None
    ) -> int:
        """Delete documents from folders."""
        return await self._folder_repo.delete_documents(
            company_id, admin_id, documents_to_delete, storage_provider
        )
    
    # ==================== Debug/Inspection Methods ====================
    # These are simple debug methods, implemented directly here
    
    async def get_all_collections_data(self) -> dict:
        """Return all documents from all collections (debug method)."""
        from datetime import datetime
        
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
        """Clear all data from all collections (debug method)."""
        await self.companies.delete_many({})
        await self.admins.delete_many({})
        await self.users.delete_many({})
        await self.documents.delete_many({})
        await self.roles.delete_many({})
        await self.folders.delete_many({})
        await self.guest_access.delete_many({})
        return {"status": "All collections cleared"}
