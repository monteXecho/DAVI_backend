"""
Guest Access domain router for company admin API.

Handles all guest access/teamlid endpoints:
- POST /guest-access - Create or update guest access
- GET /guest-workspaces - List guest workspaces for current user
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.repositories.constants import serialize_modules
from app.models.company_admin_schema import GuestAccessPayload
from app.api.company_admin.shared import get_admin_company_id

logger = logging.getLogger(__name__)
router = APIRouter()


async def _company_display_name(db, company_id: str) -> str:
    doc = await db.companies.find_one({"company_id": company_id}, {"name": 1})
    if doc and doc.get("name"):
        return str(doc["name"])
    return str(company_id)


async def _build_workspace_block_for_membership(
    db,
    repo: CompanyRepository,
    *,
    company_id: str,
    mem: dict,
    user_type: str,
    email: str,
) -> dict:
    """Self + guest workspaces for one (email, company_id) membership row."""
    user_id = mem.get("user_id")
    if not user_id:
        return {
            "company_id": company_id,
            "company_name": await _company_display_name(db, company_id),
            "self": None,
            "guestOf": [],
        }

    teamlid_only = bool(mem.get("teamlid_only"))

    guest_entries = await repo.list_guest_workspaces_for_user(
        company_id=company_id,
        guest_user_id=user_id,
    )

    if user_type == "company_admin":
        self_owner_id = user_id
        self_label = "MIJN ADMIN WERKRUIMTE"
    else:
        self_owner_id = mem.get("added_by_admin_id")
        self_label = "MIJN WERKRUIMTE"

    self_workspace = None
    if not teamlid_only and self_owner_id:
        self_workspace = {
            "ownerId": self_owner_id,
            "owner": {
                "name": mem.get("name", ""),
                "email": email,
            },
            "label": self_label,
            "permissions": None,
        }

    guest_workspaces = []
    for entry in guest_entries:
        owner_id = entry.get("owner_admin_id")
        if not owner_id:
            logger.warning("[guest-workspaces] Guest entry missing owner_admin_id: %s", entry)
            continue

        owner_admin = await db.company_admins.find_one(
            {"company_id": company_id, "user_id": owner_id}
        )

        if owner_admin:
            owner_mods_raw = owner_admin.get("modules") or {}
            if isinstance(owner_mods_raw, dict):
                owner_modules_out = serialize_modules(owner_mods_raw)
            elif isinstance(owner_mods_raw, list):
                owner_modules_out = owner_mods_raw
            else:
                owner_modules_out = []

            guest_ws = {
                "ownerId": owner_id,
                "owner": {
                    "name": owner_admin.get("name", ""),
                    "email": owner_admin.get("email", ""),
                },
                "label": f"WERKRUIMTE VAN {owner_admin.get('name', '').upper()}",
                "owner_modules": owner_modules_out,
                "permissions": {
                    "role_write": entry.get("can_role_write", False),
                    "user_write": entry.get("can_user_write", False),
                    "document_write": entry.get("can_document_write", False),
                    "folder_write": entry.get("can_folder_write", False),
                    "publicchat_write": entry.get("can_publicchat_write", False),
                    "webchat_write": entry.get("can_webchat_write", False),
                },
            }
            guest_workspaces.append(guest_ws)
        else:
            logger.warning(
                "[guest-workspaces] Owner admin not found: owner_id=%s company_id=%s guest_user_id=%s",
                owner_id,
                company_id,
                user_id,
            )

    company_name = await _company_display_name(db, company_id)
    return {
        "company_id": company_id,
        "company_name": company_name,
        # Identifies logged-in person's row in THIS company (for client storage headers)
        "member_user_id": mem.get("user_id"),
        "member_is_teamlid": bool(mem.get("is_teamlid")),
        "member_teamlid_only": teamlid_only,
        "membership_kind": user_type,
        "self": self_workspace,
        "guestOf": guest_workspaces,
    }


@router.post("/guest-access", status_code=201)
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


@router.get("/guest-workspaces")
async def get_guest_workspaces(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Workspaces for each company this email is registered in (same Keycloak user).

    Response includes ``companies``: one block per ``company_users`` / ``company_admins`` row.
    For a single-company account, ``self`` / ``guestOf`` duplicate the first block for
    backward-compatible clients.
    """
    email = user.get("email")
    roles = user.get("realm_access", {}).get("roles", [])

    if not email:
        raise HTTPException(status_code=400, detail="Missing email in token")

    if "company_admin" in roles:
        memberships = await db.company_admins.find({"email": email}).to_list(length=None)
        user_type = "company_admin"
    elif "company_user" in roles:
        memberships = await db.company_users.find({"email": email}).to_list(length=None)
        user_type = "company_user"
    else:
        raise HTTPException(status_code=403, detail="Unsupported role")

    if not memberships:
        raise HTTPException(status_code=404, detail="User not found")

    repo = CompanyRepository(db)
    company_blocks = []
    for mem in memberships:
        cid = mem.get("company_id")
        if not cid:
            continue
        block = await _build_workspace_block_for_membership(
            db,
            repo,
            company_id=str(cid),
            mem=mem,
            user_type=user_type,
            email=email,
        )
        company_blocks.append(block)

    logger.info("[guest-workspaces] email=%s type=%s company_count=%s", email, user_type, len(company_blocks))

    if not company_blocks:
        raise HTTPException(status_code=404, detail="User not found")

    single = len(company_blocks) == 1
    return {
        "companies": company_blocks,
        "self": company_blocks[0]["self"] if single else None,
        "guestOf": company_blocks[0]["guestOf"] if single else [],
    }
