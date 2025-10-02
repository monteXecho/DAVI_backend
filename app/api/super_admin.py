import os
import shutil
import logging
from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import require_role, get_keycloak_admin
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.repositories.document_repo import DocumentRepository
from app.models.company_admin_schema import CompanyCreate, CompanyAddAdmin, CompanyAdminModules

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

    # 2) remove Keycloak accounts, documents (db), and uploaded files
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

    # 3) delete admin/user records and company doc records (cascade)
    await db.company_admins.delete_many({"company_id": company_id})
    await db.company_users.delete_many({"company_id": company_id})
    # also delete any remaining documents for this company
    try:
        docs_deleted_by_company = await doc_repo.delete_documents_by_company(company_id)
        docs_deleted_total += docs_deleted_by_company
    except Exception:
        logger.exception("Failed to delete documents by company_id=%s", company_id)

    # 4) finally remove company record
    result = await db.companies.delete_one({"company_id": company_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Company not found")

    return {
        "status": "deleted",
        "company_id": company_id,
        "admins_removed": len(admins),
        "users_removed": len(users),
        "documents_deleted": docs_deleted_total,
        "files_deleted": files_deleted,
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

    # delete documents by user
    docs_deleted = await doc_repo.delete_documents_by_user(user_id)
    # delete filesystem files
    removed_paths = []
    try:
        removed_paths = await run_in_threadpool(_delete_user_files_sync, user_id)
    except Exception:
        logger.exception("File delete failed for user_id=%s", user_id)

    # finally delete admin record
    res = await db.company_admins.delete_one({"company_id": company_id, "admin_id": admin_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Admin not found (race?)")

    return {
        "status": "admin_removed",
        "admin_id": admin_id,
        "company_id": company_id,
        "keycloak_deleted": kc_deleted,
        "documents_deleted": docs_deleted,
        "files_deleted": removed_paths,
    }
