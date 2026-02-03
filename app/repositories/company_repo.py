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
from typing import List
from motor.motor_asyncio import AsyncIOMotorDatabase

# Import domain repositories
from app.repositories.base_repo import BaseRepository
from app.repositories.limits_repo import LimitsRepository
from app.repositories.modules_repo import ModulesRepository
from app.repositories.company_core_repo import CompanyCoreRepository

logger = logging.getLogger(__name__)


class CompanyRepository(BaseRepository):
    """
    Main repository facade that composes all domain-specific repositories.
    
    This class provides backward compatibility while gradually migrating
    methods to domain-specific repositories. Methods not yet migrated are
    delegated to the legacy implementation.
    """
    
    def __init__(self, db: AsyncIOMotorDatabase):
        """Initialize the repository with database connection and compose domain repositories."""
        super().__init__(db)
        
        # Initialize domain repositories
        self._limits_repo = LimitsRepository(db)
        self._modules_repo = ModulesRepository(db)
        self._company_core_repo = CompanyCoreRepository(db)
        
        # Import legacy implementation for methods not yet migrated
        # This allows gradual migration while maintaining backward compatibility
        from app.repositories.company_repo_legacy import CompanyRepository as LegacyRepository
        self._legacy = LegacyRepository(db)
    
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
    
    # ==================== Direct Delegation Methods ====================
    # These methods are explicitly delegated to maintain clear interface
    # They will be migrated to domain repositories gradually
    
    async def get_admins_by_company(self, company_id: str):
        """Get all admins for a company."""
        return await self._legacy.get_admins_by_company(company_id)
    
    async def get_users_by_company(self, company_id: str):
        """Get all users for a company."""
        return await self._legacy.get_users_by_company(company_id)
    
    async def get_users_by_company_admin(self, admin_id: str):
        """Get all users created by a specific admin."""
        return await self._legacy.get_users_by_company_admin(admin_id)
    
    async def get_admin_by_id(self, company_id: str, user_id: str):
        """Get an admin by company and user ID."""
        return await self._legacy.get_admin_by_id(company_id, user_id)
    
    async def get_role_by_name(self, company_id: str, admin_id: str, role_name: str):
        """Get a role by name for a specific company and admin."""
        return await self._legacy.get_role_by_name(company_id, admin_id, role_name)
    
    # ==================== Explicit Delegation for Common Methods ====================
    # These methods are explicitly delegated to avoid __getattr__ issues with async methods
    
    async def add_folders(self, company_id: str, admin_id: str, folder_names: List[str], storage_provider=None):
        """Add folders for a company/admin."""
        return await self._legacy.add_folders(company_id, admin_id, folder_names, storage_provider)
    
    async def get_folders(self, company_id: str, admin_id: str):
        """Get folders for a company/admin."""
        return await self._legacy.get_folders(company_id, admin_id)
    
    async def delete_folders(self, company_id: str, folder_names: List[str], role_names: List[str], admin_id: str):
        """Delete folders for a company/admin."""
        return await self._legacy.delete_folders(company_id, folder_names, role_names, admin_id)
    
    # Use __getattr__ for remaining methods to avoid listing all 50+ methods
    def __getattr__(self, name: str):
        """
        Delegate unknown methods to legacy implementation.
        
        This allows gradual migration: methods are moved to domain repositories
        one by one, and any methods not yet migrated are automatically
        delegated to the legacy implementation.
        
        Note: For async methods, Python's descriptor protocol handles binding correctly.
        """
        if hasattr(self._legacy, name):
            attr = getattr(self._legacy, name)
            # If it's a method (callable), return it directly - Python will handle binding
            if callable(attr):
                return attr
            return attr
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
