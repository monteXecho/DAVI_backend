import os
import shutil
import logging

from fastapi import APIRouter, HTTPException, File, UploadFile, Depends, status
from fastapi.concurrency import run_in_threadpool
from app.deps.auth import get_current_user  # Assuming you have a `get_current_user` function

logger = logging.getLogger(__name__)

upload_router = APIRouter(prefix="/upload", tags=["Upload"])

# Define folders for different types of uploads
UPLOAD_FOLDERS = {
    "document": os.path.join("uploads", "documents"),
    "bkr": os.path.join("uploads", "bkr"),
    "vgc": os.path.join("uploads", "vgc"),
    "3-uurs": os.path.join("uploads", "3-uurs"),
}

# Ensure the directories exist for known upload types
for folder in UPLOAD_FOLDERS.values():
    os.makedirs(folder, exist_ok=True)

# Utility function to handle file saving
def save_uploaded_file(file: UploadFile, save_path: str):
    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

# Route to handle document uploads, can be reused for other endpoints
@upload_router.post(
    "/{upload_type}",
    response_model=dict,
    responses={500: {"model": dict}},
    status_code=status.HTTP_200_OK,
)
async def upload_document(upload_type: str, file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    """
    Uploads a document to the corresponding folder based on the upload type.

    Arguments:
    - upload_type: The folder type (document, bkr, vgc, 3-uurs).
    - file: The file to be uploaded.
    - current_user: The authenticated user.

    Returns:
    - success: Boolean indicating the success of the upload.
    - file: The uploaded file's name.
    - upload_type: The upload type.
    """
    try:
        # Check if the upload type is valid
        if upload_type not in UPLOAD_FOLDERS:
            raise HTTPException(status_code=400, detail="Invalid upload type")

        # Get the upload folder path
        upload_folder = UPLOAD_FOLDERS[upload_type]

        # Check if the directory exists, and create it if not
        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder, exist_ok=True)

        # Prepare the path to save the file
        file_path = os.path.join(upload_folder, file.filename)

        logger.info(f"Received file: {file.filename} for {upload_type} from user: {current_user.get('preferred_username', 'unknown')}")

        # Use an asynchronous thread pool to save the file
        await run_in_threadpool(save_uploaded_file, file, file_path)

        logger.info(f"File uploaded successfully: {file.filename} to {upload_type} folder")
        
        return {"success": True, "file": file.filename, "upload_type": upload_type}

    except Exception as e:
        logger.exception("Unhandled exception during file upload")
        raise HTTPException(status_code=500, detail="Internal server error")
