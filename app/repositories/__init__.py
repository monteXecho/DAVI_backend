"""
Repository package initialization.

This package contains all domain-specific repositories and the main
CompanyRepository facade that composes them.
"""

from app.repositories.company_repo import CompanyRepository

__all__ = ["CompanyRepository"]

