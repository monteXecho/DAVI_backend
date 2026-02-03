"""
Main Company Admin Router

This module composes all domain-specific routers into a single router
for the company admin API. This provides a clean, modular structure
while maintaining a unified API interface.

Transitional approach: Currently includes legacy router while domain routers
are being created. As endpoints are migrated to domain routers, they will
be included here instead of the legacy router.
"""

from fastapi import APIRouter

# Create main router
company_admin_router = APIRouter(prefix="/company-admin", tags=["Company Admin"])

# TODO: As domain routers are created, include them here
# from app.api.company_admin import users, documents, roles, folders, guest_access, stats, debug
# company_admin_router.include_router(users.router, tags=["Users"])
# company_admin_router.include_router(documents.router, tags=["Documents"])
# company_admin_router.include_router(roles.router, tags=["Roles"])
# company_admin_router.include_router(folders.router, tags=["Folders"])
# company_admin_router.include_router(guest_access.router, tags=["Guest Access"])
# company_admin_router.include_router(stats.router, tags=["Statistics"])
# company_admin_router.include_router(debug.router, tags=["Debug"])

# Temporary: Include legacy router for backward compatibility
# This will be removed as endpoints are migrated to domain routers
# Note: All endpoints are currently in the legacy router
from app.api.company_admin_legacy import company_admin_router as legacy_router
company_admin_router.include_router(legacy_router)

