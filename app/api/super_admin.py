import os
import shutil
import logging
from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import require_role, get_keycloak_admin
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.repositories.document_repo import DocumentRepository
from app.models.company_admin_schema import CompanyCreate, CompanyAddAdmin, CompanyAdminModules, CompanyReAssignAdmin
from app.deps.auth import keycloak_admin, ensure_role_exists

from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


super_admin_router = APIRouter(prefix="/super-admin", tags=["Super Admin"])

# MUST match your upload root
UPLOAD_ROOT = "/app/uploads"
UPLOAD_SUBFOLDERS = ["documents", "bkr", "vgc", "3-uurs"]

# helper to delete files (runs sync)
def _delete_user_files_sync(user_id: str):
    deleted_paths = []
    for sub in UPLOAD_SUBFOLDERS:
        path = os.path.join(UPLOAD_ROOT, sub, user_id)
        if os.path.exists(path):
            shutil.rmtree(path)
            deleted_paths.append(path)
    # also attempt to remove empty parent folders if desired
    return deleted_paths


# helper to delete keycloak user by email (blocking, run in threadpool)
def _delete_keycloak_user_by_email_sync(kc_admin, email: str):
    """Return True if at least one Keycloak user was deleted."""
    # attempt to find Keycloak users with this email
    try:
        users = kc_admin.get_users({"email": email})  # blocking call
    except Exception as e:
        raise RuntimeError(f"Keycloak lookup failed for {email}: {e}")

    if not users:
        return False

    deleted_any = False
    for u in users:
        # different keycloak client versions use 'id' or 'userId'
        user_id = u.get("id") or u.get("userId")
        if not user_id:
            continue
        try:
            kc_admin.delete_user(user_id)  # blocking call
            deleted_any = True
        except Exception as e:
            logger.warning(f"Failed to delete Keycloak user id={user_id} for email={email}: {e}")
    return deleted_any


@super_admin_router.get("/companies")
async def get_companies(
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
):
    repo = CompanyRepository(db)
    companies = await repo.get_all_companies()
    return companies


@super_admin_router.post("/companies")
async def add_company(
    payload: CompanyCreate,
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
):
    repo = CompanyRepository(db)
    return await repo.create_company(payload.name)


@super_admin_router.post("/companies/{company_id}/admins")
async def add_company_admin(
    company_id: str,
    payload: CompanyAddAdmin,
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
):
    repo = CompanyRepository(db)
    modules = {m.name: m.dict() for m in payload.modules}

    result = await repo.add_admin(company_id, payload.name, payload.email, modules)
    if not result:
        raise HTTPException(404, "Company not found")

    # NOTE: Documents are *not* created here.
    return {
        "status": "admin_created",
        "admin": result
    }

@super_admin_router.patch("/companies/{company_id}/admins/{admin_id}")
async def reassign_company_admin(
    company_id: str,
    admin_id: str,
    payload: CompanyReAssignAdmin,
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
    kc_admin=Depends(get_keycloak_admin),
):
    repo = CompanyRepository(db)

    admin = await repo.get_admin_by_id(company_id, admin_id)
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    email = admin.get("email")

    kc_deleted = False
    try:
        kc_deleted = await run_in_threadpool(_delete_keycloak_user_by_email_sync, kc_admin, email)
    except Exception:
        logger.exception("Keycloak delete failed for admin %s", email)

    try:
        updated_admin = await repo.reassign_admin(
            company_id, admin_id, payload.name, payload.email
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "status": "admin_reassigned",
        "admin": updated_admin,
        "keycloak_deleted": kc_deleted,
    }


@super_admin_router.post("/companies/{company_id}/admins/{admin_id}/modules")
async def assign_modules(
    company_id: str,
    admin_id: str,
    payload: CompanyAdminModules,
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
):
    repo = CompanyRepository(db)
    modules_dict = {m.name: {"enabled": m.enabled} for m in payload.modules}

    result = await repo.assign_modules(company_id, admin_id, modules_dict)
    if not result:
        raise HTTPException(status_code=404, detail="Company or Admin not found")
    return result


def _delete_company_role_folder_sync(company_id: str) -> bool:
    """
    Delete the entire company folder from roleBased directory.
    This runs in a threadpool.
    """
    try:
        company_folder_path = os.path.join(UPLOAD_ROOT, "roleBased", company_id)
        
        if os.path.exists(company_folder_path):
            import shutil
            shutil.rmtree(company_folder_path)
            print(f"DEBUG: Successfully deleted company role folder: {company_folder_path}")
            return True
        else:
            print(f"DEBUG: Company role folder not found: {company_folder_path}")
            return False
            
    except Exception as e:
        print(f"DEBUG: Failed to delete company role folder: {e}")
        return False

