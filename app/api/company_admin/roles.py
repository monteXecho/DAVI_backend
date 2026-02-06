"""
Roles domain router for company admin API.

Handles all role-related endpoints:
- POST /roles - Create or update role
- GET /roles - List all roles
- POST /roles/delete - Delete roles
- POST /roles/assign - Assign role to user
- POST /roles/upload/{folder_name} - Upload document to role folder
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import CompanyRoleCreate, DeleteRolesPayload, AssignRolePayload
from app.api.company_admin.shared import (
    get_admin_or_user_company_id,
    check_teamlid_permission
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/roles")
async def add_or_update_role(
    payload: CompanyRoleCreate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Create or update a role."""
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


@router.get("/roles")
async def list_roles(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """List all roles for the admin."""
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    try:
        roles = await repo.list_roles(company_id, admin_id)
        return {"roles": roles}
    except Exception as e:
        logger.error(f"Failed to list roles: {e}")
        raise HTTPException(status_code=500, detail="Failed to list roles")


@router.post("/roles/delete")
async def delete_roles(
    payload: DeleteRolesPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete one or more roles."""
    await check_teamlid_permission(admin_context, db, "roles_folders")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    try:
        result = await repo.delete_roles(company_id, payload.role_names, admin_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete roles: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete roles")


@router.post("/roles/assign")
async def assign_role_to_user(
    payload: AssignRolePayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Assign a role to a user."""
    await check_teamlid_permission(admin_context, db, "users")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    try:
        result = await repo.assign_role_to_user(company_id, payload.user_id, payload.role_name)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to assign role: {e}")
        raise HTTPException(status_code=500, detail="Failed to assign role")


@router.post("/roles/upload/{folder_name}")
async def upload_document_for_role(
    folder_name: str,
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Upload a document to a role folder."""
    await check_teamlid_permission(admin_context, db, "documents")

    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Check documents limit
    allowed, error_msg = await repo.check_documents_limit(company_id)
    if not allowed:
        raise HTTPException(status_code=403, detail=error_msg)

    # Get storage provider using Keycloak SSO
    from app.storage.providers import get_storage_provider, StorageError
    from app.core.config import NEXTCLOUD_URL, NEXTCLOUD_ROOT_PATH
    
    user_email = admin_context.get("admin_email") or admin_context.get("user_email")
    access_token = admin_context.get("_access_token")
    nextcloud_user_id = admin_context.get("_nextcloud_user_id")
    
    storage_provider = None
    if user_email and access_token:
        try:
            storage_provider = get_storage_provider(
                username=user_email,
                access_token=access_token,
                url=NEXTCLOUD_URL,
                root_path=NEXTCLOUD_ROOT_PATH,
                user_id_from_token=nextcloud_user_id
            )
        except StorageError as e:
            logger.warning(f"Storage provider not available: {e}")
        except Exception as e:
            logger.warning(f"Storage provider error: {e}")

    try:
        result = await repo.upload_document_for_folder(
            company_id=company_id,
            admin_id=admin_id,
            folder_name=folder_name,
            file=file,
            storage_provider=storage_provider
        )

        # Trigger RAG indexing for the uploaded file
        from app.api.rag import rag_index_files
        try:
            await rag_index_files([result["path"]], company_id, admin_id)
        except Exception as e:
            logger.warning(f"RAG indexing failed for {result['path']}: {e}")

        return {
            "success": True,
            "folder": result["folder"],
            "file_name": result["file_name"],
            "path": result["path"],
            "storage_path": result.get("storage_path")
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to upload document for role")
        raise HTTPException(status_code=500, detail=f"Failed to upload document: {str(e)}")

