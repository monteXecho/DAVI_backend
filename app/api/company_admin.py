import logging
import pandas as pd
import os, json
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Query, status, Form, Body, Request
from app.deps.auth import require_role, get_keycloak_admin, get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import CompanyUserCreate, TeamlidPermissionAssign, CompanyUserUpdate, CompanyRoleCreate, AssignRolePayload, DeleteDocumentsPayload, DeleteFolderPayload, DeleteRolesPayload, ResetPasswordPayload, CompanyRoleModifyUsers
from app.models.company_admin_schema import (
    AddFoldersPayload,
    GuestAccessPayload,
    GuestWorkspaceOut,
)
from app.api.rag import rag_index_files

logger = logging.getLogger("uvicorn")
KEYCLOAK_HOST = os.getenv("KEYCLOAK_HOST", "host.docker.internal")

company_admin_router = APIRouter(prefix="/company-admin", tags=["Company Admin"])


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
    # Try new field name first
    value = guest_entry.get(new_field)
    if value is not None:
        return _to_bool(value)
    
    # Fallback to old field name if provided
    if old_field:
        old_value = guest_entry.get(old_field)
        if old_value is not None:
            return _to_bool(old_value)
    
    return False


async def check_teamlid_permission(
    admin_context: dict,
    db,
    permission_type: str  # "users", "roles_folders", "documents"
):
    """
    Check if teamlid has write permission for the given operation.
    Supports multiple teamlid roles by checking guest_access collection based on acting workspace.
    Works for both company_admin and company_user.
    """
    repo = CompanyRepository(db)
    user_email = admin_context.get("admin_email") or admin_context.get("user_email")
    if not user_email:
        return True  # If no email, skip check (shouldn't happen)
    
    # CRITICAL: If acting on own workspace (admin_id == real_admin_id), allow full access
    # Teamlid restrictions only apply when acting on someone else's workspace (guest mode)
    real_user_id = admin_context.get("real_admin_id")
    acting_admin_id = admin_context.get("admin_id")
    company_id = admin_context.get("company_id")
    user_type = admin_context.get("user_type", "company_admin")
    
    if real_user_id and acting_admin_id and real_user_id == acting_admin_id:
        # Acting on own workspace - full access, no restrictions (only for company_admin)
        if user_type == "company_admin":
            return True
    
    # For company_users, they're always in guest mode (acting on admin's workspace)
    # For company_admins, check if they're teamlid
    if user_type == "company_admin":
        user_record = await repo.find_admin_by_email(user_email)
    else:
        user_record = await db.company_users.find_one({"email": user_email})
    
    if not user_record:
        return True  # If not found, skip check
    
    # If not teamlid, allow (company_admin has full access, company_user always needs permission check)
    if user_type == "company_admin" and not user_record.get("is_teamlid", False):
        return True
    
    # For company_users: always check guest_access (they're always acting on admin's workspace)
    # For company_admins: only check if they're teamlid and acting on different workspace
    # Check guest_access collection first (supports multiple teamlid roles)
    guest_entry = await repo.get_guest_access(
        company_id=company_id,
        guest_user_id=real_user_id,
        owner_admin_id=acting_admin_id,
    )
    
    if guest_entry:
        # Use permissions from guest_access entry (supports multiple assignments)
        # Backward compatibility: support both old and new field names
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
        # Permission check passed
        return True
    else:
        # No guest_entry found
        # For company_users: they MUST have a guest_entry to perform write operations
        if user_type == "company_user":
            raise HTTPException(
                status_code=403,
                detail="Geen gasttoegang gevonden voor deze werkruimte. Selecteer een teamlid rol om toegang te krijgen."
            )
        
        # Fallback for backward compatibility: check old teamlid_permissions on user document (only for company_admin)
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
    
    # Check if user is company_admin or company_user
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

    # Default: normal mode, acting on own workspace
    # For company_users, their "own workspace" is their admin's workspace
    if user_type == "company_admin":
        acting_admin_id = real_user_id
        default_owner_id = real_user_id
    else:
        # Company user: their default workspace is their admin's workspace
        default_owner_id = base_user.get("added_by_admin_id")
        if not default_owner_id:
            raise HTTPException(
                status_code=403,
                detail="Company user must be added by an admin",
            )
        acting_admin_id = default_owner_id

    guest_permissions = None

    acting_owner_header = request.headers.get("X-Acting-Owner-Id")
    # For company users: check if they're in guest mode (even if ownerId matches default)
    # Check the isGuest flag from a custom header or check guest_access
    is_guest_mode = request.headers.get("X-Acting-Owner-Is-Guest", "false").lower() == "true"
    
    # Check if we need to verify guest access
    needs_guest_check = False
    if acting_owner_header:
        if acting_owner_header != default_owner_id:
            # Different ownerId - definitely guest mode
            needs_guest_check = True
        elif user_type == "company_user" and is_guest_mode:
            # Same ownerId but company user in guest mode (teamlid role for same admin)
            needs_guest_check = True
    
    if needs_guest_check:
        # Guest mode requested: verify guest access
        # Check guest_access collection first (supports multiple teamlid roles and guest access)
        guest_entry = await repo.get_guest_access(
            company_id=company_id,
            guest_user_id=real_user_id,
            owner_admin_id=acting_owner_header,
        )
        if guest_entry:
            # Found in guest_access collection (supports multiple teamlid roles)
            # Backward compatibility: support both old and new field names
            guest_permissions = {
                "role_write": _get_guest_permission(guest_entry, "can_role_write"),
                "user_write": _get_guest_permission(guest_entry, "can_user_write", "can_user_read"),
                "document_write": _get_guest_permission(guest_entry, "can_document_write", "can_document_read"),
                "folder_write": _get_guest_permission(guest_entry, "can_folder_write"),
            }
        else:
            # Fallback for backward compatibility: check old teamlid assignment on user document
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
        # WORKSPACE owner (whose data we are managing)
        "admin_id": acting_admin_id,
        # Real logged-in user
        "real_admin_id": real_user_id,
        "admin_email": user_email,  # Keep for backward compatibility
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

    # Default: normal mode, acting on own workspace
    acting_admin_id = real_admin_id
    guest_permissions = None

    acting_owner_header = request.headers.get("X-Acting-Owner-Id")
    if acting_owner_header and acting_owner_header != real_admin_id:
        # Guest mode requested: verify guest access
        # Check guest_access collection first (supports multiple teamlid roles and guest access)
        guest_entry = await repo.get_guest_access(
            company_id=company_id,
            guest_user_id=real_admin_id,
            owner_admin_id=acting_owner_header,
        )
        if guest_entry:
            # Found in guest_access collection (supports multiple teamlid roles)
            # Backward compatibility: support both old and new field names
            guest_permissions = {
                "role_write": _get_guest_permission(guest_entry, "can_role_write"),
                "user_write": _get_guest_permission(guest_entry, "can_user_write", "can_user_read"),
                "document_write": _get_guest_permission(guest_entry, "can_document_write", "can_document_read"),
                "folder_write": _get_guest_permission(guest_entry, "can_folder_write"),
            }
        else:
            # Fallback for backward compatibility: check old teamlid assignment on user document
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
        # WORKSPACE owner (whose data we are managing)
        "admin_id": acting_admin_id,
        # Real logged-in admin
        "real_admin_id": real_admin_id,
        "admin_email": admin_email,
        "guest_permissions": guest_permissions,
    }