@super_admin_router.delete("/companies/{company_id}")
async def delete_company(
    company_id: str,
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
    kc_admin=Depends(get_keycloak_admin),
):
    repo = CompanyRepository(db)
    doc_repo = DocumentRepository(db)

    # 1) fetch admins & users belonging to company
    admins = await repo.get_admins_by_company(company_id)
    users = await repo.get_users_by_company(company_id)

    kc_failed = []
    files_deleted = []
    docs_deleted_total = 0

    # 2) Delete ALL roles for this company (simple delete by company_id)
    roles_deleted = await db.roles.delete_many({"company_id": company_id})
    logger.info(f"Deleted {roles_deleted} roles for company {company_id}")

    # 3) Delete role-based documents and folders for the entire company
    company_role_docs_deleted = await repo.delete_company_role_documents(company_id)
    docs_deleted_total += company_role_docs_deleted
    logger.info(f"Deleted {company_role_docs_deleted} role documents for company {company_id}")

    # 4) remove Keycloak accounts, documents (db), and uploaded files for each person
    for person in (admins + users):
        email = person.get("email")
        user_id = person.get("user_id")
        if not user_id:
            logger.warning("Person without user_id, skipping: %s", person)
            continue

        # Keycloak: lookup & delete by email -> done in threadpool since blocking
        try:
            deleted_kc = await run_in_threadpool(_delete_keycloak_user_by_email_sync, kc_admin, email)
            if not deleted_kc:
                kc_failed.append(email)
        except Exception as e:
            logger.exception("Keycloak delete failed for %s", email)
            kc_failed.append(email)

        # documents in DB
        try:
            deleted_count = await doc_repo.delete_documents_by_user(user_id)
            docs_deleted_total += deleted_count
        except Exception:
            logger.exception("Failed to delete documents in DB for user_id=%s", user_id)

        # filesystem
        try:
            removed_paths = await run_in_threadpool(_delete_user_files_sync, user_id)
            files_deleted.extend(removed_paths)
        except PermissionError as pe:
            logger.exception("Permission error deleting files for user_id=%s: %s", user_id, pe)
        except Exception:
            logger.exception("Failed to delete files for user_id=%s", user_id)

    # 5) delete admin/user records and company doc records (cascade)
    await db.company_admins.delete_many({"company_id": company_id})
    await db.company_users.delete_many({"company_id": company_id})
    
    # also delete any remaining documents for this company
    try:
        docs_deleted_by_company = await doc_repo.delete_documents_by_company(company_id)
        docs_deleted_total += docs_deleted_by_company
    except Exception:
        logger.exception("Failed to delete documents by company_id=%s", company_id)

    # 6) Delete the entire company folder from roleBased
    company_folder_deleted = await run_in_threadpool(_delete_company_role_folder_sync, company_id)
    if company_folder_deleted:
        logger.info(f"Successfully deleted company role folder for {company_id}")

    # 7) finally remove company record
    result = await db.companies.delete_one({"company_id": company_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Company not found")

    return {
        "status": "deleted",
        "company_id": company_id,
        "admins_removed": len(admins),
        "users_removed": len(users),
        "roles_deleted": roles_deleted,
        "role_documents_deleted": company_role_docs_deleted,
        "documents_deleted": docs_deleted_total,
        "files_deleted": files_deleted,
        "company_folder_deleted": company_folder_deleted,
        "keycloak_failed_deletes": kc_failed,
    }

@super_admin_router.delete("/companies/{company_id}/admins/{admin_id}")
async def delete_company_admin(
    company_id: str,
    admin_id: str,
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
    kc_admin=Depends(get_keycloak_admin),
):
    repo = CompanyRepository(db)
    doc_repo = DocumentRepository(db)

    admin = await repo.get_admin_by_id(company_id, admin_id)
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    email = admin.get("email")
    user_id = admin.get("user_id")

    kc_deleted = False
    try:
        kc_deleted = await run_in_threadpool(_delete_keycloak_user_by_email_sync, kc_admin, email)
    except Exception:
        logger.exception("Keycloak delete failed for admin %s", email)

    # 1) Delete role documents uploaded by this admin
    role_docs_deleted = await repo.delete_role_documents_by_admin(company_id, admin_id)
    logger.info(f"Deleted {role_docs_deleted} role documents uploaded by admin {admin_id}")

    # 2) Delete users added by this admin
    users_deleted = await repo.delete_users_by_admin(company_id, admin_id, kc_admin)
    logger.info(f"Deleted {users_deleted} users added by admin {admin_id}")

    # 3) Delete roles created by this admin
    roles_deleted = await repo.delete_roles_by_admin(company_id, admin_id)
    logger.info(f"Deleted {roles_deleted} roles created by admin {admin_id}")

    # 4) Delete personal documents by admin user
    personal_docs_deleted = await doc_repo.delete_documents_by_user(user_id)
    
    # 5) Delete filesystem files
    removed_paths = []
    try:
        removed_paths = await run_in_threadpool(_delete_user_files_sync, user_id)
    except Exception:
        logger.exception("File delete failed for user_id=%s", user_id)

    # 6) Finally delete admin record
    res = await db.company_admins.delete_one({"company_id": company_id, "user_id": admin_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Admin not found (race?)")

    return {
        "status": "admin_removed",
        "user_id": admin_id,
        "company_id": company_id,
        "keycloak_deleted": kc_deleted,
        "role_documents_deleted": role_docs_deleted,
        "users_deleted": users_deleted,
        "roles_deleted": roles_deleted,
        "personal_documents_deleted": personal_docs_deleted,
        "files_deleted": removed_paths,
    }
