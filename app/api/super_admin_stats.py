"""
Super Admin Dashboard Statistics API

Provides comprehensive statistics for the super admin dashboard including:
- Total companies, admins, users, team members
- Online user tracking
- Document counts
- Module statistics
- Resource limits
"""

import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from app.deps.auth import require_role, get_keycloak_admin
from app.deps.db import get_db
from app.repositories.company_repo import CompanyRepository

logger = logging.getLogger(__name__)

super_admin_stats_router = APIRouter(prefix="/super-admin", tags=["Super Admin Stats"])


@super_admin_stats_router.get("/stats")
async def get_dashboard_stats(
    user=Depends(require_role("super_admin")),
    db=Depends(get_db),
    kc_admin=Depends(get_keycloak_admin)
):
    """
    Get comprehensive dashboard statistics for super admin.
    
    Returns:
    - Total companies, admins, users, team members
    - Online user count (users active in last 15 minutes)
    - Document counts
    - Module statistics
    - Resource limits and usage
    """
    try:
        repo = CompanyRepository(db)
        
        # 1. Basic counts
        total_companies = await db.companies.count_documents({})
        total_admins = await db.company_admins.count_documents({})
        total_users = await db.company_users.count_documents({})
        
        # 2. Team members count (users with is_teamlid=True)
        total_teamlids = await db.company_admins.count_documents({"is_teamlid": True})
        total_teamlids += await db.company_users.count_documents({"is_teamlid": True})
        
        # 3. Online users tracking
        # Track via last_activity timestamp in DAVI
        # Users are considered "online" if they've been active in the last 15 minutes
        online_users_count = await _get_online_users_from_activity(db)
        
        logger.info(f"📊 Dashboard stats: {total_users} total users, {online_users_count} online")
        
        # 4. Document counts
        total_documents = await db.documents.count_documents({})
        
        # 5. Module statistics
        companies_with_modules = await db.companies.find({
            "modules": {"$exists": True, "$ne": {}}
        }).to_list(length=None)
        active_modules_count = sum(
            len(company.get("modules", {})) 
            for company in companies_with_modules
        )
        
        # 6. Resource limits and usage
        companies = await db.companies.find({}).to_list(length=None)
        
        max_users_possible = 0
        max_admins_possible = 0
        max_documents_possible = 0
        max_roles_possible = 0
        
        unlimited_companies_users = 0
        unlimited_companies_admins = 0
        unlimited_companies_documents = 0
        
        for company in companies:
            limits = company.get("limits", {})
            
            # Users
            max_users = limits.get("max_users", 0)
            if max_users == -1:
                unlimited_companies_users += 1
            else:
                max_users_possible += max_users
            
            # Admins
            max_admins = limits.get("max_admins", 0)
            if max_admins == -1:
                unlimited_companies_admins += 1
            else:
                max_admins_possible += max_admins
            
            # Documents
            max_documents = limits.get("max_documents", 0)
            if max_documents == -1:
                unlimited_companies_documents += 1
            else:
                max_documents_possible += max_documents
            
            # Roles
            max_roles = limits.get("max_roles", 0)
            if max_roles != -1:
                max_roles_possible += max_roles
        
        # 7. Roles count
        total_roles = await db.company_roles.count_documents({})
        
        return {
            "companies": {
                "total": total_companies
            },
            "admins": {
                "total": total_admins,
                "max_possible": max_admins_possible,
                "unlimited_companies": unlimited_companies_admins
            },
            "users": {
                "total": total_users,
                "online": online_users_count or 0,
                "max_possible": max_users_possible,
                "unlimited_companies": unlimited_companies_users
            },
            "team_members": {
                "total": total_teamlids
            },
            "roles": {
                "total": total_roles,
                "max_possible": max_roles_possible
            },
            "documents": {
                "total": total_documents,
                "max_possible": max_documents_possible,
                "unlimited_companies": unlimited_companies_documents
            },
            "modules": {
                "active": active_modules_count
            }
        }
    except Exception as e:
        logger.exception("Failed to get dashboard statistics")
        raise HTTPException(status_code=500, detail=f"Failed to get statistics: {str(e)}")


async def _get_online_users_from_keycloak(kc_admin, db) -> int:
    """
    Get online user count from Keycloak active sessions.
    
    This queries Keycloak for users with active sessions (logged in within last 15 minutes).
    """
    try:
        # Get all users from Keycloak
        keycloak_users = kc_admin.get_users({})
        
        if not keycloak_users:
            return 0
        
        # Get user emails from Keycloak
        keycloak_emails = {user.get("email") for user in keycloak_users if user.get("email")}
        
        # Count DAVI users that match Keycloak users
        # Note: This is a simplified approach. For more accuracy, you could:
        # 1. Query Keycloak sessions API (if available)
        # 2. Track last login time in DAVI
        # 3. Use Keycloak events API
        
        # For now, we'll use a time-based approach with last_activity
        # This requires storing last_activity when users authenticate
        return None  # Will fall back to activity-based tracking
        
    except Exception as e:
        logger.warning(f"Failed to get online users from Keycloak: {e}")
        return None


async def _get_online_users_from_activity(db) -> int:
    """
    Get online user count based on last_activity timestamp.
    
    Users are considered "online" if they've been active in the last 15 minutes.
    This requires storing last_activity when users make API calls.
    """
    try:
        # Consider users online if they've been active in last 15 minutes
        online_threshold = datetime.utcnow() - timedelta(minutes=15)
        
        logger.info(f"Checking for online users with last_activity >= {online_threshold}")
        
        # Count admins active in last 15 minutes
        online_admins = await db.company_admins.count_documents({
            "last_activity": {"$gte": online_threshold}
        })
        
        # Count users active in last 15 minutes
        online_users = await db.company_users.count_documents({
            "last_activity": {"$gte": online_threshold}
        })
        
        total_online = online_admins + online_users
        logger.info(f"Found {total_online} online users ({online_admins} admins, {online_users} users)")
        
        # Debug: Check if any users have last_activity field at all
        admins_with_activity = await db.company_admins.count_documents({
            "last_activity": {"$exists": True}
        })
        users_with_activity = await db.company_users.count_documents({
            "last_activity": {"$exists": True}
        })
        logger.info(f"Users with last_activity field: {admins_with_activity} admins, {users_with_activity} users")
        
        return total_online
    except Exception as e:
        logger.warning(f"Failed to get online users from activity: {e}", exc_info=True)
        # If last_activity field doesn't exist, return 0
        return 0

