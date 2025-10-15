from pydantic import BaseModel, EmailStr, Field
from typing import List


class CompanyUserCreate(BaseModel):
    email: EmailStr
    company_role: str  # e.g. "teacher", "manager", etc.

class CompanyUserUpdate(BaseModel):
    name: str
    email: EmailStr
    assigned_roles: List[str] = Field()

class CompanyRoleCreate(BaseModel):
    role_name: str = Field(..., example="role_a")
    folders: List[str] = Field(..., example=["bkr", "vgc/kkr", "uur/trc"])
    
class AssignRolePayload(BaseModel):
    user_id: str
    role_name: str
