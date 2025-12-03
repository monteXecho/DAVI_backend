import logging
import pandas as pd
import os, json
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Query, status, Form, Body
from app.deps.auth import require_role, get_keycloak_admin, get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import CompanyUserCreate, CompanyUserUpdate, CompanyRoleCreate, AssignRolePayload, DeleteDocumentsPayload, DeleteFolderPayload, DeleteRolesPayload, ResetPasswordPayload, CompanyRoleModifyUsers
from app.models.company_admin_schema import AddFoldersPayload
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

@company_admin_router.get("/user")
async def get_login_user(
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    """
    Return the currently logged-in user's information.
    Works for both company_admin and company_user roles.
    """
    email = user.get("email")
    roles = user.get("realm_access", {}).get("roles", [])

    if not email:
        raise HTTPException(status_code=400, detail="Missing email in authentication token")

    # ---------- COMPANY ADMIN ----------
    if "company_admin" in roles:
        admin_record = await db.company_admins.find_one({"email": email})
        if not admin_record:
            raise HTTPException(status_code=404, detail="Admin not found in backend")

        return {
            "type": "company_admin",
            "email": admin_record["email"],
            "name": admin_record.get("name"),
            "company_id": admin_record.get("company_id"),
            "user_id": admin_record.get("user_id"),
            "modules": admin_record.get("modules", {})
        }

    # ---------- COMPANY USER ----------
    elif "company_user" in roles:
        user_record = await db.company_users.find_one({"email": email})
        if not user_record:
            raise HTTPException(status_code=404, detail="User not found")

        company_id = user_record["company_id"]
        assigned_roles = user_record.get("assigned_roles", [])

        final_modules = {
            "Documenten chat": {"enabled": False},
            "GGD Checks": {"enabled": False}
        }

        if not assigned_roles:
            return {
                "type": "company_user",
                "email": user_record["email"],
                "name": user_record.get("name"),
                "company_id": company_id,
                "user_id": user_record.get("user_id"),
                "roles": assigned_roles,
                "modules": final_modules
            }

        roles_collection = db.company_roles

        user_roles = await roles_collection.find({
            "company_id": company_id,
            "name": {"$in": assigned_roles}
        }).to_list(length=None)

        for role in user_roles:
            for module_name, cfg in role.get("modules", {}).items():

                enabled_raw = cfg.get("enabled", False)
                enabled_bool = (
                    enabled_raw.lower() == "true"
                    if isinstance(enabled_raw, str)
                    else bool(enabled_raw)
                )

                if module_name in final_modules:
                    final_modules[module_name]["enabled"] |= enabled_bool
                else:
                    final_modules[module_name] = {"enabled": enabled_bool}

        return {
            "type": "company_user",
            "email": user_record["email"],
            "name": user_record.get("name"),
            "company_id": company_id,
            "user_id": user_record.get("user_id"),
            "roles": assigned_roles,
            "modules": final_modules
        }

    # ---------- UNKNOWN ROLE ----------
    else:
        raise HTTPException(
            status_code=403,
            detail="User does not have a valid company role"
        )

@company_admin_router.get("/users")
async def get_all_users(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        users = await repo.get_all_users_created_by_admin_id(company_id, admin_id)
        
        return {
            "company_id": company_id,
            "members": users,
            "total": len(users)
        }

    except Exception as e:
        logger.exception("Failed to get users")
        raise HTTPException(status_code=500, detail=f"Failed to get users: {str(e)}")

@company_admin_router.get("/documents", summary="Get all uploaded documents by admin")
async def get_admin_uploaded_documents(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    
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

@company_admin_router.get("/documents/private")
async def get_admin_uploaded_documents(
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    email = user.get("email")

    repo = CompanyRepository(db)

    result = await repo.get_all_private_documents(email, document_type="document")

    return {
        "success": True,
        "data": result
    }


@company_admin_router.post("/users")
async def add_user(
    payload: CompanyUserCreate,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        if payload.company_role == "company_admin":
            name = getattr(payload, "name", None) or payload.email.split("@")[0]
            new_admin = await repo.add_admin(company_id, admin_id, name, payload.email)
            return {"status": "admin_created", "user": new_admin}
        else:
            new_user = await repo.add_user_by_admin(company_id, admin_id, payload.email, payload.company_role, payload.assigned_role)
            return {"status": "user_created", "user": new_user}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to add company user/admin")
        raise HTTPException(status_code=500, detail=str(e))

@company_admin_router.post("/users/upload")
async def upload_users_from_file(
    file: UploadFile = File(...),
    role: str = Form(default=""),  # Add role as optional form parameter
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
        
        # Process file using repository with role parameter
        print(f"DEBUG: Selected Role is: '{role}'")
        print(f"DEBUG: Role type: {type(role)}")
        results = await repo.add_users_from_email_file(
            company_id=company_id,
            admin_id=admin_id,
            file_content=content,
            file_extension=file_extension,
            selected_role=role  # Pass the selected role
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
    except Exception as e:
        logger.error(f"Error uploading users file: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

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


@company_admin_router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: CompanyUserUpdate,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    user_type = payload.user_type

    updated = await repo.update_user(company_id, user_id, user_type, payload.name, payload.email, payload.assigned_roles)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found or not in your company")

    return {"success": True, "user_id": user_id, "new_name": payload.name, "assigned_roles": payload.assigned_roles,}


@company_admin_router.delete("/users")
async def delete_user(
    user_ids: str = Query(..., description="Comma-separated list of user IDs"),
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    user_ids_list = user_ids.split(',')
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    kc = get_keycloak_admin()

    deleted_count = 0

    for user_id in user_ids_list:
        user = await db.company_users.find_one({
            "company_id": company_id,
            "user_id": user_id
        })

        logger.info(f"USER TO DELETE: {user}")

        if user and user.get("email"):
            email = user["email"]
            try:
                kc_users = kc.get_users(query={"email": email})
                if kc_users:
                    keycloak_user_id = kc_users[0]["id"]
                    kc.delete_user(keycloak_user_id)
                    logger.info(f"Deleted Keycloak user: {email}")
            except Exception as e:
                logger.error(f"Failed Keycloak deletion for {email}: {str(e)}")

    deleted_count = await repo.delete_users(company_id, user_ids_list, admin_id)

    if deleted_count == 0:
        raise HTTPException(status_code=404, detail="No users deleted")

    return {
        "success": True,
        "deleted_count": deleted_count,
        "deleted_user_ids": user_ids_list
    }

@company_admin_router.post("/users/role/delete")
async def delete_role_from_user(
    payload: CompanyRoleModifyUsers,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    roleName = payload.role_name
    print(f"--- Role Name: ---", roleName)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Validate role exists
    role = await CompanyRepository(db).get_role_by_name(
        company_id=company_id,
        admin_id=admin_id,
        role_name=roleName
    )

    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{roleName}' does not exist.")

    try:
        # Remove the role from the assigned_roles array for selected users
        result = await db.company_users.update_many(
            {
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id
            },
            {
                "$pull": {"assigned_roles": roleName}
            }
        )

        return {
            "status": "success",
            "removedRole": roleName,
            "affectedUsers": payload.user_ids,
            "modifiedCount": result.modified_count
        }

    except Exception as e:
        print("Error removing role:", e)
        raise HTTPException(status_code=500, detail="Could not remove role from users.")


@company_admin_router.post("/users/role/add")
async def add_role_to_users(
    payload: CompanyRoleModifyUsers,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    roleName = payload.role_name
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    # Check role exists
    role = await CompanyRepository(db).get_role_by_name(
        company_id=company_id,
        admin_id=admin_id,
        role_name=roleName
    )
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{roleName}' does not exist.")

    try:
        result = await db.company_users.update_many(
            {
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id
            },
            {
                "$addToSet": {"assigned_roles": roleName}
            }
        )

        return {
            "status": "success",
            "addedRole": roleName,
            "affectedUsers": payload.user_ids,
            "modifiedCount": result.modified_count
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not add role to users.")


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


@company_admin_router.post("/documents/delete")
async def delete_documents(
    payload: DeleteDocumentsPayload,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    
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
    

@company_admin_router.post("/documents/delete/private")
async def delete_documents(
    payload: DeleteDocumentsPayload,
    user=Depends(get_current_user),
    db=Depends(get_db)
):
    email = user.get("email")

    repo = CompanyRepository(db)
 
    try:
        deleted_count = await repo.delete_private_documents(
            email=email,
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

@company_admin_router.post("/folders")
async def add_folders(
    payload: AddFoldersPayload, 
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.add_folders(
            company_id=company_id,
            admin_id=admin_id,
            folder_names=payload.folder_names
        )

        return {
            "success": result["success"],
            "message": result["message"],
            "added_folders": result["added_folders"],
            "duplicated_folders": result["duplicated_folders"],
            "total_added": result["total_added"],
            "total_duplicates": result["total_duplicates"]
        }

    except HTTPException:
        raise

    except Exception as e:
        logger.exception("Failed to add folders")
        raise HTTPException(status_code=500, detail=f"Failed to add folders: {str(e)}")

@company_admin_router.get("/folders")
async def get_folders(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        # Call the repository method
        result = await repo.get_folders(
            company_id=company_id,
            admin_id=admin_id,
        )

        # Return the complete result from the repository
        return {
            "success": result["success"],
            "folders": result["folders"],
        }

    except HTTPException:
        # Directly rethrow known user-facing exceptions
        raise

    except Exception as e:
        logger.exception("Failed to get folders")
        raise HTTPException(status_code=500, detail=f"Failed to get folders: {str(e)}")

@company_admin_router.post("/folders/delete")
async def delete_folder(
    payload: DeleteFolderPayload,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.delete_folders(
            company_id=company_id,
            admin_id=admin_id,
            role_names=payload.role_names,
            folder_names=payload.folder_names
        )

        return {
            "success": True,
            "status": result["status"],
            "deleted_folders": result["deleted_folders"],
            "total_documents_deleted": result["total_documents_deleted"]
        }

    except HTTPException:
        # Directly rethrow known user-facing exceptions
        raise

    except Exception as e:
        logger.exception("Failed to delete folders")
        raise HTTPException(status_code=500, detail=f"Failed to delete folders: {str(e)}")

    
@company_admin_router.post("/roles")
async def add_or_update_role(
    payload: CompanyRoleCreate,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
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
    
@company_admin_router.post("/roles/upload/{folder_name}")
async def upload_document_for_role(
    folder_name: str,
    file: UploadFile = File(...),
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):

    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        result = await repo.upload_document_for_folder(
            company_id=company_id,
            admin_id=admin_id,
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

