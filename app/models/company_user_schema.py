from pydantic import BaseModel, EmailStr, Field
from typing import List
from typing import Optional

class ModuleConfig(BaseModel):
    name: str
    desc: Optional[str] = None 
    enabled: bool = False

class CompanyUserCreate(BaseModel):
    email: EmailStr
    company_role: str  # e.g. "teacher", "manager", etc.
    assigned_role: str

class CompanyUserUpdate(BaseModel):
    id: Optional[str] = None  # a1, a2…
    name: Optional[str] = ""   # optional, can be empty
    email: EmailStr            # must exist + must be valid email
    assigned_roles: List[str] = Field(default_factory=list)

class CompanyRoleCreate(BaseModel):
    role_name: str = Field(..., example="role_a")
    folders: List[str] = Field(..., example=["bkr", "vgc/kkr", "uur/trc"])
    modules: List[ModuleConfig]
    
class AssignRolePayload(BaseModel):
    user_id: str
    role_name: str

class DeleteDocumentsPayload(BaseModel):
    documents: List[dict] 

class DeleteRolesPayload(BaseModel):
    role_names: List[str]

class ResetPasswordPayload(BaseModel):
    email: str

class CompanyRoleModifyUsers(BaseModel):
    user_ids: List[str]
    role_name: str

