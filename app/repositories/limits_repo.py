"""
Resource limits repository.

Handles all operations related to company resource limits (users, admins, documents, roles).
"""

import logging
from datetime import datetime
from app.repositories.base_repo import BaseRepository

logger = logging.getLogger(__name__)


class LimitsRepository(BaseRepository):
    """Repository for managing company resource limits."""
    
    async def get_company_limits(self, company_id: str) -> dict:
        """
        Get resource limits for a company.
        
        Args:
            company_id: Company identifier
            
        Returns:
            Dictionary with max_users, max_admins, max_documents, max_roles
            (defaults to -1 for infinite if not set)
        """
        company = await self.companies.find_one({"company_id": company_id})
        if not company:
            return {
                "max_users": -1,
                "max_admins": -1,
                "max_documents": -1,
                "max_roles": -1,
            }
        return {
            "max_users": company.get("max_users", -1),
            "max_admins": company.get("max_admins", -1),
            "max_documents": company.get("max_documents", -1),
            "max_roles": company.get("max_roles", -1),
        }

    async def check_users_limit(self, company_id: str) -> tuple[bool, str]:
        """
        Check if adding a user would exceed the limit.
        
        Args:
            company_id: Company identifier
            
        Returns:
            Tuple of (allowed: bool, error_message: str)
        """
        limits = await self.get_company_limits(company_id)
        max_users = limits["max_users"]
        
        if max_users == -1:  # Infinite
            return True, ""
        
        current_count = await self.users.count_documents({"company_id": company_id})
        if current_count >= max_users:
            return False, f"Maximum aantal gebruikers ({max_users}) bereikt. Neem contact op met de super admin om de limiet te verhogen."
        
        return True, ""

    async def check_admins_limit(self, company_id: str) -> tuple[bool, str]:
        """
        Check if adding an admin would exceed the limit.
        
        Args:
            company_id: Company identifier
            
        Returns:
            Tuple of (allowed: bool, error_message: str)
        """
        limits = await self.get_company_limits(company_id)
        max_admins = limits["max_admins"]
        
        if max_admins == -1:  # Infinite
            return True, ""
        
        current_count = await self.admins.count_documents({"company_id": company_id})
        if current_count >= max_admins:
            return False, f"Maximum aantal admins ({max_admins}) bereikt. Neem contact op met de super admin om de limiet te verhogen."
        
        return True, ""

    async def check_documents_limit(self, company_id: str) -> tuple[bool, str]:
        """
        Check if uploading a document would exceed the limit.
        
        Args:
            company_id: Company identifier
            
        Returns:
            Tuple of (allowed: bool, error_message: str)
        """
        limits = await self.get_company_limits(company_id)
        max_documents = limits["max_documents"]
        
        if max_documents == -1:  # Infinite
            return True, ""
        
        # Count all documents uploaded by users and admins in the company
        admin_ids = [admin["user_id"] async for admin in self.admins.find({"company_id": company_id}, {"user_id": 1})]
        user_ids = [user["user_id"] async for user in self.users.find({"company_id": company_id}, {"user_id": 1})]
        all_user_ids = admin_ids + user_ids
        
        if not all_user_ids:
            return True, ""
        
        current_count = await self.documents.count_documents({
            "company_id": company_id,
            "user_id": {"$in": all_user_ids}
        })
        
        if current_count >= max_documents:
            return False, f"Maximum aantal documenten ({max_documents}) bereikt. Neem contact op met de super admin om de limiet te verhogen."
        
        return True, ""

    async def check_roles_limit(self, company_id: str, admin_id: str) -> tuple[bool, str]:
        """
        Check if adding a role would exceed the limit.
        
        Args:
            company_id: Company identifier
            admin_id: Admin identifier
            
        Returns:
            Tuple of (allowed: bool, error_message: str)
        """
        limits = await self.get_company_limits(company_id)
        max_roles = limits["max_roles"]
        
        if max_roles == -1:  # Infinite
            return True, ""
        
        # Count roles created by this admin
        current_count = await self.roles.count_documents({
            "company_id": company_id,
            "added_by_admin_id": admin_id
        })
        
        if current_count >= max_roles:
            return False, f"Maximum aantal rollen ({max_roles}) bereikt. Neem contact op met de super admin om de limiet te verhogen."
        
        return True, ""

    async def update_company_limits(
        self,
        company_id: str,
        max_users: int = None,
        max_admins: int = None,
        max_documents: int = None,
        max_roles: int = None
    ) -> dict:
        """
        Update resource limits for a company.
        
        Args:
            company_id: Company identifier
            max_users: Maximum number of users (None to skip)
            max_admins: Maximum number of admins (None to skip)
            max_documents: Maximum number of documents (None to skip)
            max_roles: Maximum number of roles (None to skip)
            
        Returns:
            Updated limits dictionary
        """
        update_data = {"updated_at": datetime.utcnow()}
        if max_users is not None:
            update_data["max_users"] = max_users
        if max_admins is not None:
            update_data["max_admins"] = max_admins
        if max_documents is not None:
            update_data["max_documents"] = max_documents
        if max_roles is not None:
            update_data["max_roles"] = max_roles
        
        await self.companies.update_one(
            {"company_id": company_id},
            {"$set": update_data}
        )
        
        return await self.get_company_limits(company_id)

