"""
Shared utilities and dependencies for company admin API routes.

This module contains common functions and dependencies used across
all company admin domain routers.
"""

import logging
from typing import Literal, Optional, Set, Tuple

from bson import ObjectId

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


def _can_write_publicchat(perms: dict) -> bool:
    """Check if user has write permission for PublicChat"""
    return _to_bool(perms.get("publicchat_modify_permission", False))


def _can_write_webchat(perms: dict) -> bool:
    """Check if user has write permission for WebChat"""
    return _to_bool(perms.get("webchat_modify_permission", False))


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


def company_id_header_query_filter(selected_company_header: Optional[str]):
    """
    HTTP sends company ids as strings; Mongo may store str or bson.ObjectId.
    Use as ``{"company_id": <this>}`` when looking up rows via X-Selected-Company-Id.
    """
    selected = (selected_company_header or "").strip()
    if not selected:
        return None
    variants = [selected]
    try:
        from bson import ObjectId

        if len(selected) == 24 and ObjectId.is_valid(selected):
            variants.append(ObjectId(selected))
    except Exception:
        pass
    uniq = []
    seen = set()
    for v in variants:
        key = (type(v).__name__, str(v))
        if key not in seen:
            seen.add(key)
            uniq.append(v)
    if len(uniq) == 1:
        return uniq[0]
    return {"$in": uniq}


async def resolve_email_membership_documents(
    db,
    *,
    email: str,
    realm_roles: list,
    selected_company_header: Optional[str],
) -> Tuple[dict, Literal["company_admin", "company_user"]]:
    """
    Resolve the Mongo document when the same email exists in multiple companies.

    ``X-Selected-Company-Id`` selects the ``company_users`` / ``company_admins`` row.
    If omitted and multiple rows exist, raises 400 so the client can prompt company selection.
    """
    if not email or not isinstance(email, str):
        raise HTTPException(status_code=400, detail="Missing email in authentication token")

    selected = (selected_company_header or "").strip()

    if "company_admin" in realm_roles:
        user_type: Literal["company_admin", "company_user"] = "company_admin"
        coll = db.company_admins
    elif "company_user" in realm_roles:
        user_type = "company_user"
        coll = db.company_users
    else:
        raise HTTPException(
            status_code=403,
            detail="User must be company_admin or company_user",
        )

    if selected:
        cid_expr = company_id_header_query_filter(selected)
        doc = await coll.find_one({"email": email, "company_id": cid_expr})
        if not doc:
            raise HTTPException(
                status_code=403,
                detail="Geen account voor dit e-mailadres in de gekozen organisatie.",
            )
        return doc, user_type

    matches = await coll.find({"email": email}).to_list(length=None)
    if not matches:
        raise HTTPException(status_code=403, detail="User not found in company database")
    if len(matches) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "Dit account bestaat bij meerdere organisaties. "
                'Kies een organisatie (kop X-Selected-Company-Id / "Bedrijf wisselen").'
            ),
        )
    return matches[0], user_type


async def get_repository(db=Depends(get_db)) -> CompanyRepository:
    """Dependency to get CompanyRepository instance."""
    return CompanyRepository(db)


