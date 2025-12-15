from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional
import uuid

class ModuleConfig(BaseModel):
    name: str
    desc: Optional[str] = None 
    enabled: bool = False


class RegisterRequest(BaseModel):
    fullName: str
    email: EmailStr
    password: str
    

class CompanyAdmin(BaseModel):
    id: Optional[str] = None  
    user_id: uuid.UUID = Field(default_factory=uuid.uuid4) 
    name: str
    email: EmailStr
    modules: List[ModuleConfig]


class CompanyCreate(BaseModel):
    name: str


class CompanyUpdateModules(BaseModel):
    modules: List[ModuleConfig]


class CompanyAddAdmin(BaseModel):
    name: str
    email: EmailStr
    modules: List[ModuleConfig]

class CompanyReAssignAdmin(BaseModel):
    name: str
    email: EmailStr

class CompanyAdminModules(BaseModel):
    modules: List[ModuleConfig]

class AddFoldersPayload(BaseModel):
    folder_names: List[str]

class CompanyOut(BaseModel):
    id: str 
    name: str
    admins: List[CompanyAdmin]

class GuestAccessPayload(BaseModel):
    guest_user_id: str
    can_role_write: bool = False
    can_user_write: bool = False
    can_document_write: bool = False
    can_folder_write: bool = False


class GuestWorkspacePermissions(BaseModel):
    role_write: bool = False
    user_write: bool = False
    document_write: bool = False
    folder_write: bool = False


class GuestWorkspaceOut(BaseModel):
    ownerId: str
    label: str
    permissions: Optional[GuestWorkspacePermissions] = None
