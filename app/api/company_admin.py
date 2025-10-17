import logging
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from app.deps.auth import require_role
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import CompanyUserCreate, CompanyUserUpdate, CompanyRoleCreate, AssignRolePayload
from app.api.rag import rag_index_files

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

    users_cursor = db.company_users.find({"company_id": company_id})

    users = []
    async for usr in users_cursor:
        users.append({
            "id": usr.get("user_id"),
            "name": usr.get("name"),
            "email": usr.get("email"),
            "roles": usr.get("assigned_roles")
        })

    # Combine both lists
    combined = users

    return {"company_id": company_id, "members": combined}

# -------------------------------------------------------------
# Get all documents uploaded by the admin (grouped by role/folder)
# -------------------------------------------------------------
@company_admin_router.get("/documents", summary="Get all uploaded documents by admin")
async def get_admin_uploaded_documents(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """
    Retrieve all documents uploaded by the current admin, grouped by role and folder.
    
    This endpoint returns a structured view showing:
      - Roles (e.g., role_A, role_B)
      - Folders under each role
      - Documents within each folder
      - Users (by role) that the documents are assigned to
    """
    company_id: str = admin_context["company_id"]
    admin_id: str = admin_context["admin_id"]

    repo = CompanyRepository(db)

    result = await repo.get_admin_documents(company_id, admin_id)

    return {
        "status": "success",
        "company_id": company_id,
        "admin_id": admin_id,
        "data": result
    }

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

    updated = await repo.update_user(company_id, user_id, payload.name, payload.email, payload.assigned_roles)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found or not in your company")

    return {"status": "user_updated", "user_id": user_id, "new_name": payload.name, "assigned_roles": payload.assigned_roles,}

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


# -------------------------------------------------------------
# GET Company stats
# -------------------------------------------------------------
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


# -------------------------------------------------------------
# ADD or UPDAT a company role and document folders for the role
# -------------------------------------------------------------
@company_admin_router.post("/roles")
async def add_or_update_role(
    payload: CompanyRoleCreate,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.add_or_update_role(company_id, admin_id, payload.role_name, payload.folders)
        return result
    except Exception as e:
        logger.exception("Failed to add or update role")
        raise HTTPException(status_code=500, detail=str(e))
    
@company_admin_router.get("/roles")
async def list_roles(
    admin_context=Depends(get_admin_company_id),
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


@company_admin_router.delete("/roles/{role_name}")
async def delete_role(
    role_name: str,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """Delete a role and optionally its folders."""
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    try:
        result = await repo.delete_role(company_id, role_name, admin_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        print("Failed to delete role:", e)
        raise HTTPException(status_code=500, detail="Failed to delete role")
    
@company_admin_router.post("/roles/assign")
async def assign_role_to_user(
    payload: AssignRolePayload,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):

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
    
@company_admin_router.post("/roles/upload/{role_name}/{folder_name}")
async def upload_document_for_role(
    role_name: str,
    folder_name: str,
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """
    Upload a document for a given company role and folder.
    Stores the file under /app/uploads/documents/roleBased/{company_id}/{admin_id}/{role_name}/{folder_name}/.
    Also registers it in the DB.
    """
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.upload_document_for_role(
            company_id=company_id,
            admin_id=admin_id,
            role_name=role_name,
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
    except Exception as e:
        logger.exception("Failed to upload document for role")
        raise HTTPException(status_code=500, detail=str(e))


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
