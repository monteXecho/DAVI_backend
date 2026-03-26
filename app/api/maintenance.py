"""
Maintenance mode API.
- Public GET /maintenance-status: returns current maintenance state (no auth).
- Super admin endpoints to activate/deactivate construction page.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import require_role, get_keycloak_admin
from app.deps.db import get_db

logger = logging.getLogger(__name__)

maintenance_router = APIRouter(tags=["Maintenance"])

MAINTENANCE_DOC_ID = "maintenance_config"


@maintenance_router.get("/maintenance-status")
async def get_maintenance_status(db=Depends(get_db)):
    """
    Public endpoint - no auth required.
    Returns maintenance mode state. Used by frontend to decide if construction page should be shown.
    """
    try:
        doc = await db.maintenance_config.find_one({"_id": MAINTENANCE_DOC_ID})
        enabled = doc.get("enabled", False) if doc else False
        return {"maintenance": bool(enabled)}
    except Exception as e:
        logger.warning(f"Failed to read maintenance status: {e}")
        return {"maintenance": False}

