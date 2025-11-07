from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository
from app.models.company_admin_schema import RegisterRequest
from app.deps.auth import keycloak_admin, ensure_role_exists
import traceback

auth_router = APIRouter(prefix="/auth", tags=["Auth"])

@auth_router.post("/register")
async def register_user(
    payload: RegisterRequest,
    db=Depends(get_db),
):
    try:
        repo = CompanyRepository(db)

        # 1. Check if user email exists in Mongo (invited by Super Admin)
        existing_admin = await repo.find_admin_by_email(payload.email)
        existing_user = await repo.find_user_by_email(payload.email)

        invited_user = existing_admin or existing_user  # ✅ THIS enables both

        if not invited_user:
            raise HTTPException(
                status_code=400,
                detail="Email not found."
            )

        # 2. Check if already in Keycloak
        existing_users = keycloak_admin.get_users(query={"email": payload.email})
        if existing_users:
            return {"message": "Duplicate"}

        # 3. Split name
        parts = payload.fullName.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

        email_prefix = payload.email.split("@")[0]

        # 4. Create user in Keycloak
        user_id = keycloak_admin.create_user({
            "username": email_prefix,
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

        # 5. Assign role from Mongo
        role_name = invited_user["role"]
        if not role_name:
            raise HTTPException(status_code=400, detail="Role missing in Mongo for this user")

        # Ensure role exists in Keycloak
        kc_role = ensure_role_exists(role_name)
        keycloak_admin.assign_realm_roles(user_id=user_id, roles=[kc_role])

        return {"status": "success", "user_id": user_id, "message": "Registered successfully"}

    except Exception as e:
        print("DEBUG Register error:", repr(e))
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")