async def check_nextcloud_permission(
    admin_context: dict,
    db
):
    """
    Check if user/company has Nextcloud module enabled.
    Nextcloud requires both company-level and user/admin-level permission.
    
    Raises HTTPException 403 if Nextcloud is not enabled.
    """
    from app.repositories.modules_repo import ModulesRepository
    
    repo = CompanyRepository(db)
    modules_repo = ModulesRepository(db)
    company_id = admin_context["company_id"]
    user_type = admin_context.get("user_type", "company_admin")
    user_email = admin_context.get("admin_email") or admin_context.get("user_email")
    
    # Check company-level Nextcloud permission
    company_modules = await modules_repo.get_company_modules(company_id)
    
    # Handle both dict and list formats for company_modules
    if isinstance(company_modules, list):
        # If it's a list, convert to dict for easier checking
        company_modules_dict = {}
        for module in company_modules:
            if isinstance(module, dict) and "name" in module:
                company_modules_dict[module["name"]] = {
                    "enabled": module.get("enabled", False),
                    "desc": module.get("desc", "")
                }
        company_modules = company_modules_dict
    
    # Check for both "Nexcloud" (typo) and "Nextcloud" (correct spelling)
    nexcloud_module = company_modules.get("Nexcloud", {})
    nextcloud_module = company_modules.get("Nextcloud", {})
    company_has_nextcloud = (
        (nexcloud_module.get("enabled", False) if isinstance(nexcloud_module, dict) else False) or
        (nextcloud_module.get("enabled", False) if isinstance(nextcloud_module, dict) else False)
    )
    
    if not company_has_nextcloud:
        logger.warning(
            f"Nextcloud not enabled at company level for company {company_id}. "
            f"Company modules: {list(company_modules.keys())}"
        )
        raise HTTPException(
            status_code=403,
            detail="Nextcloud module is niet ingeschakeld voor dit bedrijf. Neem contact op met de super admin."
        )
    
    # Check user/admin-level Nextcloud permission (scoped to the active company)
    if user_type == "company_admin":
        user_record = await db.company_admins.find_one({"email": user_email, "company_id": company_id})
    else:
        user_record = await db.company_users.find_one({"email": user_email, "company_id": company_id})
    
    if not user_record:
        logger.error(f"User not found: {user_email} (type: {user_type})")
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )
    
    user_modules = user_record.get("modules", {})
    
    # Handle both dict and list formats for user_modules
    if isinstance(user_modules, list):
        # If it's a list, convert to dict for easier checking
        user_modules_dict = {}
        for module in user_modules:
            if isinstance(module, dict) and "name" in module:
                user_modules_dict[module["name"]] = {
                    "enabled": module.get("enabled", False),
                    "desc": module.get("desc", "")
                }
        user_modules = user_modules_dict
    
    # Check for both "Nexcloud" (typo) and "Nextcloud" (correct spelling)
    user_nexcloud = user_modules.get("Nexcloud", {})
    user_nextcloud = user_modules.get("Nextcloud", {})
    user_has_nextcloud = (
        (user_nexcloud.get("enabled", False) if isinstance(user_nexcloud, dict) else False) or
        (user_nextcloud.get("enabled", False) if isinstance(user_nextcloud, dict) else False)
    )
    
    if not user_has_nextcloud:
        logger.warning(
            f"Nextcloud not enabled at user level for {user_email} (type: {user_type}). "
            f"User modules keys: {list(user_modules.keys()) if isinstance(user_modules, dict) else 'not a dict'}, "
            f"User modules: {user_modules}"
        )
        raise HTTPException(
            status_code=403,
            detail="Nextcloud module is niet ingeschakeld voor uw account. Neem contact op met uw beheerder."
        )
    
    logger.debug(f"Nextcloud permission check passed for {user_email} (company: {company_id})")
    return True