@company_admin_router.post("/guest-access", status_code=201)
async def create_or_update_guest_access(
    payload: GuestAccessPayload,
    ctx=Depends(get_admin_company_id),   # existing helper that enforces company_admin
    db=Depends(get_db),
):
    """
    Grant or update guest access for this admin's workspace.
    """
    repo = CompanyRepository(db)

    company_id = ctx["company_id"]
    owner_admin_id = ctx["admin_id"]

    # Validate that guest user exists in this company (either admin or user)
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

    # Self workspace: for admins, it's their own; for users, it's their normal admin workspace.
    if user_type == "company_admin":
        self_owner_id = base_user["user_id"]
        self_label = "My admin workspace"
    else:
        self_owner_id = base_user.get("added_by_admin_id", base_user["user_id"])
        self_label = "My workspace"

    response = {
        "self": {
            "ownerId": self_owner_id,
            "label": self_label,
            # permissions for self are not needed (backend already enforces)
            "permissions": None,
        },
        "guestOf": [],
    }

    def derive_permissions_from_teamlid(perms: dict) -> dict:
        # Map legacy teamlid permission flags to guest workspace permission model
        role_folder = perms.get("role_folder_modify_permission", False)
        user_modify = perms.get("user_create_modify_permission", False)
        document_modify = perms.get("document_modify_permission", False)
        return {
            "role_write": bool(role_folder),
            "user_write": bool(user_modify),
            "document_write": bool(document_modify),
            "folder_write": bool(role_folder),
        }

    # Track seen owner IDs to avoid duplicates
    seen_owner_ids = set()
    
    for entry in guest_entries:
        owner_admin_id = entry["owner_admin_id"]
        
        # For company users: if guest workspace ownerId matches self.ownerId, 
        # still include it in guestOf (it represents teamlid permissions vs default permissions)
        # For company admins: skip if it matches self.ownerId (they're the same workspace)
        if user_type == "company_admin" and owner_admin_id == self_owner_id:
            continue  # Skip - admin's own workspace is already in "self"
        
        # Skip if we've already added this owner (for other duplicates)
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

    # Note: All teamlid assignments should now be in guest_entries (from guest_access collection)
    # This fallback is only for backward compatibility with old data
    # Check if there are any teamlid assignments in guest_access that weren't already added
    
    # Fallback: if user has old-style teamlid assignment on user document but not in guest_access
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
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    
    email = user.get("email")
    roles = user.get("realm_access", {}).get("roles", [])

    if not email:
        raise HTTPException(status_code=400, detail="Missing email in authentication token")

    if "company_admin" in roles:
        admin_record = await db.company_admins.find_one({"email": email})
        if not admin_record:
            raise HTTPException(status_code=404, detail="Admin not found in backend")

        return {
            "type": "company_admin",
            "email": admin_record["email"],
            "name": admin_record.get("name"),
            "company_id": admin_record.get("company_id"),
            "user_id": admin_record.get("user_id"),
            "modules": admin_record.get("modules", {}),
            
            "is_teamlid": admin_record.get("is_teamlid", False),
            "teamlid_permissions": admin_record.get("teamlid_permissions", {}),
            "assigned_teamlid_by_name": admin_record.get("assigned_teamlid_by_name", None)
        }

    elif "company_user" in roles:
        user_record = await db.company_users.find_one({"email": email})
        if not user_record:
            raise HTTPException(status_code=404, detail="User not found")

        company_id = user_record["company_id"]
        assigned_roles = user_record.get("assigned_roles", [])

        final_modules = {
            "Documenten chat": {"enabled": False},
            "GGD Checks": {"enabled": False}
        }

        if not assigned_roles:
            return {
                "type": "company_user",
                "email": user_record["email"],
                "name": user_record.get("name"),
                "company_id": company_id,
                "user_id": user_record.get("user_id"),
                "roles": assigned_roles,
                "modules": final_modules,

                "is_teamlid": user_record.get("is_teamlid", False),
                "teamlid_permissions": user_record.get("teamlid_permissions", {}),
                "assigned_teamlid_by_name": user_record.get("assigned_teamlid_by_name", None)
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

        return {
            "type": "company_user",
            "email": user_record["email"],
            "name": user_record.get("name"),
            "company_id": company_id,
            "user_id": user_record.get("user_id"),
            "roles": assigned_roles,
            "modules": final_modules,

            "is_teamlid": user_record.get("is_teamlid", False),
            "teamlid_permissions": user_record.get("teamlid_permissions", {}),
            "assigned_teamlid_by_name": user_record.get("assigned_teamlid_by_name", None)
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


@company_admin_router.post("/users")
async def add_user(
    payload: CompanyUserCreate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    # Check teamlid permissions
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
    # CRITICAL: Only allow assigning teamlid permissions when acting on own workspace
    # Teamlids cannot assign permissions to others
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
    role: str = Form(default=""),  # Add role as optional form parameter
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    # Check teamlid permissions
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

    # Validate file type
    allowed_extensions = {'.csv', '.xlsx', '.xls'}
    file_extension = os.path.splitext(file.filename.lower())[1]
    
    if file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail="Invalid file type. Only CSV and Excel files are allowed."
        )

    try:
        # Read file content
        content = await file.read()
        
        # Process file using repository with role parameter
        print(f"DEBUG: Selected Role is: '{role}'")
        print(f"DEBUG: Role type: {type(role)}")
        results = await repo.add_users_from_email_file(
            company_id=company_id,
            admin_id=admin_id,
            file_content=content,
            file_extension=file_extension,
            selected_role=role  # Pass the selected role
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
    # Check teamlid permissions (resetting passwords is a user management operation)
    await check_teamlid_permission(admin_context, db, "users")
    
    kc = get_keycloak_admin()
    email = payload.email
    logger.info(f"Password reset requested for {email}")

    try:
        # Find user in Keycloak
        users = kc.get_users(query={"email": email})
        if not users:
            raise HTTPException(status_code=404, detail="User not found in Keycloak")

        user = users[0]
        keycloak_id = user["id"]
        username = user.get("username", email)

        logger.info(f"Found user: {username} with Keycloak ID: {keycloak_id}")

        # Correct API endpoint for password reset
        reset_url = f"{kc.connection.server_url}/admin/realms/{kc.connection.realm_name}/users/{keycloak_id}/execute-actions-email"
        
        # Headers with proper authentication
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {kc.connection.token.get('access_token')}"
        }
        
        # Send the request
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
    # Check teamlid permissions
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
    # Check teamlid permissions
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
        # Check if it's a regular user
        user = await db.company_users.find_one({
            "company_id": company_id,
            "user_id": user_id
        })

        if user:
            # It's a regular user - delete it
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
            
            # Delete user (only if added by this admin - enforced in repo method)
            try:
                count = await repo.delete_users(company_id, [user_id], admin_id)
                deleted_users_count += count
            except Exception as e:
                logger.error(f"Failed to delete user {user_id}: {str(e)}")
                failed_deletions.append(user_id)
        
        else:
            # Check if it's an admin
            admin = await db.company_admins.find_one({
                "company_id": company_id,
                "user_id": user_id
            })
            
            if admin:
                # It's an admin - only delete if added by this admin
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
                
                # Delete admin (only if added by this admin - enforced in repo method)
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
    # Check teamlid permissions
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
    print(f"--- Role Name: ---", roleName)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Validate role exists
    role = await CompanyRepository(db).get_role_by_name(
        company_id=company_id,
        admin_id=admin_id,
        role_name=roleName
    )

    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{roleName}' does not exist.")

    try:
        # Remove the role from the assigned_roles array for selected users
        result = await db.company_users.update_many(
            {
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id
            },
            {
                "$pull": {"assigned_roles": roleName}
            }
        )

        # Update role user count - decrement by the number of users who actually had the role removed
        if result.modified_count > 0:
            repo = CompanyRepository(db)
            # Pass negative count to decrement
            await repo._update_role_user_counts(company_id, admin_id, [roleName], -result.modified_count)

        return {
            "status": "success",
            "removedRole": roleName,
            "affectedUsers": payload.user_ids,
            "modifiedCount": result.modified_count
        }

    except Exception as e:
        print("Error removing role:", e)
        raise HTTPException(status_code=500, detail="Could not remove role from users.")


@company_admin_router.post("/users/role/add")
async def add_role_to_users(
    payload: CompanyRoleModifyUsers,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    # Check teamlid permissions
    await check_teamlid_permission(admin_context, db, "users")
    roleName = payload.role_name
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Check role exists
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

        # Update role user count - only count users who actually got the role added
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
    """
    Returns summary statistics for a company:
    - Number of company admins
    - Number of company users
    - Total documents uploaded by admins
    - Total documents uploaded by users
    """
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    # --- Count admins and users ---
    admin_count = await db.company_admins.count_documents({"company_id": company_id})
    user_count = await db.company_users.count_documents({"company_id": company_id})

    # --- Get all admin & user IDs for this company ---
    admin_ids = [
        a["user_id"]
        async for a in db.company_admins.find({"company_id": company_id}, {"user_id": 1})
    ]
    user_ids = [
        u["user_id"]
        async for u in db.company_users.find({"company_id": company_id}, {"user_id": 1})
    ]

    # --- Count documents for each group ---
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
    # Check teamlid permissions
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
    # Check teamlid permissions
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.add_folders(
            company_id=company_id,
            admin_id=admin_id,
            folder_names=payload.folder_names
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
        # Call the repository method
        result = await repo.get_folders(
            company_id=company_id,
            admin_id=admin_id,
        )

        # Return the complete result from the repository
        return {
            "success": result["success"],
            "folders": result["folders"],
        }

    except HTTPException:
        # Directly rethrow known user-facing exceptions
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
    # Check teamlid permissions
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
        # Directly rethrow known user-facing exceptions
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
    # Check teamlid permissions
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
    """List all roles for the authenticated user's company."""
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
    # Check teamlid permissions
    await check_teamlid_permission(admin_context, db, "roles_folders")
    """Delete one or multiple roles and their associated data."""
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
    # Check teamlid permissions (assigning roles affects users)
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
    # Check teamlid permissions
    await check_teamlid_permission(admin_context, db, "documents")

    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.upload_document_for_folder(
            company_id=company_id,
            admin_id=admin_id,
            folder_name=folder_name,
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
        logger.exception("Failed to upload document for role")
        raise HTTPException(status_code=500, detail="Failed to upload document")


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

