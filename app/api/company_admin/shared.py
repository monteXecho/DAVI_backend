"""
Shared utilities and dependencies for company admin API routes.

This module contains common functions and dependencies used across
all company admin domain routers.
"""

import logging
from fastapi import Depends, Request, HTTPException
from app.deps.auth import get_current_user, get_keycloak_admin, require_role
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
    db,
    permission_type: str
):
    """
    Check if teamlid has write permission for the given operation.
    Supports multiple teamlid roles by checking guest_access collection based on acting workspace.
    Works for both company_admin and company_user.
    """
    repo = CompanyRepository(db)
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
            if not can_user_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om gebruikers te beheren."
                )
        elif permission_type == "roles_folders":
            if not can_role_write and not can_folder_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om rollen of mappen te beheren."
                )
        elif permission_type == "documents":
            if not can_document_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om documenten te beheren."
                )
        return True
    else:
        if user_type == "company_user":
            raise HTTPException(
                status_code=403,
                detail="Geen gasttoegang gevonden voor deze werkruimte. Selecteer een teamlid rol om toegang te krijgen."
            )
        
        perms = user_record.get("teamlid_permissions", {})
        
        if permission_type == "users":
            if not _can_write_users(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om gebruikers te beheren."
                )
        elif permission_type == "roles_folders":
            if not _can_write_roles_folders(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om rollen of mappen te beheren."
                )
        elif permission_type == "documents":
            if not _can_write_documents(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om documenten te beheren."
                )
    
    return True


async def get_admin_or_user_company_id(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Extract and validate the authenticated user's company_id.
    Supports both company_admin and company_user (with teamlid permissions).

    Additionally:
      - Reads X-Acting-Owner-Id header.
      - If present and different from the real user_id, verifies guest access.
      - Returns 'admin_id' as the WORKSPACE owner (acting admin),
        plus 'real_admin_id' and 'guest_permissions' if in guest mode.
    """
    repo = CompanyRepository(db)

    user_email = user.get("email")
    if not user_email:
        raise HTTPException(
            status_code=400,
            detail="Missing email in authentication token",
        )

    roles = user.get("realm_access", {}).get("roles", [])
    
    if "company_admin" in roles:
        base_user = await db.company_admins.find_one({"email": user_email})
        user_type = "company_admin"
    elif "company_user" in roles:
        base_user = await db.company_users.find_one({"email": user_email})
        user_type = "company_user"
    else:
        raise HTTPException(
            status_code=403,
            detail="User must be company_admin or company_user",
        )

    if not base_user:
        raise HTTPException(
            status_code=403,
            detail="User not found in company database",
        )

    company_id = base_user["company_id"]
    real_user_id = base_user["user_id"]
    
    # Get Keycloak access token for Nextcloud authentication
    access_token = user.get("_raw_token")

    if user_type == "company_admin":
        acting_admin_id = real_user_id
        default_owner_id = real_user_id
    else:
        default_owner_id = base_user.get("added_by_admin_id")
        if not default_owner_id:
            raise HTTPException(
                status_code=403,
                detail="Company user must be added by an admin",
            )
        acting_admin_id = default_owner_id

    guest_permissions = None

    acting_owner_header = request.headers.get("X-Acting-Owner-Id")
    is_guest_mode = request.headers.get("X-Acting-Owner-Is-Guest", "false").lower() == "true"
    
    needs_guest_check = False
    if acting_owner_header:
        if acting_owner_header != default_owner_id:
            needs_guest_check = True
        elif user_type == "company_user" and is_guest_mode:
            needs_guest_check = True
    
    if needs_guest_check:
        guest_entry = await repo.get_guest_access(
            company_id=company_id,
            guest_user_id=real_user_id,
            owner_admin_id=acting_owner_header,
        )
        if guest_entry:
            guest_permissions = {
                "role_write": _get_guest_permission(guest_entry, "can_role_write"),
                "user_write": _get_guest_permission(guest_entry, "can_user_write", "can_user_read"),
                "document_write": _get_guest_permission(guest_entry, "can_document_write", "can_document_read"),
                "folder_write": _get_guest_permission(guest_entry, "can_folder_write"),
            }
        else:
            if (
                base_user.get("is_teamlid")
                and base_user.get("assigned_teamlid_by_id") == acting_owner_header
            ):
                teamlid_perms = base_user.get("teamlid_permissions", {})
                guest_permissions = {
                    "role_write": teamlid_perms.get("role_folder_modify_permission", False),
                    "user_write": teamlid_perms.get("user_create_modify_permission", False),
                    "document_write": teamlid_perms.get("document_modify_permission", False),
                    "folder_write": teamlid_perms.get("role_folder_modify_permission", False),
                }
            else:
                raise HTTPException(
                    status_code=403,
                    detail="No guest access for this workspace",
                )

        acting_admin_id = acting_owner_header

    return {
        "company_id": company_id,
        "admin_id": acting_admin_id,
        "real_admin_id": real_user_id,
        "admin_email": user_email, 
        "user_email": user_email,
        "user_type": user_type,
        "guest_permissions": guest_permissions,
        "_access_token": access_token,  # Keycloak access token for Nextcloud authentication
    }


async def get_admin_company_id(
    request: Request,
    user=Depends(require_role("company_admin")),
    db=Depends(get_db),
):
    """
    Extract and validate the authenticated admin's company_id.
    DEPRECATED: Use get_admin_or_user_company_id for support of company users with teamlid permissions.

    Additionally:
      - Reads X-Acting-Owner-Id header.
      - If present and different from the real admin_id, verifies guest access.
      - Returns 'admin_id' as the WORKSPACE owner (acting admin),
        plus 'real_admin_id' and 'guest_permissions' if in guest mode.
    """
    repo = CompanyRepository(db)

    admin_email = user.get("email")
    if not admin_email:
        raise HTTPException(
            status_code=400,
            detail="Missing email in authentication token",
        )

    admin_record = await repo.find_admin_by_email(admin_email)
    if not admin_record:
        raise HTTPException(
            status_code=403,
            detail="Admin not found in backend database",
        )

    full_admin = await db.company_admins.find_one({"email": admin_email})
    if not full_admin:
        raise HTTPException(
            status_code=403,
            detail="Admin not registered in company database",
        )

    company_id = full_admin["company_id"]
    real_user_id = full_admin["user_id"]
    
    # Get Keycloak access token for Nextcloud authentication
    access_token = user.get("_raw_token")

    acting_owner_header = request.headers.get("X-Acting-Owner-Id")
    acting_admin_id = real_user_id
    guest_permissions = None

    if acting_owner_header and acting_owner_header != real_user_id:
        guest_entry = await repo.get_guest_access(
            company_id=company_id,
            guest_user_id=real_user_id,
            owner_admin_id=acting_owner_header,
        )
        if guest_entry:
            guest_permissions = {
                "role_write": _get_guest_permission(guest_entry, "can_role_write"),
                "user_write": _get_guest_permission(guest_entry, "can_user_write", "can_user_read"),
                "document_write": _get_guest_permission(guest_entry, "can_document_write", "can_document_read"),
                "folder_write": _get_guest_permission(guest_entry, "can_folder_write"),
            }
            acting_admin_id = acting_owner_header
        else:
            raise HTTPException(
                status_code=403,
                detail="No guest access for this workspace",
            )

    return {
        "company_id": company_id,
        "admin_id": acting_admin_id,
        "real_admin_id": real_user_id,
        "admin_email": admin_email,
        "user_email": admin_email,
        "user_type": "company_admin",
        "guest_permissions": guest_permissions,
        "_access_token": access_token,
    }

