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
from app.models.company_admin_schema import GuestAccessPayload, GuestWorkspaceOut
from app.api.company_admin.shared import get_admin_company_id

logger = logging.getLogger(__name__)
router = APIRouter()


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
        self_owner_id = base_user.get("added_by_admin_id")
        self_label = "MIJN WERKRUIMTE"

    workspaces = []

    # Add self workspace
    if self_owner_id:
        workspaces.append({
            "owner_id": self_owner_id,
            "owner_name": base_user.get("name", ""),
            "owner_email": email,
            "label": self_label,
            "is_guest": False,
            "permissions": None
        })

    # Add guest workspaces
    for entry in guest_entries:
        owner_id = entry.get("owner_admin_id")
        owner_admin = await db.company_admins.find_one({
            "company_id": company_id,
            "user_id": owner_id
        })

        if owner_admin:
            workspaces.append({
                "owner_id": owner_id,
                "owner_name": owner_admin.get("name", ""),
                "owner_email": owner_admin.get("email", ""),
                "label": f"WERKRUIMTE VAN {owner_admin.get('name', '').upper()}",
                "is_guest": True,
                "permissions": {
                    "role_write": entry.get("can_role_write", False),
                    "user_write": entry.get("can_user_write", False),
                    "document_write": entry.get("can_document_write", False),
                    "folder_write": entry.get("can_folder_write", False),
                }
            })

    return {
        "success": True,
        "workspaces": workspaces,
        "total": len(workspaces)
    }

