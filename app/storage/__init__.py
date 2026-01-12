"""
Storage abstraction layer for DAVI.

This module provides a unified interface for file storage operations,
allowing DAVI to work with different storage backends (Nextcloud, etc.)
while maintaining DAVI as the source of truth for folder structure and permissions.
"""

from app.storage.providers import StorageProvider, get_storage_provider
from app.storage.nextcloud_provider import NextcloudStorageProvider

__all__ = [
    "StorageProvider",
    "get_storage_provider",
    "NextcloudStorageProvider",
]
