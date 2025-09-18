from pydantic import BaseModel, EmailStr
from typing import List, Optional


class ModuleConfig(BaseModel):
    name: str
    desc: Optional[str] = None 
    enabled: bool = False


class CompanyAdmin(BaseModel):
    id: Optional[str] = None  # a1, a2…
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
