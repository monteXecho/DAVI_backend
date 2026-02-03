"""
Module management repository.

Handles all operations related to company and admin module permissions.
"""

import copy
import logging
from datetime import datetime
from app.repositories.base_repo import BaseRepository
from app.repositories.constants import DEFAULT_MODULES, serialize_modules

logger = logging.getLogger(__name__)


class ModulesRepository(BaseRepository):
    """Repository for managing module permissions."""
    
    async def get_company_modules(self, company_id: str) -> dict:
        """
        Get module permissions for a company.
        
        Args:
            company_id: Company identifier
            
        Returns:
            Dictionary of module configurations
        """
        company = await self.companies.find_one({"company_id": company_id})
        if not company:
            return DEFAULT_MODULES
        return company.get("modules", DEFAULT_MODULES)

    async def update_company_modules(self, company_id: str, modules: dict) -> dict:
        """
        Update module permissions for a company.
        
        Args:
            company_id: Company identifier
            modules: Dictionary of module configurations to update
            
        Returns:
            Serialized list of updated modules
        """
        # Validate modules against DEFAULT_MODULES
        company_modules = copy.deepcopy(DEFAULT_MODULES)
        for module_name, module_config in modules.items():
            if module_name in company_modules:
                company_modules[module_name]["enabled"] = module_config.get("enabled", False)
        
        await self.companies.update_one(
            {"company_id": company_id},
            {"$set": {"modules": company_modules, "updated_at": datetime.utcnow()}}
        )
        
        # Get updated company to return modules
        company = await self.companies.find_one({"company_id": company_id})
        if company:
            return serialize_modules(company.get("modules", DEFAULT_MODULES))
        return serialize_modules(DEFAULT_MODULES)

    def filter_modules_by_company(self, admin_modules: dict, company_modules: dict) -> dict:
        """
        Filter admin modules to only include those enabled at company level.
        
        Args:
            admin_modules: Admin's module configuration
            company_modules: Company's module configuration
            
        Returns:
            Filtered admin modules dictionary
        """
        filtered = copy.deepcopy(admin_modules)
        for module_name in filtered:
            company_has_module = company_modules.get(module_name, {}).get("enabled", False)
            if not company_has_module:
                filtered[module_name]["enabled"] = False
        return filtered

