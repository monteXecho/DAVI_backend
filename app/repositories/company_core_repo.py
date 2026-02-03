"""
Core company repository.

Handles basic company CRUD operations (create, read, delete, list).
"""

import copy
import logging
import uuid
from datetime import datetime
from app.repositories.base_repo import BaseRepository
from app.repositories.constants import DEFAULT_MODULES, serialize_modules, BASE_DOC_URL

logger = logging.getLogger(__name__)


class CompanyCoreRepository(BaseRepository):
    """Repository for core company operations."""
    
    async def create_company(self, name: str) -> dict:
        """
        Create a new company with default settings.
        
        Args:
            name: Company name
            
        Returns:
            Dictionary with company details including ID and default limits
        """
        now = datetime.utcnow()
        company_id = str(uuid.uuid4())
        # Initialize company with default modules (all disabled)
        company_modules = copy.deepcopy(DEFAULT_MODULES)
        for module_name in company_modules:
            company_modules[module_name]["enabled"] = False
        
        doc = {
            "company_id": company_id,
            "name": name,
            "max_users": -1,  # Default: infinite
            "max_admins": -1,  # Default: infinite
            "max_documents": -1,  # Default: infinite
            "max_roles": -1,  # Default: infinite
            "modules": company_modules,  # Company-level module permissions
            "created_at": now,
            "updated_at": now,
        }
        await self.companies.insert_one(doc)
        return {
            "id": company_id,
            "name": name,
            "max_users": -1,
            "max_admins": -1,
            "max_documents": -1,
            "max_roles": -1,
            "modules": serialize_modules(company_modules),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    async def get_all_companies(self):
        """
        Get all companies with their admins, users, and statistics.
        
        Returns:
            Dictionary with companies list, each containing:
            - Company details (id, name, limits, modules)
            - Admins with statistics (admins created, teamlid count, users created, roles created, documents count)
            - Users with documents
        """
        companies_cursor = self.companies.find().sort("name", 1)
        companies = []

        async for company in companies_cursor:
            company_id = company["company_id"]

            admins_cursor = self.admins.find({"company_id": company_id})
            admins = []
            async for admin in admins_cursor:
                admin_id = admin["user_id"]
                # Get all documents uploaded by this admin (private + role assigned + unrole assigned)
                docs_cursor = self.documents.find({"user_id": admin_id, "company_id": company_id})
                documents = [
                    {
                        "file_name": d["file_name"],
                        "file_url": f"{BASE_DOC_URL}/{d['user_id']}/{d['file_name']}",
                        "upload_type": d.get("upload_type", "document"),
                    }
                    async for d in docs_cursor
                ]
                # Count roles created by this admin
                roles_count = await self.roles.count_documents({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id
                })
                # Count admins created by this admin
                created_admins_count = await self.admins.count_documents({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id
                })
                # Count users created by this admin
                created_users_count = await self.users.count_documents({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id
                })
                # Count teamlid users/admins created by this admin
                # For users: check is_teamlid and added_by_admin_id
                teamlid_users_count = await self.users.count_documents({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id,
                    "is_teamlid": True
                })
                # For admins: check if admin has is_teamlid=true and was created by this admin, OR
                # check guest_access where owner_admin_id = admin_id (teamlid permissions assigned by this admin)
                teamlid_admins_direct = await self.admins.count_documents({
                    "company_id": company_id,
                    "added_by_admin_id": admin_id,
                    "is_teamlid": True
                })
                teamlid_admins_via_guest = await self.guest_access.count_documents({
                    "company_id": company_id,
                    "owner_admin_id": admin_id,
                    "is_active": True
                })
                teamlid_count = teamlid_users_count + teamlid_admins_direct + teamlid_admins_via_guest
                
                admins.append({
                    "id": admin_id,
                    "user_id": admin_id,
                    "name": admin["name"],
                    "email": admin["email"],
                    "modules": serialize_modules(admin["modules"]),
                    "documents": documents,
                    "added_by_admin_id": admin.get("added_by_admin_id"),
                    "is_teamlid": admin.get("is_teamlid", False),
                    "stats": {
                        "admins_created": created_admins_count,
                        "teamlid_count": teamlid_count,
                        "users_created": created_users_count,
                        "roles_created": roles_count,
                        "documents_count": len(documents),
                    }
                })

            users_cursor = self.users.find({"company_id": company_id})
            users = []
            async for user in users_cursor:
                docs_cursor = self.documents.find({"user_id": user["user_id"], "company_id": company_id})
                documents = [
                    {
                        "file_name": d["file_name"],
                        "file_url": f"{BASE_DOC_URL}/{d['user_id']}/{d['file_name']}",
                        "upload_type": d.get("upload_type", "document"),
                    }
                    async for d in docs_cursor
                ]
                users.append({
                    "id": user["user_id"],
                    "name": user["name"],
                    "email": user["email"],
                    "documents": documents,
                    "added_by_admin_id": user.get("added_by_admin_id"),
                    "is_teamlid": user.get("is_teamlid", False),
                })

            # Get company modules
            company_modules = company.get("modules", DEFAULT_MODULES)
            # If company doesn't have modules yet, initialize with defaults
            if not company_modules:
                company_modules = copy.deepcopy(DEFAULT_MODULES)
                for module_name in company_modules:
                    company_modules[module_name]["enabled"] = False
            
            companies.append({
                "id": company_id,
                "name": company["name"],
                "max_users": company.get("max_users", -1),
                "max_admins": company.get("max_admins", -1),
                "max_documents": company.get("max_documents", -1),
                "max_roles": company.get("max_roles", -1),
                "modules": serialize_modules(company_modules),
                "admins": admins,
                "users": users,
            })

        return {"companies": companies}

    async def delete_company(self, company_id: str) -> bool:
        """
        Delete a company and all associated data.
        
        Args:
            company_id: Company identifier
            
        Returns:
            True if company was deleted, False otherwise
        """
        await self.admins.delete_many({"company_id": company_id})
        await self.users.delete_many({"company_id": company_id})
        await self.documents.delete_many({"company_id": company_id})
        result = await self.companies.delete_one({"company_id": company_id})
        return result.deleted_count > 0

