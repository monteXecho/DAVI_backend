"""
Users domain router for company admin API.

Handles all user-related endpoints:
- GET /users - List users
- POST /users - Create user
- PUT /users/{user_id} - Update user
- DELETE /users - Delete users
- POST /users/teamlid - Assign teamlid permissions
- POST /users/upload - Upload users from file
- POST /users/reset-password - Reset user password
- DELETE /users/teamlid/{target_admin_id} - Remove teamlid permissions
- POST /users/role/delete - Delete role from users
- POST /users/role/add - Add role to users
"""

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Body
from app.deps.auth import get_current_user
from app.deps.db import get_db
from app.api.company_admin.shared import get_repository, check_teamlid_permission
from app.repositories.company_repo import CompanyRepository

router = APIRouter()


# Import endpoints from legacy implementation temporarily
# These will be gradually migrated to this module
from app.api.company_admin_legacy import (
    get_users_endpoint,
    create_user_endpoint,
    update_user_endpoint,
    delete_users_endpoint,
    assign_teamlid_permissions_endpoint,
    upload_users_file_endpoint,
    reset_password_endpoint,
    remove_teamlid_permissions_endpoint,
    delete_role_from_users_endpoint,
    add_role_to_users_endpoint,
)

# Register routes
router.get("/users")(get_users_endpoint)
router.post("/users")(create_user_endpoint)
router.put("/users/{user_id}")(update_user_endpoint)
router.delete("/users")(delete_users_endpoint)
router.post("/users/teamlid")(assign_teamlid_permissions_endpoint)
router.post("/users/upload")(upload_users_file_endpoint)
router.post("/users/reset-password")(reset_password_endpoint)
router.delete("/users/teamlid/{target_admin_id}")(remove_teamlid_permissions_endpoint)
router.post("/users/role/delete")(delete_role_from_users_endpoint)
router.post("/users/role/add")(add_role_to_users_endpoint)

