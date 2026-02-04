"""
Statistics domain router for company admin API.

Handles statistics endpoints:
- GET /stats - Get company statistics
"""

import logging
from fastapi import APIRouter, Depends
from app.deps.db import get_db
from app.api.company_admin.shared import get_admin_company_id

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/stats")
async def get_company_stats(
    admin_context=Depends(get_admin_company_id),
    db=Depends(get_db)
):
    """Get company statistics."""
    company_id = admin_context["company_id"]

    admin_count = await db.company_admins.count_documents({"company_id": company_id})
    user_count = await db.company_users.count_documents({"company_id": company_id})

    admin_ids = [
        a["user_id"]
        async for a in db.company_admins.find({"company_id": company_id}, {"user_id": 1})
    ]
    user_ids = [
        u["user_id"]
        async for u in db.company_users.find({"company_id": company_id}, {"user_id": 1})
    ]

    docs_for_admins = await db.documents.count_documents({"user_id": {"$in": admin_ids}})
    docs_for_users = await db.documents.count_documents({"user_id": {"$in": user_ids}})

    return {
        "company_id": company_id,
        "company_admin_count": admin_count,
        "company_user_count": user_count,
        "documents_for_admins": docs_for_admins,
        "documents_for_users": docs_for_users,
    }

