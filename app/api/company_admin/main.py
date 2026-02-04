"""
Main Company Admin Router

This module composes all domain-specific routers into a single router
for the company admin API. This provides a clean, modular structure
while maintaining a unified API interface.
"""

from fastapi import APIRouter

# Create main router
company_admin_router = APIRouter(prefix="/company-admin", tags=["Company Admin"])

# Include all domain routers
from app.api.company_admin import users, documents, roles, folders, guest_access, stats, debug

company_admin_router.include_router(users.router)
company_admin_router.include_router(documents.router)
company_admin_router.include_router(roles.router)
company_admin_router.include_router(folders.router)
company_admin_router.include_router(guest_access.router)
company_admin_router.include_router(stats.router)
company_admin_router.include_router(debug.router)