async def check_teamlid_permission(
    admin_context: dict,
    db,
    permission_type: str,
    require_write: bool = True
):
    """
    Check if teamlid has permission for the given operation.
    When require_write=True (default), requires write permission (e.g. can_webchat_write).
    When require_write=False, only requires that the user has guest access to this workspace
    (read-only access to the module).
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
        user_record = await db.company_admins.find_one({"email": user_email, "company_id": company_id})
    else:
        user_record = await db.company_users.find_one({"email": user_email, "company_id": company_id})
    
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
        can_publicchat_write = _get_guest_permission(guest_entry, "can_publicchat_write")
        can_webchat_write = _get_guest_permission(guest_entry, "can_webchat_write")

        if permission_type == "users":
            if require_write and not can_user_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om gebruikers te beheren."
                )
        elif permission_type == "roles_folders":
            if require_write and not can_role_write and not can_folder_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om rollen of mappen te beheren."
                )
        elif permission_type == "documents":
            if require_write and not can_document_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om documenten te beheren."
                )
        elif permission_type == "publicchat":
            if require_write and not can_publicchat_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om PublicChat te beheren."
                )
        elif permission_type == "webchat":
            if require_write and not can_webchat_write:
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om WebChat te beheren."
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
            if require_write and not _can_write_users(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om gebruikers te beheren."
                )
        elif permission_type == "roles_folders":
            if require_write and not _can_write_roles_folders(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om rollen of mappen te beheren."
                )
        elif permission_type == "documents":
            if require_write and not _can_write_documents(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om documenten te beheren."
                )
        elif permission_type == "publicchat":
            if require_write and not _can_write_publicchat(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om PublicChat te beheren."
                )
        elif permission_type == "webchat":
            if require_write and not _can_write_webchat(perms):
                raise HTTPException(
                    status_code=403,
                    detail="U heeft geen toestemming om WebChat te beheren."
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
    selected_company_header = request.headers.get("X-Selected-Company-Id")

    base_user, user_type = await resolve_email_membership_documents(
        db,
        email=user_email,
        realm_roles=roles,
        selected_company_header=selected_company_header,
    )

    company_id = base_user["company_id"]
    real_user_id = base_user["user_id"]
    
    # Track user activity for online user detection
    try:
        from datetime import datetime
        now = datetime.utcnow()
        doc_filter = {"email": user_email, "company_id": company_id}
        if user_type == "company_admin":
            await db.company_admins.update_one(doc_filter, {"$set": {"last_activity": now}}, upsert=False)
        else:
            await db.company_users.update_one(doc_filter, {"$set": {"last_activity": now}}, upsert=False)
    except Exception as e:
        logger.warning(f"⚠️  Failed to update activity for {user_email}@{company_id}: {e}")
    
    # Get Keycloak access token for Nextcloud authentication
    access_token = user.get("_raw_token")
    
    # Extract user ID from token for Nextcloud WebDAV path
    # CRITICAL: Use email as the Nextcloud user ID (as explicitly requested by user)
    # Nextcloud is configured with mappingUid=email, so we MUST use email
    # Priority: email (REQUIRED) > preferred_username (fallback) > sub (last resort)
    if not user_email:
        logger.error(f"❌ CRITICAL: user_email is missing! Cannot use email for Nextcloud user ID.")
        # Fallback to preferred_username or sub only if email is truly missing
        nextcloud_user_id = user.get("preferred_username") or user.get("sub")
        logger.warning(f"Using fallback user ID for Nextcloud: {nextcloud_user_id} (email was missing)")
    else:
        # ALWAYS use email when available (Nextcloud expects email)
        nextcloud_user_id = user_email
        logger.info(f"Using email as Nextcloud user ID: {user_email}")

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
                "publicchat_write": _get_guest_permission(guest_entry, "can_publicchat_write"),
                "webchat_write": _get_guest_permission(guest_entry, "can_webchat_write"),
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
                    "publicchat_write": _to_bool(teamlid_perms.get("publicchat_modify_permission", False)),
                    "webchat_write": _to_bool(teamlid_perms.get("webchat_modify_permission", False)),
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
        "_nextcloud_user_id": nextcloud_user_id,  # User ID for Nextcloud WebDAV path (sub or preferred_username)
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

    roles = user.get("realm_access", {}).get("roles", []) or []
    selected_company_header = request.headers.get("X-Selected-Company-Id")

    full_admin, resolved_type = await resolve_email_membership_documents(
        db,
        email=admin_email,
        realm_roles=roles,
        selected_company_header=selected_company_header,
    )
    if resolved_type != "company_admin":
        raise HTTPException(
            status_code=403,
            detail="Admin record required",
        )

    company_id = full_admin["company_id"]
    real_user_id = full_admin["user_id"]
    
    try:
        from datetime import datetime
        now = datetime.utcnow()
        await db.company_admins.update_one(
            {"email": admin_email, "company_id": company_id},
            {"$set": {"last_activity": now}},
            upsert=False
        )
    except Exception as e:
        logger.debug(f"Failed to update activity for {admin_email}: {e}")
    
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


async def resolve_restricted_public_chat_ids(
    db,
    admin_context: dict,
) -> Optional[Set[str]]:
    """
    Workspace owner sees all chats (None).

    Teamlid / guest workspace viewing another owner's data: optionally restricted to explicit
    public chat Mongo ids stored on ``company_guest_access.assigned_public_chat_ids`` or
    ``teamlid_public_chat_ids`` on the user's admin/user row.

    Returns:
        ``None`` – no restriction (all chats under this workspace ``admin_id``).
        Empty ``set()`` – restriction active but no chats allowed.
        Non-empty ``set`` – only these chat id strings are visible.
    """
    real_uid = admin_context.get("real_admin_id")
    owner_uid = admin_context.get("admin_id")
    company_id = admin_context.get("company_id")
    if not real_uid or not owner_uid or not company_id:
        return None
    if real_uid == owner_uid:
        return None

    repo = CompanyRepository(db)
    guest_entry = await repo.get_guest_access(
        company_id=company_id,
        guest_user_id=real_uid,
        owner_admin_id=owner_uid,
    )

    assigned = None
    if guest_entry is not None:
        assigned = guest_entry.get("assigned_public_chat_ids")

    if assigned is None:
        email = admin_context.get("admin_email") or admin_context.get("user_email")
        user_type = admin_context.get("user_type", "company_admin")
        if email:
            if user_type == "company_admin":
                rec = await db.company_admins.find_one({"email": email, "company_id": company_id})
            else:
                rec = await db.company_users.find_one({"email": email, "company_id": company_id})
            if rec is not None:
                assigned = rec.get("teamlid_public_chat_ids")

    if assigned is None:
        return None
    return {str(x).strip() for x in assigned if str(x).strip()}


async def require_public_chat_access_for_teamlid(
    admin_context: dict,
    db,
    chat_id: str,
) -> None:
    """
    For teamlid users with an explicit assignment list: deny access unless ``chat_id`` is allowed.
    Uses HTTP 404 to avoid leaking existence of chats outside the assignment.
    """
    scope = await resolve_restricted_public_chat_ids(db, admin_context)
    if scope is not None:
        cid = (chat_id or "").strip()
        if cid not in scope:
            raise HTTPException(status_code=404, detail="Public chat not found")
        if not ObjectId.is_valid(cid):
            raise HTTPException(status_code=404, detail="Public chat not found")


def assert_workspace_owner_for_public_chat_mutation(admin_context: dict) -> None:
    """Creating or deleting a public chat is owner-only."""
    real_uid = admin_context.get("real_admin_id")
    owner_uid = admin_context.get("admin_id")
    if real_uid != owner_uid:
        raise HTTPException(
            status_code=403,
            detail="Alleen de hoofdbeheerder kan public chats aanmaken of verwijderen.",
        )

