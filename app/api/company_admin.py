import logging
import pandas as pd
import os, json
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Query
from app.deps.auth import require_role, get_keycloak_admin
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import CompanyUserCreate, CompanyUserUpdate, CompanyRoleCreate, AssignRolePayload, DeleteDocumentsPayload, DeleteRolesPayload, ResetPasswordPayload
from app.api.rag import rag_index_files

logger = logging.getLogger("uvicorn")
KEYCLOAK_HOST = os.getenv("KEYCLOAK_HOST", "host.docker.internal")

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


# GET all users belonging to the same company
@company_admin_router.get("/users")
async def get_all_users(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    users_cursor = db.company_users.find({"company_id": company_id, "added_by_admin_id": admin_id})

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

# Get all documents uploaded by the admin (grouped by role/folder)
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
        "success": True,
        "company_id": company_id,
        "admin_id": admin_id,
        "data": result
    }

# ADD new user (with email + company_role)
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

@company_admin_router.post("/users/upload")
async def upload_users_from_file(
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """
    Upload a CSV or Excel file containing email addresses to add as company users.
    File can have:
    - Just email addresses (no headers)
    - Email addresses with 'email' column header
    - Email addresses in first column (with or without header)
    Names will be automatically generated from email prefixes.
    """
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Validate file type
    allowed_extensions = {'.csv', '.xlsx', '.xls'}
    file_extension = os.path.splitext(file.filename.lower())[1]
    
    if file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail="Invalid file type. Only CSV and Excel files are allowed."
        )

    try:
        # Read file content
        content = await file.read()
        
        # Process file using repository
        results = await repo.add_users_from_email_file(
            company_id=company_id,
            admin_id=admin_id,
            file_content=content,
            file_extension=file_extension
        )

        return {
            "success": True,
            "summary": {
                "total_processed": len(results["successful"]) + len(results["failed"]) + len(results["duplicates"]),
                "successful": len(results["successful"]),
                "duplicates": len(results["duplicates"]),
                "failed": len(results["failed"])
            },
            "details": results
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except pd.errors.EmptyDataError:
        raise HTTPException(status_code=400, detail="The file is empty.")
    except pd.errors.ParserError:
        raise HTTPException(status_code=400, detail="Error parsing the file. Please check the file format.")
    except Exception as e:
        logger.exception("Failed to process user upload file")
        raise HTTPException(status_code=500, detail=f"Failed to process file: {str(e)}")

@company_admin_router.post("/users/reset-password")
async def reset_user_password(
    payload: ResetPasswordPayload,
    admin_context = Depends(get_admin_company_id)
):
    kc = get_keycloak_admin()
    email = payload.email
    logger.info(f"Password reset requested for {email}")

    try:
        # Find user in Keycloak
        users = kc.get_users(query={"email": email})
        if not users:
            raise HTTPException(status_code=404, detail="User not found in Keycloak")

        user = users[0]
        keycloak_id = user["id"]
        username = user.get("username", email)

        logger.info(f"Found user: {username} with Keycloak ID: {keycloak_id}")

        # Correct API endpoint for password reset
        reset_url = f"{kc.connection.server_url}/admin/realms/{kc.connection.realm_name}/users/{keycloak_id}/execute-actions-email"
        
        # Headers with proper authentication
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {kc.connection.token.get('access_token')}"
        }
        
        # Send the request
        response = kc.connection.raw_put(
            reset_url,
            data=json.dumps(["UPDATE_PASSWORD"]),
            headers=headers
        )
        
        if response.status_code == 204:
            logger.info(f"Password reset email sent successfully to {email}")
            return {
                "success": True, 
                "message": f"Password reset email sent to {email}",
                "user_id": keycloak_id
            }
        else:
            logger.error(f"Keycloak API error: {response.status_code} - {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Keycloak API error: {response.text}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Password reset error for {email}: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send password reset email: {str(e)}"
        )


# UPDATE user (when the user registers and sets their name)
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

    return {"success": True, "user_id": user_id, "new_name": payload.name, "assigned_roles": payload.assigned_roles,}

# DELETE user
@company_admin_router.delete("/users")
async def delete_user(
    user_ids: str = Query(..., description="Comma-separated list of user IDs"),
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    user_ids_list = user_ids.split(',')
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    kc = get_keycloak_admin()

    deleted_count = 0
    for user_id in user_ids_list:
        # 1) Get user from MongoDB to retrieve email
        user = await db.company_users.find_one({
            "company_id": company_id,
            "user_id": user_id
        })

        logger.info(f"USER EMAIL TO DELETE: {user}")

        if user and user.get("email"):
            email = user["email"]
            try:
                # 2) Find user in Keycloak (by email)
                kc_users = kc.get_users(query={"email": email})
                if kc_users:
                    keycloak_user_id = kc_users[0]["id"]
                    kc.delete_user(keycloak_user_id)
                    logger.info(f"Deleted Keycloak user: {email}")
            except Exception as e:
                logger.error(f"Failed Keycloak deletion for {email}: {str(e)}")

        # 3) Delete user from MongoDB
        result = await repo.delete_users(company_id, [user_id])
        deleted_count += result

    if deleted_count == 0:
        raise HTTPException(status_code=404, detail="No users deleted")

    return {
        "success": True,
        "deleted_count": deleted_count,
        "deleted_user_ids": user_ids_list
    }


# GET Company stats
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


# DELETE documents
@company_admin_router.post("/documents/delete")
async def delete_documents(
    payload: DeleteDocumentsPayload,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """
    Delete multiple documents by file name and role.
    Each document in the payload should have:
    - fileName: The name of the file
    - role: The role that the file belongs to
    - path: Optional file path (if available)
    """
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        deleted_count = await repo.delete_documents(
            company_id=company_id,
            admin_id=admin_id,
            documents_to_delete=payload.documents
        )

        if deleted_count == 0:
            raise HTTPException(status_code=404, detail="No documents found to delete")

        return {
            "success": True,
            "deleted_count": deleted_count,
            "deleted_documents": payload.documents
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to delete documents")
        raise HTTPException(status_code=500, detail=f"Failed to delete documents: {str(e)}")
    

# ADD or UPDAT a company role and document folders for the role
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

@company_admin_router.post("/roles/delete")
async def delete_roles(
    payload: DeleteRolesPayload,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """Delete one or multiple roles and their associated data."""
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    try:
        result = await repo.delete_roles(company_id, payload.role_names, admin_id)
        return result
    except HTTPException:
        raise
    except Exception as e:
        print("Failed to delete roles:", e)
        raise HTTPException(status_code=500, detail="Failed to delete roles")

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
    except HTTPException:
        # Allow intentional HTTP errors (like 409 Conflict) to pass through
        raise
    except Exception as e:
        logger.exception("Failed to upload document for role")
        raise HTTPException(status_code=500, detail="Failed to upload document")


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
