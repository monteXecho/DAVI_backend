"""
Shared utilities and dependencies for company admin API routes.

This module contains common functions and dependencies used across
all company admin domain routers.
"""

import logging
from fastapi import Depends
from app.deps.auth import get_current_user, get_keycloak_admin
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository

logger = logging.getLogger(__name__)


def _can_write_users(perms: dict) -> bool:
    """Check if user has write permission for users"""
    val = perms.get("user_create_modify_permission", "False")
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


def _can_write_roles_folders(perms: dict) -> bool:
    """Check if user has write permission for roles and folders"""
    val = perms.get("role_folder_modify_permission", "False")
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


def _can_write_documents(perms: dict) -> bool:
    """Check if user has write permission for documents"""
    val = perms.get("document_modify_permission", "False")
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


def _to_bool(value) -> bool:
    """Convert string/boolean to boolean, handling both old and new formats"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _get_guest_permission(guest_entry: dict, new_field: str, old_field: str = None) -> bool:
    """Get guest permission with backward compatibility for old field names"""
    value = guest_entry.get(new_field)
    if value is not None:
        return _to_bool(value)
    
    if old_field:
        old_value = guest_entry.get(old_field)
        if old_value is not None:
            return _to_bool(old_value)
    
    return False


async def get_repository(db=Depends(get_db)) -> CompanyRepository:
    """Dependency to get CompanyRepository instance."""
    return CompanyRepository(db)


async def check_teamlid_permission(
    admin_context: dict,
    repo: CompanyRepository,
    permission_type: str
):
    """
    Check if teamlid has write permission for the given operation.
    Supports multiple teamlid roles by checking guest_access collection based on acting workspace.
    Works for both company_admin and company_user.
    """
    user_email = admin_context.get("admin_email") or admin_context.get("user_email")
    if not user_email:
        return True 
    
    real_user_id = admin_context.get("real_admin_id")
    acting_admin_id = admin_context.get("admin_id")
    company_id = admin_context.get("company_id")
    user_type = admin_context.get("user_type", "company_admin")
    
    if real_user_id and acting_admin_id and real_user_id == acting_admin_id:
        if user_type == "company_admin":
            return True
    
    if user_type == "company_admin":
        user_record = await repo.find_admin_by_email(user_email)
    else:
        from motor.motor_asyncio import AsyncIOMotorDatabase
        db = repo.db
        user_record = await db.company_users.find_one({"email": user_email})
    
    if not user_record:
        return True 
    
    if user_type == "company_admin" and not user_record.get("is_teamlid", False):
        return True
    
    guest_entry = await repo.get_guest_access(
        company_id=company_id,
        guest_user_id=real_user_id,
        owner_admin_id=acting_admin_id,
    )
    
    if guest_entry:
        can_role_write = _get_guest_permission(guest_entry, "can_role_write")
        can_user_write = _get_guest_permission(guest_entry, "can_user_write", "can_user_read")
        can_document_write = _get_guest_permission(guest_entry, "can_document_write", "can_document_read")
        can_folder_write = _get_guest_permission(guest_entry, "can_folder_write")
        
        if permission_type == "users":
            return can_user_write
        elif permission_type == "roles" or permission_type == "folders":
            return can_role_write or can_folder_write
        elif permission_type == "documents":
            return can_document_write
    
    return False

