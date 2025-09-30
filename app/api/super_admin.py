from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import require_role, keycloak_admin, ensure_role_exists
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_admin_schema import CompanyCreate, CompanyAddAdmin, CompanyAdminModules
import traceback

super_admin_router = APIRouter(prefix="/super-admin", tags=["Super Admin"])

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
    try:
        repo = CompanyRepository(db)
        modules = {m.name: m.dict() for m in payload.modules}
        result = await repo.add_admin(company_id, payload.name, payload.email, modules)
        if not result:
            raise HTTPException(404, "Company not found")

        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{str(e)}")
    

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
):
    repo = CompanyRepository(db)
    if not await repo.delete_company(company_id):
        raise HTTPException(404, "Company not found")
    return {"status": "deleted", "company_id": company_id}

@super_admin_router.delete("/companies/{company_id}/admins/{admin_id}")
async def delete_company_admin(
    company_id: str,
    admin_id: str,
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
):
    repo = CompanyRepository(db)
    if not await repo.delete_admin(company_id, admin_id):
        raise HTTPException(404, "Company not found")
    return {"status": "admin_removed", "admin_id": admin_id, "company_id": company_id}
