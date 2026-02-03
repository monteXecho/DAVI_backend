"""
Base repository class providing shared database collections and utilities.

This module contains the base repository class that all domain-specific
repositories inherit from, ensuring consistent database access patterns.
"""

import logging
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)


class BaseRepository:
    """
    Base repository class providing access to all database collections.
    
    All domain-specific repositories should inherit from this class to ensure
    consistent database access patterns and shared collection references.
    """
    
    def __init__(self, db: AsyncIOMotorDatabase):
        """
        Initialize the base repository with database connection.
        
        Args:
            db: MongoDB database instance (AsyncIOMotorDatabase)
        """
        self.db = db
        self.companies = db.companies
        self.admins = db.company_admins
        self.users = db.company_users
        self.roles = db.company_roles
        self.documents = db.documents
        self.folders = db.company_folders
        self.guest_access = db.company_guest_access

