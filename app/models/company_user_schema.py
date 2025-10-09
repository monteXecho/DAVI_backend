from pydantic import BaseModel, EmailStr

class CompanyUserCreate(BaseModel):
    email: EmailStr
    company_role: str  # e.g. "teacher", "manager", etc.

class CompanyUserUpdate(BaseModel):
    name: str
    email: EmailStr
    company_role: str
