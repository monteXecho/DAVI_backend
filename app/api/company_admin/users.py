"""
Users domain router for company admin API.

Handles all user-related endpoints:
- GET /users - List all users
- POST /users - Create user/admin
- PUT /users/{user_id} - Update user
- DELETE /users - Delete users
- POST /users/teamlid - Assign teamlid permissions
- POST /users/upload - Upload users from file
- POST /users/reset-password - Reset user password
- DELETE /users/teamlid/{target_admin_id} - Remove teamlid permissions
- POST /users/role/delete - Delete role from users
- POST /users/role/add - Add role to users
- GET /user - Get current user info
"""

import logging
import os
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from app.deps.auth import get_keycloak_admin, get_current_user
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_user_schema import (
    CompanyUserCreate,
    TeamlidPermissionAssign,
    CompanyUserUpdate,
    ResetPasswordPayload,
    CompanyRoleModifyUsers
)
from app.deps.auth import get_keycloak_admin
from app.api.company_admin.shared import (
    get_admin_or_user_company_id,
    get_admin_company_id,
    check_teamlid_permission
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/users")
async def get_all_users(
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Get all users created by the admin."""
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


@router.get("/user")
async def get_current_user_info(
    user=Depends(get_current_user),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Get current user information with documents."""
    repo = CompanyRepository(db)
    email = user.get("email")
    
    if not email:
        raise HTTPException(status_code=400, detail="Missing email in token")
    
    try:
        user_data = await repo.get_user_with_documents(email)
        
        if not user_data:
            raise HTTPException(status_code=404, detail="User not found")
        
        return user_data
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to get user info")
        raise HTTPException(status_code=500, detail=f"Failed to get user info: {str(e)}")


@router.post("/users")
async def add_user(
    payload: CompanyUserCreate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Create a new company user or admin."""
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    
    await check_teamlid_permission(admin_context, db, "users")

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


@router.post("/users/teamlid")
async def assign_teamlid_permission(
    payload: TeamlidPermissionAssign,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """Assign teamlid permissions to a user."""
    real_admin_id = admin_context.get("real_admin_id")
    acting_admin_id = admin_context.get("admin_id")
    
    if real_admin_id != acting_admin_id:
        raise HTTPException(
            status_code=403,
            detail="U kunt alleen teamlid rechten toewijzen in uw eigen werkruimte."
        )
    
    repo = CompanyRepository(db)

    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    await repo.assign_teamlid_permissions(
        company_id=company_id,
        admin_id=admin_id,
        email=payload.email,
        permissions=payload.team_permissions
    )

    return {
        "success": True,
        "message": "Teamlid permissions assigned successfully",
        "data": {
            "email": payload.email,
            "assigned_by": admin_id,
            "permissions": payload.team_permissions
        }
    }


@router.post("/users/upload")
async def upload_users_from_file(
    file: UploadFile = File(...),
    role: str = Form(default=""),  
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Upload a CSV or Excel file containing email addresses to add as company users."""
    await check_teamlid_permission(admin_context, db, "users")
    
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    allowed_extensions = {'.csv', '.xlsx', '.xls'}
    file_extension = os.path.splitext(file.filename.lower())[1]
    
    if file_extension not in allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail="Invalid file type. Only CSV and Excel files are allowed."
        )

    try:
        content = await file.read()
        
        results = await repo.add_users_from_email_file(
            company_id=company_id,
            admin_id=admin_id,
            file_content=content,
            file_extension=file_extension,
            selected_role=role  
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


@router.post("/users/reset-password")
async def reset_user_password(
    payload: ResetPasswordPayload,
    admin_context = Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """Reset user password via Keycloak."""
    await check_teamlid_permission(admin_context, db, "users")
    
    kc = get_keycloak_admin()
    email = payload.email
    logger.info(f"Password reset requested for {email}")

    try:
        users = kc.get_users(query={"email": email})
        if not users:
            raise HTTPException(status_code=404, detail="User not found in Keycloak")

        user = users[0]
        keycloak_id = user["id"]
        username = user.get("username", email)

        logger.info(f"Found user: {username} with Keycloak ID: {keycloak_id}")

        reset_url = f"{kc.connection.server_url}/admin/realms/{kc.connection.realm_name}/users/{keycloak_id}/execute-actions-email"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {kc.connection.token.get('access_token')}"
        }
        
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


@router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: CompanyUserUpdate,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Update user information."""
    await check_teamlid_permission(admin_context, db, "users")
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]

    user_type = payload.user_type

    updated = await repo.update_user(company_id, user_id, user_type, payload.name, payload.email, payload.assigned_roles)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found or not in your company")

    # Ensure ObjectId is converted to string
    if updated and "_id" in updated:
        updated["_id"] = str(updated["_id"])

    return {"success": True, "user_id": user_id, "new_name": payload.name, "assigned_roles": payload.assigned_roles}


@router.delete("/users")
async def delete_user(
    user_ids: str = Query(..., description="Comma-separated list of user IDs"),
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Delete users or admins."""
    await check_teamlid_permission(admin_context, db, "users")
    user_ids_list = user_ids.split(',')
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    kc = get_keycloak_admin()

    deleted_users_count = 0
    deleted_admins_count = 0
    failed_deletions = []

    for user_id in user_ids_list:
        user = await db.company_users.find_one({
            "company_id": company_id,
            "user_id": user_id
        })

        if user:
            logger.info(f"USER TO DELETE: {user}")
            
            if user.get("email"):
                email = user["email"]
                try:
                    kc_users = kc.get_users(query={"email": email})
                    if kc_users:
                        keycloak_user_id = kc_users[0]["id"]
                        kc.delete_user(keycloak_user_id)
                        logger.info(f"Deleted Keycloak user: {email}")
                except Exception as e:
                    logger.error(f"Failed Keycloak deletion for {email}: {str(e)}")
            
            try:
                count = await repo.delete_users(company_id, [user_id], admin_id)
                deleted_users_count += count
            except Exception as e:
                logger.error(f"Failed to delete user {user_id}: {str(e)}")
                failed_deletions.append(user_id)
        
        else:
            admin = await db.company_admins.find_one({
                "company_id": company_id,
                "user_id": user_id
            })
            
            if admin:
                logger.info(f"ADMIN TO DELETE: {admin}")
                
                if admin.get("email"):
                    email = admin["email"]
                    try:
                        kc_users = kc.get_users(query={"email": email})
                        if kc_users:
                            keycloak_user_id = kc_users[0]["id"]
                            kc.delete_user(keycloak_user_id)
                            logger.info(f"Deleted Keycloak admin: {email}")
                    except Exception as e:
                        logger.error(f"Failed Keycloak deletion for {email}: {str(e)}")
                
                try:
                    success = await repo.delete_admin(company_id, user_id, admin_id)
                    if success:
                        deleted_admins_count += 1
                except HTTPException as e:
                    logger.error(f"Failed to delete admin {user_id}: {e.detail}")
                    failed_deletions.append(user_id)
                except Exception as e:
                    logger.error(f"Failed to delete admin {user_id}: {str(e)}")
                    failed_deletions.append(user_id)
            else:
                logger.warning(f"User/Admin not found: {user_id}")
                failed_deletions.append(user_id)

    total_deleted = deleted_users_count + deleted_admins_count

    if total_deleted == 0:
        raise HTTPException(status_code=404, detail="No users or admins deleted. Make sure they were added by you.")

    return {
        "success": True,
        "deleted_users_count": deleted_users_count,
        "deleted_admins_count": deleted_admins_count,
        "total_deleted": total_deleted,
        "deleted_user_ids": user_ids_list,
        "failed_deletions": failed_deletions if failed_deletions else None
    }


@router.delete("/users/teamlid/{target_admin_id}")
async def remove_teamlid_role(
    target_admin_id: str,
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """Remove teamlid role from an admin."""
    real_admin_id = admin_context.get("real_admin_id")
    acting_admin_id = admin_context.get("admin_id")
    
    if real_admin_id != acting_admin_id:
        raise HTTPException(
            status_code=403,
            detail="U kunt alleen teamlid rechten verwijderen in uw eigen werkruimte."
        )
    
    repo = CompanyRepository(db)
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

    try:
        success = await repo.remove_teamlid_role(company_id, admin_id, target_admin_id)
        if not success:
            raise HTTPException(
                status_code=404,
                detail="Teamlid role not found or you don't have permission to remove it"
            )
        
        return {
            "success": True,
            "message": "Teamlid role removed successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to remove teamlid role")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/users/role/delete")
async def delete_role_from_user(
    payload: CompanyRoleModifyUsers,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Remove a role from users. Special handling for Teamlid role."""
    await check_teamlid_permission(admin_context, db, "users")
    
    from datetime import datetime
    
    roleName = payload.role_name
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]
    repo = CompanyRepository(db)

    # Special handling for Teamlid role removal
    if roleName == "Teamlid":
        try:
            # Get all affected users (both company_users and company_admins)
            affected_users = []
            affected_admins = []
            
            # Find affected company users
            company_users = await db.company_users.find({
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id,
                "is_teamlid": True
            }).to_list(None)
            affected_users = [u["user_id"] for u in company_users]
            
            # Find affected company admins
            company_admins = await db.company_admins.find({
                "user_id": {"$in": payload.user_ids},
                "company_id": company_id,
                "is_teamlid": True
            }).to_list(None)
            affected_admins = [a["user_id"] for a in company_admins]
            
            all_affected_user_ids = list(set(affected_users + affected_admins))
            
            if not all_affected_user_ids:
                return {
                    "status": "success",
                    "removedRole": roleName,
                    "affectedUsers": payload.user_ids,
                    "modifiedCount": 0,
                    "message": "No users with Teamlid role found to remove."
                }
            
            # Remove "Teamlid" from assigned_roles if present (for company_users)
            if affected_users:
                await db.company_users.update_many(
                    {
                        "user_id": {"$in": affected_users},
                        "company_id": company_id
                    },
                    {
                        "$pull": {"assigned_roles": "Teamlid"},
                        "$set": {
                            "is_teamlid": False,
                            "updated_at": datetime.utcnow()
                        },
                        "$unset": {
                            "teamlid_permissions": "",
                            "assigned_teamlid_by_id": "",
                            "assigned_teamlid_by_name": "",
                            "assigned_teamlid_at": ""
                        }
                    }
                )
            
            # Remove teamlid status from company admins
            if affected_admins:
                await db.company_admins.update_many(
                    {
                        "user_id": {"$in": affected_admins},
                        "company_id": company_id
                    },
                    {
                        "$set": {
                            "is_teamlid": False,
                            "updated_at": datetime.utcnow()
                        },
                        "$unset": {
                            "teamlid_permissions": "",
                            "assigned_teamlid_by_id": "",
                            "assigned_teamlid_by_name": "",
                            "assigned_teamlid_at": ""
                        }
                    }
                )
            
            # Remove all guest_access entries for these users
            # This removes all teamlid permissions across all workspaces
            guest_access_result = await db.company_guest_access.delete_many({
                "company_id": company_id,
                "guest_user_id": {"$in": all_affected_user_ids}
            })
            
            modified_count = len(all_affected_user_ids)
            
            return {
                "status": "success",
                "removedRole": roleName,
                "affectedUsers": payload.user_ids,
                "modifiedCount": modified_count,
                "guestAccessRemoved": guest_access_result.deleted_count,
                "message": f"Teamlid role and all related permissions removed from {modified_count} user(s)."
            }
            
        except Exception as e:
            logger.error(f"Error removing Teamlid role: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Could not remove Teamlid role: {str(e)}")
    
    # Regular role removal
    role = await repo.get_role_by_name(
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
                "$pull": {"assigned_roles": roleName}
            }
        )

        if result.modified_count > 0:
            await repo.update_role_user_counts(company_id, admin_id, [roleName], -result.modified_count)

        return {
            "status": "success",
            "removedRole": roleName,
            "affectedUsers": payload.user_ids,
            "modifiedCount": result.modified_count
        }

    except Exception as e:
        logger.error(f"Error removing role: {str(e)}")
        raise HTTPException(status_code=500, detail="Could not remove role from users.")


@router.post("/users/role/add")
async def add_role_to_users(
    payload: CompanyRoleModifyUsers,
    admin_context=Depends(get_admin_or_user_company_id),
    db=Depends(get_db)
):
    """Add a role to multiple users."""
    await check_teamlid_permission(admin_context, db, "users")
    roleName = payload.role_name
    company_id = admin_context["company_id"]
    admin_id = admin_context["admin_id"]

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
                "company_id": company_id,
                "assigned_roles": {"$ne": roleName}
            },
            {
                "$addToSet": {"assigned_roles": roleName}
            }
        )

        if result.modified_count > 0:
            repo = CompanyRepository(db)
            await repo.update_role_user_counts(company_id, admin_id, [roleName], result.modified_count)

        return {
            "status": "success",
            "addedRole": roleName,
            "affectedUsers": payload.user_ids,
            "modifiedCount": result.modified_count
        }

    except Exception as e:
        logger.error(f"Error adding role: {str(e)}")
        raise HTTPException(status_code=500, detail="Could not add role to users.")
