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
    # username: str
    password: str
    

class CompanyAdmin(BaseModel):
    id: Optional[str] = None  # a1, a2…
    user_id: uuid.UUID = Field(default_factory=uuid.uuid4)  # auto-generated UUID
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


class CompanyAdminModules(BaseModel):
    modules: List[ModuleConfig]


class CompanyOut(BaseModel):
    id: str  # c1, c2…
    name: str
    admins: List[CompanyAdmin]
