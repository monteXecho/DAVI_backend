from fastapi import APIRouter, HTTPException, Depends
from pydantic import EmailStr
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_admin_schema import RegisterRequest
from app.deps.auth import keycloak_admin, ensure_role_exists
from keycloak.exceptions import KeycloakGetError
import traceback

auth_router = APIRouter(prefix="/auth", tags=["Auth"])


@auth_router.post("/register")
async def register_user(
    payload: RegisterRequest,
    db=Depends(get_db),
):
    try:
        repo = CompanyRepository(db)

        # -------------------------------------------------------------
        # 1. Must be invited (check MongoDB)
        # -------------------------------------------------------------
        existing_admin = await repo.find_admin_by_email(payload.email)
        existing_user = await repo.find_user_by_email(payload.email)
        invited_user = existing_admin or existing_user

        if not invited_user:
            raise HTTPException(
                status_code=400,
                detail="EMAIL_NOT_FOUND"
            )

        # -------------------------------------------------------------
        # 2. Check if email already exists in Keycloak
        # -------------------------------------------------------------
        existing_users = keycloak_admin.get_users(query={"email": payload.email})
        if existing_users:
            raise HTTPException(
                status_code=409,
                detail="EMAIL_EXISTS"
            )

        # -------------------------------------------------------------
        # 3. Prepare username, first name, last name
        # -------------------------------------------------------------
        parts = payload.fullName.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        username = (first_name + last_name).lower()

        # -------------------------------------------------------------
        # 4. Create user in Keycloak
        # -------------------------------------------------------------
        try:
            user_id = keycloak_admin.create_user({
                "username": username,
                "email": payload.email,
                "firstName": first_name,
                "lastName": last_name,
                "enabled": True,
                "credentials": [{
                    "type": "password",
                    "value": payload.password,
                    "temporary": False
                }]
            })

        except KeycloakGetError as kc_err:
            # Username conflict → Keycloak returns 409
            if kc_err.response_code == 409:
                raise HTTPException(
                    status_code=409,
                    detail="USERNAME_EXISTS"
                )

            raise HTTPException(
                status_code=400,
                detail=f"KEYCLOAK_ERROR: {kc_err.error_message}"
            )

        # -------------------------------------------------------------
        # 5. Assign role from MongoDB
        # -------------------------------------------------------------
        role_name = invited_user.get("role")
        if not role_name:
            raise HTTPException(
                status_code=400,
                detail="ROLE_MISSING"
            )

        kc_role = ensure_role_exists(role_name)
        keycloak_admin.assign_realm_roles(user_id=user_id, roles=[kc_role])

        # -------------------------------------------------------------
        # 6. Update MongoDB user → set full name
        # -------------------------------------------------------------
        full_name = payload.fullName.strip()

        await db.company_users.update_one(
            {"email": payload.email},
            {"$set": {"name": full_name}}
        )

        await db.company_admins.update_one(
            {"email": payload.email},
            {"$set": {"name": full_name}}
        ) 

        # -------------------------------------------------------------
        # 7. Success
        # -------------------------------------------------------------
        return {
            "status": "success",
            "user_id": user_id,
            "message": "Registered successfully"
        }

    except HTTPException:
        raise

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=400,
            detail=f"REGISTRATION_FAILED: {str(e)}"
        )
