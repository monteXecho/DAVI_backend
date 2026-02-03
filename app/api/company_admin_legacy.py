import logging
import pandas as pd
import os, json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Query, status, Form, Body
from fastapi import Request
from fastapi.responses import FileResponse
from app.deps.auth import require_role, get_keycloak_admin, get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.repositories.constants import serialize_modules
from app.models.company_user_schema import CompanyUserCreate, TeamlidPermissionAssign, CompanyUserUpdate, CompanyRoleCreate, AssignRolePayload, DeleteDocumentsPayload, DeleteFolderPayload, DeleteRolesPayload, ResetPasswordPayload, CompanyRoleModifyUsers
from app.models.company_admin_schema import (
    AddFoldersPayload,
    GuestAccessPayload,
    GuestWorkspaceOut,
    ImportFoldersPayload,
    FolderImportItem,
)
from app.api.rag import rag_index_files

logger = logging.getLogger("uvicorn")
KEYCLOAK_HOST = os.getenv("KEYCLOAK_HOST", "host.docker.internal")

# Router without prefix - prefix will be added by main router
company_admin_router = APIRouter(tags=["Company Admin"])


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
            if not can_role_write or not can_folder_write:
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
    real_admin_id = full_admin["user_id"]

    acting_admin_id = real_admin_id
    guest_permissions = None

    acting_owner_header = request.headers.get("X-Acting-Owner-Id")
    if acting_owner_header and acting_owner_header != real_admin_id:
        guest_entry = await repo.get_guest_access(
            company_id=company_id,
            guest_user_id=real_admin_id,
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
                full_admin.get("is_teamlid")
                and full_admin.get("assigned_teamlid_by_id") == acting_owner_header
            ):
                teamlid_perms = full_admin.get("teamlid_permissions", {})
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
        "real_admin_id": real_admin_id,
        "admin_email": admin_email,
        "guest_permissions": guest_permissions,
    }


@company_admin_router.post("/guest-access", status_code=201)
async def create_or_update_guest_access(
    payload: GuestAccessPayload,
    ctx=Depends(get_admin_company_id),   
    db=Depends(get_db),
):
    """
    Grant or update guest access for this admin's workspace.
    """
    repo = CompanyRepository(db)

    company_id = ctx["company_id"]
    owner_admin_id = ctx["admin_id"]

    guest_user = await db.company_users.find_one(
        {"company_id": company_id, "user_id": payload.guest_user_id}
    )
    if not guest_user:
        guest_user = await db.company_admins.find_one(
            {"company_id": company_id, "user_id": payload.guest_user_id}
        )

    if not guest_user:
        raise HTTPException(status_code=404, detail="Guest user not found in this company")

    doc = await repo.upsert_guest_access(
        company_id=company_id,
        owner_admin_id=owner_admin_id,
        guest_user_id=payload.guest_user_id,
        can_role_write=payload.can_role_write,
        can_user_write=payload.can_user_write,
        can_document_write=payload.can_document_write,
        can_folder_write=payload.can_folder_write,
        created_by=owner_admin_id,
    )

    return {"ok": True, "guest_access": doc}

