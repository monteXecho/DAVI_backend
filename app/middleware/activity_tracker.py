"""
Activity tracking middleware for DAVI.

Tracks user activity to determine online users.
Updates last_activity timestamp when users make API calls.
"""

import logging
from datetime import datetime
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class ActivityTrackerMiddleware(BaseHTTPMiddleware):
    """
    Middleware to track user activity for online user detection.
    
    Updates last_activity timestamp in MongoDB for authenticated users.
    """
    
    def __init__(self, app: ASGIApp, db=None):
        super().__init__(app)
        self.db = db
    
    async def dispatch(self, request: Request, call_next):
        """
        Track user activity for authenticated requests.
        """
        # Skip tracking for certain paths
        skip_paths = [
            "/docs",
            "/openapi.json",
            "/redoc",
            "/health",
            "/favicon.ico",
        ]
        
        if any(request.url.path.startswith(path) for path in skip_paths):
            return await call_next(request)
        
        # Get user from request state (set by auth dependency)
        user = getattr(request.state, "user", None)
        
        # Process request
        response = await call_next(request)
        
        # Update activity if user is authenticated
        if user and self.db:
            try:
                email = user.get("email")
                if email:
                    await self._update_user_activity(email, self.db)
            except Exception as e:
                # Don't fail the request if activity tracking fails
                logger.debug(f"Failed to update user activity: {e}")
        
        return response
    
    async def _update_user_activity(self, email: str, db):
        """
        Update last_activity timestamp for a user.
        """
        try:
            now = datetime.utcnow()
            
            # Update admin activity
            await db.company_admins.update_one(
                {"email": email},
                {"$set": {"last_activity": now}},
                upsert=False
            )
            
            # Update user activity
            await db.company_users.update_one(
                {"email": email},
                {"$set": {"last_activity": now}},
                upsert=False
            )
        except Exception as e:
            logger.debug(f"Error updating activity for {email}: {e}")

