from typing import Optional
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository


class GuestPermissions(BaseModel):
    role_write: bool = False
    user_read: bool = False
    document_read: bool = False
    folder_write: bool = False


class RequestContext(BaseModel):
    token: dict
    user_type: str
    user_id: str
    company_id: str
    owner_admin_id: str          # workspace owner (always a company_admin user_id)
    is_guest_mode: bool
    guest_permissions: Optional[GuestPermissions]


async def get_request_context(
    request: Request,
    token: dict = Depends(get_current_user),
    db=Depends(get_db),
) -> RequestContext:
    """
    Builds per-request context:
      - who is the real user (from Keycloak + DB),
      - whose workspace is active (owner_admin_id),
      - whether it's guest mode, and
      - what guest permissions apply.
    """
    repo = CompanyRepository(db)

    email = token.get("email")
    roles = token.get("realm_access", {}).get("roles", [])

    if not email:
        raise HTTPException(status_code=400, detail="Missing email in token")

    # Resolve real user record
    base_user = None
    user_type = None

    if "company_admin" in roles:
        base_user = await db.company_admins.find_one({"email": email})
        user_type = "company_admin"
    elif "company_user" in roles:
        base_user = await db.company_users.find_one({"email": email})
        user_type = "company_user"
    else:
        raise HTTPException(status_code=403, detail="Unsupported role for this feature")

    if not base_user:
        raise HTTPException(status_code=404, detail="User not found in backend")

    company_id = base_user["company_id"]
    base_user_id = base_user["user_id"]

    # Default workspace owner:
    #  - if admin: themselves
    #  - if company_user: the admin who added them (their normal workspace owner)
    default_owner_admin_id = (
        base_user["user_id"]
        if user_type == "company_admin"
        else base_user.get("added_by_admin_id", base_user["user_id"])
    )

    acting_owner_id = request.headers.get("X-Acting-Owner-Id")

    is_guest_mode = False
    guest_permissions: Optional[GuestPermissions] = None
    owner_admin_id = default_owner_admin_id

    # If a different owner is requested -> guest mode
    if acting_owner_id and acting_owner_id != default_owner_admin_id:
        guest_entry = await repo.get_guest_access(
            company_id=company_id,
            guest_user_id=base_user_id,
            owner_admin_id=acting_owner_id,
        )
        if not guest_entry:
            raise HTTPException(status_code=403, detail="No guest access for this workspace")

        is_guest_mode = True
        owner_admin_id = acting_owner_id
        guest_permissions = GuestPermissions(
            role_write=guest_entry.get("can_role_write", False),
            user_read=guest_entry.get("can_user_read", False),
            document_read=guest_entry.get("can_document_read", False),
            folder_write=guest_entry.get("can_folder_write", False),
        )

    return RequestContext(
        token=token,
        user_type=user_type,
        user_id=base_user_id,
        company_id=company_id,
        owner_admin_id=owner_admin_id,
        is_guest_mode=is_guest_mode,
        guest_permissions=guest_permissions,
    )