@company_admin_router.get("/guest-workspaces")
async def get_guest_workspaces(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Return workspaces (self + guest-of-others) available for current user.
    Works for company_admin and company_user.
    """
    email = user.get("email")
    roles = user.get("realm_access", {}).get("roles", [])

    if not email:
        raise HTTPException(status_code=400, detail="Missing email in token")

    base_user = None
    user_type = None

    if "company_admin" in roles:
        base_user = await db.company_admins.find_one({"email": email})
        user_type = "company_admin"
    elif "company_user" in roles:
        base_user = await db.company_users.find_one({"email": email})
        user_type = "company_user"
    else:
        raise HTTPException(status_code=403, detail="Unsupported role")

    if not base_user:
        raise HTTPException(status_code=404, detail="User not found")

    company_id = base_user["company_id"]
    user_id = base_user["user_id"]

    repo = CompanyRepository(db)
    guest_entries = await repo.list_guest_workspaces_for_user(
        company_id=company_id,
        guest_user_id=user_id,
    )

    if user_type == "company_admin":
        self_owner_id = base_user["user_id"]
        self_label = "MIJN ADMIN WERKRUIMTE"
    else:
        self_owner_id = base_user.get("added_by_admin_id", base_user["user_id"])
        self_label = "MIJN WERKRUIMTE"

    response = {
        "self": {
            "ownerId": self_owner_id,
            "label": self_label,
            "permissions": None,
        },
        "guestOf": [],
    }

    def derive_permissions_from_teamlid(perms: dict) -> dict:
        role_folder = perms.get("role_folder_modify_permission", False)
        user_modify = perms.get("user_create_modify_permission", False)
        document_modify = perms.get("document_modify_permission", False)
        return {
            "role_write": bool(role_folder),
            "user_write": bool(user_modify),
            "document_write": bool(document_modify),
            "folder_write": bool(role_folder),
        }

    seen_owner_ids = set()
    
    for entry in guest_entries:
        owner_admin_id = entry["owner_admin_id"]
        
        if user_type == "company_admin" and owner_admin_id == self_owner_id:
            continue 
        
        if owner_admin_id in seen_owner_ids:
            continue
            
        owner_admin = await db.company_admins.find_one(
            {
                "company_id": company_id,
                "user_id": owner_admin_id,
            }
        )
        if not owner_admin:
            continue

        seen_owner_ids.add(owner_admin_id)
        response["guestOf"].append(
            {
                "ownerId": owner_admin_id,
                "label": owner_admin.get("name") or owner_admin.get("email"),
                "owner": {
                    "id": owner_admin.get("user_id"),
                    "name": owner_admin.get("name"),
                    "email": owner_admin.get("email"),
                },
                "permissions": {
                    "role_write": _get_guest_permission(entry, "can_role_write"),
                    "user_write": _get_guest_permission(entry, "can_user_write", "can_user_read"),
                    "document_write": _get_guest_permission(entry, "can_document_write", "can_document_read"),
                    "folder_write": _get_guest_permission(entry, "can_folder_write"),
                },
            }
        )

    if (
        base_user.get("is_teamlid")
        and base_user.get("assigned_teamlid_by_id")
        and base_user.get("assigned_teamlid_by_id") not in seen_owner_ids
    ):
        assigned_owner_id = base_user["assigned_teamlid_by_id"]
        owner_admin = await db.company_admins.find_one(
            {"company_id": company_id, "user_id": assigned_owner_id}
        )
        if owner_admin:
            seen_owner_ids.add(assigned_owner_id)
            response["guestOf"].append(
                {
                    "ownerId": assigned_owner_id,
                    "label": owner_admin.get("name") or owner_admin.get("email") or "Gast workspace",
                    "owner": {
                        "id": owner_admin.get("user_id"),
                        "name": owner_admin.get("name"),
                        "email": owner_admin.get("email"),
                    },
                    "permissions": derive_permissions_from_teamlid(
                        base_user.get("teamlid_permissions", {})
                    ),
                }
            )

    return response


@company_admin_router.get("/user")
async def get_login_user(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    
    email = user.get("email")
    roles = user.get("realm_access", {}).get("roles", [])

    if not email:
        raise HTTPException(status_code=400, detail="Missing email in authentication token")

    repo = CompanyRepository(db)
    
    def _to_bool(value) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    
    def _get_guest_permission(entry: dict, new_field: str, old_field: str = None) -> bool:
        value = entry.get(new_field)
        if value is not None:
            return _to_bool(value)
        if old_field:
            old_value = entry.get(old_field)
            if old_value is not None:
                return _to_bool(old_value)
        return False

    if "company_admin" in roles:
        admin_record = await db.company_admins.find_one({"email": email})
        if not admin_record:
            raise HTTPException(status_code=404, detail="Admin not found in backend")

        company_id = admin_record.get("company_id")
        real_user_id = admin_record.get("user_id")
        acting_owner_id = request.headers.get("X-Acting-Owner-Id")
        
        guest_permissions = None
        if acting_owner_id and acting_owner_id != real_user_id:
            guest_entry = await repo.get_guest_access(
                company_id=company_id,
                guest_user_id=real_user_id,
                owner_admin_id=acting_owner_id,
            )
            if guest_entry:
                guest_permissions = {
                    "can_role_write": _get_guest_permission(guest_entry, "can_role_write"),
                    "can_user_write": _get_guest_permission(guest_entry, "can_user_write", "can_user_read"),
                    "can_document_write": _get_guest_permission(guest_entry, "can_document_write", "can_document_read"),
                    "can_folder_write": _get_guest_permission(guest_entry, "can_folder_write"),
                }

        # Get company modules to filter admin modules
        company_modules = await repo.get_company_modules(company_id)
        
        # Filter admin modules to only include those enabled at company level
        admin_modules = admin_record.get("modules", {})
        filtered_admin_modules = {}
        for module_name, module_config in admin_modules.items():
            # Only include if company has this module enabled
            if company_modules.get(module_name, {}).get("enabled", False):
                filtered_admin_modules[module_name] = module_config
        
        return {
            "type": "company_admin",
            "email": admin_record["email"],
            "name": admin_record.get("name"),
            "company_id": company_id,
            "user_id": real_user_id,
            "modules": filtered_admin_modules,  # Filtered based on company permissions
            "company_modules": serialize_modules(company_modules),  # Include company modules for role pages
            
            "is_teamlid": admin_record.get("is_teamlid", False),
            "teamlid_permissions": admin_record.get("teamlid_permissions", {}),
            "assigned_teamlid_by_name": admin_record.get("assigned_teamlid_by_name", None),
            "guest_permissions": guest_permissions
        }

    elif "company_user" in roles:
        user_record = await db.company_users.find_one({"email": email})
        if not user_record:
            raise HTTPException(status_code=404, detail="User not found")

        company_id = user_record["company_id"]
        real_user_id = user_record.get("user_id")
        assigned_roles = user_record.get("assigned_roles", [])
        added_by_admin_id = user_record.get("added_by_admin_id")
        
        acting_owner_id = request.headers.get("X-Acting-Owner-Id", added_by_admin_id)
        
        guest_permissions = None
        if acting_owner_id:
            guest_entry = await repo.get_guest_access(
                company_id=company_id,
                guest_user_id=real_user_id,
                owner_admin_id=acting_owner_id,
            )
            if guest_entry:
                guest_permissions = {
                    "can_role_write": _get_guest_permission(guest_entry, "can_role_write"),
                    "can_user_write": _get_guest_permission(guest_entry, "can_user_write", "can_user_read"),
                    "can_document_write": _get_guest_permission(guest_entry, "can_document_write", "can_document_read"),
                    "can_folder_write": _get_guest_permission(guest_entry, "can_folder_write"),
                }

        # Get company modules to filter user modules
        company_modules = await repo.get_company_modules(company_id)
        
        final_modules = {
            "Documenten chat": {"enabled": False},
            "GGD Checks": {"enabled": False}
        }

        if not assigned_roles:
            # Filter modules based on company permissions
            filtered_modules = {}
            for module_name, module_config in final_modules.items():
                if company_modules.get(module_name, {}).get("enabled", False):
                    filtered_modules[module_name] = module_config
            
            return {
                "type": "company_user",
                "email": user_record["email"],
                "name": user_record.get("name"),
                "company_id": company_id,
                "user_id": real_user_id,
                "roles": assigned_roles,
                "modules": filtered_modules,  # Filtered based on company permissions
                "company_modules": serialize_modules(company_modules),  # Include company modules for role pages

                "is_teamlid": user_record.get("is_teamlid", False),
                "teamlid_permissions": user_record.get("teamlid_permissions", {}),
                "assigned_teamlid_by_name": user_record.get("assigned_teamlid_by_name", None),
                "guest_permissions": guest_permissions
            }

        roles_collection = db.company_roles
        user_roles = await roles_collection.find({
            "company_id": company_id,
            "name": {"$in": assigned_roles}
        }).to_list(None)

        for role in user_roles:
            for module_name, cfg in role.get("modules", {}).items():

                enabled_raw = cfg.get("enabled", False)
                enabled_bool = (
                    enabled_raw.lower() == "true"
                    if isinstance(enabled_raw, str)
                    else bool(enabled_raw)
                )

                if module_name in final_modules:
                    final_modules[module_name]["enabled"] |= enabled_bool
                else:
                    final_modules[module_name] = {"enabled": enabled_bool}

        # Filter modules based on company permissions
        filtered_modules = {}
        for module_name, module_config in final_modules.items():
            if company_modules.get(module_name, {}).get("enabled", False):
                filtered_modules[module_name] = module_config
        
        return {
            "type": "company_user",
            "email": user_record["email"],
            "name": user_record.get("name"),
            "company_id": company_id,
            "user_id": real_user_id,
            "roles": assigned_roles,
            "modules": filtered_modules,  # Filtered based on company permissions
            "company_modules": serialize_modules(company_modules),  # Include company modules for role pages

            "is_teamlid": user_record.get("is_teamlid", False),
            "teamlid_permissions": user_record.get("teamlid_permissions", {}),
            "assigned_teamlid_by_name": user_record.get("assigned_teamlid_by_name", None),
            "guest_permissions": guest_permissions
        }

    else:
        raise HTTPException(
            status_code=403,
            detail="User does not have a valid company role"
        )

@company_admin_router.get("/users")
async def get_all_users(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        users = await repo.get_all_users_created_by_admin_id(company_id, admin_id)
        
        return {
            "company_id": company_id,
            "members": users,
            "total": len(users)
        }

    except Exception as e:
        logger.exception("Failed to get users")
        raise HTTPException(status_code=500, detail=f"Failed to get users: {str(e)}")

@company_admin_router.get("/documents", summary="Get all uploaded documents by admin")
async def get_admin_uploaded_documents(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    
    company_id: str = admin_context["company_id"]
    admin_id: str = admin_context["admin_id"]

    repo = CompanyRepository(db)

    result = await repo.get_admin_documents(company_id, admin_id)

    return {
        "success": True,
        "company_id": company_id,
        "admin_id": admin_id,
        "data": result
    }

@company_admin_router.get("/documents/private")
async def get_admin_uploaded_documents(
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    email = user.get("email")

    repo = CompanyRepository(db)

    result = await repo.get_all_private_documents(email, document_type="document")

    return {
        "success": True,
        "data": result
    }


@company_admin_router.get("/documents/all")
async def get_all_user_documents(
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    """
    Get all documents for the current user (both private and role-based).
    
    For company admins:
    - Returns private documents (upload_type="document") 
    - Returns all documents in folders created by this admin (upload_type=folder_name)
    
    For company users:
    - Returns private documents (upload_type="document")
    - Returns documents from folders assigned via roles:
      1. Get user's assigned_roles and added_by_admin_id
      2. Find roles with matching name, company_id, and added_by_admin_id
      3. Get folders from those roles
      4. Find documents with upload_type in folder names and user_id = added_by_admin_id
    """
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Email not found in token")

    repo = CompanyRepository(db)
    result = await repo.get_all_user_documents(email)
    
    return {
        "success": True,
        "data": result
    }


@company_admin_router.get("/documents/download")
async def download_document(
    file_path: str = Query(..., description="Path to the document file"),
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    """
    Download/view a document file. Verifies the user has access to the document
    before serving it.
    """
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Email not found in token")

    repo = CompanyRepository(db)
    
    user_data = await repo.get_all_user_documents(email)
    user_documents = user_data.get("documents", [])
    
    document_found = any(doc.get("path") == file_path for doc in user_documents)
    
    if not document_found:
        raise HTTPException(status_code=403, detail="You don't have access to this document")
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    file_name = os.path.basename(file_path)
    
    media_type = "application/octet-stream"
    if file_path.lower().endswith('.pdf'):
        media_type = "application/pdf"
    elif file_path.lower().endswith(('.doc', '.docx')):
        media_type = "application/msword"
    elif file_path.lower().endswith('.txt'):
        media_type = "text/plain"
    
    return FileResponse(
        path=file_path,
        filename=file_name,
        media_type=media_type
    )


@company_admin_router.post("/users")
async def add_user(
    payload: CompanyUserCreate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    await check_teamlid_permission(admin_context, db, "users")

    try:
        if payload.company_role == "company_admin":
            name = getattr(payload, "name", None) or payload.email.split("@")[0]
            new_admin = await repo.add_admin(company_id, admin_id, name, payload.email)
            return {"status": "admin_created", "user": new_admin}
        else:
            new_user = await repo.add_user_by_admin(company_id, admin_id, payload.email, payload.company_role, payload.assigned_role)
            return {"status": "user_created", "user": new_user}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to add company user/admin")
        raise HTTPException(status_code=500, detail=str(e))
    

@company_admin_router.post("/users/teamlid")
async def assign_teamlid_permission(
    payload: TeamlidPermissionAssign,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    real_admin_id = admin_context.get("real_admin_id")
    acting_admin_id = admin_context.get("admin_id")
    
    if real_admin_id != acting_admin_id:
        raise HTTPException(
            status_code=403,
            detail="U kunt alleen teamlid rechten toewijzen in uw eigen werkruimte."
        )
    
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    await repo.assign_teamlid_permissions(
        company_id=company_id,
        admin_id=admin_id,
        email=payload.email,
        permissions=payload.team_permissions
    )

    return {
        "success": True,
        "message": "Teamlid permissions assigned successfully",
        "data": {
            "email": payload.email,
            "assigned_by": admin_id,
            "permissions": payload.team_permissions
        }
    }


@company_admin_router.post("/users/upload")
async def upload_users_from_file(
    file: UploadFile = File(...),
    role: str = Form(default=""),  
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "users")
    """
    Upload a CSV or Excel file containing email addresses to add as company users.
    File can have:
    - Just email addresses (no headers)
    - Email addresses with 'email' column header
    - Email addresses in first column (with or without header)
    Names will be automatically generated from email prefixes.
    """
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    allowed_extensions = {'.csv', '.xlsx', '.xls'}
    file_extension = os.path.splitext(file.filename.lower())[1]
    
    if file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail="Invalid file type. Only CSV and Excel files are allowed."
        )

    try:
        content = await file.read()
        
        print(f"DEBUG: Selected Role is: '{role}'")
        print(f"DEBUG: Role type: {type(role)}")
        results = await repo.add_users_from_email_file(
            company_id=company_id,
            admin_id=admin_id,
            file_content=content,
            file_extension=file_extension,
            selected_role=role  
        )

        return {
            "success": True,
            "summary": {
                "total_processed": len(results["successful"]) + len(results["failed"]) + len(results["duplicates"]),
                "successful": len(results["successful"]),
                "duplicates": len(results["duplicates"]),
                "failed": len(results["failed"])
            },
            "details": results
        }
    except Exception as e:
        logger.error(f"Error uploading users file: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@company_admin_router.post("/users/reset-password")
async def reset_user_password(
    payload: ResetPasswordPayload,
    admin_context = Depends(get_admin_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "users")
    
    kc = get_keycloak_admin()
    email = payload.email
    logger.info(f"Password reset requested for {email}")

    try:
        users = kc.get_users(query={"email": email})
        if not users:
            raise HTTPException(status_code=404, detail="User not found in Keycloak")

        user = users[0]
        keycloak_id = user["id"]
        username = user.get("username", email)

        logger.info(f"Found user: {username} with Keycloak ID: {keycloak_id}")

        reset_url = f"{kc.connection.server_url}/admin/realms/{kc.connection.realm_name}/users/{keycloak_id}/execute-actions-email"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {kc.connection.token.get('access_token')}"
        }
        
        response = kc.connection.raw_put(
            reset_url,
            data=json.dumps(["UPDATE_PASSWORD"]),
            headers=headers
        )
        
        if response.status_code == 204:
            logger.info(f"Password reset email sent successfully to {email}")
            return {
                "success": True, 
                "message": f"Password reset email sent to {email}",
                "user_id": keycloak_id
            }
        else:
            logger.error(f"Keycloak API error: {response.status_code} - {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Keycloak API error: {response.text}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Password reset error for {email}: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send password reset email: {str(e)}"
        )


@company_admin_router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: CompanyUserUpdate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "users")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    user_type = payload.user_type

    updated = await repo.update_user(company_id, user_id, user_type, payload.name, payload.email, payload.assigned_roles)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found or not in your company")

    return {"success": True, "user_id": user_id, "new_name": payload.name, "assigned_roles": payload.assigned_roles,}


@company_admin_router.delete("/users")
async def delete_user(
    user_ids: str = Query(..., description="Comma-separated list of user IDs"),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "users")
    user_ids_list = user_ids.split(',')
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    kc = get_keycloak_admin()

    deleted_users_count = 0
    deleted_admins_count = 0
    failed_deletions = []

    for user_id in user_ids_list:
        user = await db.company_users.find_one({
            "company_id": company_id,
            "user_id": user_id
        })

        if user:
            logger.info(f"USER TO DELETE: {user}")
            
            if user.get("email"):
                email = user["email"]
                try:
                    kc_users = kc.get_users(query={"email": email})
                    if kc_users:
                        keycloak_user_id = kc_users[0]["id"]
                        kc.delete_user(keycloak_user_id)
                        logger.info(f"Deleted Keycloak user: {email}")
                except Exception as e:
                    logger.error(f"Failed Keycloak deletion for {email}: {str(e)}")
            
            try:
                count = await repo.delete_users(company_id, [user_id], admin_id)
                deleted_users_count += count
            except Exception as e:
                logger.error(f"Failed to delete user {user_id}: {str(e)}")
                failed_deletions.append(user_id)
        
        else:
            admin = await db.company_admins.find_one({
                "company_id": company_id,
                "user_id": user_id
            })
            
            if admin:
                logger.info(f"ADMIN TO DELETE: {admin}")
                
                if admin.get("email"):
                    email = admin["email"]
                    try:
                        kc_users = kc.get_users(query={"email": email})
                        if kc_users:
                            keycloak_user_id = kc_users[0]["id"]
                            kc.delete_user(keycloak_user_id)
                            logger.info(f"Deleted Keycloak admin: {email}")
                    except Exception as e:
                        logger.error(f"Failed Keycloak deletion for {email}: {str(e)}")
                
                try:
                    success = await repo.delete_admin(company_id, user_id, admin_id)
                    if success:
                        deleted_admins_count += 1
                except HTTPException as e:
                    logger.error(f"Failed to delete admin {user_id}: {e.detail}")
                    failed_deletions.append(user_id)
                except Exception as e:
                    logger.error(f"Failed to delete admin {user_id}: {str(e)}")
                    failed_deletions.append(user_id)
            else:
                logger.warning(f"User/Admin not found: {user_id}")
                failed_deletions.append(user_id)

    total_deleted = deleted_users_count + deleted_admins_count

    if total_deleted == 0:
        raise HTTPException(status_code=404, detail="No users or admins deleted. Make sure they were added by you.")

    return {
        "success": True,
        "deleted_users_count": deleted_users_count,
        "deleted_admins_count": deleted_admins_count,
        "total_deleted": total_deleted,
        "deleted_user_ids": user_ids_list,
        "failed_deletions": failed_deletions if failed_deletions else None
    }

@company_admin_router.delete("/users/teamlid/{target_admin_id}")
async def remove_teamlid_role(
    target_admin_id: str,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """
    Remove teamlid role from an admin.
    Only the admin who assigned the teamlid role can remove it.
    """
    await check_teamlid_permission(admin_context, db, "users")
    
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        success = await repo.remove_teamlid_role(
            company_id=company_id,
            admin_id=admin_id,
            target_admin_id=target_admin_id
        )
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail="Teamlid role not found or could not be removed."
            )
        
        return {
            "success": True,
            "message": "Teamlid role removed successfully",
            "target_admin_id": target_admin_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to remove teamlid role")
        raise HTTPException(status_code=500, detail=f"Failed to remove teamlid role: {str(e)}")

@company_admin_router.post("/users/role/delete")
async def delete_role_from_user(
    payload: CompanyRoleModifyUsers,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    roleName = payload.role_name
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    repo = CompanyRepository(db)

    # Special handling for Teamlid role removal
    if roleName == "Teamlid":
        try:
            # Get all affected users (both company_users and company_admins)
            affected_users = []
            affected_admins = []
            
            # Find affected company users
            company_users = await db.company_users.find({
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id,
                "is_teamlid": True
            }).to_list(None)
            affected_users = [u["user_id"] for u in company_users]
            
            # Find affected company admins
            company_admins = await db.company_admins.find({
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id,
                "is_teamlid": True
            }).to_list(None)
            affected_admins = [a["user_id"] for a in company_admins]
            
            all_affected_user_ids = list(set(affected_users + affected_admins))
            
            if not all_affected_user_ids:
                return {
                    "status": "success",
                    "removedRole": roleName,
                    "affectedUsers": payload.user_ids,
                    "modifiedCount": 0,
                    "message": "No users with Teamlid role found to remove."
                }
            
            # Remove "Teamlid" from assigned_roles if present (for company_users)
            if affected_users:
                await db.company_users.update_many(
                    {
                        "user_id": {"$in": affected_users},
                        "company_id": company_id
                    },
                    {
                        "$pull": {"assigned_roles": "Teamlid"},
                        "$set": {
                            "is_teamlid": False,
                            "updated_at": datetime.utcnow()
                        },
                        "$unset": {
                            "teamlid_permissions": "",
                            "assigned_teamlid_by_id": "",
                            "assigned_teamlid_by_name": "",
                            "assigned_teamlid_at": ""
                        }
                    }
                )
            
            # Remove teamlid status from company admins
            if affected_admins:
                await db.company_admins.update_many(
                    {
                        "user_id": {"$in": affected_admins},
                        "company_id": company_id
                    },
                    {
                        "$set": {
                            "is_teamlid": False,
                            "updated_at": datetime.utcnow()
                        },
                        "$unset": {
                            "teamlid_permissions": "",
                            "assigned_teamlid_by_id": "",
                            "assigned_teamlid_by_name": "",
                            "assigned_teamlid_at": ""
                        }
                    }
                )
            
            # Remove all guest_access entries for these users
            # This removes all teamlid permissions across all workspaces
            guest_access_result = await db.company_guest_access.delete_many({
                "company_id": company_id,
                "guest_user_id": {"$in": all_affected_user_ids}
            })
            
            modified_count = len(all_affected_user_ids)
            
            return {
                "status": "success",
                "removedRole": roleName,
                "affectedUsers": payload.user_ids,
                "modifiedCount": modified_count,
                "guestAccessRemoved": guest_access_result.deleted_count,
                "message": f"Teamlid role and all related permissions removed from {modified_count} user(s)."
            }
            
        except Exception as e:
            logger.error(f"Error removing Teamlid role: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Could not remove Teamlid role: {str(e)}")
    
    # Regular role removal
    role = await repo.get_role_by_name(
        company_id=company_id,
        admin_id=admin_id,
        role_name=roleName
    )

    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{roleName}' does not exist.")

    try:
        result = await db.company_users.update_many(
            {
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id
            },
            {
                "$pull": {"assigned_roles": roleName}
            }
        )

        if result.modified_count > 0:
            await repo._update_role_user_counts(company_id, admin_id, [roleName], -result.modified_count)

        return {
            "status": "success",
            "removedRole": roleName,
            "affectedUsers": payload.user_ids,
            "modifiedCount": result.modified_count
        }

    except Exception as e:
        logger.error(f"Error removing role: {str(e)}")
        raise HTTPException(status_code=500, detail="Could not remove role from users.")


@company_admin_router.post("/users/role/add")
async def add_role_to_users(
    payload: CompanyRoleModifyUsers,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "users")
    roleName = payload.role_name
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    role = await CompanyRepository(db).get_role_by_name(
        company_id=company_id,
        admin_id=admin_id,
        role_name=roleName
    )
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{roleName}' does not exist.")

    try:
        result = await db.company_users.update_many(
            {
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id
            },
            {
                "$addToSet": {"assigned_roles": roleName}
            }
        )

        if result.modified_count > 0:
            repo = CompanyRepository(db)
            await repo._update_role_user_counts(company_id, admin_id, [roleName], result.modified_count)

        return {
            "status": "success",
            "addedRole": roleName,
            "affectedUsers": payload.user_ids,
            "modifiedCount": result.modified_count
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not add role to users.")


@company_admin_router.get("/stats")
async def get_company_stats(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    admin_count = await db.company_admins.count_documents({"company_id": company_id})
    user_count = await db.company_users.count_documents({"company_id": company_id})

    admin_ids = [
        a["user_id"]
        async for a in db.company_admins.find({"company_id": company_id}, {"user_id": 1})
    ]
    user_ids = [
        u["user_id"]
        async for u in db.company_users.find({"company_id": company_id}, {"user_id": 1})
    ]

    docs_for_admins = await db.documents.count_documents({"user_id": {"$in": admin_ids}})
    docs_for_users = await db.documents.count_documents({"user_id": {"$in": user_ids}})

    return {
        "company_id": company_id,
        "company_admin_count": admin_count,
        "company_user_count": user_count,
        "documents_for_admins": docs_for_admins,
        "documents_for_users": docs_for_users,
    }


@company_admin_router.post("/documents/delete")
async def delete_documents(
    payload: DeleteDocumentsPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "documents")
    
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        deleted_count = await repo.delete_documents(
            company_id=company_id,
            admin_id=admin_id,
            documents_to_delete=payload.documents
        )

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="No documents found to delete")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "deleted_documents": payload.documents
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete documents")
        raise HTTPException(status_code=500, detail=f"Failed to delete documents: {str(e)}")
    

@company_admin_router.post("/documents/delete/private")
async def delete_documents(
    payload: DeleteDocumentsPayload,
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    email = user.get("email")

    repo = CompanyRepository(db)
 
    try:
        deleted_count = await repo.delete_private_documents(
            email=email,
            documents_to_delete=payload.documents
        )

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="No documents found to delete")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "deleted_documents": payload.documents
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete documents")
        raise HTTPException(status_code=500, detail=f"Failed to delete documents: {str(e)}")

@company_admin_router.post("/folders")
async def add_folders(
    payload: AddFoldersPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Create folders in DAVI and sync to Nextcloud.
    
    Scenario A1: DAVI → Nextcloud
    - Creates folder records in MongoDB
    - Creates corresponding folders in Nextcloud
    - Stores canonical storage paths for future operations
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        # Get storage provider for Nextcloud sync
        storage_provider = None
        try:
            from app.storage.providers import get_storage_provider, StorageError
            storage_provider = get_storage_provider()
        except StorageError as e:
            logger.warning(f"Storage provider not available, creating folders in DAVI only: {e}")
            # Continue without storage provider - DAVI can function without Nextcloud
        except Exception as e:
            logger.warning(f"Storage provider error, creating folders in DAVI only: {e}")
            # Continue without storage provider - DAVI can function without Nextcloud

        result = await repo.add_folders(
            company_id=company_id,
            admin_id=admin_id,
            folder_names=payload.folder_names,
            storage_provider=storage_provider
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "added_folders": result["added_folders"],
            "duplicated_folders": result["duplicated_folders"],
            "total_added": result["total_added"],
            "total_duplicates": result["total_duplicates"]
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Failed to add folders")
        raise HTTPException(status_code=500, detail=f"Failed to add folders: {str(e)}")

@company_admin_router.get("/folders")
async def get_folders(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.get_folders(
            company_id=company_id,
            admin_id=admin_id,
        )

        return {
            "success": result["success"],
            "folders": result["folders"],
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Failed to get folders")
        raise HTTPException(status_code=500, detail=f"Failed to get folders: {str(e)}")

@company_admin_router.post("/folders/delete")
async def delete_folder(
    payload: DeleteFolderPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.delete_folders(
            company_id=company_id,
            admin_id=admin_id,
            role_names=payload.role_names,
            folder_names=payload.folder_names
        )

        return {
            "success": True,
            "status": result["status"],
            "deleted_folders": result["deleted_folders"],
            "total_documents_deleted": result["total_documents_deleted"]
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Failed to delete folders")
        raise HTTPException(status_code=500, detail=f"Failed to delete folders: {str(e)}")

    
@company_admin_router.post("/roles")
async def add_or_update_role(
    payload: CompanyRoleCreate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    action = payload.action

    try:
        result = await repo.add_or_update_role(company_id, admin_id, payload.role_name, payload.folders, payload.modules, action)
        return result
    except Exception as e:
        logger.exception("Failed to add or update role")
        raise HTTPException(status_code=500, detail=str(e))
    
@company_admin_router.get("/roles")
async def list_roles(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    try:
        roles = await repo.list_roles(company_id, admin_id)
        return {"roles": roles}
    except Exception as e:
        print("Failed to list roles:", e)
        raise HTTPException(status_code=500, detail="Failed to list roles")

@company_admin_router.post("/roles/delete")
async def delete_roles(
    payload: DeleteRolesPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    try:
        result = await repo.delete_roles(company_id, payload.role_names, admin_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        print("Failed to delete roles:", e)
        raise HTTPException(status_code=500, detail="Failed to delete roles")

@company_admin_router.post("/roles/assign")
async def assign_role_to_user(
    payload: AssignRolePayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "users")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    try:
        result = await repo.assign_role_to_user(company_id, payload.user_id, payload.role_name)
        return result
    except HTTPException:
        raise
    except Exception as e:
        print("Failed to assign role:", e)
        raise HTTPException(status_code=500, detail="Failed to assign role")
    
@company_admin_router.post("/roles/upload/{folder_name}")
async def upload_document_for_role(
    folder_name: str,
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    await check_teamlid_permission(admin_context, db, "documents")

    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Check documents limit
    allowed, error_msg = await repo.check_documents_limit(company_id)
    if not allowed:
        raise HTTPException(status_code=403, detail=error_msg)

    try:
        from urllib.parse import unquote
        import os
        import re
        
        decoded_folder_name = unquote(folder_name)
        
        parts = re.split(r'[/\\]+', decoded_folder_name.strip())
        non_empty_parts = [p.strip() for p in parts if p.strip()]
        
        if not non_empty_parts:
            raise HTTPException(status_code=400, detail="Invalid folder name: empty after normalization")
        
        normalized_folder_name = non_empty_parts[-1]
        
        normalized_folder_name = normalized_folder_name.replace("/", "").replace("\\", "").strip()
        
        if not normalized_folder_name:
            raise HTTPException(status_code=400, detail="Invalid folder name: empty after normalization")
        
        if "/" in normalized_folder_name or "\\" in normalized_folder_name:
            normalized_folder_name = os.path.basename(normalized_folder_name).replace("/", "").replace("\\", "").strip()
        
        result = await repo.upload_document_for_folder(
            company_id=company_id,
            admin_id=admin_id,
            folder_name=normalized_folder_name,
            file=file
        )

        file_path = result["path"]
        file_id = company_id + '-' + admin_id

        try:
            await rag_index_files(file_id, [file_path], company_id)
            logger.info(f"RAG indexing triggered for '{file.filename}'")
        except Exception as e:
            logger.error(f"RAG indexing failed for '{file.filename}': {e}")
        
        return result
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Target folder not found for this role")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to upload document for role. Folder: {folder_name}, Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to upload document: {str(e)}")


@company_admin_router.get("/folders/import/list")
async def list_importable_folders(
    import_root: Optional[str] = Query(None, description="Root path in Nextcloud to list from"),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    List folders available for import from Nextcloud.
    
    Scenario B2: SharePoint → Nextcloud → DAVI (partial import)
    - Lists folders in Nextcloud that can be imported into DAVI
    - Shows which folders are already imported
    - Supports recursive listing for tree view
    
    Args:
        import_root: Optional root path in Nextcloud to list from (defaults to company root)
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Get storage provider
        from app.storage.providers import get_storage_provider, StorageError
        
        try:
            storage_provider = get_storage_provider()
        except StorageError as e:
            # Nextcloud is not configured - return empty list with informative message
            logger.warning(f"Nextcloud not configured: {e}")
            return {
                "success": True,
                "folders": [],
                "import_root": import_root or "",
                "message": "Nextcloud storage is not configured. Please configure NEXTCLOUD_URL, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD to enable folder import.",
                "configured": False
            }
        
        # Determine import root path
        # If not provided, start from root (empty string = storage root)
        # Users can specify a subdirectory if needed
        if import_root is None:
            import_root = ""
        
        # List folders recursively from Nextcloud
        # This will return empty list if path doesn't exist (404 handled gracefully)
        nextcloud_folders = await storage_provider.list_folders(import_root, recursive=True)
        
        # Get existing DAVI folders to mark which are already imported
        existing_folders = await repo.folders.find({
            "company_id": company_id,
            "admin_id": admin_id
        }).to_list(length=None)
        
        existing_storage_paths = {
            folder.get("storage_path", "").lower()
            for folder in existing_folders
            if folder.get("storage_path")
        }
        
        # Build response with import status
        # Show all folders, but mark structural folders as non-selectable:
        # - {company_id} (just the company_id itself) - visible but not importable
        # - {company_id}/{company_id} (company_id folder inside company_id) - visible but not importable
        # All other folders can be imported, regardless of depth
        importable_folders = []
        normalized_company_id = company_id.lower()
        
        for folder in nextcloud_folders:
            folder_path = folder["path"]
            
            # Normalize path for comparison (remove trailing slashes, lowercase)
            normalized_path = folder_path.lower().rstrip("/")
            
            # Skip empty paths
            if not normalized_path:
                continue
            
            # Split path into components for checking
            path_parts = [p for p in normalized_path.split("/") if p]  # Remove empty parts
            
            # Skip if path is empty after splitting
            if not path_parts:
                continue
            
            # Check if this is a structural folder (should be visible but not importable)
            is_structural = False
            # 1. Just the company_id itself (1 part, equals company_id)
            if len(path_parts) == 1 and path_parts[0] == normalized_company_id:
                is_structural = True
                logger.debug(f"Marking structural folder as non-importable: {folder_path} (company_id)")
            # 2. company_id/company_id (2 parts, both equal company_id)
            elif len(path_parts) == 2 and path_parts[0] == normalized_company_id and path_parts[1] == normalized_company_id:
                is_structural = True
                logger.debug(f"Marking structural folder as non-importable: {folder_path} (company_id/company_id)")
            # 3. company_id/admin_id (2 parts, first is company_id, second is admin_id - structural path)
            # Only actual folders (3+ levels: company_id/admin_id/folder_name) should be importable
            elif len(path_parts) == 2 and path_parts[0] == normalized_company_id:
                is_structural = True
                logger.debug(f"Marking structural folder as non-importable: {folder_path} (company_id/admin_id)")
            
            # Check if folder is already imported
            is_imported = normalized_path in existing_storage_paths
            
            # Extract folder name from path for display
            folder_name = folder_path.split("/")[-1] if "/" in folder_path else folder_path
            
            importable_folders.append({
                "path": folder_path,
                "name": folder_name,
                "depth": folder["depth"],
                "imported": is_imported,
                "selectable": not is_structural  # Structural folders are not selectable
            })
        
        return {
            "success": True,
            "folders": importable_folders,
            "import_root": import_root,
            "configured": True
        }
        
    except Exception as e:
        logger.exception("Failed to list importable folders")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list folders from Nextcloud: {str(e)}"
        )


@company_admin_router.post("/folders/import")
async def import_folders(
    payload: ImportFoldersPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Import selected folders from Nextcloud into DAVI.
    
    Scenario B2: SharePoint → Nextcloud → DAVI (partial import)
    - Creates DAVI folder records for selected Nextcloud folders
    - Links folders to their Nextcloud storage paths
    - Marks folders as imported (origin="imported")
    - Only selected folders are indexed and managed by DAVI
    
    Args:
        payload: ImportFoldersPayload with list of folder paths to import
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        # Get storage provider
        from app.storage.providers import get_storage_provider, StorageError
        
        try:
            storage_provider = get_storage_provider()
        except StorageError as e:
            # Nextcloud is not configured
            logger.warning(f"Nextcloud not configured: {e}")
            raise HTTPException(
                status_code=400,
                detail="Nextcloud storage is not configured. Please configure NEXTCLOUD_URL, NEXTCLOUD_USERNAME, and NEXTCLOUD_PASSWORD to enable folder import."
            )
        
        imported_folders = []
        skipped_folders = []
        errors = []
        
        # Normalize company_id for comparison
        normalized_company_id = company_id.lower()
        
        for folder_path in payload.folder_paths:
            try:
                # Normalize path for comparison
                normalized_path = folder_path.lower().rstrip("/")
                
                # Split path into components for checking
                path_parts = [p for p in normalized_path.split("/") if p]  # Remove empty parts
                
                # Prevent importing only the specific structural folders
                # Skip if path is empty
                if not path_parts:
                    errors.append(f"Cannot import empty folder path: {folder_path}")
                    logger.warning(f"Attempted to import empty folder path: {folder_path}")
                    continue
                
                # Skip if path is just the company_id (structural folder)
                if len(path_parts) == 1 and path_parts[0] == normalized_company_id:
                    errors.append(f"Cannot import structural folder: {folder_path} (company_id)")
                    logger.warning(f"Attempted to import structural folder: {folder_path}")
                    continue
                
                # Skip if path is {company_id}/{company_id} (structural folder)
                if len(path_parts) == 2 and path_parts[0] == normalized_company_id and path_parts[1] == normalized_company_id:
                    errors.append(f"Cannot import structural folder: {folder_path} (company_id/company_id)")
                    logger.warning(f"Attempted to import structural folder: {folder_path}")
                    continue
                
                # Skip if path is {company_id}/{admin_id} (structural folder - only 2 levels)
                # Only actual folders (3+ levels: company_id/admin_id/folder_name) should be importable
                if len(path_parts) == 2 and path_parts[0] == normalized_company_id:
                    errors.append(f"Cannot import structural folder: {folder_path} (company_id/admin_id)")
                    logger.warning(f"Attempted to import structural folder: {folder_path}")
                    continue
                
                # All other folders are allowed (3+ levels deep)
                
                # Verify folder exists in Nextcloud
                if not await storage_provider.folder_exists(folder_path):
                    errors.append(f"Folder not found in Nextcloud: {folder_path}")
                    continue
                
                # Extract folder name from path
                folder_name = folder_path.split("/")[-1] if "/" in folder_path else folder_path
                
                # Check if folder already exists in DAVI
                existing = await repo.folders.find_one({
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name,
                    "storage_path": folder_path
                })
                
                if existing:
                    skipped_folders.append(folder_name)
                    continue
                
                # Create folder record in DAVI
                folder_doc = {
                    "company_id": company_id,
                    "admin_id": admin_id,
                    "name": folder_name,
                    "document_count": 0,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                    "status": "active",
                    # Storage metadata (Scenario B2 support)
                    "storage_provider": "nextcloud",
                    "storage_path": folder_path,  # Canonical Nextcloud path
                    "origin": "imported",  # Imported from Nextcloud/SharePoint
                    "indexed": False,  # Will be indexed when documents are added
                    "sync_enabled": True
                }
                
                await repo.folders.insert_one(folder_doc)
                imported_folders.append(folder_name)
                
                logger.info(f"Imported folder from Nextcloud: {folder_name} (path: {folder_path})")
                
            except Exception as e:
                logger.error(f"Failed to import folder {folder_path}: {e}")
                errors.append(f"{folder_path}: {str(e)}")
        
        return {
            "success": True,
            "imported": imported_folders,
            "skipped": skipped_folders,
            "errors": errors,
            "total_imported": len(imported_folders),
            "total_skipped": len(skipped_folders),
            "total_errors": len(errors)
        }
        
    except Exception as e:
        logger.exception("Failed to import folders")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to import folders: {str(e)}"
        )


@company_admin_router.post("/folders/sync")
async def sync_documents_from_nextcloud(
    folder_id: Optional[str] = Query(None, description="Optional folder ID to sync specific folder"),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """
    Sync documents from Nextcloud to DAVI for folders that exist in Nextcloud.
    
    Syncs folders that have a storage_path in Nextcloud, including:
    - Folders with origin="imported" (folders imported from Nextcloud)
    - Folders with origin="davi" (folders created in DAVI that were synced to Nextcloud)
    
    When a user uploads a document to any folder in Nextcloud, this endpoint
    can be called to sync those documents to DAVI.
    
    Args:
        folder_id: Optional specific folder ID to sync (if None, syncs all folders with storage_path)
    """
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    try:
        result = await repo.sync_documents_from_nextcloud(
            company_id=company_id,
            admin_id=admin_id,
            folder_id=folder_id
        )
        
        # Trigger RAG indexing for newly synced documents if any
        synced_file_paths = result.get("synced_file_paths", [])
        if synced_file_paths:
            try:
                from app.api.rag import rag_index_files
                # Index all newly synced documents
                file_id = f"{company_id}-{admin_id}"
                await rag_index_files(file_id, synced_file_paths, company_id)
                logger.info(f"RAG indexing triggered for {len(synced_file_paths)} newly synced document(s)")
            except Exception as e:
                logger.error(f"RAG indexing failed for synced documents: {e}")
                # Don't fail the sync operation if indexing fails
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to sync documents from Nextcloud")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync documents: {str(e)}"
        )


@company_admin_router.get("/debug/all-data")
async def get_all_data(db=Depends(get_db)):
    repo = CompanyRepository(db)
    data = await repo.get_all_collections_data()
    return data

@company_admin_router.delete("/debug/clear-all")
async def clear_all_data(db=Depends(get_db)):
    repo = CompanyRepository(db)
    result = await repo.clear_all_data()
    return result

