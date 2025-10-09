import logging
from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import require_role
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import CompanyUserCreate, CompanyUserUpdate

logger = logging.getLogger(__name__)


company_admin_router = APIRouter(prefix="/company-admin", tags=["Company Admin"])


async def get_admin_company_id(
    user=Depends(require_role("company_admin")),
    db=Depends(get_db)
):
    """
    Extract and validate the authenticated admin's company_id.
    Ensures the admin exists in both Keycloak and backend DB.
    """
    repo = CompanyRepository(db)

    admin_email = user.get("email")
    if not admin_email:
        raise HTTPException(status_code=400, detail="Missing email in authentication token")

    admin_record = await repo.find_admin_by_email(admin_email)
    if not admin_record:
        raise HTTPException(status_code=403, detail="Admin not found in backend database")

    full_admin = await db.company_admins.find_one({"email": admin_email})
    if not full_admin:
        raise HTTPException(status_code=403, detail="Admin not registered in company database")

    company_id = full_admin["company_id"]
    admin_id = full_admin["user_id"]

    return {
        "company_id": company_id,
        "admin_id": admin_id,
        "admin_email": admin_email
    }

# -------------------------------------------------------------
# GET all users belonging to the same company
# -------------------------------------------------------------
@company_admin_router.get("/users")
async def get_all_users(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]

    # Step 4: Fetch all admins and users in the same company
    admins_cursor = db.company_admins.find({"company_id": company_id})
    users_cursor = db.company_users.find({"company_id": company_id})

    admins = []
    async for adm in admins_cursor:
        admins.append({
            "id": adm.get("user_id"),
            "name": adm.get("name"),
            "email": adm.get("email"),
            "role": "company_admin"
        })

    users = []
    async for usr in users_cursor:
        users.append({
            "id": usr.get("user_id"),
            "name": usr.get("name"),
            "email": usr.get("email"),
            "role": usr.get("company_role", "company_user")
        })

    # Combine both lists
    combined = admins + users

    return {"company_id": company_id, "members": combined}


# -------------------------------------------------------------
# ADD new user (with email + company_role)
# -------------------------------------------------------------
@company_admin_router.post("/users")
async def add_user(
    payload: CompanyUserCreate,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Step 4: Create new user
    try:
        # ✅ Step 4: If company_role is "company_admin", create admin instead of user
        if payload.company_role == "company_admin":
            # Name is optional, fallback to email prefix if missing
            name = getattr(payload, "name", None) or payload.email.split("@")[0]
            new_admin = await repo.add_admin(company_id, name, payload.email)
            return {"status": "admin_created", "user": new_admin}
        else:
            # Default: add as regular company user
            new_user = await repo.add_user_by_admin(company_id, admin_id, payload.email, payload.company_role)
            return {"status": "user_created", "user": new_user}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to add company user/admin")
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------
# UPDATE user (when the user registers and sets their name)
# -------------------------------------------------------------
@company_admin_router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: CompanyUserUpdate,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    updated = await repo.update_user(company_id, user_id, payload.name, payload.email, payload.company_role)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found or not in your company")

    return {"status": "user_updated", "user_id": user_id, "new_name": payload.name}


# -------------------------------------------------------------
# DELETE user
# -------------------------------------------------------------
@company_admin_router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):

    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]

    user_deleted = await repo.delete_user(company_id, user_id)
    admin_deleted = await repo.delete_admin(company_id, user_id)

    if not (user_deleted or admin_deleted):
        raise HTTPException(status_code=404, detail="User not found or already deleted")

    return {
        "status": "user_deleted",
        "user_id": user_id,
        "deleted_from": "company_users" if user_deleted else "company_admins"
    }



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
