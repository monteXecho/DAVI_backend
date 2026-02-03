"""
Company Admin API Package

This package contains domain-specific routers for company admin operations.
The main router composes all domain routers for a clean, modular API structure.
"""

from app.api.company_admin.main import company_admin_router

__all__ = ["company_admin_router"]

