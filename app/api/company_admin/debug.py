"""
Debug domain router for company admin API.

Handles debug/inspection endpoints:
- GET /debug/all-data - Get all data from all collections
- DELETE /debug/clear-all - Clear all data from all collections
"""

import logging
from fastapi import APIRouter, Depends
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/debug/all-data")
async def get_all_data(db=Depends(get_db)):
    """Return all documents from all collections (debug method)."""
    repo = CompanyRepository(db)
    data = await repo.get_all_collections_data()
    return data


@router.delete("/debug/clear-all")
async def clear_all_data(db=Depends(get_db)):
    """Clear all data from all collections (debug method)."""
    repo = CompanyRepository(db)
    result = await repo.clear_all_data()
    return result

