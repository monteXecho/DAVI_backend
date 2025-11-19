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


class CompanyOut(BaseModel):
    id: str 
    name: str
    admins: List[CompanyAdmin]
