"""
Guest Access Repository - Domain-specific repository for guest access management.

This module handles all guest access operations including:
- Creating and updating guest access permissions
- Listing guest workspaces
- Managing workspace sharing
"""

import logging
from datetime import datetime
from typing import Optional

from app.repositories.base_repo import BaseRepository

logger = logging.getLogger(__name__)


class GuestAccessRepository(BaseRepository):
    """Repository for guest access management operations."""
    
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
        
        Args:
            company_id: Company identifier
            owner_admin_id: Admin who owns the workspace
            guest_user_id: User who gets guest access
            can_role_write: Permission to modify roles
            can_user_write: Permission to modify users
            can_document_write: Permission to modify documents
            can_folder_write: Permission to modify folders
            created_by: User ID who created this access
            
        Returns:
            Dictionary with guest access details
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
        
        Args:
            company_id: Company identifier
            guest_user_id: Guest user identifier
            
        Returns:
            List of guest access entries
        """
        # Query for active entries (is_active=True) OR entries without is_active field (backward compatibility)
        # Exclude entries where is_active is explicitly False
        query = {
            "company_id": company_id,
            "guest_user_id": guest_user_id,
            "$or": [
                {"is_active": True},
                {"is_active": {"$exists": False}},  # Backward compatibility: treat missing as active
            ]
        }
        
        logger.info(
            f"[guest_access_repo] Querying guest workspaces: company_id={company_id}, guest_user_id={guest_user_id}"
        )
        logger.info(f"[guest_access_repo] Query: {query}")
        
        # First, let's check if ANY entries exist (without is_active filter) for debugging
        total_count = await self.guest_access.count_documents({
            "company_id": company_id,
            "guest_user_id": guest_user_id
        })
        logger.info(f"[guest_access_repo] Total entries (any is_active status): {total_count}")
        
        # Check entries with is_active explicitly set
        active_count = await self.guest_access.count_documents({
            "company_id": company_id,
            "guest_user_id": guest_user_id,
            "is_active": True
        })
        logger.info(f"[guest_access_repo] Active entries (is_active=True): {active_count}")
        
        # Check entries without is_active field
        missing_active_count = await self.guest_access.count_documents({
            "company_id": company_id,
            "guest_user_id": guest_user_id,
            "is_active": {"$exists": False}
        })
        logger.info(f"[guest_access_repo] Entries without is_active field: {missing_active_count}")
        
        cursor = self.guest_access.find(query)
        
        entries = []
        async for doc in cursor:
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
                
                doc.update(update_fields)
                if unset_fields:
                    for field in unset_fields.keys():
                        doc.pop(field, None)
            
            entries.append(doc)
        
        logger.info(
            f"[guest_access_repo] Found {len(entries)} guest access entries for guest_user_id={guest_user_id}, "
            f"company_id={company_id}"
        )
        
        if entries:
            for idx, entry in enumerate(entries):
                logger.info(
                    f"[guest_access_repo] Entry {idx+1}: owner_admin_id={entry.get('owner_admin_id')}, "
                    f"is_active={entry.get('is_active', 'missing')}, "
                    f"can_role_write={entry.get('can_role_write')}, "
                    f"can_user_write={entry.get('can_user_write')}"
                )
        else:
            logger.warning(
                f"[guest_access_repo] No guest access entries found for guest_user_id={guest_user_id}, "
                f"company_id={company_id}. This might indicate: "
                f"1) No teamlid permissions assigned, 2) All entries have is_active=False, "
                f"3) guest_user_id mismatch in database"
            )
        
        return entries
    
    async def get_guest_access(
        self,
        company_id: str,
        guest_user_id: str,
        owner_admin_id: str,
    ) -> Optional[dict]:
        """
        Get guest access entry for a specific user and workspace owner.
        
        Args:
            company_id: Company identifier
            guest_user_id: Guest user identifier
            owner_admin_id: Workspace owner admin identifier
            
        Returns:
            Guest access dictionary or None if not found
        """
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
        
        if "can_user_read" in entry and "can_user_write" not in entry:
            update_fields["can_user_write"] = entry["can_user_read"]
            needs_migration = True
        
        if "can_document_read" in entry and "can_document_write" not in entry:
            update_fields["can_document_write"] = entry["can_document_read"]
            needs_migration = True
        
        if needs_migration:
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
        """
        Disable guest access for a user.
        
        Args:
            company_id: Company identifier
            owner_admin_id: Workspace owner admin identifier
            guest_user_id: Guest user identifier
            
        Returns:
            Number of entries modified
        """
        res = await self.guest_access.update_one(
            {
                "company_id": company_id,
                "owner_admin_id": owner_admin_id,
                "guest_user_id": guest_user_id,
            },
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
        )
        return res.modified_count

