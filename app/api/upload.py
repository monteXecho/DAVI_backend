import os
import shutil
import logging
from fastapi import APIRouter, HTTPException, File, UploadFile, Depends, status
from fastapi.concurrency import run_in_threadpool
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.repositories.document_repo import DocumentRepository
from app.api.rag import rag_index_files

logger = logging.getLogger(__name__)

upload_router = APIRouter(prefix="/upload", tags=["Upload"])

UPLOAD_ROOT = "/app/uploads"

UPLOAD_FOLDERS = {
    "document": os.path.join(UPLOAD_ROOT, "documents"),
}

# Ensure all folders exist on startup
for folder in UPLOAD_FOLDERS.values():
    os.makedirs(folder, exist_ok=True)


def save_uploaded_file(file: UploadFile, save_path: str):
    """Save an uploaded file to disk."""
    try:
        file.file.seek(0)
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise IOError(f"Failed to save file: {e}")


@upload_router.post(
    "/{upload_type}",
    response_model=dict,
    responses={500: {"model": dict}},
    status_code=status.HTTP_200_OK,
)
async def upload_document(
    upload_type: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Upload a file, save it to disk, register metadata, and trigger RAG indexing.
    """
    try:
        if upload_type not in UPLOAD_FOLDERS:
            raise HTTPException(status_code=400, detail="Invalid upload type")

        email = current_user.get("email")
        if not email:
            raise HTTPException(status_code=401, detail="Email not found in token")

        company_repo = CompanyRepository(db)
        document_repo = DocumentRepository(db)

        # Fetch user info
        user = await company_repo.get_user_with_documents(email)
        if not user:
            raise HTTPException(status_code=404, detail="User not found in DB")

        user_id = user.get("user_id")
        company_id = user.get("company_id")
        if not user_id or not company_id:
            raise HTTPException(status_code=400, detail="User record missing user_id or company_id")

        # Build absolute save path
        upload_folder = os.path.join(UPLOAD_FOLDERS[upload_type], user_id)
        os.makedirs(upload_folder, exist_ok=True)
        file_path = os.path.abspath(os.path.join(upload_folder, file.filename))

        # Save file
        try:
            await run_in_threadpool(save_uploaded_file, file, file_path)
        except Exception as e:
            logger.error(f"File save failed: {e}")
            raise HTTPException(status_code=500, detail=f"File could not be saved: {e}")

        # Validate saved file
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            logger.error(f"File not found or empty after save: {file_path}")
            raise HTTPException(status_code=500, detail="File save failed (not found or empty)")

        # Store metadata
        doc_record = await document_repo.add_document(
            company_id=company_id,
            user_id=user_id,
            file_name=file.filename,
            upload_type=upload_type,
            path=file_path,
        )

        if not doc_record:
            raise HTTPException(
                status_code=409,
                detail=f"Document '{file.filename}' already exists for this user and type.",
            )

        logger.info(f" File '{file.filename}' uploaded by {email} (user_id={user_id}, company_id={company_id})")

        try:
            await rag_index_files(user_id, [file_path], company_id)
            logger.info(f"RAG indexing triggered for '{file.filename}'")
        except Exception as e:
            logger.error(f"RAG indexing failed for '{file.filename}': {e}")

        return {
            "success": True,
            "file": file.filename,
            "upload_type": upload_type,
            "user_id": user_id,
            "company_id": company_id,
            "path": file_path,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled exception during file upload")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")
