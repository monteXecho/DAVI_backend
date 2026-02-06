"""
Documents domain router for company admin API.

Handles all document-related endpoints:
- GET /documents - Get admin documents organized by roles
- GET /documents/private - Get private documents
- GET /documents/all - Get all user documents
- GET /documents/download - Download a document
- POST /documents/delete - Delete documents from folders
- POST /documents/delete/private - Delete private documents
"""

import logging
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import DeleteDocumentsPayload
from app.api.company_admin.shared import (
    get_admin_or_user_company_id,
    check_teamlid_permission
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/documents", summary="Get all uploaded documents by admin")
async def get_admin_uploaded_documents(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Get all documents organized by roles and folders for the admin."""
    company_id: str = admin_context["company_id"]
    admin_id: str = admin_context["admin_id"]

    repo = CompanyRepository(db)

    result = await repo.get_admin_documents(company_id, admin_id)

    return {
        "success": True,
        "company_id": company_id,
        "admin_id": admin_id,
        "data": result
    }


@router.get("/documents/private")
async def get_private_documents(
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    """Get private documents for the current user."""
    email = user.get("email")

    repo = CompanyRepository(db)

    result = await repo.get_all_private_documents(email, document_type="document")

    return {
        "success": True,
        "data": result
    }


@router.get("/documents/all")
async def get_all_user_documents(
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    """
    Get all documents for the current user (both private and role-based).
    
    For company admins:
    - Returns private documents (upload_type="document") 
    - Returns all documents in folders created by this admin (upload_type=folder_name)
    
    For company users:
    - Returns private documents (upload_type="document")
    - Returns documents from folders assigned via roles
    """
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Email not found in token")

    repo = CompanyRepository(db)
    result = await repo.get_all_user_documents(email)
    
    return {
        "success": True,
        "data": result
    }


@router.get("/documents/download")
async def download_document(
    file_path: str = Query(..., description="Path to the document file"),
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    """
    Download/view a document file. Verifies the user has access to the document
    before serving it.
    """
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Email not found in token")

    repo = CompanyRepository(db)
    
    user_data = await repo.get_all_user_documents(email)
    user_documents = user_data.get("documents", [])
    
    document_found = any(doc.get("path") == file_path for doc in user_documents)
    
    if not document_found:
        raise HTTPException(status_code=403, detail="You don't have access to this document")
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    file_name = os.path.basename(file_path)
    
    media_type = "application/octet-stream"
    if file_name.endswith(".pdf"):
        media_type = "application/pdf"
    elif file_name.endswith((".jpg", ".jpeg")):
        media_type = "image/jpeg"
    elif file_name.endswith(".png"):
        media_type = "image/png"
    elif file_name.endswith((".doc", ".docx")):
        media_type = "application/msword"
    
    return FileResponse(
        path=file_path,
        filename=file_name,
        media_type=media_type
    )


@router.post("/documents/delete")
async def delete_documents(
    payload: DeleteDocumentsPayload,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete documents from folders."""
    await check_teamlid_permission(admin_context, db, "documents")
    
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

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
        except (StorageError, Exception) as e:
            logger.debug(f"Storage provider not available for document deletion sync: {e}")

    try:
        deleted_count = await repo.delete_documents(
            company_id=company_id,
            admin_id=admin_id,
            documents_to_delete=payload.documents,
            storage_provider=storage_provider
        )

        return {
            "success": True,
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} document(s)"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete documents")
        raise HTTPException(status_code=500, detail=f"Failed to delete documents: {str(e)}")


@router.post("/documents/delete/private")
async def delete_private_documents(
    payload: DeleteDocumentsPayload,
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    """Delete private documents for the current user."""
    email = user.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Email not found in token")

    repo = CompanyRepository(db)

    try:
        deleted_count = await repo.delete_private_documents(
            email=email,
            documents_to_delete=payload.documents
        )

        return {
            "success": True,
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} document(s)"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete private documents")
        raise HTTPException(status_code=500, detail=f"Failed to delete private documents: {str(e)}")

